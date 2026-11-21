import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from inventario.server import create_server, lan_addresses

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NOW = datetime.now(ZoneInfo("America/Bogota"))
TIME = NOW.isoformat(timespec="seconds")


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.server = create_server(Path(self.folder.name), port=0, host="0.0.0.0")
        self.port = self.server.app.port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.close()

    def call(self, method, path, body=None, *, host="127.0.0.1", headers=None, raw=None, address="127.0.0.1"):
        connection = http.client.HTTPConnection(address, self.port, timeout=5)
        self.addCleanup(connection.close)
        final = {"Host": f"{host}:{self.port}", "X-Inventario": "1"}
        final.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if payload is not None and "Content-Type" not in final:
            final["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers={k: v for k, v in final.items() if v is not None})
        response = connection.getresponse()
        data = response.read()
        kind = response.getheader("Content-Type") or ""
        return response.status, (json.loads(data) if "json" in kind else data), response


class ServerTests(ServerCase):
    def test_full_flow_sale_alerts_and_summary(self):
        status, product, _ = self.call("POST", "/api/v1/productos", {
            "sku": "ARROZ", "name": "Arroz", "price": 500000, "cost": 350000, "minimum": 5,
            "barcode": "770", "initial_stock": 8})
        self.assertEqual((status, product["stock"]), (201, 8))
        status, found, _ = self.call("GET", "/api/v1/buscar?codigo=770")
        self.assertEqual(found["sku"], "ARROZ")
        sale = {"ticket_id": "t-1", "occurred_at": TIME, "lines": [{"sku": "ARROZ", "quantity": 4}]}
        status, receipt, _ = self.call("POST", "/api/v1/ventas", sale)
        self.assertEqual((status, receipt["total_minor"], receipt["low_stock"]), (200, 2000000, ["ARROZ"]))
        self.assertEqual(self.call("POST", "/api/v1/ventas", sale)[1], receipt)  # reintento
        status, error, _ = self.call("POST", "/api/v1/ventas", {**sale, "ticket_id": "t-2",
                                                                 "lines": [{"sku": "ARROZ", "quantity": 99}]})
        self.assertEqual(status, 422)
        self.assertIn("stock insuficiente", error["detail"])
        status, alerts, _ = self.call("GET", "/api/v1/alertas")
        self.assertEqual(alerts[0]["type"], "minimo")
        status, summary, _ = self.call("GET", "/api/v1/resumen")
        self.assertEqual((summary["revenue_minor"], len(summary["by_day"])), (2000000, 7))
        status, detail, _ = self.call("GET", "/api/v1/productos/ARROZ")
        self.assertEqual(detail["product"]["stock"], 4)
        self.assertEqual(len(detail["movements"]), 2)
        status, board, _ = self.call("GET", "/api/v1/tablero")
        self.assertEqual(board["alert_count"], len(alerts))

    def test_dashboard_cache_sees_sales_from_the_writer_connection(self):
        self.call("POST", "/api/v1/productos", {"sku": "P", "price": 100, "cost": 50, "initial_stock": 5})
        before = self.call("GET", "/api/v1/tablero")[1]["today_summary"]["sold_units"]
        self.call("POST", "/api/v1/ventas", {"ticket_id": "c1", "lines": [{"sku": "P", "quantity": 2}]})
        after = self.call("GET", "/api/v1/tablero")[1]["today_summary"]["sold_units"]
        self.assertEqual(after - before, 2)
        self.assertEqual(self.call("GET", "/api/v1/productos/P")[1]["product"]["stock"], 3)

    def test_price_change_between_scan_and_payment_is_rejected(self):
        self.call("POST", "/api/v1/productos", {"sku": "CAFE", "price": 980000, "cost": 700000, "initial_stock": 5})
        self.call("PATCH", "/api/v1/productos/CAFE", {"price": 1250000})
        sale = {"ticket_id": "p1", "occurred_at": TIME, "lines": [{"sku": "CAFE", "quantity": 1}],
                "expected_total_minor": 980000}
        status, error, _ = self.call("POST", "/api/v1/ventas", sale)
        self.assertEqual((status, error["total_minor"], error["prices"]), (409, 1250000, {"CAFE": 1250000}))
        self.assertEqual(self.call("GET", "/api/v1/productos/CAFE")[1]["product"]["stock"], 5)  # nada registrado
        status, receipt, _ = self.call("POST", "/api/v1/ventas", {**sale, "expected_total_minor": 1250000})
        self.assertEqual((status, receipt["total_minor"]), (200, 1250000))

    def test_device_clock_far_off_is_rejected(self):
        self.call("POST", "/api/v1/productos", {"sku": "P", "price": 100, "cost": 50, "initial_stock": 5})
        for when in ("2019-01-01T10:00:00-05:00", (NOW + timedelta(hours=2)).isoformat(), "2026-01-01T10:00:00"):
            status, error, _ = self.call("POST", "/api/v1/ventas", {"ticket_id": when, "occurred_at": when,
                                                                    "lines": [{"sku": "P", "quantity": 1}]})
            self.assertEqual(status, 422, when)
        # Una venta guardada sin conexión hace dos días sí entra.
        earlier = (NOW - timedelta(days=2)).isoformat(timespec="seconds")
        self.assertEqual(self.call("POST", "/api/v1/ventas", {"ticket_id": "offline", "occurred_at": earlier,
                                                              "lines": [{"sku": "P", "quantity": 1}]})[0], 200)

    def test_top_sellers_and_template(self):
        for sku, sold in (("A", 1), ("B", 3)):
            self.call("POST", "/api/v1/productos", {"sku": sku, "price": 100, "cost": 50, "initial_stock": 9})
            self.call("POST", "/api/v1/ventas", {"ticket_id": sku, "lines": [{"sku": sku, "quantity": sold}]})
        self.assertEqual([p["sku"] for p in self.call("GET", "/api/v1/productos?top=1")[1]], ["B", "A"])
        status, body, _ = self.call("GET", "/api/v1/exportar/plantilla-productos.csv", headers={"X-Inventario": None})
        self.assertIn("sku;nombre", body.decode("utf-8"))

    def test_ticket_history_returns_and_voids_over_http(self):
        self.call("POST", "/api/v1/productos", {"sku": "A", "price": 1000, "cost": 600, "initial_stock": 10})
        self.call("POST", "/api/v1/productos", {"sku": "B", "price": 500, "cost": 300, "initial_stock": 10})
        self.call("POST", "/api/v1/ventas", {"ticket_id": "t1", "lines": [{"sku": "A", "quantity": 2},
                                                                           {"sku": "B", "quantity": 1}]})
        self.call("POST", "/api/v1/ventas", {"ticket_id": "t2", "lines": [{"sku": "B", "quantity": 3}]})
        status, tickets, _ = self.call("GET", "/api/v1/ventas")
        self.assertEqual([t["ticket_id"] for t in tickets], ["t2", "t1"])
        line_a = tickets[1]["lines"][0]
        self.assertEqual((line_a["sku"], line_a["quantity"], line_a["active"]), ("A", 2, 2))
        status, receipt, _ = self.call("POST", "/api/v1/devoluciones", {
            "command_id": "d1", "seq": line_a["seq"], "quantity": 1, "reason": "Llegó roto"})
        self.assertEqual((status, receipt["stock"], receipt["revenue_minor"]), (200, 9, -1000))
        self.assertEqual(self.call("POST", "/api/v1/devoluciones", {
            "command_id": "d2", "seq": line_a["seq"], "quantity": 5, "reason": "x"})[0], 422)
        self.assertEqual(self.call("POST", "/api/v1/devoluciones", {
            "command_id": "d3", "seq": line_a["seq"], "quantity": 1})[0], 422)  # sin motivo
        status, voided, _ = self.call("POST", "/api/v1/anulaciones/ticket", {
            "command_id": "a1", "ticket_id": "t2", "reason": "Cobrada dos veces"})
        self.assertEqual((status, voided["revenue_minor"]), (200, -1500))
        status, tickets, _ = self.call("GET", "/api/v1/ventas")
        states = {t["ticket_id"]: t["status"] for t in tickets}
        self.assertEqual(states, {"t1": "con_devoluciones", "t2": "anulado"})
        status, products, _ = self.call("GET", "/api/v1/productos/B")
        entry = next(m for m in products["movements"] if m["kind"] == "receive")
        status, receipt, _ = self.call("POST", "/api/v1/anulaciones", {
            "command_id": "a2", "seq": entry["seq"], "reason": "Entrada repetida"})
        self.assertEqual(status, 422)  # B ya vendió: quedaría negativo
        self.assertIn("negativo", receipt["detail"])
        summary = self.call("GET", "/api/v1/tablero")[1]["today_summary"]
        self.assertEqual((summary["sold_units"], summary["revenue_minor"]), (2, 1500))
        self.assertEqual(self.call("GET", "/api/v1/ventas?dia=no-es-fecha")[0], 422)

    def test_movements_counts_and_validation(self):
        self.call("POST", "/api/v1/productos", {"sku": "P", "price": 100, "cost": 50})
        status, receipt, _ = self.call("POST", "/api/v1/movimientos", {
            "command_id": "m1", "sku": "P", "kind": "receive", "quantity": 10, "note": "Proveedor X"})
        self.assertEqual((status, receipt["stock"]), (200, 10))
        self.assertEqual(self.call("POST", "/api/v1/movimientos", {
            "command_id": "m2", "sku": "P", "kind": "sale", "quantity": 1})[0], 422)
        status, count, _ = self.call("POST", "/api/v1/conteos", {"command_id": "c1", "sku": "P", "counted": 8})
        self.assertEqual((count["changed"], count["product"]["stock"]), (True, 8))
        self.assertEqual(self.call("POST", "/api/v1/productos", {"sku": "Q"})[0], 422)
        self.assertEqual(self.call("GET", "/api/v1/productos/NO")[0], 422)
        self.assertEqual(self.call("POST", "/api/v1/ventas", raw=b"{no json")[0], 400)
        self.assertEqual(self.call("GET", "/api/v1/nada")[0], 404)
        status, patched, _ = self.call("PATCH", "/api/v1/productos/P", {"price": 120, "active": False})
        self.assertEqual((patched["price"], patched["active"]), (120, 0))

    def test_csrf_and_dns_rebinding_protection(self):
        self.assertEqual(self.call("GET", "/api/v1/estado", headers={"X-Inventario": None})[0], 400)
        self.assertEqual(self.call("GET", "/api/v1/estado", host="malicioso.example")[0], 403)
        self.assertEqual(self.call("GET", "/", host="malicioso.example")[0], 401)
        # Las descargas son enlaces normales: sin cabecera, pero con el mismo control de Host.
        self.assertEqual(self.call("GET", "/api/v1/exportar/productos.csv", headers={"X-Inventario": None})[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/exportar/productos.csv", host="malicioso.example")[0], 403)

    def test_static_files_and_security_headers(self):
        status, body, response = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", body)
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(self.call("GET", "/../server.py")[0], 404)
        self.assertEqual(self.call("GET", "/%2e%2e/server.py")[0], 404)
        self.assertEqual(self.call("GET", "/app.js")[0], 200)

    def test_import_preview_apply_and_exports(self):
        csv = "sku;nombre;precio;costo;stock_inicial\nA;Arroz;5.000;3.500;10\nB;Malo;x;1;0\n".encode()
        status, plan, _ = self.call("POST", "/api/v1/importar", raw=csv, headers={"Content-Type": "text/csv"})
        self.assertEqual((status, plan["new"], len(plan["errors"])), (200, 1, 1))
        self.assertEqual(self.call("POST", "/api/v1/importar?aplicar=1&token=otro")[0], 409)
        status, result, _ = self.call("POST", f"/api/v1/importar?aplicar=1&token={plan['token']}")
        self.assertEqual(result["created"], 1)
        status, body, response = self.call("GET", "/api/v1/exportar/productos.csv")
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        self.assertIn("Arroz", body.decode("utf-8"))

    def test_backups_daily_and_on_demand(self):
        status, files, _ = self.call("GET", "/api/v1/respaldo")
        self.assertEqual(len(files), 1)  # respaldo diario al arrancar
        status, created, _ = self.call("POST", "/api/v1/respaldo")
        status, data, _ = self.call("GET", f"/api/v1/respaldo/{created['file']}")
        self.assertTrue(data.startswith(b"SQLite format 3"))
        self.assertEqual(self.call("GET", "/api/v1/respaldo/..%2Finventario.sqlite3")[0], 404)

    def test_settings_show_key_only_locally(self):
        status, settings, _ = self.call("GET", "/api/v1/ajustes")
        self.assertTrue(settings["access_key"])
        self.assertFalse(settings["remote_enabled"])
        status, changed, _ = self.call("PATCH", "/api/v1/ajustes", {"store_name": "Tienda Don José"})
        self.assertEqual(changed["store_name"], "Tienda Don José")
        self.assertEqual(self.call("PATCH", "/api/v1/ajustes", {"store_name": ""})[0], 422)


@unittest.skipUnless(lan_addresses(), "sin dirección de red local para simular otro dispositivo")
class RemoteAccessTests(ServerCase):
    """Un cliente que entra por la IP de la red local cuenta como otro dispositivo."""

    def remote(self, method, path, body=None, **kwargs):
        ip = lan_addresses()[0]
        return self.call(method, path, body, host=ip, address=ip, **kwargs)

    def test_remote_requires_enabled_access_and_key(self):
        key = self.call("GET", "/api/v1/ajustes")[1]["access_key"]
        self.assertEqual(self.remote("GET", "/api/v1/estado", headers={"X-Clave": key})[0], 401)
        self.call("PATCH", "/api/v1/ajustes", {"remote_enabled": True})
        self.assertEqual(self.remote("GET", "/api/v1/estado")[0], 401)
        self.assertEqual(self.remote("GET", "/api/v1/estado", headers={"X-Clave": "mala"})[0], 401)
        status, state, _ = self.remote("GET", "/api/v1/estado", headers={"X-Clave": key})
        self.assertEqual((status, state["local"]), (200, False))
        # Otro dispositivo no ve la clave ni puede cambiar el acceso remoto.
        status, settings, _ = self.remote("GET", "/api/v1/ajustes", headers={"X-Clave": key})
        self.assertNotIn("access_key", settings)
        self.assertEqual(self.remote("PATCH", "/api/v1/ajustes", {"remote_enabled": False},
                                     headers={"X-Clave": key})[0], 403)
        # El enlace con ?clave= deja una cookie y redirige a / sin la clave en la URL.
        status, _, response = self.remote("GET", f"/?clave={key}")
        self.assertEqual((status, response.getheader("Location")), (303, "/"))
        cookie = response.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.assertEqual(self.remote("GET", "/", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.remote("GET", "/api/v1/tablero", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.remote("GET", "/?clave=mala")[0], 401)
        # Cambiar la clave expulsa a los dispositivos con la anterior.
        self.call("PATCH", "/api/v1/ajustes", {"new_key": True})
        self.assertEqual(self.remote("GET", "/api/v1/tablero", headers={"Cookie": cookie})[0], 401)


if __name__ == "__main__":
    unittest.main()
