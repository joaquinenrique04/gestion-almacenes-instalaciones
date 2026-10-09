"""Local development prototype for warehouse and field operations.

This app binds to localhost and uses a local SQLite database. It includes local
account authentication and server-enforced warehouse permissions, but is not a
production deployment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from getpass import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATABASE_URL = os.environ.get("GESTION_DATABASE_URL", os.environ.get("TELECOM_DATABASE_URL", "")).strip()
DB_PATH = Path(os.environ.get("GESTION_DB_PATH", os.environ.get("TELECOM_DB_PATH", BASE_DIR / "instance" / "gestion.sqlite3")))
DEFAULT_WAREHOUSE_NAME = "Primera prueba"
DEFAULT_LOCATION_NAME = "General"
SESSION_COOKIE = "gestion_session"
SESSION_HOURS = 12
PERMISSIONS = {
    "view_inventory": "Consultar inventario",
    "view_movements": "Consultar movimientos",
    "receive_stock": "Registrar ingresos",
    "transfer_stock": "Transferir a técnicos",
    "manage_people": "Gestionar personal y técnicos",
    "manage_items": "Gestionar artículos",
    "manage_warehouses": "Gestionar almacenes",
}
ROLE_DEFAULTS = {
    "Administrador": set(PERMISSIONS),
    "Almacenero": {"view_inventory", "view_movements", "receive_stock", "transfer_stock"},
    "Supervisor": {"view_inventory", "view_movements"},
    "Técnico": {"view_inventory"},
}
MAX_BODY_BYTES = 1_000_000
EMPLOYEE_ROLES = {"Técnico", "Almacenero", "Supervisor", "Otro"}
ITEM_TYPES = {"Material", "Equipo"}


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PostgresConnection:
    """Small adapter that keeps the application's SQLite-style DB API."""

    def __init__(self, connection):
        self.raw = connection
        self.last_inserted_id = None

    def execute(self, sql: str, parameters=()):
        import re

        ignore_conflict = "INSERT OR IGNORE INTO" in sql.upper()
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        sql = sql.replace("COLLATE NOCASE", "")
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        sql = re.sub(r"\bAUTOINCREMENT\b", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bINTEGER PRIMARY KEY\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.IGNORECASE)
        sql = sql.replace("?", "%s")
        if ignore_conflict:
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        cursor = self.raw.cursor()
        match = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.IGNORECASE)
        tables_with_ids = {"warehouses", "employees", "locations", "items", "movements", "movement_lines",
                           "serial_units", "movement_serials", "access_roles", "user_accounts", "auth_sessions"}
        returning_id = bool(match and match.group(1).lower() in tables_with_ids) and " RETURNING " not in sql.upper()
        if returning_id:
            sql += " RETURNING id"
        cursor.execute(sql, parameters)
        if returning_id:
            row = cursor.fetchone()
            self.last_inserted_id = row[0] if row else None
            cursor = self.raw.cursor()
        else:
            self.last_inserted_id = None
        return PostgresCursor(cursor, self)

    def executescript(self, sql: str) -> None:
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __iter__(self):
        return iter(PostgresCursor(self.raw.cursor(), self))


class PostgresCursor:
    def __init__(self, cursor, connection=None):
        self.raw = cursor
        self.connection = connection

    @property
    def lastrowid(self):
        return self.connection.last_inserted_id if self.connection else None

    def fetchone(self):
        row = self.raw.fetchone()
        return PostgresRow(row, self.raw.description) if row is not None else None

    def fetchall(self):
        return [PostgresRow(row, self.raw.description) for row in self.raw.fetchall()]

    def __iter__(self):
        for row in self.raw:
            yield PostgresRow(row, self.raw.description)


class PostgresRow(dict):
    def __init__(self, row, description):
        super().__init__(zip((column.name for column in description), row))
        self._values = tuple(row)

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else super().__getitem__(key)


def connect(db_path: Path = DB_PATH):
    if DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import tuple_row
        except ImportError as error:
            raise RuntimeError("Para GESTION_DATABASE_URL instala las dependencias con: .venv\\Scripts\\python.exe -m pip install -r requirements.txt") from error
        return PostgresConnection(psycopg.connect(DATABASE_URL, row_factory=tuple_row, connect_timeout=10, sslmode="require"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(db_path: Path = DB_PATH) -> None:
    if DATABASE_URL:
        raise RuntimeError("Para preparar PostgreSQL, aplica las migraciones de supabase/migrations/ antes de iniciar.")
    with closing(connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS warehouses (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('Técnico', 'Almacenero', 'Supervisor', 'Otro')),
                warehouse_id INTEGER REFERENCES warehouses(id),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('central', 'technician')),
                warehouse_id INTEGER REFERENCES warehouses(id),
                employee_id INTEGER UNIQUE REFERENCES employees(id),
                created_at TEXT NOT NULL,
                CHECK ((kind = 'central' AND employee_id IS NULL) OR
                       (kind = 'technician' AND employee_id IS NOT NULL))
            );

            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                item_type TEXT NOT NULL CHECK (item_type IN ('Material', 'Equipo')),
                unit TEXT NOT NULL,
                serial_control INTEGER NOT NULL DEFAULT 0 CHECK (serial_control IN (0, 1)),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('Ingreso', 'Transferencia')),
                document TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                responsible TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS movement_lines (
                id INTEGER PRIMARY KEY,
                movement_id INTEGER NOT NULL REFERENCES movements(id),
                item_id INTEGER NOT NULL REFERENCES items(id),
                source_location_id INTEGER REFERENCES locations(id),
                destination_location_id INTEGER NOT NULL REFERENCES locations(id),
                quantity REAL NOT NULL CHECK (quantity > 0)
            );

            CREATE TABLE IF NOT EXISTS serial_units (
                id INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES items(id),
                serial_number TEXT NOT NULL COLLATE NOCASE UNIQUE,
                current_location_id INTEGER NOT NULL REFERENCES locations(id),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS movement_serials (
                id INTEGER PRIMARY KEY,
                movement_line_id INTEGER NOT NULL REFERENCES movement_lines(id),
                serial_unit_id INTEGER NOT NULL REFERENCES serial_units(id),
                source_location_id INTEGER REFERENCES locations(id),
                destination_location_id INTEGER NOT NULL REFERENCES locations(id),
                UNIQUE (movement_line_id, serial_unit_id)
            );

            CREATE TABLE IF NOT EXISTS access_roles (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                is_system INTEGER NOT NULL DEFAULT 1 CHECK (is_system IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS user_accounts (
                id INTEGER PRIMARY KEY,
                employee_id INTEGER UNIQUE REFERENCES employees(id),
                role_id INTEGER NOT NULL REFERENCES access_roles(id),
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_warehouse_access (
                user_id INTEGER NOT NULL REFERENCES user_accounts(id) ON DELETE CASCADE,
                warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
                PRIMARY KEY (user_id, warehouse_id)
            );

            CREATE TABLE IF NOT EXISTS user_warehouse_permissions (
                user_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                permission TEXT NOT NULL,
                effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
                PRIMARY KEY (user_id, warehouse_id, permission),
                FOREIGN KEY (user_id, warehouse_id) REFERENCES user_warehouse_access(user_id, warehouse_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS login_attempts (
                username_key TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                window_started_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES user_accounts(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_movement_lines_item ON movement_lines(item_id);
            CREATE INDEX IF NOT EXISTS idx_serial_units_location ON serial_units(current_location_id);
            CREATE INDEX IF NOT EXISTS idx_movements_created ON movements(created_at DESC);
            """
        )
        for role_name in ROLE_DEFAULTS:
            connection.execute("INSERT OR IGNORE INTO access_roles(name) VALUES (?)", (role_name,))
        connection.commit()
        employee_columns = {row["name"] for row in connection.execute("PRAGMA table_info(employees)")}
        if "warehouse_id" not in employee_columns:
            connection.execute("ALTER TABLE employees ADD COLUMN warehouse_id INTEGER REFERENCES warehouses(id)")
        location_columns = {row["name"] for row in connection.execute("PRAGMA table_info(locations)")}
        if "warehouse_id" not in location_columns:
            connection.execute("ALTER TABLE locations ADD COLUMN warehouse_id INTEGER REFERENCES warehouses(id)")

        first_warehouse = connection.execute("SELECT id FROM warehouses ORDER BY id LIMIT 1").fetchone()
        if first_warehouse is None:
            cursor = connection.execute(
                "INSERT INTO warehouses(name, active, created_at) VALUES (?, 1, ?)",
                (DEFAULT_WAREHOUSE_NAME, now_iso()),
            )
            first_warehouse_id = cursor.lastrowid
        else:
            first_warehouse_id = first_warehouse["id"]
        connection.execute(
            "UPDATE locations SET warehouse_id = ? WHERE warehouse_id IS NULL",
            (first_warehouse_id,),
        )
        connection.execute("UPDATE employees SET warehouse_id = ? WHERE warehouse_id IS NULL", (first_warehouse_id,))
        for warehouse in connection.execute("SELECT id FROM warehouses WHERE active = 1"):
            default_location = connection.execute(
                "SELECT id FROM locations WHERE warehouse_id = ? AND kind = 'central' ORDER BY id LIMIT 1",
                (warehouse["id"],),
            ).fetchone()
            if default_location is None:
                connection.execute(
                    "INSERT INTO locations(name, kind, warehouse_id, employee_id, created_at) VALUES (?, 'central', ?, NULL, ?)",
                    (DEFAULT_LOCATION_NAME, warehouse["id"], now_iso()),
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_location_name ON locations(warehouse_id, name) WHERE kind = 'central'"
        )
        connection.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: object, label: str, max_length: int = 160, required: bool = True) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        result = str(value).strip()
    if required and not result:
        raise AppError(f"Completa el campo {label}.")
    if len(result) > max_length:
        raise AppError(f"El campo {label} supera {max_length} caracteres.")
    return result


def positive_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AppError(f"Ingresa una cantidad válida para {label}.") from None
    if number <= 0 or number > 1_000_000_000:
        raise AppError(f"La cantidad de {label} debe ser mayor que cero.")
    return number


def serial_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = value.replace("\r", "\n").replace(",", "\n").split("\n")
    elif isinstance(value, list):
        raw_values = value
    else:
        raise AppError("La lista de series no tiene un formato válido.")
    values = [clean_text(item, "serie", 120) for item in raw_values if str(item).strip()]
    normalized = [item.casefold() for item in values]
    if len(normalized) != len(set(normalized)):
        raise AppError("Hay números de serie repetidos en el movimiento.")
    return values


def begin(connection: sqlite3.Connection) -> None:
    if isinstance(connection, PostgresConnection):
        # psycopg opens a transaction automatically on the first statement.
        return
    connection.execute("BEGIN IMMEDIATE")


def get_warehouse(connection: sqlite3.Connection, warehouse_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM warehouses WHERE id = ? AND active = 1", (warehouse_id,)
    ).fetchone()
    if row is None:
        raise AppError("No se encontró el almacén activo indicado.", 404)
    return row


def get_default_stock_location(connection: sqlite3.Connection, warehouse_id: int) -> sqlite3.Row:
    location = connection.execute(
        "SELECT * FROM locations WHERE warehouse_id = ? AND kind = 'central' ORDER BY id LIMIT 1",
        (warehouse_id,),
    ).fetchone()
    if location is None:
        raise AppError("El almacén no tiene subalmacenes disponibles.", 409)
    return location


def first_warehouse_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT id FROM warehouses WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise AppError("Crea un almacén primero.", 409)
    return int(row["id"])


def password_hash(password: str) -> str:
    if len(password) < 12:
        raise AppError("La contraseña debe tener al menos 12 caracteres.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def role_permissions(connection: sqlite3.Connection, role_id: int) -> set[str]:
    role = connection.execute("SELECT name FROM access_roles WHERE id = ?", (role_id,)).fetchone()
    if role is None:
        return set()
    return set(ROLE_DEFAULTS.get(role["name"], set()))


def permissions_for(connection: sqlite3.Connection, user_id: int, role_id: int, warehouse_id: int) -> set[str]:
    role = connection.execute("SELECT name FROM access_roles WHERE id = ?", (role_id,)).fetchone()
    if role and role["name"] == "Administrador":
        return set(PERMISSIONS)
    permissions = role_permissions(connection, role_id)
    overrides = connection.execute(
        "SELECT permission, effect FROM user_warehouse_permissions WHERE user_id = ? AND warehouse_id = ?",
        (user_id, warehouse_id),
    )
    for row in overrides:
        if row["effect"] == "allow":
            permissions.add(row["permission"])
        else:
            permissions.discard(row["permission"])
    return permissions


def actor_warehouses(connection: sqlite3.Connection, actor: dict) -> set[int]:
    if actor.get("is_admin"):
        return {int(row["id"]) for row in connection.execute("SELECT id FROM warehouses WHERE active = 1")}
    return {int(row["warehouse_id"]) for row in connection.execute(
        "SELECT uwa.warehouse_id FROM user_warehouse_access uwa JOIN warehouses w ON w.id = uwa.warehouse_id WHERE uwa.user_id = ? AND w.active = 1",
        (actor["id"],),
    )}


def require_admin(connection: sqlite3.Connection, actor: dict | None) -> None:
    if actor is not None and not actor.get("is_admin"):
        raise AppError("Solo un administrador puede realizar esta acción.", 403)


def require_permission(connection: sqlite3.Connection, actor: dict | None, warehouse_id: int, permission: str) -> None:
    if actor is None:
        return
    allowed_warehouses = actor_warehouses(connection, actor)
    if warehouse_id not in allowed_warehouses:
        raise AppError("No tienes acceso a ese almacén.", 403)
    grants = permissions_for(connection, actor["id"], actor["role_id"], warehouse_id)
    if permission not in grants:
        raise AppError("No tienes permiso para realizar esta acción en este almacén.", 403)


def create_account(payload: dict, db_path: Path = DB_PATH) -> dict:
    username = clean_text(payload.get("username"), "usuario", 80)
    password = payload.get("password")
    if not isinstance(password, str):
        raise AppError("Ingresa una contraseña.")
    encoded = password_hash(password)
    try:
        role_name = clean_text(payload.get("role"), "rol", 40)
        employee_id = int(payload["employee_id"]) if payload.get("employee_id") else None
    except (TypeError, ValueError, KeyError):
        raise AppError("Selecciona un rol y un trabajador válido.") from None
    connection = connect(db_path)
    try:
        begin(connection)
        role = connection.execute("SELECT id, name FROM access_roles WHERE lower(name) = lower(?)", (role_name,)).fetchone()
        if role is None:
            raise AppError("Selecciona un rol válido.")
        if role["name"] != "Administrador" and employee_id is None:
            raise AppError("Asocia la cuenta a un trabajador.")
        employee = connection.execute("SELECT id, warehouse_id FROM employees WHERE id = ? AND active = 1", (employee_id,)).fetchone() if employee_id is not None else None
        if employee_id is not None and employee is None:
            raise AppError("El trabajador no existe o está inactivo.")
        if role["name"] != "Administrador":
            requested_access = payload.get("access", [])
            if not any(isinstance(grant, dict) and int(grant.get("warehouse_id", 0)) == employee["warehouse_id"] for grant in requested_access):
                raise AppError("Incluye el almacén asignado al trabajador.")
        cursor = connection.execute(
            "INSERT INTO user_accounts(employee_id, role_id, username, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (employee_id, role["id"], username, encoded, now_iso()),
        )
        user_id = int(cursor.lastrowid)
        if role["name"] != "Administrador":
            grants = payload.get("access", [])
            if not isinstance(grants, list) or not grants:
                raise AppError("Asigna al menos un almacén.")
            for grant in grants:
                if not isinstance(grant, dict):
                    raise AppError("La asignación de almacén no es válida.")
                warehouse_id = int(grant.get("warehouse_id"))
                get_warehouse(connection, warehouse_id)
                connection.execute("INSERT INTO user_warehouse_access(user_id, warehouse_id) VALUES (?, ?)", (user_id, warehouse_id))
                granted = set(grant.get("permissions", []))
                if not granted.issubset(PERMISSIONS):
                    raise AppError("La asignación contiene permisos desconocidos.")
                defaults = ROLE_DEFAULTS.get(role["name"], set())
                for permission in PERMISSIONS:
                    if permission not in defaults or permission not in granted:
                        effect = "allow" if permission in granted else "deny"
                        connection.execute(
                            "INSERT INTO user_warehouse_permissions(user_id, warehouse_id, permission, effect) VALUES (?, ?, ?, ?)",
                            (user_id, warehouse_id, permission, effect),
                        )
        connection.commit()
        return {"id": user_id, "username": username, "role": role["name"], "employee_id": employee_id}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "user_accounts.username" in str(error):
            raise AppError("Ese nombre de usuario ya está registrado.", 409) from None
        if "user_accounts.employee_id" in str(error):
            raise AppError("Ese trabajador ya tiene una cuenta.", 409) from None
        raise AppError("No se pudo crear la cuenta.", 409) from None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_account_access(account_id: int, access: list, db_path: Path = DB_PATH) -> None:
    connection = connect(db_path)
    try:
        begin(connection)
        account = connection.execute(
            "SELECT ua.id, ua.role_id, ar.name AS role FROM user_accounts ua JOIN access_roles ar ON ar.id = ua.role_id WHERE ua.id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise AppError("No se encontró la cuenta.", 404)
        if account["role"] == "Administrador":
            raise AppError("El administrador siempre conserva acceso completo.", 409)
        if not isinstance(access, list) or not access:
            raise AppError("Asigna al menos un almacén.")
        connection.execute("DELETE FROM user_warehouse_access WHERE user_id = ?", (account_id,))
        defaults = ROLE_DEFAULTS.get(account["role"], set())
        for grant in access:
            if not isinstance(grant, dict):
                raise AppError("La asignación de almacén no es válida.")
            warehouse_id = int(grant.get("warehouse_id"))
            get_warehouse(connection, warehouse_id)
            connection.execute("INSERT INTO user_warehouse_access(user_id, warehouse_id) VALUES (?, ?)", (account_id, warehouse_id))
            granted = set(grant.get("permissions", []))
            if not granted.issubset(PERMISSIONS):
                raise AppError("La asignación contiene permisos desconocidos.")
            for permission in PERMISSIONS:
                if permission not in defaults or permission not in granted:
                    effect = "allow" if permission in granted else "deny"
                    connection.execute("INSERT INTO user_warehouse_permissions(user_id, warehouse_id, permission, effect) VALUES (?, ?, ?, ?)",
                                       (account_id, warehouse_id, permission, effect))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_bootstrap_admin(username: str, password: str, db_path: Path = DB_PATH) -> None:
    username = clean_text(username, "usuario", 80)
    connection = connect(db_path)
    try:
        begin(connection)
        if connection.execute("SELECT 1 FROM user_accounts ua JOIN access_roles ar ON ar.id = ua.role_id WHERE ar.name = 'Administrador'").fetchone():
            connection.rollback()
            raise AppError("Ya existe una cuenta administradora.", 409)
        role_id = connection.execute("SELECT id FROM access_roles WHERE name = 'Administrador'").fetchone()["id"]
        connection.execute(
            "INSERT INTO user_accounts(employee_id, role_id, username, password_hash, created_at) VALUES (NULL, ?, ?, ?, ?)",
            (role_id, username, password_hash(password), now_iso()),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        raise AppError("Ese nombre de usuario ya está registrado.", 409) from None
    finally:
        connection.close()


def auth_metadata(actor: dict, db_path: Path = DB_PATH) -> dict:
    connection = connect(db_path)
    try:
        warehouses = []
        for row in connection.execute("SELECT id, name FROM warehouses WHERE active = 1 ORDER BY id"):
            wid = int(row["id"])
            if not actor["is_admin"] and wid not in actor_warehouses(connection, actor):
                continue
            warehouses.append({"id": wid, "name": row["name"], "permissions": sorted(permissions_for(connection, actor["id"], actor["role_id"], wid))})
        return {"user": {"id": actor["id"], "username": actor["username"], "name": actor["name"], "role": actor["role"], "is_admin": actor["is_admin"]},
                "warehouses": warehouses, "permission_labels": PERMISSIONS}
    finally:
        connection.close()


def list_accounts(db_path: Path = DB_PATH) -> list[dict]:
    connection = connect(db_path)
    try:
        accounts = []
        for row in connection.execute(
            """SELECT ua.id, ua.username, ua.active, ua.employee_id, ar.id AS role_id, ar.name AS role,
                      e.first_name, e.last_name
               FROM user_accounts ua JOIN access_roles ar ON ar.id = ua.role_id
               LEFT JOIN employees e ON e.id = ua.employee_id ORDER BY ua.id"""
        ):
            account = dict(row)
            account["warehouses"] = []
            for access in connection.execute(
                "SELECT warehouse_id FROM user_warehouse_access WHERE user_id = ? ORDER BY warehouse_id", (row["id"],)
            ):
                wid = int(access["warehouse_id"])
                permissions = permissions_for(connection, int(row["id"]), int(row["role_id"]), wid)
                warehouse = connection.execute("SELECT name FROM warehouses WHERE id = ?", (wid,)).fetchone()
                account["warehouses"].append({"id": wid, "name": warehouse["name"], "permissions": sorted(permissions)})
            accounts.append(account)
        return accounts
    finally:
        connection.close()


def authenticate(username: str, password: str, db_path: Path = DB_PATH) -> tuple[dict, str]:
    if not isinstance(password, str) or len(password) > 256:
        raise AppError("Usuario o contraseña incorrectos.", 401)
    username = username.strip()
    key = username.casefold()
    connection = connect(db_path)
    try:
        begin(connection)
        now = datetime.now(timezone.utc)
        attempt = connection.execute("SELECT failures, window_started_at FROM login_attempts WHERE username_key = ?", (key,)).fetchone()
        if attempt:
            window_start = datetime.fromisoformat(attempt["window_started_at"])
            if now - window_start < timedelta(minutes=15) and attempt["failures"] >= 5:
                connection.rollback()
                raise AppError("Demasiados intentos. Espera 15 minutos antes de volver a intentar.", 429)
        row = connection.execute(
            """SELECT ua.id, ua.username, ua.password_hash, ua.role_id, ar.name AS role,
                      ua.employee_id, e.first_name, e.last_name
               FROM user_accounts ua JOIN access_roles ar ON ar.id = ua.role_id
               LEFT JOIN employees e ON e.id = ua.employee_id
               WHERE lower(ua.username) = lower(?) AND ua.active = 1""", (username,)
        ).fetchone()
        valid = row is not None and verify_password(password, row["password_hash"])
        if not valid:
            if attempt and now - datetime.fromisoformat(attempt["window_started_at"]) < timedelta(minutes=15):
                failures = int(attempt["failures"]) + 1
                window_started = attempt["window_started_at"]
            else:
                failures = 1
                window_started = now.isoformat(timespec="seconds")
            connection.execute("INSERT INTO login_attempts(username_key, failures, window_started_at) VALUES (?, ?, ?) ON CONFLICT(username_key) DO UPDATE SET failures = excluded.failures, window_started_at = excluded.window_started_at",
                               (key, failures, window_started))
            connection.commit()
            raise AppError("Usuario o contraseña incorrectos.", 401)
        connection.execute("DELETE FROM login_attempts WHERE username_key = ?", (key,))
        token = secrets.token_urlsafe(32)
        expires_at = (now + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now.isoformat(timespec="seconds"),))
        connection.execute("INSERT INTO auth_sessions(user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                           (row["id"], hashlib.sha256(token.encode()).hexdigest(), expires_at, now.isoformat(timespec="seconds")))
        connection.commit()
        actor = {"id": int(row["id"]), "username": row["username"], "role_id": int(row["role_id"]),
                 "role": row["role"], "employee_id": row["employee_id"],
                 "name": " ".join(filter(None, [row["first_name"], row["last_name"]])) or row["username"],
                 "is_admin": row["role"] == "Administrador"}
        return actor, token
    finally:
        connection.close()


def session_actor(token: str | None, db_path: Path = DB_PATH) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection = connect(db_path)
    try:
        row = connection.execute(
            """SELECT ua.id, ua.username, ua.role_id, ar.name AS role, ua.employee_id,
                      e.first_name, e.last_name
               FROM auth_sessions s JOIN user_accounts ua ON ua.id = s.user_id
               JOIN access_roles ar ON ar.id = ua.role_id LEFT JOIN employees e ON e.id = ua.employee_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND ua.active = 1""", (token_hash, now_iso())
        ).fetchone()
        if row is None:
            return None
        return {"id": int(row["id"]), "username": row["username"], "role_id": int(row["role_id"]),
                "role": row["role"], "employee_id": row["employee_id"],
                "name": " ".join(filter(None, [row["first_name"], row["last_name"]])) or row["username"],
                "is_admin": row["role"] == "Administrador"}
    finally:
        connection.close()


def revoke_session(token: str | None, db_path: Path = DB_PATH) -> None:
    if not token:
        return
    connection = connect(db_path)
    try:
        connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
        connection.commit()
    finally:
        connection.close()


def access_token(handler: "RequestHandler") -> str | None:
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE:
            return value
    return None


def create_warehouse(payload: dict, db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    name = clean_text(payload.get("name"), "nombre del almacén", 100)
    connection = connect(db_path)
    try:
        begin(connection)
        require_admin(connection, actor)
        cursor = connection.execute(
            "INSERT INTO warehouses(name, active, created_at) VALUES (?, 1, ?)",
            (name, now_iso()),
        )
        warehouse_id = cursor.lastrowid
        location_cursor = connection.execute(
            "INSERT INTO locations(name, kind, warehouse_id, employee_id, created_at) VALUES (?, 'central', ?, NULL, ?)",
            (DEFAULT_LOCATION_NAME, warehouse_id, now_iso()),
        )
        connection.commit()
        return {"id": warehouse_id, "name": name, "active": True,
                "default_location_id": location_cursor.lastrowid}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "warehouses.name" in str(error):
            raise AppError("Ya existe un almacén con ese nombre.", 409) from None
        raise AppError("No se pudo crear el almacén.", 409) from None
    finally:
        connection.close()


def create_subwarehouse(payload: dict, db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    name = clean_text(payload.get("name"), "nombre del subalmacén", 100)
    try:
        warehouse_id = int(payload.get("warehouse_id"))
    except (TypeError, ValueError):
        raise AppError("Selecciona un almacén.") from None
    connection = connect(db_path)
    try:
        begin(connection)
        warehouse = get_warehouse(connection, warehouse_id)
        require_permission(connection, actor, warehouse_id, "manage_warehouses")
        cursor = connection.execute(
            "INSERT INTO locations(name, kind, warehouse_id, employee_id, created_at) VALUES (?, 'central', ?, NULL, ?)",
            (name, warehouse_id, now_iso()),
        )
        connection.commit()
        return {"id": cursor.lastrowid, "name": name, "kind": "central",
                "warehouse_id": warehouse["id"], "warehouse_name": warehouse["name"]}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "locations.warehouse_id" in str(error):
            raise AppError("Ya existe un subalmacén con ese nombre en este almacén.", 409) from None
        raise AppError("No se pudo crear el subalmacén.", 409) from None
    finally:
        connection.close()


def create_employee(payload: dict, db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    code = clean_text(payload.get("code"), "código", 40)
    first_name = clean_text(payload.get("first_name"), "nombres", 100)
    last_name = clean_text(payload.get("last_name"), "apellidos", 100)
    role = clean_text(payload.get("role"), "cargo", 40)
    if role not in EMPLOYEE_ROLES:
        raise AppError("Selecciona un cargo válido.")
    timestamp = now_iso()
    connection = connect(db_path)
    try:
        begin(connection)
        try:
            warehouse_id = int(payload.get("warehouse_id")) if payload.get("warehouse_id") else first_warehouse_id(connection)
        except (TypeError, ValueError):
            raise AppError("Selecciona el almacén asignado al trabajador.") from None
        warehouse = get_warehouse(connection, warehouse_id)
        require_permission(connection, actor, warehouse_id, "manage_people")
        cursor = connection.execute(
            "INSERT INTO employees(code, first_name, last_name, role, warehouse_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, first_name, last_name, role, warehouse_id, timestamp),
        )
        employee_id = cursor.lastrowid
        location_id = None
        if role == "Técnico":
            location = f"ALMACÉN TÉCNICO - {first_name} {last_name}"
            location_cursor = connection.execute(
                "INSERT INTO locations(name, kind, warehouse_id, employee_id, created_at) VALUES (?, 'technician', ?, ?, ?)",
                (location, warehouse["id"], employee_id, timestamp),
            )
            location_id = location_cursor.lastrowid
        connection.commit()
        return {"id": employee_id, "code": code, "first_name": first_name,
                "last_name": last_name, "role": role, "location_id": location_id,
                "warehouse_id": warehouse_id}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "employees.code" in str(error):
            raise AppError("Ya existe un trabajador con ese código.", 409) from None
        raise AppError("No se pudo registrar el trabajador por una relación duplicada.", 409) from None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_item(payload: dict, db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    code = clean_text(payload.get("code"), "código de artículo", 60)
    name = clean_text(payload.get("name"), "nombre de artículo", 160)
    item_type = clean_text(payload.get("item_type"), "tipo de artículo", 20)
    unit = clean_text(payload.get("unit", "unidad"), "unidad", 30)
    serial_control = bool(payload.get("serial_control"))
    if item_type not in ITEM_TYPES:
        raise AppError("Selecciona Material o Equipo.")
    connection = connect(db_path)
    try:
        begin(connection)
        if actor is not None:
            for row in connection.execute("SELECT id FROM warehouses WHERE active = 1"):
                require_permission(connection, actor, int(row["id"]), "manage_items")
        cursor = connection.execute(
            "INSERT INTO items(code, name, item_type, unit, serial_control, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, item_type, unit, int(serial_control), now_iso()),
        )
        connection.commit()
        return {"id": cursor.lastrowid, "code": code, "name": name, "item_type": item_type,
                "unit": unit, "serial_control": serial_control, "active": True}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "items.code" in str(error):
            raise AppError("Ya existe un artículo con ese código.", 409) from None
        raise AppError("No se pudo registrar el artículo.", 409) from None
    finally:
        connection.close()


def get_location(connection: sqlite3.Connection, location_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if row is None:
        raise AppError("No se encontró el almacén indicado.", 404)
    return row


def get_location_for_warehouse(connection: sqlite3.Connection, location_id: int, warehouse_id: int, label: str) -> sqlite3.Row:
    location = get_location(connection, location_id)
    warehouse = get_warehouse(connection, warehouse_id)
    if location["kind"] != "central" or location["warehouse_id"] != warehouse["id"]:
        raise AppError(f"El subalmacén de {label} no pertenece al almacén activo.", 409)
    return location


def get_item(connection: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
    if row is None:
        raise AppError("No se encontró el artículo indicado.", 404)
    return row


def stock_quantity(connection: sqlite3.Connection, item_id: int, location_id: int) -> float:
    item = get_item(connection, item_id)
    if item["serial_control"]:
        row = connection.execute(
            "SELECT COUNT(*) AS quantity FROM serial_units WHERE item_id = ? AND current_location_id = ?",
            (item_id, location_id),
        ).fetchone()
        return float(row["quantity"])
    row = connection.execute(
        """SELECT COALESCE(SUM(
               CASE WHEN destination_location_id = ? THEN quantity ELSE 0 END -
               CASE WHEN source_location_id = ? THEN quantity ELSE 0 END
           ), 0) AS quantity
           FROM movement_lines WHERE item_id = ?""",
        (location_id, location_id, item_id),
    ).fetchone()
    return float(row["quantity"])


def ensure_lines(payload: dict) -> list[dict]:
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        raise AppError("Agrega al menos un artículo al movimiento.")
    seen_items: set[int] = set()
    normalized = []
    for line in lines:
        if not isinstance(line, dict):
            raise AppError("Hay una línea de artículo inválida.")
        try:
            item_id = int(line.get("item_id"))
        except (TypeError, ValueError):
            raise AppError("Selecciona un artículo para cada línea.") from None
        if item_id in seen_items:
            raise AppError("Combina las cantidades del mismo artículo en una sola línea.")
        seen_items.add(item_id)
        quantity = positive_number(line.get("quantity"), "artículo")
        serials = serial_list(line.get("serials"))
        normalized.append({"item_id": item_id, "quantity": quantity, "serials": serials})
    return normalized


def receive_stock(payload: dict, db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    document = clean_text(payload.get("document"), "documento", 120)
    notes = clean_text(payload.get("notes", ""), "observación", 500, required=False)
    responsible = clean_text(payload.get("responsible", "Usuario de prueba"), "responsable", 100)
    lines = ensure_lines(payload)
    connection = connect(db_path)
    try:
        begin(connection)
        if payload.get("location_id"):
            try:
                warehouse_id = int(payload.get("warehouse_id"))
                destination = get_location_for_warehouse(connection, int(payload["location_id"]), warehouse_id, "destino")
            except (TypeError, ValueError):
                raise AppError("Selecciona un subalmacén de destino.") from None
        else:
            try:
                warehouse_id = int(payload.get("warehouse_id")) if payload.get("warehouse_id") else None
            except (TypeError, ValueError):
                raise AppError("Selecciona un almacén de destino.") from None
            if warehouse_id is None:
                warehouse = connection.execute(
                    "SELECT id FROM warehouses WHERE active = 1 ORDER BY id LIMIT 1"
                ).fetchone()
                if warehouse is None:
                    raise AppError("Crea un almacén antes de registrar ingresos.", 409)
                warehouse_id = warehouse["id"]
            get_warehouse(connection, warehouse_id)
            destination = get_default_stock_location(connection, warehouse_id)
        if destination["kind"] != "central":
            raise AppError("Los ingresos deben llegar a un subalmacén, no a un almacén técnico.")
        get_warehouse(connection, destination["warehouse_id"])
        require_permission(connection, actor, destination["warehouse_id"], "receive_stock")
        movement_cursor = connection.execute(
            "INSERT INTO movements(kind, document, notes, responsible, created_at) VALUES ('Ingreso', ?, ?, ?, ?)",
            (document, notes, responsible, now_iso()),
        )
        movement_id = movement_cursor.lastrowid
        for line in lines:
            item = get_item(connection, line["item_id"])
            quantity = line["quantity"]
            serials = line["serials"]
            if item["serial_control"]:
                if not quantity.is_integer() or int(quantity) != len(serials):
                    raise AppError(f"Para {item['name']}, la cantidad debe coincidir con el número de series.")
                if not serials:
                    raise AppError(f"Ingresa las series de {item['name']}.")
            elif serials:
                raise AppError(f"{item['name']} no tiene control por serie.")
            cursor = connection.execute(
                """INSERT INTO movement_lines(movement_id, item_id, source_location_id,
                   destination_location_id, quantity) VALUES (?, ?, NULL, ?, ?)""",
                (movement_id, item["id"], destination["id"], quantity),
            )
            for serial in serials:
                serial_cursor = connection.execute(
                    "INSERT INTO serial_units(item_id, serial_number, current_location_id, created_at) VALUES (?, ?, ?, ?)",
                    (item["id"], serial, destination["id"], now_iso()),
                )
                connection.execute(
                    "INSERT INTO movement_serials(movement_line_id, serial_unit_id, source_location_id, destination_location_id) VALUES (?, ?, NULL, ?)",
                    (cursor.lastrowid, serial_cursor.lastrowid, destination["id"]),
                )
        connection.commit()
        return {"id": movement_id, "kind": "Ingreso", "document": document}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "serial_units.serial_number" in str(error):
            raise AppError("Una o más series ya están registradas.", 409) from None
        raise AppError("No se pudo registrar el ingreso.", 409) from None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def transfer_stock(payload: dict, db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    document = clean_text(payload.get("document"), "documento", 120)
    notes = clean_text(payload.get("notes", ""), "observación", 500, required=False)
    responsible = clean_text(payload.get("responsible", "Usuario de prueba"), "responsable", 100)
    try:
        employee_id = int(payload.get("employee_id"))
    except (TypeError, ValueError):
        raise AppError("Selecciona el técnico que recibirá el material.") from None
    lines = ensure_lines(payload)
    connection = connect(db_path)
    try:
        begin(connection)
        if payload.get("source_location_id"):
            try:
                warehouse_id = int(payload.get("warehouse_id"))
                source = get_location_for_warehouse(connection, int(payload["source_location_id"]), warehouse_id, "origen")
            except (TypeError, ValueError):
                raise AppError("Selecciona el subalmacén de origen.") from None
        else:
            try:
                warehouse_id = int(payload.get("warehouse_id")) if payload.get("warehouse_id") else None
            except (TypeError, ValueError):
                raise AppError("Selecciona un almacén de origen.") from None
            if warehouse_id is None:
                warehouse = connection.execute(
                    "SELECT id FROM warehouses WHERE active = 1 ORDER BY id LIMIT 1"
                ).fetchone()
                if warehouse is None:
                    raise AppError("Crea un almacén antes de transferir materiales.", 409)
                warehouse_id = warehouse["id"]
            get_warehouse(connection, warehouse_id)
            source = get_default_stock_location(connection, warehouse_id)
        if source["kind"] != "central":
            raise AppError("El origen debe ser un subalmacén, no una ubicación de técnico.")
        warehouse = get_warehouse(connection, source["warehouse_id"])
        require_permission(connection, actor, warehouse["id"], "transfer_stock")
        employee = connection.execute(
            "SELECT id, first_name, last_name, role, active FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()
        if employee is None or employee["role"] != "Técnico" or not employee["active"]:
            raise AppError("El destinatario debe ser un técnico activo.")
        destination = connection.execute(
            "SELECT id, warehouse_id FROM locations WHERE employee_id = ? AND kind = 'technician'", (employee_id,)
        ).fetchone()
        if destination is None:
            raise AppError("El técnico no tiene un almacén personal configurado.", 409)
        if destination["warehouse_id"] != warehouse["id"]:
            raise AppError("El técnico pertenece a otro almacén. Selecciona uno de ese almacén.", 409)
        movement_cursor = connection.execute(
            "INSERT INTO movements(kind, document, notes, responsible, created_at) VALUES ('Transferencia', ?, ?, ?, ?)",
            (document, notes, responsible, now_iso()),
        )
        movement_id = movement_cursor.lastrowid
        for line in lines:
            item = get_item(connection, line["item_id"])
            quantity = line["quantity"]
            serials = line["serials"]
            available = stock_quantity(connection, item["id"], source["id"])
            if quantity > available:
                raise AppError(f"Stock insuficiente de {item['name']}: disponibles {format_quantity(available)}.")
            if item["serial_control"]:
                if not quantity.is_integer() or int(quantity) != len(serials):
                    raise AppError(f"Para {item['name']}, la cantidad debe coincidir con el número de series.")
                if not serials:
                    raise AppError(f"Ingresa las series que se transferirán de {item['name']}.")
                serial_rows = []
                for serial in serials:
                    row = connection.execute(
                        "SELECT id FROM serial_units WHERE item_id = ? AND serial_number = ? AND current_location_id = ?",
                        (item["id"], serial, source["id"]),
                    ).fetchone()
                    if row is None:
                        raise AppError(f"La serie {serial} no está disponible en el almacén central.")
                    serial_rows.append(row["id"])
            elif serials:
                raise AppError(f"{item['name']} no tiene control por serie.")
            cursor = connection.execute(
                """INSERT INTO movement_lines(movement_id, item_id, source_location_id,
                   destination_location_id, quantity) VALUES (?, ?, ?, ?, ?)""",
                (movement_id, item["id"], source["id"], destination["id"], quantity),
            )
            if item["serial_control"]:
                for serial_id in serial_rows:
                    connection.execute(
                        "UPDATE serial_units SET current_location_id = ? WHERE id = ?",
                        (destination["id"], serial_id),
                    )
                    connection.execute(
                        "INSERT INTO movement_serials(movement_line_id, serial_unit_id, source_location_id, destination_location_id) VALUES (?, ?, ?, ?)",
                        (cursor.lastrowid, serial_id, source["id"], destination["id"]),
                    )
        connection.commit()
        return {"id": movement_id, "kind": "Transferencia", "document": document,
                "employee": f"{employee['first_name']} {employee['last_name']}"}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "movement_lines" in str(error):
            raise AppError("No se pudo guardar el detalle de la transferencia.", 409) from None
        raise AppError("No se pudo registrar la transferencia.", 409) from None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def format_quantity(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def active_warehouses(db_path: Path = DB_PATH) -> list[dict]:
    connection = connect(db_path)
    try:
        return [dict(row) for row in connection.execute("SELECT id, name FROM warehouses WHERE active = 1 ORDER BY id")]
    finally:
        connection.close()


def available_employees(db_path: Path = DB_PATH) -> list[dict]:
    connection = connect(db_path)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT id, code, first_name, last_name, role, warehouse_id FROM employees WHERE active = 1 ORDER BY last_name, first_name")]
    finally:
        connection.close()


def snapshot(db_path: Path = DB_PATH, actor: dict | None = None) -> dict:
    connection = connect(db_path)
    try:
        warehouses = [dict(row) for row in connection.execute(
            "SELECT id, name, active FROM warehouses WHERE active = 1 ORDER BY id"
        )]
        employees = [dict(row) for row in connection.execute(
            """SELECT e.id, e.code, e.first_name, e.last_name, e.role, e.active,
                      l.id AS location_id, l.name AS location_name,
                      l.warehouse_id, w.name AS warehouse_name
               FROM employees e LEFT JOIN locations l ON l.employee_id = e.id
               LEFT JOIN warehouses w ON w.id = e.warehouse_id
               ORDER BY e.last_name, e.first_name"""
        )]
        items = [dict(row) for row in connection.execute(
            "SELECT id, code, name, item_type, unit, serial_control, active FROM items WHERE active = 1 ORDER BY name"
        )]
        locations = [dict(row) for row in connection.execute(
            """SELECT l.id, l.name, l.kind, l.warehouse_id, w.name AS warehouse_name,
                      l.employee_id, e.first_name, e.last_name
               FROM locations l LEFT JOIN employees e ON e.id = l.employee_id
               JOIN warehouses w ON w.id = l.warehouse_id
               WHERE w.active = 1
               ORDER BY w.id, CASE l.kind WHEN 'central' THEN 0 ELSE 1 END, l.name"""
        )]
        inventory = []
        for item in items:
            for location in locations:
                quantity = stock_quantity(connection, item["id"], location["id"])
                if quantity <= 0:
                    continue
                serials = []
                if item["serial_control"]:
                    serials = [row["serial_number"] for row in connection.execute(
                        "SELECT serial_number FROM serial_units WHERE item_id = ? AND current_location_id = ? ORDER BY serial_number",
                        (item["id"], location["id"]),
                    )]
                inventory.append({"item_id": item["id"], "item_code": item["code"],
                                  "item_name": item["name"], "item_type": item["item_type"],
                                  "unit": item["unit"], "serial_control": bool(item["serial_control"]),
                                  "quantity": format_quantity(quantity), "serials": serials,
                                  "location_id": location["id"], "location_name": location["name"],
                                  "location_kind": location["kind"],
                                  "warehouse_id": location["warehouse_id"],
                                  "warehouse_name": location["warehouse_name"]})
        movements = [dict(row) for row in connection.execute(
            """SELECT m.id, m.kind, m.document, m.notes, m.responsible, m.created_at,
                      COUNT(ml.id) AS line_count,
                      GROUP_CONCAT(i.name, ', ') AS item_names,
                      sl.name AS source_name, dl.name AS destination_name,
                      sw.id AS source_warehouse_id, sw.name AS source_warehouse_name,
                      dw.id AS destination_warehouse_id, dw.name AS destination_warehouse_name
               FROM movements m
               JOIN movement_lines ml ON ml.movement_id = m.id
               JOIN items i ON i.id = ml.item_id
               LEFT JOIN locations sl ON sl.id = ml.source_location_id
               JOIN locations dl ON dl.id = ml.destination_location_id
               LEFT JOIN warehouses sw ON sw.id = sl.warehouse_id
               JOIN warehouses dw ON dw.id = dl.warehouse_id
               GROUP BY m.id ORDER BY m.id DESC LIMIT 30"""
        )]
        if actor is not None:
            allowed_ids = actor_warehouses(connection, actor)
            warehouses = [row for row in warehouses if row["id"] in allowed_ids]
            grants = {wid: permissions_for(connection, actor["id"], actor["role_id"], wid) for wid in allowed_ids}
            employees = [row for row in employees if row["warehouse_id"] in allowed_ids
                         and ({"manage_people", "transfer_stock"} & grants.get(row["warehouse_id"], set()))]
            locations = [row for row in locations if row["warehouse_id"] in allowed_ids
                         and ({"view_inventory", "receive_stock", "transfer_stock"} & grants.get(row["warehouse_id"], set()))]
            inventory = [row for row in inventory if row["warehouse_id"] in allowed_ids
                         and "view_inventory" in grants.get(row["warehouse_id"], set())]
            movements = [row for row in movements if (
                row["source_warehouse_id"] in allowed_ids and "view_movements" in grants.get(row["source_warehouse_id"], set())
            ) or (
                row["destination_warehouse_id"] in allowed_ids and "view_movements" in grants.get(row["destination_warehouse_id"], set())
            )]
            if not any({"view_inventory", "receive_stock", "transfer_stock", "manage_items"} & permissions for permissions in grants.values()):
                items = []
            return {"warehouses": warehouses, "employees": employees, "items": items,
                    "locations": locations, "inventory": inventory, "movements": movements,
                    "access": auth_metadata(actor, db_path)}
        return {"warehouses": warehouses, "employees": employees, "items": items,
                "locations": locations, "inventory": inventory, "movements": movements}
    finally:
        connection.close()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "WarehouseOperationsDev/0.1"
    database_path = DB_PATH

    def send_json(self, value: object, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError("Tamaño de solicitud inválido.") from None
        if length <= 0 or length > MAX_BODY_BYTES:
            raise AppError("El contenido está vacío o supera el tamaño permitido.", 413)
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise AppError("La solicitud debe usar formato JSON.", 415)
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("El cuerpo de la solicitud no es JSON válido.") from None
        if not isinstance(payload, dict):
            raise AppError("El cuerpo de la solicitud debe ser un objeto JSON.")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        actor = session_actor(access_token(self), self.database_path)
        if path == "/api/auth/me":
            if actor is None:
                self.send_json({"error": "Inicia sesión para continuar."}, 401)
            else:
                self.send_json(auth_metadata(actor, self.database_path))
            return
        if path == "/api/snapshot":
            if actor is None:
                self.send_json({"error": "Inicia sesión para continuar."}, 401)
                return
            self.send_json(snapshot(self.database_path, actor=actor))
            return
        if path == "/api/admin/accounts":
            if actor is None or not actor.get("is_admin"):
                self.send_json({"error": "Solo un administrador puede consultar las cuentas."}, 403 if actor else 401)
                return
            self.send_json({"accounts": list_accounts(self.database_path), "employees": available_employees(self.database_path),
                            "warehouses": active_warehouses(self.database_path), "roles": list(ROLE_DEFAULTS),
                            "permission_labels": PERMISSIONS, "role_defaults": {role: sorted(perms) for role, perms in ROLE_DEFAULTS.items()}})
            return
        files = {"/": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
                 "/static/app.js": (STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
                 "/static/styles.css": (STATIC_DIR / "styles.css", "text/css; charset=utf-8")}
        file_entry = files.get(path)
        if file_entry is None:
            self.send_json({"error": "No encontrado."}, 404)
            return
        file_path, content_type = file_entry
        try:
            body = file_path.read_bytes()
        except OSError:
            self.send_json({"error": "No se encontró el archivo de la aplicación."}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send_json({"error": "Origen de solicitud no permitido."}, 403)
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_json({"error": "Origen de solicitud no permitido."}, 403)
            return
        if path == "/api/auth/login":
            try:
                payload = self.read_json()
                actor, token = authenticate(clean_text(payload.get("username"), "usuario", 80), payload.get("password", ""), self.database_path)
                self.send_json(auth_metadata(actor, self.database_path), headers={
                    "Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}",
                })
            except AppError as error:
                self.send_json({"error": str(error)}, error.status)
            return
        if path == "/api/auth/logout":
            revoke_session(access_token(self), self.database_path)
            self.send_json({"ok": True}, headers={
                "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            })
            return
        actor = session_actor(access_token(self), self.database_path)
        if actor is None:
            self.send_json({"error": "Inicia sesión para continuar."}, 401)
            return
        try:
            payload = self.read_json()
            if path == "/api/admin/accounts":
                if not actor.get("is_admin"):
                    raise AppError("Solo un administrador puede crear cuentas.", 403)
                self.send_json({"result": create_account(payload, self.database_path)}, 201)
                return
            actions = {"/api/employees": create_employee,
                       "/api/items": create_item,
                       "/api/warehouses": create_warehouse,
                       "/api/subwarehouses": create_subwarehouse,
                       "/api/receipts": receive_stock,
                       "/api/transfers": transfer_stock}
            action = actions.get(path)
            if action is None:
                self.send_json({"error": "No encontrado."}, 404)
                return
            result = action(payload, db_path=self.database_path, actor=actor)
            self.send_json({"result": result}, 201)
        except AppError as error:
            self.send_json({"error": str(error)}, error.status)
        except sqlite3.IntegrityError as error:
            if "UNIQUE constraint failed" in str(error):
                self.send_json({"error": "El código o número de serie ya existe."}, 409)
            else:
                self.send_json({"error": "Los datos no cumplen las reglas del inventario."}, 400)
        except (ValueError, TypeError):
            self.send_json({"error": "Revisa los datos enviados."}, 400)
        except Exception as error:
            if error.__class__.__module__.startswith("psycopg") and error.__class__.__name__ == "IntegrityError":
                message = str(error)
                self.send_json({"error": "El código o número de serie ya existe." if "unique" in message.lower()
                                else "Los datos no cumplen las reglas del inventario."}, 409 if "unique" in message.lower() else 400)
                return
            self.log_error("Unexpected error while processing request")
            self.send_json({"error": "Ocurrió un error interno."}, 500)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        origin = self.headers.get("Origin")
        if (origin and urlparse(origin).netloc != self.headers.get("Host")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_json({"error": "Origen de solicitud no permitido."}, 403)
            return
        actor = session_actor(access_token(self), self.database_path)
        if actor is None:
            self.send_json({"error": "Inicia sesión para continuar."}, 401)
            return
        if not actor.get("is_admin"):
            self.send_json({"error": "Solo un administrador puede cambiar permisos."}, 403)
            return
        path = urlparse(self.path).path
        prefix = "/api/admin/accounts/"
        if not path.startswith(prefix) or not path.endswith("/access"):
            self.send_json({"error": "No encontrado."}, 404)
            return
        try:
            account_id = int(path[len(prefix):-len("/access")].strip("/"))
            payload = self.read_json()
            update_account_access(account_id, payload.get("access"), self.database_path)
            self.send_json({"ok": True})
        except AppError as error:
            self.send_json({"error": str(error)}, error.status)
        except (ValueError, TypeError):
            self.send_json({"error": "La cuenta o los permisos enviados no son válidos."}, 400)

    def log_message(self, format: str, *args: object) -> None:
        # Keep local development logs free of request bodies and user data.
        super().log_message(format, *args)


def main() -> None:
    if not DATABASE_URL:
        initialize_database()
    else:
        with closing(connect()) as connection:
            connection.execute("SELECT 1").fetchone()
    if len(os.sys.argv) > 1 and os.sys.argv[1] == "--create-admin":
        username = input("Usuario administrador: ").strip()
        password = getpass("Contraseña (mínimo 12 caracteres): ")
        confirm = getpass("Repite la contraseña: ")
        if password != confirm:
            raise SystemExit("Las contraseñas no coinciden.")
        create_bootstrap_admin(username, password)
        print("Cuenta administradora creada. Inicia el servidor con: py app.py")
        return
    server = ThreadingHTTPServer(("127.0.0.1", 8765), RequestHandler)
    database_label = "Supabase PostgreSQL" if DATABASE_URL else str(DB_PATH)
    print(f"Prototipo local: http://127.0.0.1:8765  |  Base: {database_label}")
    print("Solo para desarrollo con datos de prueba; no exponer a internet.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()

if __name__ == "__main__":
    main()

