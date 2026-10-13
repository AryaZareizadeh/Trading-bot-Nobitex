from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from accounts import Accounts
from multiuser import AccountCredentials
from storage import Store
from bot_manager import BotManager
from server import create_app


def account_store(tmp_path):
    accounts = Accounts(tmp_path / "accounts.sqlite3", Fernet.generate_key().decode())
    users = []
    for name in ("first", "second"):
        user = accounts.register(name, f"{name}@example.com", "test-password-long-enough", "invite", "invite")
        assert accounts.verify_email(user["email"], accounts.issue_verification(user["id"]))
        users.append(user)
    return accounts, users


def test_profile_verification_rejects_rotated_duplicate_credentials(tmp_path):
    accounts, users = account_store(tmp_path)
    for i, user in enumerate(users):
        store = AccountCredentials(accounts, user["id"])
        manager = BotManager(store=Store(tmp_path / str(i) / "trader.sqlite3"))
        app = create_app(manager=manager, credential_store=store, offline=True, security=False,
                         credential_verifier=lambda values: {"nationalCode": "0111223334", "verifications": {"identity": True}})
        with TestClient(app) as client:
            response = client.post("/api/settings/credentials", json={"token": f"rotated-token-{i}"})
            assert response.status_code == (200 if i == 0 else 409)
    assert accounts.credentials(users[0]["id"])["verified_exchange_id"].startswith("national:")
    assert accounts.credentials(users[1]["id"]) == {}


def test_unavailable_profile_retains_previous_credentials_and_hides_details(tmp_path):
    accounts, users = account_store(tmp_path)
    store = AccountCredentials(accounts, users[0]["id"])
    store.set({"token": "previous-token"}, verified_exchange_id="old-id")
    def failed(values):
        raise RuntimeError("Authorization: Token secret-value")
    app = create_app(manager=BotManager(store=Store(tmp_path / "tenant" / "trader.sqlite3")),
                     credential_store=store, offline=True, security=False, credential_verifier=failed)
    with TestClient(app) as client:
        response = client.post("/api/settings/credentials", json={"token": "candidate-token"})
        assert response.status_code == 503
        assert "secret-value" not in response.text
    assert store.get()["token"] == "previous-token"
