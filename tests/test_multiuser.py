"""Account isolation and recovery contracts for the container dashboard."""
from fastapi.testclient import TestClient


def test_invite_registration_and_private_bot_state(tmp_path, monkeypatch):
    from multiuser import create_multiuser_app

    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-tests")
    monkeypatch.setenv("TRADER_MASTER_KEY", "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8=")
    sent = []
    app = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                               send_code=lambda email, code, purpose: sent.append((email, code, purpose)))
    with TestClient(app) as client:
        registration = {"username": "alice", "email": "alice@example.test",
                        "password": "alice password long enough", "invitation_code": "wrong"}
        assert client.post("/api/auth/register", json=registration).status_code == 403
        registration["invitation_code"] = "invite-secret-for-tests"
        assert client.post("/api/auth/register", json=registration).status_code == 201
        assert client.post("/api/auth/register/verify", json={"email": "alice@example.test", "code": sent[-1][1]}).status_code == 200
        registration.update(username="bob", email="bob@example.test", password="bob password long enough")
        assert client.post("/api/auth/register", json=registration).status_code == 201
        assert client.post("/api/auth/register/verify", json={"email": "bob@example.test", "code": sent[-1][1]}).status_code == 200
        a = client.post("/api/auth/login", json={"username": "alice", "password": "alice password long enough"})
        alice_cookie = client.cookies.get("dashboard_session")
        client.cookies.delete("dashboard_session")
        b = client.post("/api/auth/login", json={"username": "bob", "password": "bob password long enough"})
        bob_cookie = client.cookies.get("dashboard_session")
        assert a.status_code == b.status_code == 200
        client.cookies.set("dashboard_session", alice_cookie)
        bot = client.post("/api/bots", headers={"X-CSRF-Token": a.json()["csrf_token"]},
                         json={"symbol": "btcusdt", "strategy_name": "AdvRSI", "timeframe": "60", "credit": 100})
        assert bot.status_code == 201, bot.text
        assert client.post("/api/settings/credentials", headers={"X-CSRF-Token": a.json()["csrf_token"]},
                           json={"token": "alice-secret-token"}).status_code == 200
        assert client.post("/api/settings/execution", headers={"X-CSRF-Token": a.json()["csrf_token"]},
                           json={"entry_fee_rate": 0.001}).status_code == 200
        assert b"alice-secret-token" not in (tmp_path / "accounts.sqlite3").read_bytes()
        client.cookies.set("dashboard_session", bob_cookie)
        assert client.get("/api/bots").json() == []
        assert client.get(f"/api/bots/{bot.json()['id']}").status_code == 404
        assert client.get("/api/settings/credentials").json()["has_credentials"] is False
        assert client.get("/api/settings/execution").json()["entry_fee_rate"] == 0
        monkeypatch.setenv("NOBITEX_TOKEN", "global-server-token")
        assert client.get("/api/wallet").json()["error"] == "no_credentials"
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as socket:
            assert socket.receive_json()["bots"] == []
    restarted = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                                     send_code=lambda email, code, purpose: None)
    with TestClient(restarted) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "alice password long enough"}).status_code == 200
        assert len(client.get("/api/bots").json()) == 1
        assert client.get("/api/settings/credentials").json()["has_credentials"] is True
        assert client.get("/api/settings/execution").json()["entry_fee_rate"] == 0.001


def test_recovery_email_code_resets_only_matching_user(tmp_path, monkeypatch):
    from multiuser import create_multiuser_app

    sent = []
    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-tests")
    monkeypatch.setenv("TRADER_MASTER_KEY", "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8=")
    app = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                               send_code=lambda email, code, purpose: sent.append((email, code, purpose)))
    with TestClient(app) as client:
        assert client.post("/api/auth/register", json={"username": "alice", "email": "alice@example.test",
            "password": "old password long enough", "invitation_code": "invite-secret-for-tests"}).status_code == 201
        assert client.post("/api/auth/register/verify", json={"email": "alice@example.test", "code": sent[-1][1]}).status_code == 200
        assert client.post("/api/auth/login", json={"username": "alice", "password": "old password long enough"}).status_code == 200
        assert client.get("/api/auth/session").json()["authenticated"] is True
        assert client.post("/api/auth/recovery/request", json={"email": "alice@example.test"}).status_code == 200
        assert sent[-1][0] == "alice@example.test"
        reset = client.post("/api/auth/recovery/reset", json={"email": "alice@example.test",
            "code": sent[-1][1], "password": "new password long enough"})
        assert reset.status_code == 200
        assert client.get("/api/auth/session").json()["authenticated"] is False
        assert client.post("/api/auth/recovery/reset", json={"email": "alice@example.test",
            "code": sent[-1][1], "password": "another password long enough"}).status_code == 400
        assert client.post("/api/auth/login", json={"username": "alice", "password": "old password long enough"}).status_code == 401
        assert client.post("/api/auth/login", json={"username": "alice", "password": "new password long enough"}).status_code == 200


def test_legacy_import_keeps_source_and_targets_verified_account(tmp_path, monkeypatch):
    from accounts import Accounts
    from bot import Bot, BotConfig
    from dataclasses import asdict
    from multiuser import create_multiuser_app
    from storage import Store
    from main import main

    master_key = "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8="
    new_root = tmp_path / "new"
    monkeypatch.setenv("TRADER_DATA_DIR", str(new_root))
    monkeypatch.setenv("TRADER_MASTER_KEY", master_key)
    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-tests")
    monkeypatch.setenv("NOBITEX_TOKEN", "legacy-user-token")
    source = Store(tmp_path / "old" / "trader.sqlite3")
    source.put("runtime", "legacy-bot", {"balance": 42})
    legacy_bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100))
    legacy_bot._account = "production:default"
    source.put("bots", legacy_bot.id, asdict(legacy_bot.cfg))
    source.put("runtime", legacy_bot.id, legacy_bot.snapshot())
    accounts = Accounts(new_root / "accounts.sqlite3", master_key)
    user = accounts.register("alice", "alice@example.test", "old password long enough", "invite", "invite")
    assert accounts.verify_email(user["email"], accounts.issue_verification(user["id"]))
    main(["import-legacy", "--username", "alice", "--source", str(source.path)])
    target = new_root / "users" / user["id"] / "trader.sqlite3"
    assert Store(target).get("runtime", "legacy-bot") == {"balance": 42}
    assert source.get("runtime", "legacy-bot") == {"balance": 42}
    assert not (tmp_path / "old" / "trader.sqlite3.lock").exists()
    assert accounts.credentials(user["id"])["token"] == "legacy-user-token"
    app = create_multiuser_app(data_root=new_root, offline=True, allowed_hosts={"testserver"},
                               send_code=lambda email, code, purpose: None)
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "alice", "password": "old password long enough"})
        assert login.status_code == 200
        assert len(client.get("/api/bots").json()) == 1
        tenant = app.state.tenants.tenants[user["id"]][0]
        assert tenant.state.bot_manager.get_bot(legacy_bot.id)._account_key() == "production:default"
        assert client.post("/api/settings/credentials", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                           json={"token": "replacement-token"}).status_code == 200
        assert tenant.state.bot_manager.get_bot(legacy_bot.id)._account_key() == "production:default"


def test_optimizer_slots_limit_total_and_one_per_user():
    from multiuser import OptimizerSlots

    slots = OptimizerSlots(2)
    assert slots.try_start("alice")
    assert not slots.try_start("alice")
    assert slots.try_start("bob")
    assert not slots.try_start("charlie")
    slots.finish("alice")
    assert slots.try_start("charlie")


def test_optimizer_worker_cap_applies_to_requested_and_auto_workers(monkeypatch):
    import optimizer

    monkeypatch.setattr(optimizer, "_system_cpu_count", lambda: 32)
    monkeypatch.setenv("OPTIMIZER_MAX_CORES_PER_JOB", "2")
    monkeypatch.setenv("OPTIMIZER_WORKERS", "-1")
    assert optimizer._clamp_cores(16) == 2
    assert optimizer._resolve_workers() == 2


def test_malformed_unicode_password_is_rejected_during_registration(tmp_path, monkeypatch):
    from multiuser import create_multiuser_app

    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-tests")
    monkeypatch.setenv("TRADER_MASTER_KEY", "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8=")
    app = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                               send_code=lambda email, code, purpose: None)
    with TestClient(app) as client:
        response = client.post("/api/auth/register", content=(
            b'{"username":"alice","email":"alice@example.test","password":"\\ud800aaaaaaaaaaa",'
            b'"invitation_code":"invite-secret-for-tests"}'),
            headers={"Content-Type": "application/json"})
        assert response.status_code == 400
