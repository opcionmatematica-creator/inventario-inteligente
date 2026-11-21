"""Core local sin dependencias de red: catálogo, stock, movimientos, tickets y outbox."""

from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from .analytics import integer

CATEGORIES = ("tienda", "perecedero", "ferreteria")
KINDS = ("receive", "sale", "waste")
MAX_TICKET_LINES = 200
PRODUCT_FIELDS = ("name", "barcode", "category", "unit", "price", "cost", "minimum",
                  "lead_days", "pack", "moq", "active")

# Columnas añadidas después de la referencia inicial. Se crean con ALTER TABLE
# condicional para no romper bases ya existentes.
PRODUCT_MIGRATIONS = (
    ("name", "TEXT NOT NULL DEFAULT ''"),
    ("barcode", "TEXT"),
    ("category", "TEXT NOT NULL DEFAULT 'tienda'"),
    ("unit", "TEXT NOT NULL DEFAULT 'unidad'"),
    ("lead_days", "REAL NOT NULL DEFAULT 3"),
    ("pack", "INTEGER NOT NULL DEFAULT 1"),
    ("moq", "INTEGER NOT NULL DEFAULT 1"),
    ("active", "INTEGER NOT NULL DEFAULT 1"),
)
EVENT_MIGRATIONS = (("sku", "TEXT"), ("kind", "TEXT"), ("day", "TEXT"), ("note", "TEXT"), ("quantity", "INTEGER"),
                    ("ref_seq", "INTEGER"), ("sold_delta", "INTEGER"))
TICKET_MIGRATIONS = (("day", "TEXT"),)
# Quién hizo cada movimiento y quién lo autorizó (dueño). Solo local: no viaja en la sincronización.
EVENT_MIGRATIONS += (("user_id", "INTEGER"), ("authorized_by", "INTEGER"))
ROLES = ("dueno", "cajero")
PIN_ITERATIONS = 200_000
# Movimientos compensatorios: nunca se borra la historia (ver docs/arquitectura.md).
COMPENSATIONS = ("void", "return")
# Métrica de unidades que mueve cada tipo, como en las filas de sincronización.
METRIC = {"receive": "received_units", "sale": "sold_units", "waste": "waste_units"}


class PriceChanged(ValueError):
    """El total calculado no coincide con el que vio el cajero."""

    def __init__(self, total_minor: int, prices: dict[str, int]):
        super().__init__("los precios cambiaron: revise el ticket antes de cobrar")
        self.total_minor = total_minor
        self.prices = prices


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _text(name: str, value, *, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} debe ser texto")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} requerido")
    if len(value) > maximum:
        raise ValueError(f"{name}: máximo {maximum} caracteres")
    return value


def _instant(occurred_at: str) -> datetime:
    if not isinstance(occurred_at, str):
        raise ValueError("se requiere fecha con zona horaria")
    instant = datetime.fromisoformat(occurred_at)
    if instant.utcoffset() is None:
        raise ValueError("se requiere fecha con zona horaria")
    return instant


def _sold_delta(item: dict) -> int:
    """Efecto de un evento en las unidades vendidas netas (venta +, devolución/anulación −)."""
    if item["kind"] == "sale":
        return item["quantity"]
    if item["kind"] in COMPENSATIONS and item.get("metric") == "sold_units":
        return item["units"]
    return 0


def stock_delta(item: dict) -> int:
    """Cambio de stock que produjo un evento (cualquier tipo)."""
    if item["kind"] in COMPENSATIONS:
        return item["units"] if item["metric"] == "received_units" else -item["units"]
    return item["quantity"] if item["kind"] == "receive" else -item["quantity"]


def _command_id(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("command_id requerido, máximo 100 caracteres")
    return value


class Edge:
    def __init__(self, path, timezone="America/Bogota"):
        ZoneInfo(timezone)
        # check_same_thread=False: el servidor local serializa el acceso con un
        # cerrojo y usa la conexión desde varios hilos, nunca a la vez.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products(
                sku TEXT PRIMARY KEY, price INTEGER NOT NULL CHECK(price >= 0),
                cost INTEGER NOT NULL CHECK(cost >= 0),
                minimum INTEGER NOT NULL CHECK(minimum >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0));
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL UNIQUE,
                request TEXT NOT NULL, payload TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sync_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                ack_seq INTEGER NOT NULL DEFAULT 0, pending TEXT);
            CREATE TABLE IF NOT EXISTS tickets(
                ticket_id TEXT PRIMARY KEY, request TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                role TEXT NOT NULL CHECK(role IN ('dueno','cajero')),
                pin_salt BLOB NOT NULL, pin_hash BLOB NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            INSERT OR IGNORE INTO sync_state(singleton) VALUES(1);
        """)
        self._migrate()
        # (usuario que hace el movimiento, dueño que lo autoriza). El servidor lo fija bajo su
        # cerrojo antes de cada operación; None sin usuarios configurados.
        self.actor: tuple[int | None, int | None] = (None, None)
        with transaction(self.db):
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('stream_id', ?)", (str(uuid4()),))
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('timezone', ?)", (timezone,))
        self.stream_id = self.db.execute("SELECT value FROM metadata WHERE key='stream_id'").fetchone()[0]
        self.timezone = self.db.execute("SELECT value FROM metadata WHERE key='timezone'").fetchone()[0]
        if self.timezone != timezone:
            self.close()
            raise ValueError("la zona horaria persistida no puede cambiarse implícitamente")

    # ---------------------------------------------------------
    # Esquema
    # ---------------------------------------------------------

    def _migrate(self):
        with transaction(self.db):
            for table, columns in (("products", PRODUCT_MIGRATIONS), ("events", EVENT_MIGRATIONS),
                                   ("tickets", TICKET_MIGRATIONS)):
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns:
                    if name not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            # Eventos anteriores a las columnas indexadas: se rellenan desde el payload.
            for row in self.db.execute("SELECT seq, payload FROM events WHERE sku IS NULL OR quantity IS NULL"
                                       " OR sold_delta IS NULL").fetchall():
                item = json.loads(row["payload"])
                self.db.execute("UPDATE events SET sku=?, kind=?, day=?, quantity=?, sold_delta=?, ref_seq=? WHERE seq=?",
                                (item["sku"], item["kind"], item["day"], item["quantity"], _sold_delta(item),
                                 item.get("ref_seq"), row["seq"]))
            # Tickets anteriores a la columna day (solo los de venta tienen «day» en el recibo).
            for row in self.db.execute("SELECT ticket_id, receipt FROM tickets WHERE day IS NULL").fetchall():
                day = json.loads(row["receipt"]).get("occurred_at") and json.loads(row["receipt"]).get("day")
                if day:
                    self.db.execute("UPDATE tickets SET day=? WHERE ticket_id=?", (day, row["ticket_id"]))
            self.db.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS products_barcode ON products(barcode) WHERE barcode IS NOT NULL;
                CREATE INDEX IF NOT EXISTS events_day ON events(day);
                CREATE INDEX IF NOT EXISTS events_sku_day ON events(sku, day);
                CREATE INDEX IF NOT EXISTS events_kind_day ON events(kind, day, sku, quantity);
                CREATE INDEX IF NOT EXISTS events_ref ON events(ref_seq) WHERE ref_seq IS NOT NULL;
                CREATE INDEX IF NOT EXISTS tickets_day ON tickets(day) WHERE day IS NOT NULL;
                CREATE INDEX IF NOT EXISTS events_sold ON events(day, sku, sold_delta) WHERE sold_delta <> 0;
            """)

    def close(self):
        self.db.close()

    def local_today(self) -> str:
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()

    # ---------------------------------------------------------
    # Catálogo
    # ---------------------------------------------------------

    def _product_values(self, fields: dict, *, partial: bool) -> dict:
        unknown = set(fields) - set(PRODUCT_FIELDS)
        if unknown:
            raise ValueError(f"campos desconocidos: {', '.join(sorted(unknown))}")
        out = {}
        for key, value in fields.items():
            if key == "name":
                out[key] = _text("nombre", value, maximum=120)
            elif key == "barcode":
                code = None if value is None else _text("código de barras", value, maximum=64)
                out[key] = code or None
            elif key == "unit":
                out[key] = _text("unidad", value, maximum=20, required=True)
            elif key == "category":
                if value not in CATEGORIES:
                    raise ValueError("categoría desconocida")
                out[key] = value
            elif key == "lead_days":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 365:
                    raise ValueError("días de entrega entre 0 y 365")
                out[key] = float(value)
            elif key == "active":
                if not isinstance(value, bool):
                    raise ValueError("activo debe ser verdadero o falso")
                out[key] = int(value)
            else:
                integer(**{key: value})
                if key in ("pack", "moq") and value < 1:
                    raise ValueError(f"{key} debe ser al menos 1")
                out[key] = value
        if not partial:
            for key in ("price", "cost"):
                if key not in out:
                    raise ValueError(f"{key} requerido")
        return out

    def add_product(self, sku: str, *, price: int, cost: int, minimum: int = 0, **extra):
        if not isinstance(sku, str) or not sku.strip() or len(sku.strip()) > 100:
            raise ValueError("SKU requerido, máximo 100 caracteres")
        sku = sku.strip()
        values = self._product_values({"price": price, "cost": cost, "minimum": minimum, **extra},
                                      partial=False)
        values.setdefault("name", sku)
        columns = ["sku", *values]
        try:
            self.db.execute(f"INSERT INTO products({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                            (sku, *values.values()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("SKU o código de barras ya existe") from exc
        return self.product(sku)

    def update_product(self, sku: str, **fields) -> dict:
        self.product(sku)
        values = self._product_values(fields, partial=True)
        if not values:
            return self.product(sku)
        try:
            self.db.execute(f"UPDATE products SET {', '.join(f'{k}=?' for k in values)} WHERE sku=?",
                            (*values.values(), sku))
        except sqlite3.IntegrityError as exc:
            raise ValueError("código de barras ya existe") from exc
        return self.product(sku)

    def product(self, sku: str) -> dict:
        row = self.db.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        if row is None:
            raise ValueError("producto desconocido")
        return dict(row)

    def find(self, code: str) -> dict | None:
        """Por SKU exacto o código de barras (lo que entrega un lector USB)."""
        row = self.db.execute("SELECT * FROM products WHERE sku=? OR barcode=? LIMIT 1",
                              (code, code)).fetchone()
        return dict(row) if row else None

    def products(self, *, query: str = "", include_inactive: bool = False, limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM products WHERE 1=1"
        args: list = []
        if not include_inactive:
            sql += " AND active=1"
        if query:
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql += r" AND (sku LIKE ? ESCAPE '\' OR name LIKE ? ESCAPE '\' OR barcode=?)"
            args += [like, like, query]
        sql += " ORDER BY name COLLATE NOCASE, sku LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.db.execute(sql, args)]

    # ---------------------------------------------------------
    # Movimientos
    # ---------------------------------------------------------

    def _apply(self, command_id: str, sku: str, kind: str, quantity: int, day: str,
               unit_price_minor: int | None, request: str, note: str | None) -> dict:
        """Aplica un movimiento ya validado. Debe llamarse dentro de una transacción."""
        product = self.product(sku)
        delta = quantity if kind == "receive" else -quantity
        stock = product["stock"] + delta
        if stock < 0:
            raise ValueError(f"stock insuficiente de {product['name'] or sku} (hay {product['stock']})")
        price = product["price"] if unit_price_minor is None else unit_price_minor
        payload = {"sku": sku, "day": day, "kind": kind, "quantity": quantity,
                   "revenue_minor": quantity * price if kind == "sale" else 0,
                   "cogs_minor": quantity * product["cost"] if kind == "sale" else 0,
                   "waste_minor": quantity * product["cost"] if kind == "waste" else 0,
                   "stock_after": stock}
        receipt = {"command_id": command_id, "sku": sku, "stock": stock,
                   "low_stock": stock <= product["minimum"],
                   "gross_margin_minor": payload["revenue_minor"] - payload["cogs_minor"]}
        self.db.execute("UPDATE products SET stock=? WHERE sku=?", (stock, sku))
        self.db.execute("INSERT INTO events(command_id, request, payload, receipt, sku, kind, day, note, quantity,"
                        " sold_delta, user_id, authorized_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (command_id, request, canonical(payload), canonical(receipt), sku, kind, day, note, quantity,
                         _sold_delta(payload), *self.actor))
        return receipt

    # ---------------------------------------------------------
    # Anulaciones y devoluciones (movimientos compensatorios)
    # ---------------------------------------------------------

    def _event(self, seq: int) -> dict:
        if type(seq) is not int:
            raise ValueError("movimiento inválido")
        row = self.db.execute("SELECT seq, command_id, payload FROM events WHERE seq=?", (seq,)).fetchone()
        if row is None:
            raise ValueError("movimiento desconocido")
        return {"seq": row["seq"], "command_id": row["command_id"], **json.loads(row["payload"])}

    def _status(self, seq: int) -> dict:
        """Estado de un movimiento: si está anulado y cuántas unidades se devolvieron (devoluciones vigentes)."""
        voided = self.db.execute("SELECT 1 FROM events WHERE ref_seq=? AND kind='void'", (seq,)).fetchone() is not None
        returned = self.db.execute(
            "SELECT coalesce(sum(r.quantity), 0) FROM events r WHERE r.ref_seq=? AND r.kind='return'"
            " AND NOT EXISTS (SELECT 1 FROM events v WHERE v.ref_seq=r.seq AND v.kind='void')", (seq,)).fetchone()[0]
        return {"voided": voided, "returned": returned}

    def _compensate(self, command_id: str, original: dict, kind: str, quantity: int, day: str,
                    request: str, reason: str) -> dict:
        """Registra un movimiento que deshace `quantity` unidades del original. Dentro de una transacción."""
        product = self.product(original["sku"])
        if original["kind"] in COMPENSATIONS:  # anular una devolución: rehace la venta devuelta
            metric, units = original["metric"], -original["units"]
            factor = 1
            money = {k: -original[k] for k in ("revenue_minor", "cogs_minor", "waste_minor")}
        else:
            metric, units = METRIC[original["kind"]], -quantity
            money = {k: -(original[k] // original["quantity"]) * quantity
                     for k in ("revenue_minor", "cogs_minor", "waste_minor")}
            factor = quantity
        payload = {"sku": original["sku"], "day": day, "kind": kind, "quantity": quantity if factor else 0,
                   **money, "ref_seq": original["seq"], "metric": metric, "units": units}
        stock = product["stock"] + stock_delta(payload)
        if stock < 0:
            raise ValueError(f"no se puede: dejaría el stock de {product['name'] or product['sku']} en negativo"
                             f" (hay {product['stock']})")
        payload["stock_after"] = stock
        receipt = {"command_id": command_id, "sku": original["sku"], "stock": stock, "kind": kind,
                   "ref_seq": original["seq"], "low_stock": stock <= product["minimum"],
                   "revenue_minor": money["revenue_minor"]}
        self.db.execute("UPDATE products SET stock=? WHERE sku=?", (stock, original["sku"]))
        self.db.execute("INSERT INTO events(command_id, request, payload, receipt, sku, kind, day, note, quantity,"
                        " ref_seq, sold_delta, user_id, authorized_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (command_id, request, canonical(payload), canonical(receipt), original["sku"], kind, day,
                         reason, payload["quantity"], original["seq"], _sold_delta(payload), *self.actor))
        return receipt

    def _void_one(self, command_id: str, seq: int, occurred_at: str, reason: str, request: str) -> dict:
        original = self._event(seq)
        if original["kind"] == "void":
            raise ValueError("una anulación no se puede anular")
        status = self._status(seq)
        if status["voided"]:
            raise ValueError("ese movimiento ya está anulado")
        quantity = original["quantity"]
        if original["kind"] == "sale":
            quantity -= status["returned"]  # lo devuelto ya está compensado
            if quantity == 0:
                raise ValueError("esa venta ya se devolvió completa")
        if original["kind"] == "return" and self._status(original["ref_seq"])["voided"]:
            raise ValueError("la venta de esa devolución está anulada")
        _instant(occurred_at)
        # La anulación corrige el día del movimiento original: el error «no ocurrió».
        return self._compensate(command_id, original, "void", quantity, original["day"], request, reason)

    def void(self, command_id: str, seq: int, occurred_at: str, reason: str) -> dict:
        """Anula un movimiento (entrada, merma, venta o devolución) con un movimiento compensatorio."""
        _command_id(command_id)
        reason = _text("motivo", reason, maximum=200, required=True)
        request = canonical(["void", seq, occurred_at, reason])
        with transaction(self.db):
            previous = self.db.execute("SELECT request, receipt FROM events WHERE command_id=?", (command_id,)).fetchone()
            if previous:
                if previous["request"] != request:
                    raise ValueError("command_id reutilizado con contenido diferente")
                return json.loads(previous["receipt"])
            return self._void_one(command_id, seq, occurred_at, reason, request)

    def return_sale(self, command_id: str, seq: int, quantity: int, occurred_at: str, reason: str) -> dict:
        """Devolución de un cliente sobre una línea de venta: repone stock y descuenta ingreso y costo cobrados."""
        _command_id(command_id)
        integer(quantity=quantity)
        if quantity == 0:
            raise ValueError("cantidad debe ser positiva")
        reason = _text("motivo", reason, maximum=200, required=True)
        day = self._day(occurred_at)
        request = canonical(["return", seq, quantity, occurred_at, reason])
        with transaction(self.db):
            previous = self.db.execute("SELECT request, receipt FROM events WHERE command_id=?", (command_id,)).fetchone()
            if previous:
                if previous["request"] != request:
                    raise ValueError("command_id reutilizado con contenido diferente")
                return json.loads(previous["receipt"])
            original = self._event(seq)
            if original["kind"] != "sale":
                raise ValueError("solo se devuelven ventas")
            status = self._status(seq)
            if status["voided"]:
                raise ValueError("esa venta está anulada")
            pending = original["quantity"] - status["returned"]
            if quantity > pending:
                raise ValueError(f"solo quedan {pending} por devolver de esa venta")
            return self._compensate(command_id, original, "return", quantity, day, request, reason)

    def void_ticket(self, command_id: str, ticket_id: str, occurred_at: str, reason: str) -> dict:
        """Anula todas las líneas vigentes de un ticket a la vez (todo o nada)."""
        _command_id(command_id)
        reason = _text("motivo", reason, maximum=200, required=True)
        request = canonical(["void_ticket", ticket_id, occurred_at, reason])
        with transaction(self.db):
            previous = self.db.execute("SELECT request, receipt FROM tickets WHERE ticket_id=?", (command_id,)).fetchone()
            if previous:
                if previous["request"] != request:
                    raise ValueError("command_id reutilizado con contenido diferente")
                return json.loads(previous["receipt"])
            if not self.db.execute("SELECT 1 FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone():
                raise ValueError("ticket desconocido")
            lines = []
            for row in self._ticket_events(ticket_id):
                status = self._status(row["seq"])
                if status["voided"] or row["quantity"] == status["returned"]:
                    continue
                lines.append(self._void_one(f"{command_id}#{len(lines) + 1}", row["seq"], occurred_at, reason,
                                            canonical(["void", row["seq"], occurred_at, reason])))
            if not lines:
                raise ValueError("ese ticket ya está anulado o devuelto por completo")
            receipt = {"command_id": command_id, "ticket_id": ticket_id, "lines": lines,
                       "revenue_minor": sum(line["revenue_minor"] for line in lines)}
            self.db.execute("INSERT INTO tickets(ticket_id, request, receipt) VALUES(?,?,?)",
                            (command_id, request, canonical(receipt)))
        return receipt

    def _ticket_events(self, ticket_id: str) -> list[dict]:
        # Las líneas se guardan como «ticket#n»: rango sobre la clave única (usa el índice;
        # «$» es el carácter siguiente a «#»).
        rows = self.db.execute("SELECT seq FROM events WHERE command_id >= ? AND command_id < ? AND kind='sale'"
                               " ORDER BY seq", (ticket_id + "#", ticket_id + "$")).fetchall()
        return [self._event(row[0]) for row in rows]

    def tickets(self, *, day: str | None = None, limit: int = 50) -> list[dict]:
        """Tickets de venta (más recientes primero) con el estado de cada línea."""
        # Solo los tickets de venta tienen día (las anulaciones de ticket se guardan sin él).
        if day:
            rows = self.db.execute("SELECT ticket_id, receipt FROM tickets WHERE day=? ORDER BY rowid DESC LIMIT ?",
                                   (day, limit))
        else:
            rows = self.db.execute("SELECT ticket_id, receipt FROM tickets WHERE day IS NOT NULL"
                                   " ORDER BY rowid DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            receipt = json.loads(row["receipt"])
            lines, net = [], 0
            for event in self._ticket_events(row["ticket_id"]):
                status = self._status(event["seq"])
                name = self.db.execute("SELECT name FROM products WHERE sku=?", (event["sku"],)).fetchone()
                unit = event["revenue_minor"] // event["quantity"]
                active = 0 if status["voided"] else event["quantity"] - status["returned"]
                net += unit * active
                lines.append({"seq": event["seq"], "sku": event["sku"], "name": name[0] if name else event["sku"],
                              "quantity": event["quantity"], "unit_price_minor": unit,
                              "returned": status["returned"], "voided": status["voided"], "active": active})
            seller = self.db.execute("SELECT u.name FROM events e JOIN users u ON u.id = e.user_id WHERE e.seq=?",
                                     (lines[0]["seq"],)).fetchone() if lines else None
            out.append({"ticket_id": row["ticket_id"], "occurred_at": receipt["occurred_at"], "day": receipt["day"],
                        "user": seller[0] if seller else None,
                        "total_minor": receipt["total_minor"], "net_minor": net, "lines": lines,
                        "status": "anulado" if all(l["voided"] for l in lines)
                        else "con_devoluciones" if any(l["returned"] or l["voided"] for l in lines) else "vigente"})
        return out

    def _check_line(self, kind, quantity, unit_price_minor):
        if kind not in KINDS:
            raise ValueError("tipo de movimiento desconocido")
        integer(quantity=quantity)
        if quantity == 0:
            raise ValueError("cantidad debe ser positiva")
        if unit_price_minor is not None:
            integer(unit_price_minor=unit_price_minor)
            if kind != "sale":
                raise ValueError("precio solo aplica a ventas")

    def _day(self, occurred_at: str) -> str:
        return _instant(occurred_at).astimezone(ZoneInfo(self.timezone)).date().isoformat()

    def move(self, command_id: str, sku: str, kind: str, quantity: int,
             occurred_at: str, *, unit_price_minor: int | None = None, note: str | None = None) -> dict:
        _command_id(command_id)
        self._check_line(kind, quantity, unit_price_minor)
        day = self._day(occurred_at)
        fields = [sku, kind, quantity, occurred_at, unit_price_minor]
        if note is not None:
            note = _text("nota", note, maximum=200) or None
            if note:
                fields.append(note)
        request = canonical(fields)
        with transaction(self.db):
            previous = self.db.execute("SELECT request, receipt FROM events WHERE command_id=?", (command_id,)).fetchone()
            if previous:
                if previous["request"] != request:
                    raise ValueError("command_id reutilizado con contenido diferente")
                return json.loads(previous["receipt"])
            return self._apply(command_id, sku, kind, quantity, day, unit_price_minor, request, note)

    def sell(self, ticket_id: str, lines: list, occurred_at: str, *, expected_total_minor: int | None = None) -> dict:
        """Venta de varias líneas: todo el ticket se confirma o nada (idempotente por ticket_id).

        expected_total_minor: total que vio el cajero. Si los precios cambiaron entre tanto,
        se rechaza con PriceChanged y no se registra nada (nunca cobrar otra cifra).
        """
        _command_id(ticket_id)
        if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_TICKET_LINES:
            raise ValueError(f"el ticket necesita entre 1 y {MAX_TICKET_LINES} líneas")
        clean = []
        for line in lines:
            if not isinstance(line, dict) or not {"sku", "quantity"} <= set(line) <= {"sku", "quantity", "unit_price_minor"}:
                raise ValueError("línea inválida")
            price = line.get("unit_price_minor")
            self._check_line("sale", line["quantity"], price)
            clean.append((line["sku"], line["quantity"], price))
        day = self._day(occurred_at)
        request = canonical([clean, occurred_at])
        with transaction(self.db):
            previous = self.db.execute("SELECT request, receipt FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            if previous:
                if previous["request"] != request:
                    raise ValueError("ticket reutilizado con contenido diferente")
                return json.loads(previous["receipt"])
            results, total = [], 0
            for number, (sku, quantity, price) in enumerate(clean, 1):
                line_price = self.product(sku)["price"] if price is None else price
                total += quantity * line_price
                results.append(self._apply(f"{ticket_id}#{number}", sku, "sale", quantity, day, price,
                                           canonical([sku, "sale", quantity, occurred_at, price]), None))
            if expected_total_minor is not None and total != expected_total_minor:
                raise PriceChanged(total, {sku: self.product(sku)["price"] for sku, _, _ in clean})
            receipt = {"ticket_id": ticket_id, "occurred_at": occurred_at, "day": day,
                       "total_minor": total, "lines": results,
                       "low_stock": sorted({r["sku"] for r in results if r["low_stock"]})}
            self.db.execute("INSERT INTO tickets(ticket_id, request, receipt, day) VALUES(?,?,?,?)",
                            (ticket_id, request, canonical(receipt), day))
        return receipt

    def adjust_count(self, command_id: str, sku: str, counted: int, occurred_at: str) -> dict | None:
        """Conteo físico: la diferencia queda como entrada (sobrante) o merma (faltante).

        Idempotente: el mismo command_id devuelve el recibo original. Sin diferencia no
        se registra nada y devuelve None.
        """
        _command_id(command_id)
        integer(counted=counted)
        day = self._day(occurred_at)
        with transaction(self.db):
            previous = self.db.execute("SELECT receipt FROM events WHERE command_id=?", (command_id,)).fetchone()
            if previous:
                return json.loads(previous[0])
            stock = self.product(sku)["stock"]
            if counted == stock:
                return None
            kind = "receive" if counted > stock else "waste"
            quantity = abs(counted - stock)
            note = f"Ajuste por conteo: {stock} → {counted}"
            request = canonical([sku, kind, quantity, occurred_at, None, note])
            return self._apply(command_id, sku, kind, quantity, day, None, request, note)

    def movements(self, *, sku: str | None = None, limit: int = 100) -> list[dict]:
        sql = ("SELECT e.seq, e.command_id, e.sku, e.kind, e.day, e.note, e.payload, e.ref_seq, p.name,"
               " (SELECT name FROM users WHERE id = e.user_id) AS user_name,"
               " (SELECT name FROM users WHERE id = e.authorized_by) AS authorized_name,"
               " EXISTS (SELECT 1 FROM events v WHERE v.ref_seq = e.seq AND v.kind = 'void') AS voided"
               " FROM events e LEFT JOIN products p ON p.sku = e.sku")
        args: list = []
        if sku:
            sql += " WHERE e.sku=?"
            args.append(sku)
        sql += " ORDER BY e.seq DESC LIMIT ?"
        args.append(limit)
        out = []
        for row in self.db.execute(sql, args):
            item = json.loads(row["payload"])
            out.append({"seq": row["seq"], "command_id": row["command_id"], "sku": row["sku"],
                        "name": row["name"], "kind": row["kind"], "day": row["day"], "note": row["note"],
                        "quantity": item["quantity"], "revenue_minor": item["revenue_minor"],
                        "cogs_minor": item["cogs_minor"], "waste_minor": item["waste_minor"],
                        "stock_after": item["stock_after"], "stock_delta": stock_delta(item),
                        "ref_seq": row["ref_seq"], "voided": bool(row["voided"]),
                        "user": row["user_name"], "authorized_by": row["authorized_name"],
                        "ticket_id": row["command_id"].rsplit("#", 1)[0] if item["kind"] == "sale" and "#" in row["command_id"] else None})
        return out

    def daily_units(self, kind: str = "sale", *, since: str = "", sku: str | None = None) -> dict[str, dict[str, int]]:
        """{sku: {día: unidades}} desde `since` (incluido). Para ventas: netas de devoluciones y
        anulaciones (un día con venta neta 0 no aparece); para otros tipos, brutas."""
        if kind == "sale":
            sql = "SELECT sku, day, sum(sold_delta) FROM events WHERE sold_delta <> 0 AND day>=?"
            args: list = [since]
        else:
            sql = "SELECT sku, day, sum(quantity) FROM events WHERE kind=? AND day>=?"
            args = [kind, since]
        if sku is not None:
            sql += " AND sku=?"
            args.append(sku)
        out: dict[str, dict[str, int]] = {}
        for row in self.db.execute(sql + " GROUP BY sku, day", args):
            if row[2] > 0:
                out.setdefault(row[0], {})[row[1]] = row[2]
        return out

    def top_sellers(self, *, since: str, limit: int = 12) -> list[dict]:
        """Productos activos más vendidos desde `since` (para la caja sin búsqueda)."""
        rows = self.db.execute(
            "SELECT p.* FROM (SELECT sku, sum(sold_delta) AS units FROM events WHERE sold_delta <> 0 AND day>=?"
            " GROUP BY sku HAVING units > 0 ORDER BY units DESC LIMIT ?) s JOIN products p ON p.sku = s.sku"
            " WHERE p.active=1"
            " ORDER BY s.units DESC", (since, limit)).fetchall()
        return [dict(row) for row in rows]

    def active_days(self, *, since: str = "") -> list[str]:
        """Días con algún movimiento (ordenados): el comercio operó y registró algo."""
        return [row[0] for row in self.db.execute("SELECT DISTINCT day FROM events WHERE day>=? ORDER BY day", (since,))]

    def first_days(self, sku: str | None = None) -> dict[str, str]:
        if sku is not None:
            row = self.db.execute("SELECT min(day) FROM events WHERE sku=?", (sku,)).fetchone()
            return {sku: row[0]} if row[0] else {}
        return {row[0]: row[1] for row in self.db.execute("SELECT sku, min(day) FROM events GROUP BY sku")}

    # ---------------------------------------------------------
    # Usuarios y PIN
    # ---------------------------------------------------------

    @staticmethod
    def _pin_hash(pin: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ITERATIONS)

    @staticmethod
    def _check_pin_format(pin) -> str:
        if not isinstance(pin, str) or not re.fullmatch(r"\d{4,8}", pin):
            raise ValueError("el PIN debe tener de 4 a 8 dígitos")
        if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
            raise ValueError("ese PIN es demasiado fácil de adivinar")
        return pin

    def has_users(self) -> bool:
        return self.db.execute("SELECT 1 FROM users WHERE active=1 LIMIT 1").fetchone() is not None

    def users(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id, name, role, active, created_at FROM users ORDER BY active DESC, role, name COLLATE NOCASE")]

    def user(self, user_id: int) -> dict | None:
        row = self.db.execute("SELECT id, name, role, active FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def add_user(self, name: str, role: str, pin: str) -> dict:
        name = _text("nombre", name, maximum=40, required=True)
        if role not in ROLES:
            raise ValueError("rol desconocido")
        if role == "cajero" and not self.db.execute(
                "SELECT 1 FROM users WHERE role='dueno' AND active=1").fetchone():
            raise ValueError("primero cree el usuario dueño")
        salt = secrets.token_bytes(16)
        try:
            cursor = self.db.execute(
                "INSERT INTO users(name, role, pin_salt, pin_hash, created_at) VALUES(?,?,?,?,?)",
                (name, role, salt, self._pin_hash(self._check_pin_format(pin), salt),
                 datetime.now(ZoneInfo(self.timezone)).isoformat(timespec="seconds")))
        except sqlite3.IntegrityError as exc:
            raise ValueError("ya existe un usuario con ese nombre") from exc
        return self.user(cursor.lastrowid)

    def update_user(self, user_id: int, *, pin: str | None = None, active: bool | None = None,
                    name: str | None = None) -> dict:
        current = self.user(user_id)
        if current is None:
            raise ValueError("usuario desconocido")
        with transaction(self.db):
            if name is not None:
                try:
                    self.db.execute("UPDATE users SET name=? WHERE id=?",
                                    (_text("nombre", name, maximum=40, required=True), user_id))
                except sqlite3.IntegrityError as exc:
                    raise ValueError("ya existe un usuario con ese nombre") from exc
            if pin is not None:
                salt = secrets.token_bytes(16)
                self.db.execute("UPDATE users SET pin_salt=?, pin_hash=? WHERE id=?",
                                (salt, self._pin_hash(self._check_pin_format(pin), salt), user_id))
            if active is not None:
                if not isinstance(active, bool):
                    raise ValueError("activo debe ser verdadero o falso")
                if not active and current["role"] == "dueno" and self.db.execute(
                        "SELECT count(*) FROM users WHERE role='dueno' AND active=1").fetchone()[0] <= 1:
                    raise ValueError("debe quedar al menos un dueño activo")
                self.db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
        return self.user(user_id)

    def check_pin(self, user_id: int, pin) -> dict | None:
        """Usuario activo si el PIN coincide (comparación en tiempo constante)."""
        row = self.db.execute("SELECT id, name, role, pin_salt, pin_hash FROM users WHERE id=? AND active=1",
                              (user_id,)).fetchone()
        if row is None or not isinstance(pin, str) or len(pin) > 16:
            return None
        if hmac.compare_digest(self._pin_hash(pin, row["pin_salt"]), row["pin_hash"]):
            return {"id": row["id"], "name": row["name"], "role": row["role"]}
        return None

    def owner_by_pin(self, pin) -> dict | None:
        """Dueño activo cuyo PIN coincide (para autorizar una devolución o anulación de un cajero)."""
        for row in self.db.execute("SELECT id FROM users WHERE role='dueno' AND active=1").fetchall():
            owner = self.check_pin(row[0], pin)
            if owner:
                return owner
        return None

    # ---------------------------------------------------------
    # Informes
    # ---------------------------------------------------------

    def weekly_summary(self, start_day: str, end_day: str) -> dict:
        """Intervalo local semiabierto; el llamador elige los límites semanales."""
        from .analytics import margin
        if date.fromisoformat(start_day) >= date.fromisoformat(end_day):
            raise ValueError("intervalo inválido")
        totals = {"sold_units": 0, "revenue_minor": 0, "cogs_minor": 0, "waste_minor": 0}
        # Neto: las devoluciones y anulaciones llevan unidades e importes con signo.
        for row in self.db.execute("SELECT payload, sold_delta FROM events WHERE day >= ? AND day < ? ORDER BY seq",
                                   (start_day, end_day)):
            item = json.loads(row[0])
            totals["sold_units"] += row[1]
            for key in ("revenue_minor", "cogs_minor", "waste_minor"):
                totals[key] += item[key]
        return {"start_day": start_day, "end_day_exclusive": end_day,
                "timezone": self.timezone, "currency": "COP", "money_scale": 2,
                **totals, **margin(revenue_minor=totals["revenue_minor"],
                                  cogs_minor=totals["cogs_minor"], waste_minor=totals["waste_minor"])}

    def backup(self, destination) -> None:
        """Copia consistente con la API de backup de SQLite (no copiar el .db abierto con WAL)."""
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
        finally:
            target.close()
