"""Reglas explicables. Cantidades enteras; importes en unidades monetarias menores."""

from dataclasses import dataclass
from math import ceil, floor, isfinite, sqrt


def nonnegative(**values: float) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isfinite(value) or value < 0:
            raise ValueError(f"{name} debe ser finito y no negativo")


def integer(**values: int) -> None:
    for name, value in values.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} debe ser un entero no negativo")


@dataclass(frozen=True)
class Demand:
    mean: float = 0.0
    variance: float = 0.0
    observed: int = 0
    missing: int = 0

    def __post_init__(self):
        nonnegative(mean=self.mean, variance=self.variance)
        integer(observed=self.observed, missing=self.missing)

    def update(self, units: int | None, alpha: float = 0.2) -> "Demand":
        if not isfinite(alpha) or not 0 < alpha <= 1:
            raise ValueError("alpha debe estar entre 0 y 1")
        if units is None:
            return Demand(self.mean, self.variance, self.observed, self.missing + 1)
        integer(units=units)
        if not self.observed:
            return Demand(float(units), 0.0, 1, self.missing)
        delta = units - self.mean
        return Demand(
            self.mean + alpha * delta,
            (1 - alpha) * (self.variance + alpha * delta * delta),
            self.observed + 1,
            self.missing,
        )


@dataclass(frozen=True)
class PurchasePolicy:
    lead_days: float
    review_days: float = 7
    lead_std_days: float = 0
    service_z: float = 1.645
    manual_min: int = 0
    pack: int = 1
    moq: int = 1
    shelf_days_at_receipt: float | None = None
    capacity_units: int | None = None  # Espacio disponible para esta compra.
    budget_minor: int | None = None  # Presupuesto asignado a este SKU.
    unit_cost_minor: int = 0

    def __post_init__(self):
        nonnegative(lead_days=self.lead_days, review_days=self.review_days,
                    lead_std_days=self.lead_std_days, service_z=self.service_z)
        integer(manual_min=self.manual_min, pack=self.pack, moq=self.moq,
                unit_cost_minor=self.unit_cost_minor)
        if self.pack == 0:
            raise ValueError("pack debe ser positivo")
        if self.shelf_days_at_receipt is not None:
            nonnegative(shelf_days=self.shelf_days_at_receipt)
        for name in ("capacity_units", "budget_minor"):
            if getattr(self, name) is not None:
                integer(**{name: getattr(self, name)})
        if self.budget_minor is not None and self.unit_cost_minor == 0:
            raise ValueError("presupuesto requiere costo unitario conocido y positivo")


def recommend(demand: Demand, policy: PurchasePolicy, *, usable: int,
              inbound: int = 0, reserved: int = 0,
              scheduled_review: bool = True) -> dict:
    """IP depurado por el llamador: inbound solo confirmado y a tiempo."""
    integer(usable=usable, inbound=inbound, reserved=reserved)
    ip = usable + inbound - reserved
    reasons = []
    enough = demand.observed >= 14
    coverage = demand.observed / max(1, demand.observed + demand.missing)
    confidence = "media" if enough and coverage >= 0.8 else "baja"
    if not enough:
        rop = target = policy.manual_min
        reasons.append("historial_insuficiente_minimo_manual")
    else:
        def safety(horizon):
            return policy.service_z * sqrt(
                horizon * demand.variance + demand.mean**2 * policy.lead_std_days**2
            )
        rop = ceil(demand.mean * policy.lead_days + safety(policy.lead_days))
        horizon = policy.lead_days + policy.review_days
        target = ceil(demand.mean * horizon + safety(horizon))
        reasons.append("revision_periodica" if scheduled_review else "revision_urgente")
    if coverage < 0.8:
        reasons.append("datos_incompletos_o_censurados")
    raw = max(0, target - ip) if scheduled_review or ip <= rop else 0
    quantity = ((max(raw, policy.moq) + policy.pack - 1) // policy.pack) * policy.pack if raw else 0
    limits = []
    if policy.capacity_units is not None:
        limits.append(("capacidad", policy.capacity_units))
    if policy.budget_minor is not None:
        limits.append(("presupuesto", policy.budget_minor // policy.unit_cost_minor))
    if policy.shelf_days_at_receipt is not None:
        arrival_stock = max(0, ip - demand.mean * policy.lead_days)
        limit = max(0, floor(demand.mean * policy.shelf_days_at_receipt - arrival_stock)) if enough else 0
        limits.append(("vida_util", limit))
    for reason, limit in limits:
        capped = (limit // policy.pack) * policy.pack
        if quantity > capped:
            quantity = capped
            reasons.append(f"limite_{reason}")
    if raw > 0 and quantity < policy.moq:
        quantity = 0
        reasons.append("restricciones_impiden_MOQ_revisar")
    if quantity < raw:
        reasons.append("cobertura_incompleta_revisar")
    return {"quantity": quantity, "raw_need": raw, "inventory_position": ip,
            "reorder_point": rop, "target": target, "confidence": confidence,
            "estimated_cost_minor": quantity * policy.unit_cost_minor,
            "daily_demand": round(demand.mean, 4), "reasons": reasons,
            "model_version": "local-ewma-v1", "status": "draft"}


def rotation(demand: Demand, *, stock: int, unit_cost_minor: int,
             category: str, eligible_days_without_sale: int,
             median_sale_interval: float = 1,
             lots: tuple[tuple[int, int], ...] = ()) -> dict:
    """lots: (días calendario hasta vencimiento, unidades), solo stock presente.

    Día 0 = ya no vendible. Intervalos sin venta se cuentan con stock y apertura.
    """
    integer(stock=stock, unit_cost_minor=unit_cost_minor,
            eligible_days_without_sale=eligible_days_without_sale)
    nonnegative(median_sale_interval=median_sale_interval)
    bases = {"perecedero": 7, "tienda": 30, "ferreteria": 90}
    if category not in bases:
        raise ValueError("categoría desconocida")
    for days, units in lots:
        if type(days) is not int:
            raise ValueError("vencimiento debe expresarse en días enteros")
        integer(lot_units=units)
    if sum(units for _, units in lots) > stock:
        raise ValueError("los lotes no pueden exceder el stock")
    threshold = max(bases[category], ceil(3 * median_sale_interval))
    coverage = stock / demand.mean if demand.mean > 0 else None
    enough = demand.observed >= 14
    cumulative = expired = risk = 0
    for days, units in sorted(lots):
        if days <= 0:
            expired += units
            continue
        cumulative += units
        if enough:
            risk = max(risk, max(0, ceil(cumulative - demand.mean * days)))
    status = "normal"
    if not stock:
        status = "sin_stock"
    elif expired:
        status = "vencido"
    elif risk:
        status = "riesgo_vencimiento"
    elif not enough:
        status = "datos_insuficientes"
    elif eligible_days_without_sale >= threshold:
        status = "congelado"
    elif coverage is not None and coverage > 2 * threshold:
        status = "exceso"
    frozen = stock if enough and eligible_days_without_sale >= threshold else 0
    return {"status": status, "coverage_days": coverage,
            "frozen_threshold_days": threshold, "frozen_units": frozen,
            "frozen_capital_minor": frozen * unit_cost_minor,
            "expiry_risk_units": risk if enough else None,
            "expired_units": expired,
            "expiry_risk_cost_minor": risk * unit_cost_minor if enough else None}


def margin(*, revenue_minor: int, cogs_minor: int, waste_minor: int = 0,
           variable_fees_minor: int = 0, target_basis_points: int = 3000) -> dict:
    # Ingresos y costo vendido pueden ser negativos en semanas de devoluciones.
    if type(revenue_minor) is not int or type(cogs_minor) is not int:
        raise ValueError("ingresos y costos deben ser enteros")
    integer(waste_minor=waste_minor, variable_fees_minor=variable_fees_minor,
            target_basis_points=target_basis_points)
    if target_basis_points > 10000:
        raise ValueError("margen objetivo debe estar entre 0 y 10000 puntos básicos")
    contribution = revenue_minor - cogs_minor - waste_minor - variable_fees_minor
    gap = max(0, (revenue_minor * target_basis_points + 9999) // 10000 - contribution) if revenue_minor > 0 else None
    return {"contribution_minor": contribution,
            "margin_ratio": contribution / revenue_minor if revenue_minor > 0 else None,
            "target_gap_minor": gap}
