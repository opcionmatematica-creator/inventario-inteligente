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


class CompensationTests(unittest.TestCase):
    """Anulaciones y devoluciones: movimientos compensatorios, nunca se borra la historia."""

    DAY1, DAY2 = "2026-09-21", "2026-09-23"

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.edge = Edge(Path(self.folder.name) / "edge.sqlite3")
        self.addCleanup(lambda: self.edge.close())
        self.edge.add_product("A", price=1000, cost=600, minimum=1)
        self.edge.add_product("B", price=250, cost=100)
        self.edge.move("r1", "A", "receive", 10, at(self.DAY1, 8))
        self.edge.move("r2", "B", "receive", 10, at(self.DAY1, 8))

    def seq(self, command_id):
        return self.edge.db.execute("SELECT seq FROM events WHERE command_id=?", (command_id,)).fetchone()[0]

    def summary(self, day):
        following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        return self.edge.weekly_summary(day, following)

    def test_void_wrong_receive_restores_stock_without_waste(self):
        # El caso real: se tecleó 2424 en vez de 24.
        self.edge.move("error", "A", "receive", 2424, at(self.DAY1, 9))
        receipt = self.edge.void("anula-1", self.seq("error"), at(self.DAY2), "Error de tecleo: eran 24")
        self.assertEqual((receipt["stock"], self.edge.product("A")["stock"]), (10, 10))
        self.assertEqual(self.edge.void("anula-1", self.seq("error"), at(self.DAY2), "Error de tecleo: eran 24"), receipt)
        self.assertEqual(self.summary(self.DAY1)["waste_minor"], 0)  # no queda como merma
        history = self.edge.movements(sku="A")
        self.assertEqual([m["kind"] for m in history], ["void", "receive", "receive"])
        self.assertTrue(history[1]["voided"])
        self.assertEqual(history[0]["day"], self.DAY1)  # corrige el día del original
        self.assertEqual(history[0]["stock_delta"], -2424)
        with self.assertRaisesRegex(ValueError, "ya está anulado"):
            self.edge.void("anula-2", self.seq("error"), at(self.DAY2), "otra vez")
        with self.assertRaisesRegex(ValueError, "no se puede anular"):
            self.edge.void("anula-3", self.seq("anula-1"), at(self.DAY2), "anular la anulación")
        with self.assertRaisesRegex(ValueError, "motivo"):
            self.edge.void("anula-4", self.seq("r2"), at(self.DAY2), "  ")

    def test_void_receive_needs_the_stock_to_still_be_there(self):
        self.edge.sell("t1", [{"sku": "A", "quantity": 8}], at(self.DAY1, 10))
        with self.assertRaisesRegex(ValueError, "negativo"):
            self.edge.void("x", self.seq("r1"), at(self.DAY2), "recepción duplicada")
        self.assertEqual(self.edge.product("A")["stock"], 2)

    def test_partial_returns_and_void_of_the_rest(self):
        self.edge.sell("t1", [{"sku": "A", "quantity": 3, "unit_price_minor": 900}], at(self.DAY1, 10))
        line = self.seq("t1#1")
        returned = self.edge.return_sale("dev-1", line, 1, at(self.DAY2), "Llegó dañado")
        self.assertEqual((returned["stock"], returned["revenue_minor"]), (8, -900))  # al precio cobrado
        self.assertEqual(self.summary(self.DAY2)["revenue_minor"], -900)  # día de la devolución
        self.assertEqual(self.summary(self.DAY2)["cogs_minor"], -600)
        self.assertEqual(self.summary(self.DAY2)["sold_units"], -1)
        with self.assertRaisesRegex(ValueError, "solo quedan 2"):
            self.edge.return_sale("dev-2", line, 3, at(self.DAY2), "todo")
        with self.assertRaisesRegex(ValueError, "solo se devuelven ventas"):
            self.edge.return_sale("dev-3", self.seq("r1"), 1, at(self.DAY2), "no es venta")
        voided = self.edge.void("anula", line, at(self.DAY2), "Venta registrada por error")
        self.assertEqual((voided["stock"], voided["revenue_minor"]), (10, -1800))  # solo las 2 pendientes
        self.assertEqual(self.summary(self.DAY1)["revenue_minor"], 900)  # 2700 − 1800 anulado ese día
        with self.assertRaisesRegex(ValueError, "anulada"):
            self.edge.return_sale("dev-4", line, 1, at(self.DAY2), "tarde")
        total = self.edge.weekly_summary("2026-09-21", "2026-09-28")
        self.assertEqual((total["revenue_minor"], total["cogs_minor"], total["sold_units"]), (0, 0, 0))

    def test_void_of_a_return_puts_the_sale_back(self):
        self.edge.sell("t1", [{"sku": "A", "quantity": 2}], at(self.DAY1, 10))
        self.edge.return_sale("dev", self.seq("t1#1"), 2, at(self.DAY2), "cambio de opinión")
        self.edge.void("anula-dev", self.seq("dev"), at(self.DAY2), "La devolución no era de esta tienda")
        self.assertEqual(self.edge.product("A")["stock"], 8)
        self.assertEqual(self.summary(self.DAY2)["revenue_minor"], 0)
        self.assertEqual(self.edge.return_sale("dev-2", self.seq("t1#1"), 2, at(self.DAY2), "ahora sí")["stock"], 10)

    def test_void_whole_ticket_atomically_and_ticket_history(self):
        lines = [{"sku": "A", "quantity": 2}, {"sku": "B", "quantity": 4}]
        self.edge.sell("t1", lines, at(self.DAY1, 10))
        self.edge.sell("t2", [{"sku": "B", "quantity": 1}], at(self.DAY1, 11))
        self.edge.return_sale("dev", self.seq("t1#2"), 1, at(self.DAY1, 12), "sobraba")
        receipt = self.edge.void_ticket("anula-t1", "t1", at(self.DAY1, 13), "Cliente no pagó")
        self.assertEqual([line["sku"] for line in receipt["lines"]], ["A", "B"])
        self.assertEqual(receipt["revenue_minor"], -2000 - 750)  # B: solo las 3 no devueltas
        self.assertEqual(self.edge.void_ticket("anula-t1", "t1", at(self.DAY1, 13), "Cliente no pagó"), receipt)
        with self.assertRaisesRegex(ValueError, "ya está anulado"):
            self.edge.void_ticket("anula-t1-bis", "t1", at(self.DAY1, 13), "otra vez")
        self.assertEqual((self.edge.product("A")["stock"], self.edge.product("B")["stock"]), (10, 9))
        tickets = {t["ticket_id"]: t for t in self.edge.tickets(day=self.DAY1)}
        self.assertEqual(set(tickets), {"t1", "t2"})  # la anulación no aparece como ticket
        self.assertEqual((tickets["t1"]["status"], tickets["t1"]["net_minor"]), ("anulado", 0))
        self.assertEqual((tickets["t2"]["status"], tickets["t2"]["net_minor"]), ("vigente", 250))
        self.assertEqual(self.summary(self.DAY1)["revenue_minor"], 250)
        with self.assertRaisesRegex(ValueError, "desconocido"):
            self.edge.void_ticket("x", "no-existe", at(self.DAY1), "x")

    def test_demand_and_top_sellers_use_net_sales(self):
        self.edge.sell("t1", [{"sku": "A", "quantity": 4}, {"sku": "B", "quantity": 3}], at(self.DAY1, 10))
        self.edge.void("v", self.seq("t1#1"), at(self.DAY1, 11), "error")
        self.edge.return_sale("d", self.seq("t1#2"), 1, at(self.DAY2), "dañado")
        units = self.edge.daily_units("sale")
        self.assertEqual(units, {"B": {self.DAY1: 3}})  # A anulada; el día de la devolución neta negativa no cuenta
        self.assertEqual([p["sku"] for p in self.edge.top_sellers(since=self.DAY1)], ["B"])

    def test_sync_sends_adjustments_as_v2_and_cloud_nets_them(self):
        from inventario.sync import Gateway, Receiver, validate_batch
        self.edge.sell("t1", [{"sku": "A", "quantity": 3}], at(self.DAY1, 10))
        gateway = Gateway(self.edge)
        cloud = Receiver(Path(self.folder.name) / "cloud.sqlite3")
        self.addCleanup(cloud.close)
        first = gateway.prepare()
        self.assertEqual(first["schema_version"], 1)  # sin compensaciones: contrato v1 intacto
        gateway.acknowledge(cloud.accept(first))
        self.edge.return_sale("d", self.seq("t1#1"), 1, at(self.DAY2), "dañado")
        self.edge.void("v", self.seq("r2"), at(self.DAY2), "recepción duplicada")
        second = gateway.prepare()
        self.assertEqual((second["schema_version"], len(second["rows"]), len(second["adjustments"])), (2, 0, 2))
        text = canonical_text(second)  # privacidad: sin SKU, identificadores de comando ni motivos
        for secret in ('"A"', '"B"', "sku", "command_id", "dañado", "duplicada", "ref_seq"):
            self.assertNotIn(secret, text)
        ack = cloud.accept(second)
        self.assertEqual(cloud.accept(second), ack)  # reenvío idempotente
        gateway.acknowledge(ack)
        rows = {json.loads(b)["day"] + json.loads(b)["product_ref"][:4]: json.loads(b)
                for (b,) in cloud.db.execute("SELECT body FROM daily")}
        sold = sum(r["sold_units"] for r in rows.values())
        received = sum(r["received_units"] for r in rows.values())
        revenue = sum(r["revenue_minor"] for r in rows.values())
        self.assertEqual((sold, received, revenue), (2, 10, 2000))  # 3−1 vendidas; 20−10 recibidas
        tampered = json.loads(json.dumps(second))
        tampered["adjustments"][0]["kind"] = "regalo"
        with self.assertRaises(ValueError):
            validate_batch(tampered)
        tampered = json.loads(json.dumps(second))
        tampered["adjustments"][1]["seq"] = tampered["adjustments"][0]["seq"]
        with self.assertRaises(ValueError):
            validate_batch(tampered)


def canonical_text(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class UserTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.edge = Edge(Path(self.folder.name) / "edge.sqlite3")
        self.addCleanup(lambda: self.edge.close())

    def test_pin_rules_hash_and_last_owner(self):
        self.assertFalse(self.edge.has_users())
        with self.assertRaisesRegex(ValueError, "primero cree el usuario dueño"):
            self.edge.add_user("Caja 1", "cajero", "4826")
        for bad in ("12", "abcd", "1111", "1234", "9876", "123456789"):
            with self.assertRaises(ValueError, msg=bad):
                self.edge.add_user("Ana", "dueno", bad)
        ana = self.edge.add_user("Ana", "dueno", "4826")
        self.assertTrue(self.edge.has_users())
        raw = self.edge.db.execute("SELECT pin_hash FROM users").fetchone()[0]
        self.assertNotIn(b"4826", raw)  # nunca en claro
        self.assertEqual(self.edge.check_pin(ana["id"], "4826")["role"], "dueno")
        self.assertIsNone(self.edge.check_pin(ana["id"], "4827"))
        self.assertIsNone(self.edge.check_pin(ana["id"], None))
        with self.assertRaisesRegex(ValueError, "ya existe"):
            self.edge.add_user("ana", "cajero", "5937")
        luis = self.edge.add_user("Luis", "cajero", "5937")
        self.assertIsNone(self.edge.owner_by_pin("5937"))  # un cajero no autoriza
        self.assertEqual(self.edge.owner_by_pin("4826")["name"], "Ana")
        with self.assertRaisesRegex(ValueError, "al menos un dueño"):
            self.edge.update_user(ana["id"], active=False)
        self.edge.update_user(luis["id"], active=False)
        self.assertIsNone(self.edge.check_pin(luis["id"], "5937"))
        self.edge.update_user(ana["id"], pin="7391")
        self.assertIsNone(self.edge.check_pin(ana["id"], "4826"))

    def test_movements_record_who_and_who_authorized(self):
        ana = self.edge.add_user("Ana", "dueno", "4826")
        luis = self.edge.add_user("Luis", "cajero", "5937")
        self.edge.add_product("A", price=1000, cost=600)
        self.edge.actor = (ana["id"], None)
        self.edge.move("r", "A", "receive", 5, TIME)
        self.edge.actor = (luis["id"], None)
        self.edge.sell("t1", [{"sku": "A", "quantity": 2}], TIME)
        seq = self.edge.db.execute("SELECT seq FROM events WHERE command_id='t1#1'").fetchone()[0]
        self.edge.actor = (luis["id"], ana["id"])
        self.edge.return_sale("d", seq, 1, TIME, "roto")
        self.edge.actor = (None, None)
        history = self.edge.movements(sku="A")
        self.assertEqual([(m["kind"], m["user"], m["authorized_by"]) for m in history],
                         [("return", "Luis", "Ana"), ("sale", "Luis", None), ("receive", "Ana", None)])
        self.assertEqual(self.edge.tickets()[0]["user"], "Luis")
        from inventario.sync import Gateway
        batch = json.dumps(Gateway(self.edge).prepare())
        for secret in ("Luis", "Ana", "user_id", "authorized_by"):
            self.assertNotIn(secret, batch)  # no viaja en la sincronización


class WindowlessTests(unittest.TestCase):
    def test_pythonw_without_console_logs_to_file(self):
        import sys
        from unittest import mock
        from inventario import app
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            app.redirect_output_if_windowless(Path(folder.name))
            print("hola desde pythonw")
            sys.stderr.write("traza\n")
            stream = sys.stdout
        stream.close()
        self.assertEqual((Path(folder.name) / "registro.log").read_text(encoding="utf-8"),
                         "hola desde pythonw\ntraza\n")
