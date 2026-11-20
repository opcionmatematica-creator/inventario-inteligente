"""Ejecutar con python3 -m inventario.demo; no usa red ni archivos permanentes."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .analytics import Demand, PurchasePolicy, recommend, rotation
from .edge import Edge
from .sync import Gateway, Receiver


def main():
    with TemporaryDirectory(prefix="inventario-demo-") as folder:
        edge = Edge(Path(folder) / "edge.sqlite3")
        receiver = Receiver(Path(folder) / "cloud-simulator.sqlite3")
        try:
            edge.add_product("ARROZ-1KG", price=500000, cost=350000, minimum=5)
            edge.move("entrada-1", "ARROZ-1KG", "receive", 10, "2026-09-21T08:00:00-05:00")
            sale = edge.move("venta-1", "ARROZ-1KG", "sale", 6, "2026-09-21T12:00:00-05:00")
            edge.move("merma-1", "ARROZ-1KG", "waste", 1, "2026-09-22T09:00:00-05:00")
            demand = Demand()
            for units in [3, 4, 4, 5, 2, 6, 4] * 4:
                demand = demand.update(units)
            gateway = Gateway(edge)
            batch = gateway.prepare()
            ack = receiver.accept(batch)
            assert receiver.accept(batch) == ack  # Simula ACK perdido y reenvío.
            gateway.acknowledge(ack)
            result = {
                "nota": "Datos ficticios. Receptor simulado local, sin red; importes en centavos COP.",
                "venta_offline": sale,
                "compra": recommend(demand, PurchasePolicy(lead_days=3, pack=6,
                                    unit_cost_minor=350000, budget_minor=15000000), usable=3),
                "rotacion_repuesto": rotation(Demand(0.1, 0.2, 120), stock=20,
                                    unit_cost_minor=2500000, category="ferreteria",
                                    eligible_days_without_sale=100, median_sale_interval=20),
                "resumen_semanal": edge.weekly_summary("2026-09-21", "2026-09-28"),
                "sync_batch": batch, "ack": ack,
                "cola_vacia": gateway.prepare() is None,
            }
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        finally:
            receiver.close()
            edge.close()


if __name__ == "__main__":
    main()
