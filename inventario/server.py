"""Servidor local HTTP (solo biblioteca estándar) con la API y la interfaz web.

    python3 -m inventario.server [--puerto 8470] [--datos CARPETA]

Seguridad:
- Desde el propio equipo (127.0.0.1/::1) no se pide clave, pero el Host debe ser
  localhost/127.0.0.1 (evita «DNS rebinding»).
- Toda llamada a /api exige la cabecera X-Inventario (una web ajena no puede
  enviarla sin permiso CORS, que nunca se concede): evita CSRF.
- Otros equipos de la red solo entran si se activa «acceso desde otros
  dispositivos» y presentan la clave (cabecera X-Clave o cookie inv_clave).
"""

import argparse
from datetime import date, datetime, timedelta
from email.utils import formatdate
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import __version__, csvio, licencia
from .edge import Edge, PriceChanged
from .insights import Insights

WEB = Path(__file__).parent / "web"
LEGAL = Path(__file__).parent / "legal"
# Versión del contrato de licencia que se acepta al activar una licencia (inventario/legal/).
TERMS_VERSION = "2026-11-21"
DEFAULT_PORT = 8470
MAX_BODY = 5 * 1024 * 1024
# Fecha que manda el dispositivo: se acepta hasta 10 min en el futuro (relojes algo
# adelantados) y hasta 30 días en el pasado (ventas guardadas sin conexión).
MAX_FUTURE = timedelta(minutes=10)
MAX_PAST = timedelta(days=30)
TEMPLATE_CSV = ("\ufeffsku;nombre;codigo_barras;categoria;unidad;precio;costo;minimo;stock_inicial;"
                "dias_entrega;empaque;pedido_minimo\n"
                "ARROZ-1KG;Arroz 1 kg;7701234567890;tienda;unidad;5.000;3.500;5;20;3;12;12\n")
KEEP_BACKUPS = 14
SETTINGS = {"store_name": "Mi comercio", "remote_enabled": "0"}


def data_dir() -> Path:
    configured = os.environ.get("INVENTARIO_DATOS")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InventarioInteligente"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "InventarioInteligente"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "inventario-inteligente"


def lan_addresses() -> list[str]:
    """IP privadas del equipo, para mostrar la dirección que usa el celular."""
    found = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no envía nada: solo elige la interfaz
            found.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in found if ipaddress.ip_address(ip).is_private and not ip.startswith("127."))


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


SESSION_HOURS = 12
# Rutas que puede usar un cajero; devolver y anular exigen además el PIN de un dueño.
CASHIER_ROUTES = {("GET", "estado"), ("GET", "productos"), ("GET", "buscar"), ("POST", "ventas"),
                  ("GET", "ventas"), ("POST", "anulaciones"), ("POST", "devoluciones"), ("POST", "sesion")}
NEEDS_OWNER_PIN = {"anulaciones", "devoluciones"}
COST_FIELDS = ("cost", "lead_days", "pack", "moq")


class App:
    """Estado compartido: una conexión SQLite y un cerrojo que serializa su uso."""

    def __init__(self, folder: Path, port: int):
        self.folder = folder
        self.port = port
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "respaldos").mkdir(exist_ok=True)
        self.edge = Edge(folder / "inventario.sqlite3")
        self.lock = threading.RLock()
        # La analítica lee con su propia conexión y su propio cerrojo: en WAL un lector no
        # bloquea al escritor, así un cálculo largo nunca retrasa una venta.
        self.reader = Edge(folder / "inventario.sqlite3", timezone=self.edge.timezone)
        self.insights = Insights(self.reader)
        self.insights_lock = threading.Lock()
        with self.lock:
            self.edge.db.execute("INSERT OR IGNORE INTO metadata VALUES('access_key', ?)",
                                 (secrets.token_urlsafe(18),))
            for key, value in SETTINGS.items():
                self.edge.db.execute("INSERT OR IGNORE INTO metadata VALUES(?, ?)", (f"setting_{key}", value))
        self.imports: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}   # sha256(token) → {user_id, expires}
        self.failures: dict[str, list] = {}   # ip → [fallos, bloqueado_hasta]

    # -- sesiones y PIN ------------------------------------------------

    def check_lock(self, ip: str):
        count, until = self.failures.get(ip, [0, 0.0])
        wait = until - time.monotonic()
        if wait > 0:
            raise ApiError(429, f"demasiados intentos con PIN incorrecto; espere {int(wait) + 1} s")

    def register_attempt(self, ip: str, ok: bool):
        if ok:
            self.failures.pop(ip, None)
            return
        count, _ = self.failures.get(ip, [0, 0.0])
        count += 1
        # 5 fallos → 30 s; cada fallo más dobla la espera (máximo 15 min).
        until = time.monotonic() + min(900, 30 * 2 ** (count - 5)) if count >= 5 else 0.0
        self.failures[ip] = [count, until]

    def new_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {
            "user_id": user_id, "expires": now + SESSION_HOURS * 3600}
        return token

    def session_user(self, token: str) -> dict | None:
        if not token:
            return None
        entry = self.sessions.get(hashlib.sha256(token.encode()).hexdigest())
        if not entry or entry["expires"] < time.time():
            return None
        user = self.edge.user(entry["user_id"])
        return user if user and user["active"] else None

    def drop_session(self, token: str):
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def close(self):
        self.reader.close()
        self.edge.close()

    def license(self) -> dict:
        """Estado de la licencia de esta instalación (plan y límites efectivos)."""
        row = self.edge.db.execute("SELECT value FROM metadata WHERE key='license_text'").fetchone()
        return licencia.status(row[0] if row else None, self.edge.local_today(), self.edge.stream_id)

    def setting(self, key: str) -> str:
        return self.edge.db.execute("SELECT value FROM metadata WHERE key=?", (f"setting_{key}",)).fetchone()[0]

    def access_key(self) -> str:
        return self.edge.db.execute("SELECT value FROM metadata WHERE key='access_key'").fetchone()[0]

    def now(self) -> str:
        return datetime.now(ZoneInfo(self.edge.timezone)).isoformat(timespec="seconds")

    def occurred_at(self, value) -> str:
        """Fecha del dispositivo o la del servidor; rechaza relojes claramente mal puestos."""
        if value is None:
            return self.now()
        if not isinstance(value, str):
            raise ApiError(422, "fecha inválida")
        try:
            instant = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ApiError(422, "fecha inválida") from exc
        if instant.utcoffset() is None:
            raise ApiError(422, "la fecha necesita zona horaria")
        gap = instant - datetime.now(ZoneInfo(self.edge.timezone))
        if gap > MAX_FUTURE or gap < -MAX_PAST:
            raise ApiError(422, "la fecha y hora de este dispositivo no coinciden con las del equipo"
                                f" principal ({self.now()[:16].replace('T', ' ')}); corríjalas y vuelva a intentarlo")
        return value

    def daily_backup(self) -> Path | None:
        """Un respaldo por día al arrancar o al pedirlo; conserva los últimos KEEP_BACKUPS."""
        folder = self.folder / "respaldos"
        target = folder / f"inventario-{self.edge.local_today()}.sqlite3"
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            self.edge.backup(temporary)
            temporary.replace(target)
        for old in sorted(folder.glob("inventario-*.sqlite3"))[:-KEEP_BACKUPS]:
            old.unlink()
        return target


def _body_json(handler) -> dict:
    if getattr(handler, "_json", None) is not None:  # ya leído por la capa de permisos
        return handler._json
    raw = handler.body()
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "JSON inválido") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "se esperaba un objeto JSON")
    handler._json = data
    return data


def _require(data: dict, *keys):
    missing = [k for k in keys if k not in data]
    if missing:
        raise ApiError(422, "faltan campos: " + ", ".join(missing))


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InventarioInteligente"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # sin datos de negocio en el registro
            if os.environ.get("INVENTARIO_LOG"):
                super().log_message(fmt, *args)

        # -- utilidades ------------------------------------------------

        def body(self) -> bytes:
            if self._body is not None:
                return self._body
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True  # no se lee: la conexión no se reutiliza
                raise ApiError(413, "petición demasiado grande (máx. 5 MB)")
            self._body = self.rfile.read(length) if length else b""
            return self._body

        def send(self, status: int, payload, *, content_type="application/json; charset=utf-8",
                 headers: dict | None = None):
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            elif isinstance(payload, str):
                data = payload.encode()
            else:
                data = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def is_local(self) -> bool:
            try:
                return ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:")).is_loopback
            except ValueError:
                return False

        def cookie_key(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            return unquote(cookie["inv_clave"].value) if "inv_clave" in cookie else ""

        def authorized(self) -> bool:
            if self.is_local():
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return host in ("localhost", "127.0.0.1", "::1")
            with app.lock:
                if app.setting("remote_enabled") != "1":
                    return False
                key = app.access_key()
            given = self.headers.get("X-Clave") or self.cookie_key()
            return bool(given) and hmac.compare_digest(given.encode(), key.encode())

        # -- verbos ----------------------------------------------------

        def do_GET(self):
            self.dispatch("GET")

        def do_HEAD(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_PATCH(self):
            self.dispatch("PATCH")

        def dispatch(self, method: str):
            self._body = None
            try:
                self.handle_request(method)
            finally:
                # Un cuerpo sin leer quedaría en la conexión persistente y corrompería la
                # siguiente petición: se descarta siempre (o se cierra la conexión).
                if self._body is None and int(self.headers.get("Content-Length") or 0):
                    try:
                        self.body()
                    except ApiError:
                        pass

        def handle_request(self, method: str):
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            try:
                if path.startswith("/api/"):
                    if not self.authorized():
                        raise ApiError(401 if not self.is_local() else 403, "acceso no autorizado")
                    # Las descargas son enlaces normales (también desde el DownloadManager de
                    # Android): un GET de otra web no puede leer la respuesta, así que no hay CSRF.
                    download = method == "GET" and (path.startswith("/api/v1/exportar/")
                                                    or path.startswith("/api/v1/respaldo/"))
                    if self.headers.get("X-Inventario") != "1" and not download:
                        raise ApiError(400, "falta la cabecera X-Inventario")
                    status, payload, extra = self.api(method, path, query)
                    self.send(status, payload, **extra)
                elif method == "GET":
                    self.static(path, query)
                else:
                    raise ApiError(405, "método no permitido")
            except ApiError as exc:
                self.send(exc.status, {"detail": str(exc), **exc.extra})
            except PriceChanged as exc:
                self.send(409, {"detail": str(exc), "total_minor": exc.total_minor, "prices": exc.prices})
            except ValueError as exc:
                self.send(422, {"detail": str(exc)})
            except Exception:  # noqa: BLE001 - nunca exponer el traceback al cliente
                import traceback
                traceback.print_exc()
                self.send(500, {"detail": "error interno; revise el registro del servidor"})

        def static(self, path: str, query: dict):
            # Enlace del QR: /?clave=... guarda la clave en una cookie y la quita de la URL.
            if "clave" in query and not self.is_local():
                with app.lock:
                    valid = app.setting("remote_enabled") == "1" and hmac.compare_digest(
                        query["clave"][0].encode(), app.access_key().encode())
                if not valid:
                    raise ApiError(401, "clave incorrecta o acceso remoto desactivado")
                self.send(303, "", content_type="text/plain", headers={
                    "Location": "/",
                    "Set-Cookie": f"inv_clave={quote(query['clave'][0])}; Path=/; Max-Age=31536000;"
                                  " HttpOnly; SameSite=Strict"})
                return
            if not self.authorized():
                self.send(401, (WEB / "sin-acceso.html").read_bytes(), content_type="text/html; charset=utf-8")
                return
            root = WEB
            relative = "index.html" if path in ("/", "") else path.lstrip("/")
            if relative.startswith("legal/"):  # contrato, privacidad…: texto, también sin licencia
                root, relative = LEGAL, relative[len("legal/"):]
            target = (root / relative).resolve()
            if root.resolve() not in target.parents or not target.is_file():
                raise ApiError(404, "no encontrado")
            kind = ("text/plain" if target.suffix == ".md" else
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            if target.suffix in (".js", ".css", ".html", ".webmanifest", ".svg", ".md"):
                kind += "; charset=utf-8"
            headers = {"Cache-Control": "no-cache"}
            if target.name == "index.html":
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
                    " connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send(200, target.read_bytes(), content_type=kind, headers=headers)

        # -- API -------------------------------------------------------

        def session_token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            return cookie["inv_sesion"].value if "inv_sesion" in cookie else ""

        def api(self, method: str, path: str, query: dict):
            """Capa de usuarios: sesión, permisos por rol y autorización del dueño."""
            self._json = None
            self.user = None
            parts = [unquote(p) for p in path.split("/")[3:] if p]
            if not path.startswith("/api/v1/") or not parts:
                raise ApiError(404, "ruta desconocida")
            name, rest = parts[0], parts[1:]
            ip = self.client_address[0]
            if name == "sesion":
                return self.session_route(method, rest, ip)
            with app.lock:
                enabled = app.edge.has_users()
                self.user = app.session_user(self.session_token()) if enabled else None
            if name == "usuarios":
                with app.lock:
                    app.edge.limits = app.license()
                try:
                    return self.users_route(method, rest, enabled)
                finally:
                    app.edge.limits = None
            if enabled and self.user is None and (method, name) != ("GET", "estado"):
                raise ApiError(401, "inicie sesión", login=True)
            authorized_by = None
            if self.user and self.user["role"] == "cajero":
                if (method, name) not in CASHIER_ROUTES or (name == "productos" and rest):
                    raise ApiError(403, "esta acción la hace el dueño")
                if method == "POST" and name in NEEDS_OWNER_PIN:
                    pin = _body_json(self).get("pin_dueno")
                    with app.lock:
                        app.check_lock(ip)
                        owner = app.edge.owner_by_pin(pin) if pin else None
                        app.register_attempt(ip, owner is not None)
                    if owner is None:
                        raise ApiError(403, "hace falta el PIN del dueño para autorizar", owner_pin=True)
                    authorized_by = owner["id"]
            elif self.user:
                authorized_by = self.user["id"]  # un dueño se autoriza a sí mismo
            if method == "GET":
                status, payload, extra = self.routes(method, path, query, name, rest)
            else:
                with app.lock:
                    app.edge.actor = (self.user["id"] if self.user else None,
                                      authorized_by if name in NEEDS_OWNER_PIN else None)
                    app.edge.limits = app.license()
                    try:
                        status, payload, extra = self.routes(method, path, query, name, rest)
                    finally:
                        app.edge.actor = (None, None)
                        app.edge.limits = None
            if self.user and self.user["role"] == "cajero" and name in ("productos", "buscar"):
                payload = ([{k: v for k, v in p.items() if k not in COST_FIELDS} for p in payload]
                           if isinstance(payload, list) else {k: v for k, v in payload.items() if k not in COST_FIELDS})
            return status, payload, extra

        def session_route(self, method: str, rest: list, ip: str):
            if method != "POST":
                raise ApiError(405, "método no permitido")
            if rest == ["salir"]:
                with app.lock:
                    app.drop_session(self.session_token())
                return 200, {"ok": True}, {"headers": {"Set-Cookie": "inv_sesion=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"}}
            if rest:
                raise ApiError(404, "ruta desconocida")
            data = _body_json(self)
            _require(data, "user_id", "pin")
            with app.lock:
                app.check_lock(ip)
                user = app.edge.check_pin(data["user_id"], data["pin"]) if type(data["user_id"]) is int else None
                app.register_attempt(ip, user is not None)
                if user is None:
                    raise ApiError(401, "PIN incorrecto")
                token = app.new_session(user["id"])
            return 200, user, {"headers": {"Set-Cookie": f"inv_sesion={token}; Path=/; Max-Age={SESSION_HOURS * 3600};"
                                                        " HttpOnly; SameSite=Strict"}}

        def users_route(self, method: str, rest: list, enabled: bool):
            # Sin usuarios, cualquiera con acceso al equipo puede crear el primer dueño.
            if enabled and (self.user is None or self.user["role"] != "dueno"):
                raise ApiError(401 if self.user is None else 403, "solo el dueño gestiona usuarios",
                               login=self.user is None)
            with app.lock:
                if method == "GET" and not rest:
                    return 200, app.edge.users(), {}
                if method == "POST" and not rest:
                    data = _body_json(self)
                    _require(data, "name", "role", "pin")
                    if not enabled and data["role"] != "dueno":
                        raise ApiError(422, "el primer usuario debe ser el dueño")
                    user = app.edge.add_user(data["name"], data["role"], data["pin"])
                    extra = {}
                    if not enabled:  # quien activa el PIN queda con la sesión abierta
                        token = app.new_session(user["id"])
                        extra = {"headers": {"Set-Cookie": f"inv_sesion={token}; Path=/; Max-Age={SESSION_HOURS * 3600};"
                                                           " HttpOnly; SameSite=Strict"}}
                    return 201, user, extra
                if method == "PATCH" and len(rest) == 1 and rest[0].isdigit():
                    data = _body_json(self)
                    unknown = set(data) - {"pin", "active", "name"}
                    if unknown:
                        raise ApiError(422, "campos desconocidos: " + ", ".join(sorted(unknown)))
                    return 200, app.edge.update_user(int(rest[0]), **data), {}
            raise ApiError(404, "ruta desconocida")

        def routes(self, method: str, path: str, query: dict, name: str, rest: list):
            q = {k: v[-1] for k, v in query.items()}
            if method == "GET" and name in ("tablero", "alertas", "compras", "analisis"):
                with app.insights_lock:
                    return 200, {"tablero": app.insights.dashboard, "alertas": app.insights.alerts,
                                 "compras": app.insights.purchases, "analisis": app.insights.reports}[name](), {}
            if method == "GET" and name == "productos" and len(rest) == 1:
                with app.insights_lock:
                    report = app.insights.product_report(app.reader.product(rest[0]))
                    report["movements"] = app.reader.movements(sku=rest[0], limit=50)
                return 200, report, {}
            with app.lock:
                edge = app.edge
                if name == "estado" and method == "GET":
                    return 200, self.state(), {}
                if name == "productos":
                    if method == "GET" and not rest:
                        if q.get("top") == "1":
                            since = (date.fromisoformat(edge.local_today()) - timedelta(days=30)).isoformat()
                            return 200, edge.top_sellers(since=since), {}
                        return 200, edge.products(query=q.get("q", "").strip(),
                                                  include_inactive=q.get("inactivos") == "1"), {}
                    if method == "POST" and not rest:
                        data = _body_json(self)
                        _require(data, "sku", "price", "cost")
                        sku = data.pop("sku")
                        initial = data.pop("initial_stock", 0)
                        product = edge.add_product(sku, **data)
                        if initial:
                            edge.move(f"alta-{uuid4()}", product["sku"], "receive", initial, app.now(),
                                      note="Stock inicial")
                            product = edge.product(product["sku"])
                        return 201, product, {}
                    if rest and method == "PATCH":
                        return 200, edge.update_product(rest[0], **_body_json(self)), {}
                if name == "buscar" and method == "GET":
                    product = edge.find(q.get("codigo", "").strip())
                    if not product:
                        raise ApiError(404, "no hay ningún producto con ese código")
                    return 200, product, {}
                if name == "ventas" and method == "GET":
                    day = q.get("dia") or None
                    if day:
                        date.fromisoformat(day)
                    return 200, edge.tickets(day=day, limit=min(200, int(q.get("limite", "50")))), {}
                if name == "anulaciones" and method == "POST":
                    data = _body_json(self)
                    when = app.occurred_at(data.get("occurred_at"))
                    if rest == ["ticket"]:
                        _require(data, "command_id", "ticket_id", "reason")
                        return 200, edge.void_ticket(data["command_id"], data["ticket_id"], when, data["reason"]), {}
                    if not rest:
                        _require(data, "command_id", "seq", "reason")
                        return 200, edge.void(data["command_id"], data["seq"], when, data["reason"]), {}
                if name == "devoluciones" and method == "POST":
                    data = _body_json(self)
                    _require(data, "command_id", "seq", "quantity", "reason")
                    return 200, edge.return_sale(data["command_id"], data["seq"], data["quantity"],
                                                 app.occurred_at(data.get("occurred_at")), data["reason"]), {}
                if name == "ventas" and method == "POST":
                    data = _body_json(self)
                    _require(data, "ticket_id", "lines")
                    expected = data.get("expected_total_minor")
                    if expected is not None and (type(expected) is not int or expected < 0):
                        raise ApiError(422, "expected_total_minor inválido")
                    return 200, edge.sell(data["ticket_id"], data["lines"], app.occurred_at(data.get("occurred_at")),
                                          expected_total_minor=expected), {}
                if name == "movimientos":
                    if method == "GET":
                        limit = min(500, int(q.get("limite", "100")))
                        return 200, edge.movements(sku=q.get("sku") or None, limit=limit), {}
                    if method == "POST":
                        data = _body_json(self)
                        _require(data, "command_id", "sku", "kind", "quantity")
                        if data["kind"] not in ("receive", "waste"):
                            raise ApiError(422, "use /ventas para vender; aquí: receive o waste")
                        return 200, edge.move(data["command_id"], data["sku"], data["kind"], data["quantity"],
                                              app.occurred_at(data.get("occurred_at")), note=data.get("note")), {}
                if name == "conteos" and method == "POST":
                    data = _body_json(self)
                    _require(data, "command_id", "sku", "counted")
                    receipt = edge.adjust_count(data["command_id"], data["sku"], data["counted"],
                                                app.occurred_at(data.get("occurred_at")))
                    return 200, {"changed": receipt is not None, "receipt": receipt,
                                 "product": edge.product(data["sku"])}, {}
                if name == "resumen" and method == "GET":
                    today = date.fromisoformat(edge.local_today())
                    start = q.get("desde") or (today - timedelta(days=today.weekday())).isoformat()
                    end = q.get("hasta") or (date.fromisoformat(start) + timedelta(days=7)).isoformat()
                    summary = edge.weekly_summary(start, end)
                    summary["by_day"] = [edge.weekly_summary(d.isoformat(), (d + timedelta(days=1)).isoformat())
                                         for d in (date.fromisoformat(start) + timedelta(days=i)
                                                   for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days))][:62]
                    return 200, summary, {}
                if name == "importar" and method == "POST":
                    return 200, self.import_csv(q), {}
                if name == "exportar" and method == "GET" and rest:
                    builders = {"productos.csv": csvio.export_products, "movimientos.csv": csvio.export_movements,
                                "plantilla-productos.csv": lambda _edge: TEMPLATE_CSV}
                    if rest[0] not in builders:
                        raise ApiError(404, "exportación desconocida")
                    return 200, builders[rest[0]](edge), {
                        "content_type": "text/csv; charset=utf-8",
                        "headers": {"Content-Disposition": f'attachment; filename="{rest[0]}"'}}
                if name == "respaldo":
                    return self.backup(method, rest)
                if name == "licencia" and not rest:
                    return 200, self.license_route(method), {}
                if name == "ajustes":
                    return 200, self.settings(method), {}
            raise ApiError(404, "ruta desconocida")

        def license_route(self, method: str) -> dict:
            if method == "POST":
                data = _body_json(self)
                _require(data, "license", "accept_terms")
                if data["accept_terms"] is not True:
                    raise ApiError(422, "para activar la licencia hay que aceptar el contrato de licencia")
                claims = licencia.decode(data["license"])
                if claims.get("instalacion") and claims["instalacion"] != app.edge.stream_id:
                    raise ApiError(422, "esta licencia es de otra instalación")
                text = "".join(data["license"].split())
                accepted = {"version": TERMS_VERSION, "at": app.now(), "licencia_id": claims["licencia_id"],
                            "user": self.user["name"] if self.user else None}
                app.edge.db.execute("INSERT OR REPLACE INTO metadata VALUES('license_text', ?)", (text,))
                app.edge.db.execute("INSERT OR REPLACE INTO metadata VALUES('terms_accepted', ?)",
                                    (json.dumps(accepted, ensure_ascii=False),))
            status = app.license()
            row = app.edge.db.execute("SELECT value FROM metadata WHERE key='terms_accepted'").fetchone()
            return {**status, "usage": app.edge.usage(), "installation": app.edge.stream_id,
                    "terms_version": TERMS_VERSION, "terms_accepted": json.loads(row[0]) if row else None}

        def state(self) -> dict:
            enabled = app.edge.has_users()
            login = [{"id": u["id"], "name": u["name"], "role": u["role"]}
                     for u in app.edge.users() if u["active"]] if enabled and self.user is None else None
            plan = app.license()
            return {"version": __version__, "store_name": app.setting("store_name"),
                    "plan": {"name": plan["name"], "licensed": plan["licensed"], "warning": plan["warning"]},
                    "users_enabled": enabled, "user": self.user, "login_users": login,
                    "timezone": app.edge.timezone, "currency": "COP", "today": app.edge.local_today(),
                    "local": self.is_local(), "time": formatdate(usegmt=True)}

        def settings(self, method: str) -> dict:
            if method == "PATCH":
                data = _body_json(self)
                if "store_name" in data:
                    name = str(data["store_name"]).strip()
                    if not 1 <= len(name) <= 80:
                        raise ApiError(422, "nombre del comercio: 1 a 80 caracteres")
                    app.edge.db.execute("UPDATE metadata SET value=? WHERE key='setting_store_name'", (name,))
                if "remote_enabled" in data:
                    if not self.is_local():
                        raise ApiError(403, "el acceso remoto solo se cambia desde el propio equipo")
                    app.edge.db.execute("UPDATE metadata SET value=? WHERE key='setting_remote_enabled'",
                                        ("1" if data["remote_enabled"] is True else "0",))
                if data.get("new_key") is True:
                    if not self.is_local():
                        raise ApiError(403, "la clave solo se cambia desde el propio equipo")
                    app.edge.db.execute("UPDATE metadata SET value=? WHERE key='access_key'",
                                        (secrets.token_urlsafe(18),))
            out = {"store_name": app.setting("store_name"),
                   "remote_enabled": app.setting("remote_enabled") == "1", "data_dir": None}
            if self.is_local():  # la clave y las rutas nunca se muestran a otro dispositivo
                out.update(access_key=app.access_key(), data_dir=str(app.folder), port=app.port,
                           addresses=[f"http://{ip}:{app.port}" for ip in lan_addresses()])
            return out

        def import_csv(self, q: dict) -> dict:
            if q.get("aplicar") == "1":
                token = q.get("token", "")
                plan = app.imports.pop(token, None)
                if plan is None:
                    raise ApiError(409, "la vista previa caducó; vuelva a subir el archivo")
                return csvio.apply_import(app.edge, plan, app.now(), plan["hash"])
            raw = self.body()
            parsed = csvio.parse_products(raw)
            plan = csvio.plan_import(app.edge, parsed)
            plan["hash"] = hashlib.sha256(raw).hexdigest()
            token = secrets.token_urlsafe(12)
            app.imports = {token: plan}  # una vista previa pendiente a la vez
            return {**{k: v for k, v in plan.items() if k != "hash"}, "token": token}

        def backup(self, method: str, rest: list):
            folder = app.folder / "respaldos"
            if method == "POST" and not rest:
                target = folder / f"inventario-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.sqlite3"
                app.edge.backup(target)
                return 200, {"file": target.name}, {}
            if method == "GET" and not rest:
                files = sorted(folder.glob("inventario-*.sqlite3"), reverse=True)
                return 200, [{"file": f.name, "bytes": f.stat().st_size} for f in files], {}
            if method == "GET" and len(rest) == 1:
                target = (folder / rest[0]).resolve()
                if target.parent != folder.resolve() or not target.is_file():
                    raise ApiError(404, "respaldo no encontrado")
                return 200, target.read_bytes(), {
                    "content_type": "application/vnd.sqlite3",
                    "headers": {"Content-Disposition": f'attachment; filename="{target.name}"'}}
            raise ApiError(404, "ruta desconocida")

    return Handler


def create_server(folder: Path, port: int = DEFAULT_PORT, host: str = "0.0.0.0"):
    app = App(folder, port)
    with app.lock:
        app.daily_backup()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.port = server.server_address[1]
    server.app = app
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Servidor local de Inventario Inteligente")
    parser.add_argument("--puerto", type=int, default=int(os.environ.get("INVENTARIO_PUERTO", DEFAULT_PORT)))
    parser.add_argument("--datos", type=Path, default=None)
    parser.add_argument("--solo-local", action="store_true", help="escuchar solo en 127.0.0.1")
    args = parser.parse_args(argv)
    server = create_server(args.datos or data_dir(), args.puerto, "127.0.0.1" if args.solo_local else "0.0.0.0")
    print(f"Inventario Inteligente {__version__} en http://127.0.0.1:{server.app.port}  (datos: {server.app.folder})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.app.close()


if __name__ == "__main__":
    main()
