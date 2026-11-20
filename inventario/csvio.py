"""Importación y exportación CSV del catálogo, con vista previa y errores por fila."""

import csv
import hashlib
import io
import re

from .edge import CATEGORIES

MAX_ROWS = 20000
COLUMNS = ("sku", "nombre", "codigo_barras", "categoria", "unidad", "precio", "costo",
           "minimo", "stock_inicial", "dias_entrega", "empaque", "pedido_minimo")
ALIASES = {
    "sku": "sku", "codigo": "sku", "código": "sku", "referencia": "sku", "ref": "sku",
    "nombre": "nombre", "producto": "nombre", "descripcion": "nombre", "descripción": "nombre",
    "codigo_barras": "codigo_barras", "código de barras": "codigo_barras", "codigo de barras": "codigo_barras",
    "barras": "codigo_barras", "ean": "codigo_barras",
    "categoria": "categoria", "categoría": "categoria", "rubro": "categoria",
    "unidad": "unidad",
    "precio": "precio", "precio venta": "precio", "precio de venta": "precio", "pvp": "precio",
    "costo": "costo", "coste": "costo", "precio compra": "costo", "costo unitario": "costo",
    "minimo": "minimo", "mínimo": "minimo", "stock minimo": "minimo", "stock mínimo": "minimo",
    "stock_inicial": "stock_inicial", "stock": "stock_inicial", "existencias": "stock_inicial",
    "cantidad": "stock_inicial",
    "dias_entrega": "dias_entrega", "días de entrega": "dias_entrega", "dias de entrega": "dias_entrega",
    "empaque": "empaque", "pack": "empaque",
    "pedido_minimo": "pedido_minimo", "pedido mínimo": "pedido_minimo", "pedido minimo": "pedido_minimo",
}
FIELD_OF = {"nombre": "name", "codigo_barras": "barcode", "categoria": "category", "unidad": "unit",
            "precio": "price", "costo": "cost", "minimo": "minimum", "dias_entrega": "lead_days",
            "empaque": "pack", "pedido_minimo": "moq"}
CATEGORY_ALIASES = {"tienda": "tienda", "": "tienda", "general": "tienda", "abarrotes": "tienda",
                    "perecedero": "perecedero", "perecederos": "perecedero", "fresco": "perecedero",
                    "ferreteria": "ferreteria", "ferretería": "ferreteria", "repuestos": "ferreteria"}


def parse_money(text: str) -> int:
    """Pesos escritos a mano → centavos. «5.000», «5000», «$ 5.000,50», «5000.5»."""
    value = re.sub(r"[\s$]|COP", "", str(text or ""), flags=re.I)
    if not value:
        raise ValueError("importe vacío")
    if not re.fullmatch(r"\d[\d.,]*", value):
        raise ValueError(f"importe inválido: {text!r}")
    if "." in value and "," in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        value = value.replace(thousands, "").replace(decimal, ".")
    else:
        sep = "." if "." in value else "," if "," in value else ""
        if sep:
            parts = value.split(sep)
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                if not all(len(p) == 3 for p in parts[1:]):
                    raise ValueError(f"importe inválido: {text!r}")
                value = "".join(parts)  # separador de miles
            else:
                value = value.replace(sep, ".")
    whole, _, cents = value.partition(".")
    if len(cents) > 2:
        raise ValueError(f"importe con más de 2 decimales: {text!r}")
    return int(whole or "0") * 100 + int((cents + "00")[:2])


def _int(text, name, default=None):
    value = str(text or "").strip().replace(".", "") if str(text or "").strip() else ""
    if not value:
        if default is None:
            raise ValueError(f"{name} vacío")
        return default
    if not value.isdigit():
        raise ValueError(f"{name} debe ser un entero sin signo: {text!r}")
    return int(value)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("no se pudo leer el archivo (use UTF-8)")


def parse_products(data: bytes) -> dict:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if not header:
        raise ValueError("archivo vacío")
    mapping = {}
    for index, name in enumerate(header):
        key = ALIASES.get(name.strip().lower())
        if key and key not in mapping.values():
            mapping[index] = key
    missing = {"sku", "precio", "costo"} - set(mapping.values())
    if missing:
        raise ValueError("faltan columnas: " + ", ".join(sorted(missing)))
    rows, errors, seen_sku, seen_code = [], [], set(), set()
    for number, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue
        if len(rows) + len(errors) >= MAX_ROWS:
            raise ValueError(f"máximo {MAX_ROWS} filas por archivo")
        cells = {key: (raw[i].strip() if i < len(raw) else "") for i, key in mapping.items()}
        try:
            sku = cells.get("sku", "")
            if not sku or len(sku) > 100:
                raise ValueError("SKU vacío o de más de 100 caracteres")
            if sku in seen_sku:
                raise ValueError("SKU repetido en el archivo")
            category = CATEGORY_ALIASES.get(cells.get("categoria", "").lower())
            if category not in CATEGORIES:
                raise ValueError(f"categoría desconocida: {cells.get('categoria')!r}")
            code = cells.get("codigo_barras") or None
            if code and code in seen_code:
                raise ValueError("código de barras repetido en el archivo")
            row = {"sku": sku, "name": cells.get("nombre") or sku, "barcode": code, "category": category,
                   "unit": cells.get("unidad") or "unidad",
                   "price": parse_money(cells.get("precio")), "cost": parse_money(cells.get("costo")),
                   "minimum": _int(cells.get("minimo"), "mínimo", 0),
                   "initial_stock": _int(cells.get("stock_inicial"), "stock inicial", 0),
                   "lead_days": _int(cells.get("dias_entrega"), "días de entrega", 3),
                   "pack": max(1, _int(cells.get("empaque"), "empaque", 1)),
                   "moq": max(1, _int(cells.get("pedido_minimo"), "pedido mínimo", 1))}
            if row["lead_days"] > 365:
                raise ValueError("días de entrega: máximo 365")
            seen_sku.add(sku)
            if code:
                seen_code.add(code)
            rows.append({"line": number, **row})
        except ValueError as exc:
            errors.append({"line": number, "sku": cells.get("sku", ""), "error": str(exc)})
    return {"rows": rows, "errors": errors, "columns": sorted(set(mapping.values()))}


def plan_import(edge, parsed: dict) -> dict:
    """Clasifica cada fila: nuevo, actualizar (precio/costo/datos) o conflicto."""
    plan = []
    for row in parsed["rows"]:
        existing = edge.db.execute("SELECT sku FROM products WHERE sku=?", (row["sku"],)).fetchone()
        if row["barcode"]:
            other = edge.db.execute("SELECT sku FROM products WHERE barcode=? AND sku<>?",
                                    (row["barcode"], row["sku"])).fetchone()
            if other:
                plan.append({**row, "action": "conflicto",
                             "error": f"código de barras ya usado por {other[0]}"})
                continue
        plan.append({**row, "action": "actualizar" if existing else "nuevo"})
    return {"rows": plan, "errors": parsed["errors"], "columns": parsed["columns"],
            "new": sum(r["action"] == "nuevo" for r in plan),
            "update": sum(r["action"] == "actualizar" for r in plan),
            "conflicts": sum(r["action"] == "conflicto" for r in plan)}


def apply_import(edge, plan: dict, occurred_at: str, file_hash: str) -> dict:
    """Crea o actualiza productos. El stock inicial solo se carga en productos nuevos,
    como una entrada con command_id derivado del archivo (reimportar no lo duplica)."""
    created = updated = 0
    for row in plan["rows"]:
        if row["action"] == "conflicto":
            continue
        fields = {k: row[k] for k in ("name", "barcode", "category", "unit", "price", "cost",
                                      "minimum", "lead_days", "pack", "moq")}
        if row["action"] == "nuevo":
            sku = row["sku"]
            edge.add_product(sku, **fields)
            created += 1
            if row["initial_stock"]:
                edge.move(f"importacion-{file_hash[:16]}-{hashlib.sha256(sku.encode()).hexdigest()[:16]}",
                          sku, "receive", row["initial_stock"], occurred_at, note="Stock inicial importado")
        else:
            # Solo se actualizan las columnas que trae el archivo: un CSV sin
            # código de barras no borra los códigos ya cargados.
            present = {FIELD_OF[c] for c in plan["columns"] if c in FIELD_OF}
            edge.update_product(row["sku"], **{k: v for k, v in fields.items() if k in present})
            updated += 1
    return {"created": created, "updated": updated, "skipped": plan["conflicts"] + len(plan["errors"])}


def _pesos(minor: int) -> str:
    return f"{minor // 100}" if minor % 100 == 0 else f"{minor // 100},{minor % 100:02d}"


def export_products(edge) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(("sku", "nombre", "codigo_barras", "categoria", "unidad", "precio", "costo",
                     "minimo", "stock", "dias_entrega", "empaque", "pedido_minimo", "activo"))
    for p in edge.products(include_inactive=True, limit=1_000_000):
        writer.writerow((p["sku"], p["name"], p["barcode"] or "", p["category"], p["unit"],
                         _pesos(p["price"]), _pesos(p["cost"]), p["minimum"], p["stock"],
                         f"{p['lead_days']:g}", p["pack"], p["moq"], "si" if p["active"] else "no"))
    return "﻿" + out.getvalue()


def export_movements(edge) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(("secuencia", "dia", "sku", "producto", "tipo", "cantidad", "ingreso", "costo_vendido",
                     "merma", "stock_despues", "nota"))
    names = {"receive": "entrada", "sale": "venta", "waste": "merma"}
    for m in reversed(edge.movements(limit=10_000_000)):
        writer.writerow((m["seq"], m["day"], m["sku"], m["name"] or "", names.get(m["kind"], m["kind"]),
                         m["quantity"], _pesos(m["revenue_minor"]), _pesos(m["cogs_minor"]),
                         _pesos(m["waste_minor"]), m["stock_after"], m["note"] or ""))
    return "﻿" + out.getvalue()
