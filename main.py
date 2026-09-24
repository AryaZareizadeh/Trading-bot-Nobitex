"""Local dashboard launcher and operator password setup."""

import argparse
import getpass
import os
from pathlib import Path


def main(argv=None):
    from settings import load_environment, update_environment

    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("serve", "setup-password", "backup", "import-legacy"), default="serve")
    parser.add_argument("--output", type=Path, help="New SQLite backup file (backup command only)")
    parser.add_argument("--username", help="Verified destination account for import-legacy")
    parser.add_argument("--source", type=Path, help="Legacy SQLite database for import-legacy")
    parser.add_argument("--host", default=os.getenv("TRADER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("TRADER_PORT", "8011")))
    args = parser.parse_args(argv)
    if args.command == "import-legacy":
        from accounts import Accounts
        from settings import data_dir
        import sqlite3

        if not args.username:
            parser.error("import-legacy requires --username")
        if args.output is not None:
            parser.error("--output is only valid with backup")
        master_key = os.getenv("TRADER_MASTER_KEY", "")
        if not master_key:
            parser.error("TRADER_MASTER_KEY must match the multi-user deployment")
        root = data_dir()
        accounts = Accounts(root / "accounts.sqlite3", master_key)
        user = accounts.by_username(args.username)
        if not user or not user["verified"]:
            parser.error("Destination account must exist and have a confirmed email")
        source = args.source.resolve() if args.source is not None else root / "trader.sqlite3"
        destination = root / "users" / user["id"] / "trader.sqlite3"
        if not source.is_file():
            parser.error("No legacy database exists in TRADER_DATA_DIR")
        if destination.exists():
            parser.error("Destination already has trading data; import refused")
        legacy = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            has_records = legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
            if not has_records or legacy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                parser.error("Legacy database is missing its records or failed integrity check")
            import json
            saved_accounts = {state.get("_account") for (raw,) in legacy.execute(
                "SELECT value FROM records WHERE namespace='runtime'")
                if (state := json.loads(raw)).get("_account")}
            if len(saved_accounts) > 1:
                parser.error("Legacy bots contain multiple exchange identities; reconcile them before import")
            saved_account = next(iter(saved_accounts), None)
            account_id = saved_account.split(":", 1)[1] if saved_account else os.getenv("TRADER_ACCOUNT_ID", "default")
            testnet = saved_account.startswith("testnet:") if saved_account else (
                os.getenv("NOBITEX_TESTNET", "false").lower() == "true")
            destination.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(destination)
            try:
                legacy.backup(target)
            finally:
                target.close()
            token = os.getenv("NOBITEX_TOKEN", "")
            public_key = os.getenv("NOBITEX_API_PUBLIC_KEY", "")
            private_key = os.getenv("NOBITEX_API_PRIVATE_KEY", "")
            accounts.set_credentials(user["id"], {
                "token": token, "public_key": public_key, "private_key": private_key,
                "testnet": testnet, "account_id": account_id})
        finally:
            legacy.close()
        print(f"Legacy data copied to account {user['username']}; original database retained.")
        return
    if args.username is not None:
        parser.error("--username is only valid with import-legacy")
    if args.source is not None:
        parser.error("--source is only valid with import-legacy")
    if args.command == "backup":
        from settings import data_dir
        from storage import Store

        source = data_dir() / "trader.sqlite3"
        if args.output is None:
            parser.error("backup requires --output PATH")
        if not source.is_file():
            parser.error("No database exists in the configured TRADER_DATA_DIR")
        if args.output.exists():
            parser.error("Backup destination already exists; choose a new file")
        Store(source).backup(args.output)
        print(f"Database backup saved to {args.output.resolve()}")
        return
    if args.output is not None:
        parser.error("--output is only valid with backup")
    if args.command == "setup-password":
        from auth import hash_password

        password = getpass.getpass("New dashboard password (at least 12 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match; nothing was saved.")
        try:
            encoded = hash_password(password)
        except ValueError as exc:
            parser.error(str(exc))
        update_environment({"DASHBOARD_PASSWORD_HASH": encoded})
        print("Dashboard password saved. Restart the dashboard to apply it.")
        return

    import uvicorn

    target = "multiuser:app" if os.getenv("TRADER_MULTIUSER", "false").lower() == "true" else "server:app"
    uvicorn.run(target, host=args.host, port=args.port, workers=1, reload=False,
                proxy_headers=False)


if __name__ == "__main__":
    main()
