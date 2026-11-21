"""Licencias comerciales verificables sin internet y límites por plan.

Una licencia es un texto «INV1.<datos>.<firma>» (base64url): <datos> es un JSON con el titular, el
plan y las fechas; <firma> es RSA-2048 PKCS#1 v1.5 con SHA-256 sobre <datos>, hecha con la clave
privada del vendedor (nunca en este repositorio; ver herramientas/licencias.py). Aquí solo está la
clave pública, así que una licencia no se puede fabricar ni modificar sin invalidar la firma.

Política (docs/arquitectura.md, sección D): al llegar al límite del plan solo se bloquean las altas
nuevas (productos y usuarios); vender, consultar, devolver, exportar y editar lo existente siguen
funcionando siempre. Una licencia vencida conserva su plan GRACE_DAYS días y después la instalación
vuelve a los límites de evaluación, sin borrar nada.
"""

import base64
from datetime import date, timedelta
import hashlib
import hmac
import json

PREFIX = "INV1"
GRACE_DAYS = 15
PLANS = {
    "evaluacion": {"name": "Evaluación", "products": 100, "users": 2},
    "esencial": {"name": "Esencial", "products": 500, "users": 2},
    "inteligente": {"name": "Inteligente", "products": 5000, "users": 5},
    "negocio": {"name": "Negocio", "products": 25000, "users": 20},
}

# Clave pública del vendedor (RSA-2048, e=65537). La privada correspondiente se guarda fuera del
# repositorio. Para cambiarla: herramientas/licencias.py generar-claves.
PUBLIC_N = int(
    "223285451098664777536880908900743634724594328721511392710936621611347867921044987893942330"
    "877789245666133832809563224520635087334674741416753786051360750609054290279936791807549289"
    "345354933268261300506394599299041559061515791166709159853444454855463281357477055583094148"
    "729544691548231991754299451678531716511335885059070293663834041669078977444821812408906469"
    "629075065992014747178660076058628324183117636258718242684661344767093537291381254159349519"
    "026655501140702719449576426267254918602384982613570889340333303656007710009377256724778740"
    "72682492862857004131711036258015257731836013785485402059997164746639272139713"
)
PUBLIC_E = 65537

# DigestInfo DER de SHA-256 (RFC 8017, sección 9.2, nota 1).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def encoded_digest(message: bytes, size: int) -> bytes:
    """EMSA-PKCS1-v1_5 con SHA-256 para una clave de `size` bytes."""
    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if size < len(digest) + 11:
        raise ValueError("clave demasiado corta")
    return b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest


def verify_signature(message: bytes, signature: bytes, n: int | None = None, e: int | None = None) -> bool:
    n, e = n or PUBLIC_N, e or PUBLIC_E
    size = (n.bit_length() + 7) // 8
    if n < 2 or len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= n:
        return False
    recovered = pow(value, e, n).to_bytes(size, "big")
    return hmac.compare_digest(recovered, encoded_digest(message, size))


def sign(message: bytes, n: int, d: int) -> bytes:
    size = (n.bit_length() + 7) // 8
    return pow(int.from_bytes(encoded_digest(message, size), "big"), d, n).to_bytes(size, "big")


def encode(claims: dict, n: int, d: int) -> str:
    data = json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return f"{PREFIX}.{_b64encode(data)}.{_b64encode(sign(data, n, d))}"


def decode(text: str, n: int | None = None, e: int | None = None) -> dict:
    """Licencia → datos, si la firma es válida y el formato correcto (si no, ValueError)."""
    if not isinstance(text, str):
        raise ValueError("licencia inválida")
    parts = "".join(text.split()).split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise ValueError("eso no parece una licencia de Inventario Inteligente")
    try:
        data, signature = _b64decode(parts[1]), _b64decode(parts[2])
    except ValueError as exc:
        raise ValueError("licencia dañada (¿se copió incompleta?)") from exc
    if not verify_signature(data, signature, n, e):
        raise ValueError("la firma de la licencia no es válida")
    claims = json.loads(data)
    if claims.get("v") != 1 or claims.get("plan") not in PLANS or claims["plan"] == "evaluacion":
        raise ValueError("licencia de una versión o plan desconocidos")
    for key in ("licencia_id", "titular", "emitida"):
        if not isinstance(claims.get(key), str) or not claims[key]:
            raise ValueError("licencia incompleta")
    date.fromisoformat(claims["emitida"])
    if claims.get("vence") is not None:
        date.fromisoformat(claims["vence"])
    return claims


def status(text: str | None, today: str, installation: str) -> dict:
    """Estado efectivo de una instalación: plan, límites y avisos."""
    evaluation = {"plan": "evaluacion", **PLANS["evaluacion"], "licensed": False, "claims": None,
                  "warning": None}
    if not text:
        return evaluation
    try:
        claims = decode(text)
    except ValueError as exc:
        return {**evaluation, "warning": f"Licencia no válida: {exc}"}
    if claims.get("instalacion") and claims["instalacion"] != installation:
        return {**evaluation, "warning": "La licencia es de otra instalación."}
    plan = PLANS[claims["plan"]]
    result = {"plan": claims["plan"], "name": plan["name"],
              "products": int(claims.get("productos") or plan["products"]),
              "users": int(claims.get("usuarios") or plan["users"]),
              "licensed": True, "claims": claims, "warning": None}
    expires = claims.get("vence")
    if expires:
        end = date.fromisoformat(expires)
        current = date.fromisoformat(today)
        if current > end + timedelta(days=GRACE_DAYS):
            return {**evaluation, "claims": claims,
                    "warning": f"La licencia venció el {expires}: se aplican los límites de evaluación."}
        if current > end:
            result["warning"] = (f"La licencia venció el {expires}. Renuévela antes del "
                                 f"{(end + timedelta(days=GRACE_DAYS)).isoformat()} para conservar el plan.")
        elif (end - current).days <= 30:
            result["warning"] = f"La licencia vence el {expires}."
    return result
