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
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import __version__, csvio
from .edge import Edge, PriceChanged
from .insights import Insights

WEB = Path(__file__).parent / "web"
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
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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

    def close(self):
        self.reader.close()
        self.edge.close()

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
    raw = handler.body()
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "JSON inválido") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "se esperaba un objeto JSON")
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
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "petición demasiado grande (máx. 5 MB)")
            return self.rfile.read(length) if length else b""

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
                self.send(exc.status, {"detail": str(exc)})
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
            relative = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (WEB / relative).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                raise ApiError(404, "no encontrado")
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in (".js", ".css", ".html", ".webmanifest", ".svg"):
                kind += "; charset=utf-8"
            headers = {"Cache-Control": "no-cache"}
            if target.name == "index.html":
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
                    " connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send(200, target.read_bytes(), content_type=kind, headers=headers)

        # -- API -------------------------------------------------------

        def api(self, method: str, path: str, query: dict):
            parts = [unquote(p) for p in path.split("/")[3:] if p]  # tras /api/v1/
            q = {k: v[-1] for k, v in query.items()}
            if not path.startswith("/api/v1/") or not parts:
                raise ApiError(404, "ruta desconocida")
            name, rest = parts[0], parts[1:]
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
                if name == "ajustes":
                    return 200, self.settings(method), {}
            raise ApiError(404, "ruta desconocida")

        def state(self) -> dict:
            return {"version": __version__, "store_name": app.setting("store_name"),
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
