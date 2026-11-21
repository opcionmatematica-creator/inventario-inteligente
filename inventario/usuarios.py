"""Recuperar el acceso desde el propio equipo (requiere acceso a los archivos de datos).

    python3 -m inventario.usuarios listar      [--datos CARPETA]
    python3 -m inventario.usuarios restablecer NOMBRE [--datos CARPETA]

«restablecer» pide un PIN nuevo para ese usuario y lo reactiva. Úselo si el dueño olvidó su PIN.
Cierre antes la aplicación (o reiníciela después) para que no queden sesiones antiguas.
"""

import argparse
from getpass import getpass
from pathlib import Path

from .edge import Edge
from .server import data_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description="Usuarios de Inventario Inteligente")
    parser.add_argument("accion", choices=("listar", "restablecer"))
    parser.add_argument("nombre", nargs="?")
    parser.add_argument("--datos", type=Path, default=None)
    args = parser.parse_args(argv)
    path = (args.datos or data_dir()) / "inventario.sqlite3"
    if not path.exists():
        raise SystemExit(f"No hay datos en {path}")
    edge = Edge(path)
    try:
        users = edge.users()
        if args.accion == "listar":
            for user in users:
                print(f"{user['name']:<24} {user['role']:<7} {'activo' if user['active'] else 'inactivo'}")
            if not users:
                print("Sin usuarios: la aplicación no pide PIN.")
            return
        match = [u for u in users if u["name"].lower() == (args.nombre or "").lower()]
        if not match:
            raise SystemExit("Usuario desconocido. Use «listar» para ver los nombres.")
        pin = getpass("PIN nuevo (4 a 8 dígitos): ")
        if pin != getpass("Repítalo: "):
            raise SystemExit("Los PIN no coinciden.")
        edge.update_user(match[0]["id"], pin=pin, active=True)
        print(f"Listo: PIN de {match[0]['name']} restablecido.")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        edge.close()


if __name__ == "__main__":
    main()
