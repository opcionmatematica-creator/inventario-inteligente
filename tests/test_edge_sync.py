from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from inventario.edge import Edge
from inventario.sync import Gateway, Receiver, validate_batch

TIME = "2026-09-27T10:00:00-05:00"


class EdgeSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "edge.sqlite3"
        self.edge = Edge(self.path)
        self.addCleanup(lambda: self.edge.close())
        self.cloud = Receiver(Path(self.folder.name) / "cloud.sqlite3")
        self.addCleanup(self.cloud.close)
        self.edge.add_product("P1", price=150, cost=100, minimum=3)
        self.edge.move("receive-1", "P1", "receive", 10, TIME)

    def test_sale_retry_does_not_repeat_stock_or_revenue(self):
        first = self.edge.move("sale-1", "P1", "sale", 7, TIME)
        self.assertEqual(first, self.edge.move("sale-1", "P1", "sale", 7, TIME))
        self.assertEqual(self.edge.product("P1")["stock"], 3)
        self.assertTrue(first["low_stock"])
        self.assertEqual(self.edge.db.execute("SELECT count(*) FROM events").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            self.edge.move("sale-1", "P1", "sale", 1, TIME)

    def test_insufficient_stock_has_no_side_effect(self):
        with self.assertRaises(ValueError):
            self.edge.move("sale-1", "P1", "sale", 11, TIME)
        self.assertEqual(self.edge.product("P1")["stock"], 10)
        self.assertEqual(self.edge.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_failure_writing_outbox_rolls_back_stock(self):
        self.edge.db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.edge.move("sale-1", "P1", "sale", 3, TIME)
        self.assertEqual(self.edge.product("P1")["stock"], 10)
        self.edge.db.execute("DROP TRIGGER fail_event")
        self.assertEqual(self.edge.move("sale-1", "P1", "sale", 3, TIME)["stock"], 7)

    def test_concurrent_sales_do_not_oversell(self):
        def sale(number):
            connection = Edge(self.path)
            try:
                connection.move(f"sale-{number}", "P1", "sale", 7, TIME)
                return True
            except ValueError:
                return False
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(sale, (1, 2))), 1)
        self.assertEqual(self.edge.product("P1")["stock"], 3)

    def test_restart_preserves_receipt_and_pending_batch(self):
        sale = self.edge.move("sale-1", "P1", "sale", 2, TIME)
        batch = Gateway(self.edge).prepare()
        self.edge.close()
        self.edge = Edge(self.path)
        self.assertEqual(Gateway(self.edge).prepare(), batch)
        self.assertEqual(self.edge.move("sale-1", "P1", "sale", 2, TIME), sale)

    def test_lost_ack_retry_and_next_delta_do_not_double_count(self):
        self.edge.move("sale-1", "P1", "sale", 2, TIME)
        gateway = Gateway(self.edge)
        batch = gateway.prepare()
        ack = self.cloud.accept(batch)
        self.edge.move("sale-2", "P1", "sale", 3, TIME)
        self.assertEqual(gateway.prepare(), batch)
        self.assertEqual(self.cloud.accept(batch), ack)
        gateway.acknowledge(ack)
        second = gateway.prepare()
        gateway.acknowledge(self.cloud.accept(second))
        total = json.loads(self.cloud.db.execute("SELECT body FROM daily").fetchone()[0])
        self.assertEqual(total["sold_units"], 5)
        self.assertEqual(total["received_units"], 10)
        self.assertEqual(total["revenue_minor"], 750)
        self.assertEqual(total["stock_after"], 5)
        self.assertIsNone(gateway.prepare())

    def test_conflicting_batch_and_new_id_same_range_are_rejected(self):
        batch = Gateway(self.edge).prepare()
        self.cloud.accept(batch)
        changed = deepcopy(batch)
        changed["rows"][0]["stock_after"] += 1
        with self.assertRaises(ValueError):
            self.cloud.accept(changed)
        changed = deepcopy(batch)
        changed["batch_id"] = str(uuid4())
        with self.assertRaises(ValueError):
            self.cloud.accept(changed)

    def test_gap_and_wrong_ack_do_not_advance_cursor(self):
        gateway = Gateway(self.edge)
        batch = gateway.prepare()
        gap = deepcopy(batch)
        gap["first_seq"] += 1
        gap["last_seq"] += 1
        gap["rows"][0]["last_seq"] += 1
        with self.assertRaises(ValueError):
            self.cloud.accept(gap)
        ack = self.cloud.accept(batch)
        ack["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            gateway.acknowledge(ack)
        self.assertEqual(gateway.prepare(), batch)

    def test_batch_boundaries_are_contiguous(self):
        self.edge.move("sale-1", "P1", "sale", 1, TIME)
        gateway = Gateway(self.edge)
        first = gateway.prepare(max_events=1)
        self.assertEqual(first["last_seq"], 1)
        gateway.acknowledge(self.cloud.accept(first))
        second = gateway.prepare(max_events=1)
        self.assertEqual(second["first_seq"], 2)
        gateway.acknowledge(self.cloud.accept(second))
        self.assertIsNone(gateway.prepare())

    def test_local_date_and_half_open_week(self):
        self.edge.move("sale-1", "P1", "sale", 2, "2026-09-28T04:59:00+00:00")
        self.edge.move("sale-2", "P1", "sale", 1, "2026-09-28T05:00:00+00:00")
        summary = self.edge.weekly_summary("2026-09-21", "2026-09-28")
        self.assertEqual(summary["sold_units"], 2)
        self.assertEqual(summary["contribution_minor"], 100)

    def test_batch_uses_pseudonym_and_no_customer_data(self):
        batch = Gateway(self.edge).prepare()
        serialized = json.dumps(batch)
        self.assertNotIn('"P1"', serialized)
        self.assertNotIn("command_id", serialized)
        self.assertNotIn("occurred_at", serialized)
        validate_batch(batch)

    def test_contract_rejects_unknown_fields_and_inconsistent_counts(self):
        batch = Gateway(self.edge).prepare()
        wrong = deepcopy(batch)
        wrong["customer_name"] = "should not be here"
        with self.assertRaises(ValueError):
            validate_batch(wrong)
        wrong = deepcopy(batch)
        wrong["rows"][0]["event_count"] = 2
        with self.assertRaises(ValueError):
            validate_batch(wrong)

    def test_checked_in_example_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "sync-batch.json"
        validate_batch(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
