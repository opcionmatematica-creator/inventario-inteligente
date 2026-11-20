"""Analítica sobre el historial real del Core: demanda, compras, rotación, alertas y tablero.

Regla de días (ver docs/algoritmo.md): un día con algún movimiento registrado en el
comercio es un día operado; ahí, un producto sin ventas vendió 0. Un día sin ningún
movimiento es desconocido (cerrado o sin registrar) y no se cuenta como cero.
El día en curso no entra en la demanda: está incompleto.

Rendimiento: la demanda usa los últimos DEMAND_WINDOW_DAYS días (con alpha=0.2 el peso
de lo anterior es 0.8^365 ≈ 1e-35) y aplica en forma cerrada cada racha de días
abiertos sin venta, así que cuesta O(días con venta) por producto y no O(días de historial).
Los informes se guardan en caché mientras no cambien los datos (total_changes de SQLite).
"""

from bisect import bisect_left
from datetime import date, timedelta
from statistics import median

from .analytics import Demand, PurchasePolicy, recommend, rotation

REASONS = {
    "historial_insuficiente_minimo_manual": "Menos de 14 días con datos: se usa el mínimo que configuraste.",
    "revision_periodica": "Revisión semanal según la venta diaria estimada.",
    "revision_urgente": "Por debajo del punto de pedido.",
    "datos_incompletos_o_censurados": "Hay días sin registros; la estimación es menos fiable.",
    "limite_capacidad": "Recortado por el espacio disponible.",
    "limite_presupuesto": "Recortado por el presupuesto.",
    "limite_vida_util": "Recortado para no vencer antes de venderse.",
    "restricciones_impiden_MOQ_revisar": "Las restricciones no permiten el pedido mínimo del proveedor: revísalo.",
    "cobertura_incompleta_revisar": "La compra no cubre toda la demanda objetivo: revísalo.",
}

STATUS_TEXT = {
    "normal": "Normal",
    "sin_stock": "Sin stock",
    "vencido": "Vencido",
    "riesgo_vencimiento": "Riesgo de vencimiento",
    "datos_insuficientes": "Pocos datos",
    "congelado": "Sin movimiento",
    "exceso": "Exceso de stock",
}


DEMAND_WINDOW_DAYS = 365
ALPHA = 0.2


def demand_from_history(open_days: list[str], sales: dict[str, int], start: str, end: str,
                        alpha: float = ALPHA) -> Demand:
    """Equivale a recorrer los días de [start, end) con Demand.update: ventas del día en días
    abiertos (0 si no hubo), None en días sin ningún movimiento. open_days debe estar ordenada."""
    if start >= end:
        return Demand()
    lo, hi = bisect_left(open_days, start), bisect_left(open_days, end)
    calendar = (date.fromisoformat(end) - date.fromisoformat(start)).days
    beta = 1 - alpha
    mean = variance = 0.0
    observed = 0
    position = lo

    def zeros(k):
        nonlocal mean, variance, observed
        if k <= 0:
            return
        if observed == 0:  # el primer día observado fija la media (aquí 0) y la varianza 0
            mean = variance = 0.0
        else:
            decay = beta ** k
            variance = decay * (variance + mean * mean * (1 - decay))
            mean = decay * mean
        observed += k

    for day in sorted(d for d in sales if start <= d < end):
        index = bisect_left(open_days, day)
        zeros(index - position)
        units = sales[day]
        if observed == 0:
            mean, variance = float(units), 0.0
        else:
            delta = units - mean
            mean, variance = mean + alpha * delta, beta * (variance + alpha * delta * delta)
        observed += 1
        position = index + 1
    zeros(hi - position)
    return Demand(mean=max(0.0, mean), variance=max(0.0, variance), observed=observed,
                  missing=calendar - (hi - lo))


class Insights:
    def __init__(self, edge):
        self.edge = edge
        self._cache: tuple | None = None

    def _context(self, today: str | None = None, sku: str | None = None) -> dict:
        today = today or self.edge.local_today()
        since = (date.fromisoformat(today) - timedelta(days=DEMAND_WINDOW_DAYS)).isoformat()
        return {"today": today, "since": since, "open_days": self.edge.active_days(since=since),
                "sales": self.edge.daily_units("sale", since=since, sku=sku),
                "first": self.edge.first_days(sku)}

    def demand(self, sku: str, ctx: dict) -> Demand:
        first = ctx["first"].get(sku)
        if first is None:
            return Demand()
        return demand_from_history(ctx["open_days"], ctx["sales"].get(sku, {}),
                                   max(first, ctx["since"]), ctx["today"])

    def _rotation(self, product: dict, demand: Demand, ctx: dict) -> dict:
        first = ctx["first"].get(product["sku"])
        sale_days = sorted(ctx["sales"].get(product["sku"], {}))
        start = sale_days[-1] if sale_days else first
        idle = 0
        if start:
            start = max(start, ctx["since"])
            idle = (bisect_left(ctx["open_days"], ctx["today"]) - bisect_left(ctx["open_days"], start)
                    - (1 if sale_days and start == sale_days[-1] else 0))
        gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(sale_days, sale_days[1:])]
        result = rotation(demand, stock=product["stock"], unit_cost_minor=product["cost"],
                          category=product["category"], eligible_days_without_sale=max(0, idle),
                          median_sale_interval=median(gaps) if gaps else 1)
        result["status_text"] = STATUS_TEXT[result["status"]]
        result["days_without_sale"] = max(0, idle)
        return result

    def product_report(self, product: dict, ctx: dict | None = None) -> dict:
        ctx = ctx or self._context(sku=product["sku"])
        demand = self.demand(product["sku"], ctx)
        policy = PurchasePolicy(lead_days=product["lead_days"], pack=product["pack"], moq=product["moq"],
                                manual_min=product["minimum"], unit_cost_minor=product["cost"])
        purchase = recommend(demand, policy, usable=product["stock"])
        purchase["reason_text"] = [REASONS.get(r, r) for r in purchase["reasons"]]
        return {"product": product, "purchase": purchase, "rotation": self._rotation(product, demand, ctx),
                "demand": {"daily_mean": round(demand.mean, 3), "observed_days": demand.observed,
                           "missing_days": demand.missing}}

    def reports(self, today: str | None = None) -> list[dict]:
        today = today or self.edge.local_today()
        # data_version cambia cuando otra conexión confirma escrituras; total_changes, con las propias.
        key = (self.edge.db.execute("PRAGMA data_version").fetchone()[0], self.edge.db.total_changes, today)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        ctx = self._context(today)
        result = [self.product_report(p, ctx) for p in self.edge.products(limit=1_000_000)]
        self._cache = (key, result)
        return result

    def alerts(self, today: str | None = None) -> list[dict]:
        out = []
        for report in self.reports(today):
            p, rot, buy = report["product"], report["rotation"], report["purchase"]
            name = p["name"] or p["sku"]
            if p["stock"] == 0:
                out.append({"level": "alta", "type": "sin_stock", "sku": p["sku"],
                            "text": f"{name}: sin stock."})
            elif p["stock"] <= p["minimum"]:
                out.append({"level": "alta", "type": "minimo", "sku": p["sku"],
                            "text": f"{name}: quedan {p['stock']} (mínimo {p['minimum']})."})
            if rot["status"] == "congelado":
                out.append({"level": "media", "type": "congelado", "sku": p["sku"],
                            "text": f"{name}: {rot['days_without_sale']} días abiertos sin vender.",
                            "capital_minor": rot["frozen_capital_minor"]})
            elif rot["status"] == "exceso":
                out.append({"level": "baja", "type": "exceso", "sku": p["sku"],
                            "text": f"{name}: stock para más de {round(rot['coverage_days'])} días."})
            # Solo avisa al cruzar el punto de pedido; la reposición semanal normal
            # aparece en el pedido sugerido, no como alerta.
            if (buy["quantity"] > 0 and p["stock"] > p["minimum"]
                    and buy["reorder_point"] and p["stock"] <= buy["reorder_point"]):
                out.append({"level": "media", "type": "comprar", "sku": p["sku"],
                            "text": f"{name}: llegó al punto de pedido ({p['stock']} ≤ {buy['reorder_point']});"
                                    f" conviene pedir {buy['quantity']}."})
        order = {"alta": 0, "media": 1, "baja": 2}
        return sorted(out, key=lambda a: order[a["level"]])

    def purchases(self, today: str | None = None) -> dict:
        lines = []
        for report in self.reports(today):
            buy = report["purchase"]
            if buy["quantity"] > 0:
                p = report["product"]
                lines.append({"sku": p["sku"], "name": p["name"], "stock": p["stock"],
                              "quantity": buy["quantity"], "estimated_cost_minor": buy["estimated_cost_minor"],
                              "confidence": buy["confidence"], "daily_demand": buy["daily_demand"],
                              "reason_text": buy["reason_text"]})
        return {"status": "draft", "lines": lines,
                "total_minor": sum(line["estimated_cost_minor"] for line in lines),
                "note": "Borrador para revisar: no se envía a ningún proveedor."}

    def dashboard(self, today: str | None = None) -> dict:
        today = today or self.edge.local_today()
        day = date.fromisoformat(today)
        week_start = (day - timedelta(days=day.weekday())).isoformat()
        tomorrow = (day + timedelta(days=1)).isoformat()
        products = self.edge.products(limit=1_000_000)
        alerts = self.alerts(today)
        return {"today": today,
                "today_summary": self.edge.weekly_summary(today, tomorrow),
                "week_summary": self.edge.weekly_summary(week_start, tomorrow),
                "products": len(products),
                "stock_value_minor": sum(p["stock"] * p["cost"] for p in products),
                "alerts": alerts[:8], "alert_count": len(alerts)}
