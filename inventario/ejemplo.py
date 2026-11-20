"""Datos de ejemplo para demostraciones: una tienda de barrio con cinco semanas de historial.

    python3 -m inventario.ejemplo CARPETA

Crea CARPETA/inventario.sqlite3 (debe no existir). Datos ficticios y reproducibles.
"""

from datetime import datetime, timedelta
from pathlib import Path
import random
import sys
from zoneinfo import ZoneInfo

from .edge import Edge

# sku, nombre, código de barras, categoría, precio, costo, mínimo, venta diaria media, empaque
CATALOGO = [
    ("ARROZ-1KG", "Arroz 1 kg", "7702001000011", "tienda", 4800, 3600, 10, 6, 12),
    ("ACEITE-1L", "Aceite 1 L", "7702001000028", "tienda", 11500, 8900, 6, 2.5, 12),
    ("AZUCAR-1KG", "Azúcar 1 kg", "7702001000035", "tienda", 4200, 3100, 8, 4, 12),
    ("CAFE-250", "Café molido 250 g", "7702001000042", "tienda", 9800, 7100, 4, 1.5, 6),
    ("LECHE-1L", "Leche entera 1 L", "7702001000059", "perecedero", 4300, 3300, 12, 9, 12),
    ("HUEVOS-30", "Huevos x30", "7702001000066", "perecedero", 18500, 15000, 3, 1.8, 1),
    ("PAN-TAJ", "Pan tajado", "7702001000073", "perecedero", 6200, 4300, 5, 3.5, 1),
    ("GASEOSA-15", "Gaseosa 1,5 L", "7702001000080", "tienda", 5500, 3900, 12, 7, 6),
    ("JABON-BARRA", "Jabón en barra", "7702001000097", "tienda", 3200, 2100, 6, 2, 24),
    ("PAPEL-4", "Papel higiénico x4", "7702001000103", "tienda", 7900, 5800, 6, 2.2, 12),
    ("ATUN-170", "Atún 170 g", "7702001000110", "tienda", 6900, 5100, 8, 1.2, 24),
    ("VELAS-6", "Velas x6", "7702001000127", "tienda", 4500, 2600, 2, 0, 1),  # no se vende: capital quieto
]


def generar(folder: Path, dias: int = 35, semilla: int = 7) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "inventario.sqlite3"
    if path.exists():
        raise SystemExit(f"{path} ya existe: use otra carpeta")
    rng = random.Random(semilla)
    edge = Edge(path)
    try:
        zone = ZoneInfo(edge.timezone)
        hoy = datetime.now(zone).date()
        inicio = hoy - timedelta(days=dias)
        stock = {}
        for sku, name, code, cat, price, cost, minimum, _, pack in CATALOGO:
            edge.add_product(sku, name=name, barcode=code, category=cat, price=price * 100, cost=cost * 100,
                             minimum=minimum, pack=pack, moq=pack, lead_days=2 if cat == "perecedero" else 4)
            inicial = 40 if sku != "VELAS-6" else 25
            edge.move(f"ej-ini-{sku}", sku, "receive", inicial, f"{inicio.isoformat()}T07:00:00-05:00",
                      note="Stock inicial (ejemplo)")
            stock[sku] = inicial
        ticket = 0
        for offset in range(1, dias + 1):
            dia = inicio + timedelta(days=offset)
            if dia.weekday() == 6 and offset % 2:  # algunos domingos cerrado
                continue
            if dia >= hoy:
                break
            for sku, _, _, cat, _, _, minimum, media, pack in CATALOGO:
                # Reposición los lunes y jueves cuando queda poco.
                if dia.weekday() in (0, 3) and stock[sku] <= minimum * 2 and media:
                    cantidad = pack * max(1, round(media * 5 / pack))
                    edge.move(f"ej-rec-{sku}-{offset}", sku, "receive", cantidad, f"{dia}T07:30:00-05:00",
                              note="Pedido al proveedor (ejemplo)")
                    stock[sku] += cantidad
            for hora in range(8, 20, 2):
                lineas = []
                for sku, _, _, _, _, _, _, media, _ in CATALOGO:
                    unidades = sum(1 for _ in range(4) if rng.random() < media / 24)
                    unidades = min(unidades, stock[sku])
                    if unidades:
                        lineas.append({"sku": sku, "quantity": unidades})
                        stock[sku] -= unidades
                if lineas:
                    ticket += 1
                    edge.sell(f"ej-t{ticket}", lineas, f"{dia}T{hora:02d}:15:00-05:00")
            if dia.weekday() == 5 and stock["PAN-TAJ"] > 2:
                edge.move(f"ej-merma-{offset}", "PAN-TAJ", "waste", 2, f"{dia}T20:00:00-05:00", note="Vencido")
                stock["PAN-TAJ"] -= 2
        edge.db.execute("INSERT OR REPLACE INTO metadata VALUES('setting_store_name', 'Tienda La Esquina (ejemplo)')")
        return path
    finally:
        edge.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(generar(Path(sys.argv[1])))
