from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herramientas"))
import licencias as herramienta  # noqa: E402

from inventario import csvio, licencia  # noqa: E402
from inventario.edge import Edge  # noqa: E402

TODAY = "2026-11-21"
KEY = herramienta.generate_keys(1024)  # clave de prueba: la real nunca se usa en las pruebas
N, D = int(KEY["n"]), int(KEY["d"])


def make(**changes):
    claims = {"v": 1, "licencia_id": "lic-1", "titular": "Tienda Prueba", "nit": "900", "plan": "esencial",
              "emitida": TODAY, "vence": None}
    claims.update(changes)
    return licencia.encode({k: v for k, v in claims.items() if v is not ...}, N, D)


class LicenseTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(licencia, "PUBLIC_N", N)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_signature_and_tampering(self):
        text = make()
        self.assertEqual(licencia.decode(text)["plan"], "esencial")
        self.assertEqual(licencia.decode("  " + text[:40] + "\n" + text[40:])["titular"], "Tienda Prueba")  # pegada con saltos
        prefix, data, signature = text.split(".")
        other = make(plan="negocio").split(".")[1]
        for bad in (f"{prefix}.{other}.{signature}", text[:-8], "INV2." + data + "." + signature, "hola", None):
            with self.assertRaises(ValueError, msg=str(bad)[:20]):
                licencia.decode(bad)
        with self.assertRaises(ValueError):
            licencia.decode(make(plan="evaluacion"))
        real_key = mock.patch.object(licencia, "PUBLIC_N", int(herramienta.generate_keys(1024)["n"]))
        with real_key, self.assertRaisesRegex(ValueError, "firma"):
            licencia.decode(text)  # firmada con otra clave

    def test_status_expiry_grace_and_installation(self):
        self.assertEqual(licencia.status(None, TODAY, "x")["plan"], "evaluacion")
        self.assertEqual(licencia.status("basura", TODAY, "x")["warning"][:18], "Licencia no válida")
        ok = licencia.status(make(), TODAY, "x")
        self.assertEqual((ok["plan"], ok["products"], ok["users"], ok["warning"]), ("esencial", 500, 2, None))
        custom = licencia.status(make(productos=50, usuarios=9), TODAY, "x")
        self.assertEqual((custom["products"], custom["users"]), (50, 9))
        soon = licencia.status(make(vence="2026-12-01"), TODAY, "x")
        self.assertIn("vence", soon["warning"])
        grace = licencia.status(make(vence="2026-11-15"), TODAY, "x")
        self.assertEqual(grace["plan"], "esencial")
        self.assertIn("Renuévela", grace["warning"])
        expired = licencia.status(make(vence="2026-10-01"), TODAY, "x")
        self.assertEqual(expired["plan"], "evaluacion")
        other = licencia.status(make(instalacion="otra"), TODAY, "mia")
        self.assertEqual((other["plan"], other["warning"]), ("evaluacion", "La licencia es de otra instalación."))
        self.assertEqual(licencia.status(make(instalacion="mia"), TODAY, "mia")["plan"], "esencial")

    def test_limits_block_only_new_additions(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        edge = Edge(Path(folder.name) / "e.sqlite3")
        self.addCleanup(edge.close)
        edge.limits = licencia.status(make(productos=2, usuarios=1), TODAY, edge.stream_id)
        edge.add_product("A", price=100, cost=50)
        edge.add_product("B", price=100, cost=50)
        with self.assertRaisesRegex(ValueError, "permite 2 productos"):
            edge.add_product("C", price=100, cost=50)
        # Vender, recibir, editar y desactivar siguen funcionando con el límite alcanzado.
        edge.move("r", "A", "receive", 5, "2026-11-21T10:00:00-05:00")
        edge.sell("t", [{"sku": "A", "quantity": 1}], "2026-11-21T10:05:00-05:00")
        edge.update_product("A", price=120)
        edge.update_product("B", active=False)
        edge.add_product("C", price=100, cost=50)  # hay cupo otra vez
        with self.assertRaisesRegex(ValueError, "permite 2"):
            edge.update_product("B", active=True)  # reactivar cuenta como alta
        edge.add_user("Ana", "dueno", "4826")
        with self.assertRaisesRegex(ValueError, "usuarios"):
            edge.add_user("Luis", "cajero", "5937")
        # La importación marca las filas que no caben en lugar de cortar a medias.
        edge.limits = licencia.status(make(productos=3), TODAY, edge.stream_id)  # activos: A y C
        plan = csvio.plan_import(edge, csvio.parse_products(b"sku,precio,costo\nA,1,1\nD,1,1\nE,1,1\n"))
        self.assertEqual([r["action"] for r in plan["rows"]], ["actualizar", "nuevo", "conflicto"])
        self.assertIn("límite del plan", plan["rows"][2]["error"])

    def test_vendor_tool_refuses_to_overwrite_key(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "clave.json"
        with mock.patch.object(herramienta, "KEY_PATH", path), mock.patch("builtins.print"):
            herramienta.main(["generar-claves"])
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            with self.assertRaises(SystemExit):
                herramienta.main(["generar-claves"])


if __name__ == "__main__":
    unittest.main()
