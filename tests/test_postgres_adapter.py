import unittest

from app import PostgresConnection


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner
        self.description = None

    def execute(self, sql, parameters=()):
        self.owner.statements.append((sql, parameters))
        self.description = [type("Column", (), {"name": "id"})()]

    def fetchone(self):
        return (41,)


class FakeRawConnection:
    def __init__(self):
        self.statements = []

    def cursor(self):
        return FakeCursor(self)


class PostgresAdapterTests(unittest.TestCase):
    def setUp(self):
        self.raw = FakeRawConnection()
        self.connection = PostgresConnection(self.raw)

    def test_insert_returns_generated_id_and_translates_placeholders(self):
        cursor = self.connection.execute(
            "INSERT INTO warehouses(name) VALUES (?)", ("Prueba",)
        )
        self.assertEqual(cursor.lastrowid, 41)
        self.assertIn("VALUES (%s) RETURNING id", self.raw.statements[0][0])
        self.assertEqual(self.raw.statements[0][1], ("Prueba",))

    def test_composite_key_insert_does_not_request_missing_id_column(self):
        cursor = self.connection.execute(
            "INSERT INTO user_warehouse_access(user_id, warehouse_id) VALUES (?, ?)",
            (1, 2),
        )
        self.assertIsNone(cursor.lastrowid)
        self.assertNotIn("RETURNING", self.raw.statements[0][0])

    def test_sqlite_ignore_syntax_is_converted(self):
        self.connection.execute("INSERT OR IGNORE INTO access_roles(name) VALUES (?)", ("Admin",))
        sql = self.raw.statements[0][0]
        self.assertTrue(sql.startswith("INSERT INTO access_roles"))
        self.assertIn("ON CONFLICT DO NOTHING", sql)


if __name__ == "__main__":
    unittest.main()

