import unittest

from inventario.analytics import Demand, PurchasePolicy, margin, recommend, rotation


class AnalyticsTests(unittest.TestCase):
    def test_missing_days_are_not_zero_sales(self):
        demand = Demand().update(10)
        missing = demand.update(None)
        self.assertEqual(missing.mean, 10)
        self.assertEqual(missing.observed, 1)
        self.assertEqual(missing.missing, 1)
        self.assertEqual(demand.update(0).mean, 8)

    def test_incremental_state_matches_replay(self):
        values = [0, 2, None, 10, 0, 4, 3] * 8
        whole = Demand()
        for value in values:
            whole = whole.update(value)
        partial = Demand()
        for value in values[:20]:
            partial = partial.update(value)
        restored = Demand(**vars(partial))
        for value in values[20:]:
            restored = restored.update(value)
        self.assertEqual(whole, restored)

    def test_purchase_example_and_pack(self):
        result = recommend(Demand(4, 1, 28), PurchasePolicy(3, pack=6), usable=8, inbound=6)
        self.assertEqual(result["reorder_point"], 15)
        self.assertEqual(result["target"], 46)
        self.assertEqual(result["raw_need"], 32)
        self.assertEqual(result["quantity"], 36)

    def test_periodic_review_orders_even_above_rop(self):
        demand, policy = Demand(4, 1, 28), PurchasePolicy(3)
        self.assertGreater(recommend(demand, policy, usable=20)["quantity"], 0)
        self.assertEqual(recommend(demand, policy, usable=20, scheduled_review=False)["quantity"], 0)

    def test_inbound_and_reservations(self):
        result = recommend(Demand(4, 0, 28), PurchasePolicy(3), usable=8, inbound=40, reserved=10)
        self.assertEqual(result["inventory_position"], 38)
        self.assertEqual(result["quantity"], 2)

    def test_budget_and_moq_conflict_does_not_overspend(self):
        result = recommend(Demand(4, 1, 28), PurchasePolicy(3, pack=6, moq=12,
                           unit_cost_minor=100, budget_minor=1000), usable=0)
        self.assertEqual(result["quantity"], 0)
        self.assertIn("restricciones_impiden_MOQ_revisar", result["reasons"])

    def test_perishable_limit_applies_after_rounding(self):
        result = recommend(Demand(4, 0, 28), PurchasePolicy(3, pack=6,
                           shelf_days_at_receipt=2), usable=0)
        self.assertEqual(result["quantity"], 6)
        self.assertLessEqual(result["quantity"], 8)

    def test_cold_start_does_not_invent_demand(self):
        result = recommend(Demand(), PurchasePolicy(3, manual_min=5), usable=1)
        self.assertEqual(result["quantity"], 4)
        self.assertEqual(result["confidence"], "baja")
        perishable = recommend(Demand(), PurchasePolicy(3, manual_min=5, shelf_days_at_receipt=3), usable=0)
        self.assertEqual(perishable["quantity"], 0)

    def test_category_and_intermittent_intervals_change_frozen_threshold(self):
        common = dict(stock=2, unit_cost_minor=100, eligible_days_without_sale=40)
        self.assertEqual(rotation(Demand(1, 0, 40), category="tienda", **common)["status"], "congelado")
        self.assertEqual(rotation(Demand(1, 0, 40), category="ferreteria", **common)["status"], "normal")
        self.assertEqual(rotation(Demand(1, 0, 40), category="tienda", median_sale_interval=20, **common)["status"], "normal")

    def test_new_product_is_not_frozen(self):
        result = rotation(Demand(), stock=5, unit_cost_minor=100, category="tienda", eligible_days_without_sale=50)
        self.assertEqual(result["status"], "datos_insuficientes")

    def test_expiry_risk_counts_cumulative_lots_once(self):
        result = rotation(Demand(2, 0, 30), stock=20, unit_cost_minor=100,
                          category="perecedero", eligible_days_without_sale=0,
                          lots=((1, 10), (2, 10)))
        self.assertEqual(result["expiry_risk_units"], 16)
        self.assertEqual(result["status"], "riesgo_vencimiento")

    def test_expired_stock_needs_no_forecast(self):
        result = rotation(Demand(), stock=10, unit_cost_minor=100, category="perecedero",
                          eligible_days_without_sale=0, lots=((-1, 3), (5, 7)))
        self.assertEqual(result["expired_units"], 3)
        self.assertEqual(result["status"], "vencido")
        self.assertIsNone(result["expiry_risk_units"])

    def test_margin_includes_waste_and_fees_without_double_counting(self):
        result = margin(revenue_minor=10000, cogs_minor=6000, waste_minor=1000, variable_fees_minor=500)
        self.assertEqual(result["contribution_minor"], 2500)
        self.assertEqual(result["margin_ratio"], 0.25)
        self.assertEqual(result["target_gap_minor"], 500)
        self.assertIsNone(margin(revenue_minor=0, cogs_minor=0)["margin_ratio"])

    def test_invalid_numeric_inputs(self):
        for call in (lambda: Demand(float("nan")), lambda: Demand().update(-1),
                     lambda: PurchasePolicy(3, pack=0),
                     lambda: PurchasePolicy(3, budget_minor=100),
                     lambda: recommend(Demand(), PurchasePolicy(3), usable=True)):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
