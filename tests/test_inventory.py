import http.cookiejar
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import (
    AppError,
    authenticate,
    create_account,
    create_bootstrap_admin,
    connect,
    create_employee,
    create_warehouse,
    create_subwarehouse,
    create_item,
    initialize_database,
    permissions_for,
    receive_stock,
    require_permission,
    actor_warehouses,
    RequestHandler,
    session_actor,
    update_account_access,
    snapshot,
    transfer_stock,
)


class InventoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        initialize_database(self.db_path)
        self.tech = create_employee({
            "code": "TEC-001",
            "first_name": "Ana",
            "last_name": "Prueba",
            "role": "Técnico",
        }, self.db_path)
        self.material = create_item({
            "code": "MAT-001",
            "name": "Conector de prueba",
            "item_type": "Material",
            "unit": "unidad",
            "serial_control": False,
        }, self.db_path)
        self.equipment = create_item({
            "code": "EQ-001",
            "name": "Módem de prueba",
            "item_type": "Equipo",
            "unit": "unidad",
            "serial_control": True,
        }, self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_receipt_and_transfer_track_quantity_and_serial_location(self):
        receive_stock({
            "document": "Guía: G-001",
            "responsible": "Usuario de prueba",
            "lines": [
                {"item_id": self.material["id"], "quantity": 12, "serials": []},
                {"item_id": self.equipment["id"], "quantity": 2, "serials": ["SN-100", "SN-101"]},
            ],
        }, self.db_path)

        transfer_stock({
            "employee_id": self.tech["id"],
            "document": "Vale V-001",
            "responsible": "Almacenero de prueba",
            "lines": [
                {"item_id": self.material["id"], "quantity": 5, "serials": []},
                {"item_id": self.equipment["id"], "quantity": 1, "serials": ["SN-100"]},
            ],
        }, self.db_path)

        result = snapshot(self.db_path)
        material_stock = {(row["location_kind"], row["location_name"]): row["quantity"]
                          for row in result["inventory"] if row["item_id"] == self.material["id"]}
        self.assertEqual(material_stock[("central", "General")], 7)
        self.assertEqual(material_stock[("technician", "ALMACÉN TÉCNICO - Ana Prueba")], 5)

        equipment_stock = {row["location_kind"]: row for row in result["inventory"]
                           if row["item_id"] == self.equipment["id"]}
        self.assertEqual(equipment_stock["central"]["serials"], ["SN-101"])
        self.assertEqual(equipment_stock["technician"]["serials"], ["SN-100"])
        self.assertEqual(len(result["movements"]), 2)

    def test_overdraw_rolls_back_entire_multi_item_transfer(self):
        receive_stock({
            "document": "Guía: G-002",
            "responsible": "Usuario de prueba",
            "lines": [{"item_id": self.material["id"], "quantity": 4, "serials": []}],
        }, self.db_path)

        with self.assertRaisesRegex(AppError, "Stock insuficiente"):
            transfer_stock({
                "employee_id": self.tech["id"],
                "document": "Vale V-002",
                "responsible": "Almacenero de prueba",
                "lines": [
                    {"item_id": self.material["id"], "quantity": 2, "serials": []},
                    {"item_id": self.equipment["id"], "quantity": 1, "serials": ["SN-NO-EXISTE"]},
                ],
            }, self.db_path)

        result = snapshot(self.db_path)
        self.assertEqual(len(result["movements"]), 1)
        self.assertEqual(len([row for row in result["inventory"] if row["location_kind"] == "technician"]), 0)
        central_material = next(row for row in result["inventory"] if row["item_id"] == self.material["id"])
        self.assertEqual(central_material["quantity"], 4)

    def test_duplicate_serial_is_rejected_without_partial_receipt(self):
        with self.assertRaisesRegex(AppError, "serie repetidos"):
            receive_stock({
                "document": "Guía: G-003",
                "responsible": "Usuario de prueba",
                "lines": [{"item_id": self.equipment["id"], "quantity": 2, "serials": ["SN-200", "SN-200"]}],
            }, self.db_path)

        result = snapshot(self.db_path)
        self.assertEqual(result["movements"], [])
        self.assertEqual(result["inventory"], [])

    def test_non_technician_cannot_receive_technician_transfer(self):
        staff = create_employee({
            "code": "ALM-001",
            "first_name": "Luis",
            "last_name": "Prueba",
            "role": "Almacenero",
        }, self.db_path)
        with self.assertRaisesRegex(AppError, "técnico activo"):
            transfer_stock({
                "employee_id": staff["id"],
                "document": "Vale V-004",
                "lines": [{"item_id": self.material["id"], "quantity": 1, "serials": []}],
            }, self.db_path)

    def test_warehouses_have_isolated_stock_and_technicians(self):
        second = create_warehouse({"name": "Centro Lima"}, self.db_path)
        tech_two = create_employee({"code": "TEC-002", "first_name": "Marco", "last_name": "Prueba", "role": "Técnico", "warehouse_id": second["id"]}, self.db_path)
        extra = create_subwarehouse({"name": "Mantenimiento", "warehouse_id": second["id"]}, self.db_path)
        receive_stock({"document": "Guía: G-SECOND", "warehouse_id": second["id"], "lines": [{"item_id": self.material["id"], "quantity": 6, "serials": []}]}, self.db_path)
        transfer_stock({"employee_id": tech_two["id"], "warehouse_id": second["id"], "document": "Vale V-SECOND", "lines": [{"item_id": self.material["id"], "quantity": 2, "serials": []}]}, self.db_path)
        result = snapshot(self.db_path)
        stock = {(row["warehouse_id"], row["location_kind"]): row["quantity"] for row in result["inventory"] if row["item_id"] == self.material["id"]}
        self.assertNotIn((1, "central"), stock)
        self.assertEqual(stock[(second["id"], "central")], 4)
        self.assertEqual(stock[(second["id"], "technician")], 2)
        self.assertEqual(extra["warehouse_id"], second["id"])
        self.assertEqual(tech_two["warehouse_id"], second["id"])
        self.assertEqual({row["warehouse_id"] for row in result["inventory"]}, {second["id"]})

    def test_legacy_database_gets_first_warehouse_without_losing_location(self):
        legacy = self.db_path.parent / "legacy.sqlite3"
        connection = connect(legacy)
        connection.execute("CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, employee_id INTEGER, created_at TEXT NOT NULL)")
        connection.execute("INSERT INTO locations(name, kind, employee_id, created_at) VALUES ('Almacen central', 'central', NULL, '2025-01-01')")
        connection.commit()
        connection.close()
        initialize_database(legacy)
        connection = connect(legacy)
        try:
            warehouse = connection.execute("SELECT name FROM warehouses").fetchone()
            location = connection.execute("SELECT name, warehouse_id FROM locations WHERE name = 'Almacen central'").fetchone()
            self.assertEqual(warehouse["name"], "Primera prueba")
            self.assertEqual(location["name"], "Almacen central")
            self.assertIsNotNone(location["warehouse_id"])
        finally:
            connection.close()

    def test_login_sessions_and_admin_per_warehouse_permission_overrides(self):
        second = create_warehouse({"name": "Manchay"}, self.db_path)
        worker = create_employee({"code": "ALM-009", "first_name": "Rosa", "last_name": "Manchay", "role": "Almacenero", "warehouse_id": second["id"]}, self.db_path)
        create_bootstrap_admin("admin", "ClaveSegura-Temporal-2026", self.db_path)
        account = create_account({
            "employee_id": worker["id"], "username": "rosa", "password": "Almacen-Manchay-2026!",
            "role": "Almacenero", "access": [{"warehouse_id": second["id"], "permissions": ["view_inventory"]}],
        }, self.db_path)
        actor, token = authenticate("rosa", "Almacen-Manchay-2026!", self.db_path)
        self.assertEqual(session_actor(token, self.db_path)["id"], account["id"])
        connection = connect(self.db_path)
        try:
            self.assertEqual(actor_warehouses(connection, actor), {second["id"]})
            self.assertEqual(permissions_for(connection, actor["id"], actor["role_id"], second["id"]), {"view_inventory"})
            with self.assertRaisesRegex(AppError, "No tienes permiso"):
                require_permission(connection, actor, second["id"], "receive_stock")
            with self.assertRaisesRegex(AppError, "No tienes acceso"):
                require_permission(connection, actor, 1, "view_inventory")
        finally:
            connection.close()
        update_account_access(account["id"], [{"warehouse_id": second["id"], "permissions": ["view_inventory", "receive_stock"]}], self.db_path)
        actor, _ = authenticate("rosa", "Almacen-Manchay-2026!", self.db_path)
        connection = connect(self.db_path)
        try:
            require_permission(connection, actor, second["id"], "receive_stock")
        finally:
            connection.close()
        with self.assertRaisesRegex(AppError, "Ya existe una cuenta administradora"):
            create_bootstrap_admin("otro-admin", "ClaveSegura-Temporal-2026", self.db_path)

    def test_http_api_requires_session_and_enforces_warehouse_permissions(self):
        import app

        second = create_warehouse({"name": "Manchay"}, self.db_path)
        worker = create_employee({"code": "ALM-010", "first_name": "Leo", "last_name": "Local", "role": "Almacenero", "warehouse_id": second["id"]}, self.db_path)
        create_account({"employee_id": worker["id"], "username": "leo", "password": "Manchay-Acceso-2026!", "role": "Almacenero", "access": [{"warehouse_id": second["id"], "permissions": ["view_inventory"]}]}, self.db_path)

        class TestHandler(RequestHandler):
            database_path = self.db_path

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as unauthenticated:
                opener.open(base + "/api/snapshot")
            self.assertEqual(unauthenticated.exception.code, 401)
            unauthenticated.exception.close()

            cross_origin = Request(base + "/api/auth/login", data=b"{}", headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"}, method="POST")
            with self.assertRaises(HTTPError) as csrf_block:
                opener.open(cross_origin)
            self.assertEqual(csrf_block.exception.code, 403)
            csrf_block.exception.close()

            login = Request(base + "/api/auth/login", data=json.dumps({"username": "leo", "password": "Manchay-Acceso-2026!"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with opener.open(login) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("HttpOnly", response.headers.get("Set-Cookie", ""))

            with opener.open(base + "/api/snapshot") as response:
                data = json.load(response)
            self.assertEqual([warehouse["id"] for warehouse in data["warehouses"]], [second["id"]])
            self.assertEqual(data["inventory"], [])

            location = next(location for location in data["locations"] if location["warehouse_id"] == second["id"])
            body = {"warehouse_id": second["id"], "location_id": location["id"], "document": "Guía: DENY", "lines": [{"item_id": self.material["id"], "quantity": 1, "serials": []}]}
            denied = Request(base + "/api/receipts", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(HTTPError) as forbidden:
                opener.open(denied)
            self.assertEqual(forbidden.exception.code, 403)
            forbidden.exception.close()

            logout = Request(base + "/api/auth/logout", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with opener.open(logout) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as logged_out:
                opener.open(base + "/api/snapshot")
            self.assertEqual(logged_out.exception.code, 401)
            logged_out.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_login_failure_is_rate_limited(self):
        create_bootstrap_admin("admin", "ClaveSegura-Temporal-2026", self.db_path)
        for _ in range(5):
            with self.assertRaisesRegex(AppError, "incorrectos"):
                authenticate("admin", "password-incorrecto", self.db_path)
        with self.assertRaisesRegex(AppError, "Demasiados intentos"):
            authenticate("admin", "ClaveSegura-Temporal-2026", self.db_path)

    def test_duplicate_employee_code_is_rejected(self):
        with self.assertRaisesRegex(AppError, "Ya existe un trabajador"):
            create_employee({
                "code": "tec-001",
                "first_name": "Otra",
                "last_name": "Persona",
                "role": "Técnico",
            }, self.db_path)
        connection = connect(self.db_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0], 1)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

