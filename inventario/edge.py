"""Core local sin dependencias de red: catálogo, stock, movimientos, tickets y outbox."""

from contextlib import contextmanager
from datetime import date, datetime
import json
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
EVENT_MIGRATIONS = (("sku", "TEXT"), ("kind", "TEXT"), ("day", "TEXT"), ("note", "TEXT"), ("quantity", "INTEGER"))


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
            INSERT OR IGNORE INTO sync_state(singleton) VALUES(1);
        """)
        self._migrate()
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
            for table, columns in (("products", PRODUCT_MIGRATIONS), ("events", EVENT_MIGRATIONS)):
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns:
                    if name not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            # Eventos anteriores a las columnas indexadas: se rellenan desde el payload.
            for row in self.db.execute("SELECT seq, payload FROM events WHERE sku IS NULL OR quantity IS NULL").fetchall():
                item = json.loads(row["payload"])
                self.db.execute("UPDATE events SET sku=?, kind=?, day=?, quantity=? WHERE seq=?",
                                (item["sku"], item["kind"], item["day"], item["quantity"], row["seq"]))
            self.db.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS products_barcode ON products(barcode) WHERE barcode IS NOT NULL;
                CREATE INDEX IF NOT EXISTS events_day ON events(day);
                CREATE INDEX IF NOT EXISTS events_sku_day ON events(sku, day);
                CREATE INDEX IF NOT EXISTS events_kind_day ON events(kind, day, sku, quantity);
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
        self.db.execute("INSERT INTO events(command_id, request, payload, receipt, sku, kind, day, note, quantity)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (command_id, request, canonical(payload), canonical(receipt), sku, kind, day, note, quantity))
        return receipt

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
            self.db.execute("INSERT INTO tickets VALUES(?,?,?)", (ticket_id, request, canonical(receipt)))
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
        sql = ("SELECT e.seq, e.command_id, e.sku, e.kind, e.day, e.note, e.payload, p.name"
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
                        "stock_after": item["stock_after"]})
        return out

    def daily_units(self, kind: str = "sale", *, since: str = "", sku: str | None = None) -> dict[str, dict[str, int]]:
        """{sku: {día: unidades}} de un tipo de movimiento, desde `since` (incluido)."""
        sql = "SELECT sku, day, sum(quantity) FROM events WHERE kind=? AND day>=?"
        args: list = [kind, since]
        if sku is not None:
            sql += " AND sku=?"
            args.append(sku)
        out: dict[str, dict[str, int]] = {}
        for row in self.db.execute(sql + " GROUP BY sku, day", args):
            out.setdefault(row[0], {})[row[1]] = row[2]
        return out

    def top_sellers(self, *, since: str, limit: int = 12) -> list[dict]:
        """Productos activos más vendidos desde `since` (para la caja sin búsqueda)."""
        rows = self.db.execute(
            "SELECT p.* FROM (SELECT sku, sum(quantity) AS units FROM events WHERE kind='sale' AND day>=?"
            " GROUP BY sku ORDER BY units DESC LIMIT ?) s JOIN products p ON p.sku = s.sku WHERE p.active=1"
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
    # Informes
    # ---------------------------------------------------------

    def weekly_summary(self, start_day: str, end_day: str) -> dict:
        """Intervalo local semiabierto; el llamador elige los límites semanales."""
        from .analytics import margin
        if date.fromisoformat(start_day) >= date.fromisoformat(end_day):
            raise ValueError("intervalo inválido")
        totals = {"sold_units": 0, "revenue_minor": 0, "cogs_minor": 0, "waste_minor": 0}
        for row in self.db.execute("SELECT payload FROM events WHERE day >= ? AND day < ? ORDER BY seq",
                                   (start_day, end_day)):
            item = json.loads(row[0])
            totals["sold_units"] += item["quantity"] if item["kind"] == "sale" else 0
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
