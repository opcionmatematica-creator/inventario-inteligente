"""Herramienta del VENDEDOR: claves de firma y emisión de licencias comerciales.

    python3 herramientas/licencias.py generar-claves
    python3 herramientas/licencias.py emitir --titular "Tienda La Esquina SAS" --nit 900123456-7 \\
        --plan esencial [--vence 2027-12-31] [--instalacion STREAM_ID] [--productos N] [--usuarios N]
    python3 herramientas/licencias.py ver LICENCIA

La clave privada vive FUERA del repositorio (por defecto
~/.config/inventario-inteligente-vendedor/clave_privada.json, permisos 600; cambiar con
INVENTARIO_CLAVE_VENDEDOR). Haga una copia de seguridad cifrada: si se pierde, no podrá emitir
licencias válidas para las versiones ya instaladas. Nunca la suba a git ni la envíe a nadie.
Cada licencia emitida se anota en registro-licencias.csv junto a la clave (control de ventas).
"""

import argparse
import csv
from datetime import date
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inventario import licencia  # noqa: E402

KEY_PATH = Path(os.environ.get("INVENTARIO_CLAVE_VENDEDOR",
                               Path.home() / ".config" / "inventario-inteligente-vendedor" / "clave_privada.json"))
SMALL_PRIMES = [p for p in range(3, 2000) if all(p % q for q in range(2, int(p ** 0.5) + 1))]


def probable_prime(n: int, rounds: int = 64) -> bool:
    if n < 2:
        return False
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def random_prime(bits: int, e: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if probable_prime(candidate) and (candidate - 1) % e:
            return candidate


def generate_keys(bits: int = 2048) -> dict:
    e = licencia.PUBLIC_E
    while True:
        p, q = random_prime(bits // 2, e), random_prime(bits // 2, e)
        n = p * q
        if p != q and n.bit_length() == bits:
            break
    d = pow(e, -1, (p - 1) * (q - 1))
    return {"n": str(n), "e": e, "d": str(d), "creada": date.today().isoformat()}


def load_key() -> dict:
    if not KEY_PATH.exists():
        raise SystemExit(f"No hay clave privada en {KEY_PATH}. Use «generar-claves» (una sola vez).")
    key = json.loads(KEY_PATH.read_text())
    if int(key["n"]) != licencia.PUBLIC_N:
        raise SystemExit("La clave privada no corresponde a la clave pública de inventario/licencia.py.")
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="accion", required=True)
    sub.add_parser("generar-claves")
    emitir = sub.add_parser("emitir")
    emitir.add_argument("--titular", required=True)
    emitir.add_argument("--nit", default="")
    emitir.add_argument("--plan", required=True, choices=[p for p in licencia.PLANS if p != "evaluacion"])
    emitir.add_argument("--vence", default=None, help="AAAA-MM-DD; sin fecha = perpetua")
    emitir.add_argument("--instalacion", default=None, help="stream_id de la instalación (opcional)")
    emitir.add_argument("--productos", type=int, default=None)
    emitir.add_argument("--usuarios", type=int, default=None)
    ver = sub.add_parser("ver")
    ver.add_argument("licencia")
    args = parser.parse_args(argv)

    if args.accion == "generar-claves":
        if KEY_PATH.exists():
            raise SystemExit(f"Ya existe {KEY_PATH}: no se sobrescribe (invalidaría las licencias emitidas).")
        key = generate_keys()
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(key, handle)
        print(f"Clave privada guardada en {KEY_PATH} (permisos 600). Haga una copia de seguridad cifrada.")
        print("Clave pública para PUBLIC_N en inventario/licencia.py:")
        print(key["n"])
        return
    if args.accion == "ver":
        print(json.dumps(licencia.decode(args.licencia), ensure_ascii=False, indent=2))
        return
    key = load_key()
    if args.vence:
        date.fromisoformat(args.vence)
    claims = {"v": 1, "licencia_id": str(uuid4()), "titular": args.titular.strip(), "nit": args.nit.strip(),
              "plan": args.plan, "emitida": date.today().isoformat(), "vence": args.vence}
    for name in ("instalacion", "productos", "usuarios"):
        if getattr(args, name):
            claims[name] = getattr(args, name)
    text = licencia.encode(claims, int(key["n"]), int(key["d"]))
    licencia.decode(text)  # comprobación: la licencia recién emitida debe validar
    registry = KEY_PATH.parent / "registro-licencias.csv"
    new = not registry.exists()
    with open(registry, "a", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(("licencia_id", "emitida", "titular", "nit", "plan", "vence", "instalacion"))
        writer.writerow((claims["licencia_id"], claims["emitida"], claims["titular"], claims["nit"],
                         claims["plan"], claims["vence"] or "perpetua", claims.get("instalacion", "")))
    print(text)


if __name__ == "__main__":
    main()
