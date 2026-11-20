"""Agregación y entrega idempotente de referencia, sin transporte HTTP real."""

from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .analytics import integer
from .edge import canonical, transaction

MAX_BYTES = 256 * 1024
MAX_EVENTS = 500
TOTALS = ("received_units", "sold_units", "waste_units", "revenue_minor", "cogs_minor", "waste_minor")
ROW_KEYS = {"product_ref", "day", "event_count", "last_seq", "stock_after", *TOTALS}
ENVELOPE_KEYS = {"schema_version", "batch_id", "stream_id", "created_at", "timezone",
                 "currency", "money_scale", "first_seq", "last_seq", "event_count", "rows"}


def digest(batch: dict) -> str:
    return hashlib.sha256(canonical(batch).encode()).hexdigest()


def validate_batch(batch: dict) -> None:
    """Valida forma y relaciones; JSON Schema acompaña el contrato publicado."""
    if not isinstance(batch, dict) or set(batch) != ENVELOPE_KEYS:
        raise ValueError("envoltura inválida")
    if len(canonical(batch).encode()) > MAX_BYTES:
        raise ValueError("lote demasiado grande")
    if type(batch["schema_version"]) is not int or batch["schema_version"] != 1:
        raise ValueError("versión desconocida")
    if batch["currency"] != "COP" or type(batch["money_scale"]) is not int or batch["money_scale"] != 2:
        raise ValueError("moneda/escala no soportada por esta referencia")
    for field in ("batch_id", "stream_id"):
        if str(UUID(batch[field])) != batch[field]:
            raise ValueError("UUID no canónico")
    ZoneInfo(batch["timezone"])
    if datetime.fromisoformat(batch["created_at"]).utcoffset() is None:
        raise ValueError("created_at necesita zona horaria")
    integer(first_seq=batch["first_seq"], last_seq=batch["last_seq"], event_count=batch["event_count"])
    if not 1 <= batch["event_count"] <= MAX_EVENTS or batch["first_seq"] < 1:
        raise ValueError("cantidad o secuencia inválida")
    if batch["last_seq"] - batch["first_seq"] + 1 != batch["event_count"]:
        raise ValueError("rango no contiguo")
    if not isinstance(batch["rows"], list) or not 1 <= len(batch["rows"]) <= MAX_EVENTS:
        raise ValueError("filas inválidas")
    seen = set()
    row_sequences = set()
    for row in batch["rows"]:
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise ValueError("fila inválida")
        if not isinstance(row["product_ref"], str) or not re.fullmatch(r"[0-9a-f]{32}", row["product_ref"]):
            raise ValueError("referencia inválida")
        if date.fromisoformat(row["day"]).isoformat() != row["day"]:
            raise ValueError("día inválido")
        integer(**{key: row[key] for key in ROW_KEYS - {"product_ref", "day"}})
        if row["event_count"] < 1 or not batch["first_seq"] <= row["last_seq"] <= batch["last_seq"]:
            raise ValueError("secuencia de fila inválida")
        key = (row["product_ref"], row["day"])
        if key in seen or row["last_seq"] in row_sequences:
            raise ValueError("fila o secuencia duplicada")
        seen.add(key)
        row_sequences.add(row["last_seq"])
    if sum(row["event_count"] for row in batch["rows"]) != batch["event_count"]:
        raise ValueError("conteo no coincide")
    if max(row_sequences) != batch["last_seq"]:
        raise ValueError("última secuencia no coincide")


class Gateway:
    """Adaptador dentro del proceso en la demo; en producción usar IPC al Core."""

    def __init__(self, edge):
        self.edge = edge
        with transaction(edge.db):
            edge.db.execute("INSERT OR IGNORE INTO metadata VALUES('product_key', ?)", (secrets.token_hex(32),))
        self.key = bytes.fromhex(edge.db.execute("SELECT value FROM metadata WHERE key='product_key'").fetchone()[0])

    def prepare(self, max_events: int = MAX_EVENTS) -> dict | None:
        integer(max_events=max_events)
        if not 1 <= max_events <= MAX_EVENTS:
            raise ValueError("límite de eventos inválido")
        db = self.edge.db
        with transaction(db):
            state = db.execute("SELECT * FROM sync_state WHERE singleton=1").fetchone()
            if state["pending"]:
                return json.loads(state["pending"])
            events = db.execute("SELECT seq, payload FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                                (state["ack_seq"], max_events)).fetchall()
            if not events:
                return None
            rows = {}
            for event in events:
                item = json.loads(event["payload"])
                ref = hmac.new(self.key, item["sku"].encode(), hashlib.sha256).hexdigest()[:32]
                key = (ref, item["day"])
                row = rows.setdefault(key, {"product_ref": ref, "day": item["day"],
                                           "event_count": 0, "last_seq": 0, "stock_after": 0,
                                           **dict.fromkeys(TOTALS, 0)})
                metric = {"receive": "received_units", "sale": "sold_units", "waste": "waste_units"}[item["kind"]]
                row[metric] += item["quantity"]
                for field in ("revenue_minor", "cogs_minor", "waste_minor"):
                    row[field] += item[field]
                row["event_count"] += 1
                row["last_seq"] = event["seq"]
                row["stock_after"] = item["stock_after"]
            batch = {"schema_version": 1, "batch_id": str(uuid4()), "stream_id": self.edge.stream_id,
                     "created_at": datetime.now(timezone.utc).isoformat(), "timezone": self.edge.timezone,
                     "currency": "COP", "money_scale": 2, "first_seq": events[0]["seq"],
                     "last_seq": events[-1]["seq"], "event_count": len(events), "rows": list(rows.values())}
            validate_batch(batch)
            db.execute("UPDATE sync_state SET pending=? WHERE singleton=1", (canonical(batch),))
            return batch

    def acknowledge(self, ack: dict) -> None:
        with transaction(self.edge.db):
            pending = self.edge.db.execute("SELECT pending FROM sync_state WHERE singleton=1").fetchone()[0]
            if pending is None:
                raise ValueError("no hay lote pendiente")
            batch = json.loads(pending)
            if ack != make_ack(batch):
                raise ValueError("ACK no corresponde al lote persistido")
            self.edge.db.execute("UPDATE sync_state SET ack_seq=?, pending=NULL WHERE singleton=1", (batch["last_seq"],))


def make_ack(batch):
    return {"batch_id": batch["batch_id"], "stream_id": batch["stream_id"],
            "last_seq": batch["last_seq"], "sha256": digest(batch), "status": "persisted"}


class Receiver:
    """Simulador cloud monocomercio durable, NO endpoint expuesto a internet."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY, hash TEXT NOT NULL, ack TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors(stream_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS daily(stream_id TEXT, product_ref TEXT, day TEXT, body TEXT NOT NULL,
                PRIMARY KEY(stream_id, product_ref, day));
        """)

    def close(self):
        self.db.close()

    def accept(self, batch: dict) -> dict:
        validate_batch(batch)
        hashed = digest(batch)
        with transaction(self.db):
            existing = self.db.execute("SELECT hash, ack FROM batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()
            if existing:
                if existing[0] != hashed:
                    raise ValueError("batch_id reutilizado con otro contenido")
                return json.loads(existing[1])
            cursor = self.db.execute("SELECT last_seq FROM cursors WHERE stream_id=?", (batch["stream_id"],)).fetchone()
            expected = cursor[0] + 1 if cursor else 1
            if batch["first_seq"] != expected:
                raise ValueError("rango repetido o hueco de secuencia")
            for row in batch["rows"]:
                key = (batch["stream_id"], row["product_ref"], row["day"])
                previous = self.db.execute("SELECT body FROM daily WHERE stream_id=? AND product_ref=? AND day=?", key).fetchone()
                total = dict(row)
                if previous:
                    old = json.loads(previous[0])
                    for field in (*TOTALS, "event_count"):
                        total[field] += old[field]
                self.db.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?)", (*key, canonical(total)))
            ack = make_ack(batch)
            self.db.execute("INSERT INTO batches VALUES(?,?,?)", (batch["batch_id"], hashed, canonical(ack)))
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (batch["stream_id"], batch["last_seq"]))
        return ack
