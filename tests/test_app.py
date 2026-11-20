from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from inventario import csvio
from inventario.edge import Edge
from inventario.insights import Insights
from inventario.sync import Gateway, Receiver

TIME = "2026-09-27T10:00:00-05:00"


def at(day: str, hour: int = 10) -> str:
    return f"{day}T{hour:02d}:00:00-05:00"


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "edge.sqlite3"
        self.edge = Edge(self.path)
        self.addCleanup(lambda: self.edge.close())

    def test_product_fields_search_and_barcode(self):
        self.edge.add_product("ARROZ", price=500000, cost=350000, minimum=5, name="Arroz 1 kg",
                              barcode="7701234", category="tienda", pack=12)
        self.edge.add_product("LECHE", price=420000, cost=300000, name="Leche entera", category="perecedero")
        self.assertEqual(self.edge.find("7701234")["sku"], "ARROZ")
        self.assertEqual(self.edge.find("LECHE")["name"], "Leche entera")
        self.assertIsNone(self.edge.find("nada"))
        self.assertEqual([p["sku"] for p in self.edge.products(query="lech")], ["LECHE"])
        self.assertEqual([p["sku"] for p in self.edge.products(query="100%")], [])
        with self.assertRaises(ValueError):
            self.edge.add_product("OTRO", price=1, cost=1, barcode="7701234")
        with self.assertRaises(ValueError):
            self.edge.add_product("ARROZ", price=1, cost=1)
        with self.assertRaises(ValueError):
            self.edge.add_product("X", price=1, cost=1, category="joyeria")

    def test_update_product_validates_and_hides_inactive(self):
        self.edge.add_product("P1", price=100, cost=50)
        updated = self.edge.update_product("P1", name="Producto", price=150, active=False)
        self.assertEqual((updated["name"], updated["price"], updated["active"]), ("Producto", 150, 0))
        self.assertEqual(self.edge.products(), [])
        self.assertEqual(len(self.edge.products(include_inactive=True)), 1)
        for bad in ({"price": -1}, {"pack": 0}, {"stock": 5}, {"lead_days": 400}, {"active": 1}):
            with self.assertRaises(ValueError):
                self.edge.update_product("P1", **bad)

    def test_ticket_is_atomic_and_idempotent(self):
        self.edge.add_product("A", price=1000, cost=600, minimum=1)
        self.edge.add_product("B", price=250, cost=100)
        self.edge.move("r1", "A", "receive", 3, TIME)
        self.edge.move("r2", "B", "receive", 2, TIME)
        lines = [{"sku": "A", "quantity": 2}, {"sku": "B", "quantity": 1, "unit_price_minor": 200}]
        receipt = self.edge.sell("t1", lines, TIME)
        self.assertEqual(receipt["total_minor"], 2200)
        self.assertEqual(receipt["low_stock"], ["A"])
        self.assertEqual(self.edge.sell("t1", lines, TIME), receipt)
        with self.assertRaises(ValueError):
            self.edge.sell("t1", lines[:1], TIME)
        # La segunda línea falla: no se descuenta nada de la primera.
        with self.assertRaises(ValueError):
            self.edge.sell("t2", [{"sku": "A", "quantity": 1}, {"sku": "B", "quantity": 5}], TIME)
        self.assertEqual((self.edge.product("A")["stock"], self.edge.product("B")["stock"]), (1, 1))
        with self.assertRaises(ValueError):
            self.edge.sell("t3", [], TIME)
        with self.assertRaises(ValueError):
            self.edge.sell("t4", [{"sku": "A", "quantity": 1, "extra": 1}], TIME)

    def test_ticket_lines_travel_in_sync_batch(self):
        self.edge.add_product("A", price=1000, cost=600)
        self.edge.move("r1", "A", "receive", 5, TIME)
        self.edge.sell("t1", [{"sku": "A", "quantity": 2}], TIME)
        batch = Gateway(self.edge).prepare()
        cloud = Receiver(Path(self.folder.name) / "cloud.sqlite3")
        self.addCleanup(cloud.close)
        self.assertEqual(cloud.accept(batch)["status"], "persisted")
        self.assertEqual(batch["rows"][0]["sold_units"], 2)

    def test_count_adjustment_creates_receive_or_waste_once(self):
        self.edge.add_product("A", price=1000, cost=600)
        self.edge.move("r1", "A", "receive", 10, TIME)
        self.assertIsNone(self.edge.adjust_count("c0", "A", 10, TIME))
        low = self.edge.adjust_count("c1", "A", 7, TIME)
        self.assertEqual(low["stock"], 7)
        self.assertEqual(self.edge.adjust_count("c1", "A", 7, TIME), low)
        self.assertEqual(self.edge.product("A")["stock"], 7)
        self.assertEqual(self.edge.adjust_count("c2", "A", 9, TIME)["stock"], 9)
        kinds = [(m["kind"], m["quantity"]) for m in self.edge.movements(sku="A")]
        self.assertEqual(kinds, [("receive", 2), ("waste", 3), ("receive", 10)])
        self.assertIn("Ajuste por conteo", self.edge.movements(sku="A")[0]["note"])
        self.assertEqual(self.edge.weekly_summary("2026-09-21", "2026-09-28")["waste_minor"], 1800)

    def test_old_database_is_migrated_without_losing_events(self):
        old = Path(self.folder.name) / "old.sqlite3"
        db = sqlite3.connect(old)
        db.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE products(sku TEXT PRIMARY KEY, price INTEGER NOT NULL, cost INTEGER NOT NULL,
                minimum INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL UNIQUE,
                request TEXT NOT NULL, payload TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE TABLE sync_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                ack_seq INTEGER NOT NULL DEFAULT 0, pending TEXT);
            INSERT INTO products VALUES('P1', 150, 100, 3, 4);
        """)
        payload = {"sku": "P1", "day": "2026-09-27", "kind": "receive", "quantity": 4, "revenue_minor": 0,
                   "cogs_minor": 0, "waste_minor": 0, "stock_after": 4}
        db.execute("INSERT INTO events(command_id, request, payload, receipt) VALUES('r1','[]',?,'{}')",
                   (json.dumps(payload),))
        db.commit()
        db.close()
        edge = Edge(old)
        self.addCleanup(edge.close)
        self.assertEqual(edge.product("P1")["category"], "tienda")
        self.assertEqual(edge.movements()[0]["day"], "2026-09-27")
        self.assertEqual(edge.daily_units("receive"), {"P1": {"2026-09-27": 4}})

    def test_backup_is_a_consistent_copy(self):
        self.edge.add_product("A", price=1, cost=1)
        target = Path(self.folder.name) / "copia.sqlite3"
        self.edge.backup(target)
        copy = sqlite3.connect(target)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT sku FROM products").fetchall(), [("A",)])


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.edge = Edge(Path(self.folder.name) / "edge.sqlite3")
        self.addCleanup(lambda: self.edge.close())
        self.insights = Insights(self.edge)
        self.start = date(2026, 9, 1)

    def day(self, offset: int) -> str:
        return (self.start + timedelta(days=offset)).isoformat()

    def test_closed_days_are_unknown_and_open_days_without_sale_are_zero(self):
        self.edge.add_product("A", price=1000, cost=500, minimum=2, lead_days=2)
        self.edge.add_product("B", price=1000, cost=500)
        self.edge.move("r", "A", "receive", 500, at(self.day(0)))
        self.edge.move("rb", "B", "receive", 500, at(self.day(0)))
        number = 0
        for offset in range(20):
            if offset % 7 == 6:
                continue  # domingo cerrado: ningún movimiento
            number += 1
            # A vende 4 los días pares; los impares solo vende B (A vende 0 ese día).
            if offset % 2 == 0:
                self.edge.sell(f"t{number}", [{"sku": "A", "quantity": 4}], at(self.day(offset)))
            else:
                self.edge.sell(f"t{number}", [{"sku": "B", "quantity": 1}], at(self.day(offset)))
        ctx = self.insights._context(self.day(20))
        demand = self.insights.demand("A", ctx)
        self.assertEqual(demand.missing, 2)        # dos domingos
        self.assertEqual(demand.observed, 18)
        self.assertTrue(1.5 < demand.mean < 2.5)    # ~2 por día, no 4
        report = self.insights.product_report(self.edge.product("A"), ctx)
        self.assertEqual(report["purchase"]["status"], "draft")
        self.assertTrue(report["purchase"]["reason_text"])

    def test_alerts_purchases_and_frozen_stock(self):
        self.edge.add_product("SIN", price=1000, cost=500, name="Sin stock")
        self.edge.add_product("BAJO", price=1000, cost=500, minimum=5, name="Bajo", lead_days=1)
        self.edge.add_product("QUIETO", price=1000, cost=800, name="Quieto", category="perecedero")
        self.edge.move("r1", "BAJO", "receive", 3, at(self.day(0)))
        self.edge.move("r2", "QUIETO", "receive", 10, at(self.day(0)))
        for offset in range(1, 20):
            self.edge.move(f"x{offset}", "BAJO", "receive", 1, at(self.day(offset)))
        alerts = self.insights.alerts(self.day(20))
        types = {(a["sku"], a["type"]) for a in alerts}
        self.assertIn(("SIN", "sin_stock"), types)
        self.assertIn(("QUIETO", "congelado"), types)
        self.assertEqual(alerts[0]["level"], "alta")
        frozen = next(a for a in alerts if a["type"] == "congelado")
        self.assertEqual(frozen["capital_minor"], 8000)
        purchases = self.insights.purchases(self.day(20))
        self.assertEqual(purchases["status"], "draft")
        dashboard = self.insights.dashboard(self.day(20))
        self.assertEqual(dashboard["products"], 3)
        self.assertEqual(dashboard["stock_value_minor"], (3 + 19) * 500 + 10 * 800)


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.edge = Edge(Path(self.folder.name) / "edge.sqlite3")
        self.addCleanup(lambda: self.edge.close())

    def test_money_formats(self):
        cases = {"5.000": 500000, "5000": 500000, "$ 5.000,50": 500050, "5000.5": 500050,
                 "1.234.567": 123456700, "0,5": 50, "COP 12.000": 1200000}
        for text, expected in cases.items():
            self.assertEqual(csvio.parse_money(text), expected, text)
        for bad in ("", "abc", "5.00.0", "1,234,56", "-5", "1.2345,6789"):
            with self.assertRaises(ValueError, msg=bad):
                csvio.parse_money(bad)

    def test_preview_then_import_is_repeatable(self):
        data = ("Código;Producto;Precio;Costo;Stock;Código de barras;Categoría\n"
                "A1;Arroz;5.000;3.500;20;770;tienda\n"
                "A2;Leche;4.200;3.000;0;;perecedero\n"
                "A2;Repetido;1;1;0;;tienda\n"
                "A3;Malo;abc;1;0;;tienda\n"
                ";Sin sku;1;1;0;;\n").encode("utf-8-sig")
        parsed = csvio.parse_products(data)
        self.assertEqual([r["sku"] for r in parsed["rows"]], ["A1", "A2"])
        self.assertEqual(len(parsed["errors"]), 3)
        plan = csvio.plan_import(self.edge, parsed)
        self.assertEqual((plan["new"], plan["update"]), (2, 0))
        result = csvio.apply_import(self.edge, plan, TIME, "h" * 64)
        self.assertEqual(result["created"], 2)
        self.assertEqual(self.edge.product("A1")["stock"], 20)
        self.assertEqual(self.edge.product("A1")["barcode"], "770")
        # Reimportar el mismo archivo actualiza datos sin volver a cargar stock.
        again = csvio.plan_import(self.edge, csvio.parse_products(data))
        self.assertEqual(again["update"], 2)
        csvio.apply_import(self.edge, again, TIME, "h" * 64)
        self.assertEqual(self.edge.product("A1")["stock"], 20)
        # Un CSV sin columna de código de barras no borra los códigos ya cargados.
        partial = csvio.plan_import(self.edge, csvio.parse_products(b"sku,precio,costo\nA1,6000,3500\n"))
        csvio.apply_import(self.edge, partial, TIME, "i" * 64)
        self.assertEqual((self.edge.product("A1")["price"], self.edge.product("A1")["barcode"]), (600000, "770"))

    def test_barcode_conflict_and_missing_columns(self):
        self.edge.add_product("X", price=1, cost=1, barcode="999")
        plan = csvio.plan_import(self.edge, csvio.parse_products(b"sku,precio,costo,ean\nY,1,1,999\n"))
        self.assertEqual(plan["conflicts"], 1)
        self.assertEqual(csvio.apply_import(self.edge, plan, TIME, "x" * 64)["created"], 0)
        with self.assertRaises(ValueError):
            csvio.parse_products(b"nombre,precio\nA,1\n")

    def test_export_round_trip(self):
        self.edge.add_product("A1", price=500050, cost=350000, name="Arroz; 1 kg", barcode="770")
        self.edge.move("r", "A1", "receive", 4, TIME)
        exported = csvio.export_products(self.edge)
        self.assertTrue(exported.startswith("﻿"))
        other = Edge(Path(self.folder.name) / "otro.sqlite3")
        self.addCleanup(other.close)
        plan = csvio.plan_import(other, csvio.parse_products(exported.encode("utf-8")))
        csvio.apply_import(other, plan, TIME, "r" * 64)
        copy = other.product("A1")
        self.assertEqual((copy["name"], copy["price"], copy["stock"]), ("Arroz; 1 kg", 500050, 4))
        self.assertIn("entrada", csvio.export_movements(self.edge))


if __name__ == "__main__":
    unittest.main()


class DemandEquivalenceTests(unittest.TestCase):
    """La forma cerrada debe dar lo mismo que recorrer los días con Demand.update."""

    def test_closed_form_matches_day_by_day_replay(self):
        import random
        from inventario.analytics import Demand
        from inventario.insights import demand_from_history
        rng = random.Random(3)
        for _ in range(300):
            start = date(2026, 1, 1)
            length = rng.randint(1, 120)
            days = [(start + timedelta(days=i)).isoformat() for i in range(length)]
            open_days = sorted(d for d in days if rng.random() < 0.85)
            sales = {d: rng.choice([1, 2, 3, 7, 20]) for d in open_days if rng.random() < rng.random()}
            replay = Demand()
            for d in days:
                replay = replay.update(sales.get(d, 0) if d in open_days else None)
            fast = demand_from_history(open_days, sales, days[0], (start + timedelta(days=length)).isoformat())
            self.assertEqual((fast.observed, fast.missing), (replay.observed, replay.missing))
            self.assertAlmostEqual(fast.mean, replay.mean, delta=1e-9)
            self.assertAlmostEqual(fast.variance, replay.variance, delta=1e-7 * max(1, replay.variance))

    def test_more_than_500_products_are_analyzed(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        edge = Edge(Path(folder.name) / "e.sqlite3")
        self.addCleanup(edge.close)
        for i in range(520):
            edge.add_product(f"P{i:03d}", price=10, cost=5)
        self.assertEqual(len(Insights(edge).reports()), 520)
        self.assertEqual(len([a for a in Insights(edge).alerts() if a["type"] == "sin_stock"]), 520)
