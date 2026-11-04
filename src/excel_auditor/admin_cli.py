from __future__ import annotations

import argparse
import getpass
import os

from .persistence import DatabaseRepository


def bootstrap_admin(database: DatabaseRepository, *, username: str, password: str):
    return database.ensure_local_admin(username=username, password=password)


def bootstrap_owner(database: DatabaseRepository, *, username: str, password: str, tenant_id: str = "local", tenant_name: str = "ExcelFlow"):
    """Compatibility wrapper for callers of the old multi-tenant bootstrap API."""
    return bootstrap_admin(database, username=username, password=password)


def main() -> None:
    parser = argparse.ArgumentParser(prog="excel-auditor-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create the single ExcelFlow administrator")
    create.add_argument("--username", required=True)
    create.add_argument("--password-env", default="EXCEL_AUDITOR_BOOTSTRAP_PASSWORD")
    reset = subparsers.add_parser("reset-password", help="explicitly reset an existing administrator password")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password-env", default="EXCEL_AUDITOR_BOOTSTRAP_PASSWORD")
    args = parser.parse_args()
    password = os.environ.get(args.password_env) or getpass.getpass("初始管理员密码：")
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        database = DatabaseRepository(database_url, create_schema=False)
    else:
        data_root = os.environ.get("EXCEL_AUDITOR_DATA", "var")
        database = DatabaseRepository(f"sqlite:///{data_root}/auth.sqlite3", create_schema=True)
    if args.command == "create":
        bootstrap_admin(database, username=args.username, password=password)
        print("administrator account initialized; existing credentials preserved")
    else:
        user = database.get_user_by_username("local", args.username)
        if user is None or user.role not in {"owner", "admin"}:
            raise SystemExit("administrator account not found")
        database.set_user_password(user.id, password)
        database.revoke_user_sessions(user.id)
        print("administrator password reset; existing sessions revoked")


if __name__ == "__main__":
    main()
