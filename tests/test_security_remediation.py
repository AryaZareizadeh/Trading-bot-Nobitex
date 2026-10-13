"""Offline regressions for recovery workload and exchange ownership."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi.testclient import TestClient

KEY = "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8="


def account_store(tmp_path):
    from accounts import Accounts
    store = Accounts(tmp_path / "accounts.sqlite3", KEY)
    user = store.register("alice", "alice@example.test", "old password long enough", "invite", "invite")
    assert store.verify_email(user["email"], store.issue_verification(user["id"]))
    return store, user


@pytest.mark.parametrize("state", ["missing", "expired", "exhausted", "wrong"])
def test_unusable_recovery_codes_do_not_hash(tmp_path, monkeypatch, state):
    import accounts
    store, user = account_store(tmp_path)
    code = "12345678"
    if state != "missing":
        code = store.issue_recovery(user["id"])
        with store.connect() as conn:
            if state == "expired":
                conn.execute("UPDATE recovery SET expires_at=0")
            if state == "exhausted":
                conn.execute("UPDATE recovery SET attempts=5")
        if state == "wrong":
            code = "00000000" if code != "00000000" else "11111111"
    calls = []
    monkeypatch.setattr(accounts, "hash_password", lambda password: calls.append(password) or "unused")
    assert not store.reset_password(user["email"], code, "new password long enough")
    assert calls == []
    if state == "wrong":
        with store.connect() as conn:
            assert conn.execute("SELECT attempts FROM recovery").fetchone()[0] == 1


def test_recovery_code_is_consumed_once_under_concurrent_hashing(tmp_path, monkeypatch):
    import accounts
    store, user = account_store(tmp_path)
    code = store.issue_recovery(user["id"])
    barrier = threading.Barrier(2)
    real_hash = accounts.hash_password
    def simultaneous(password):
        barrier.wait(timeout=5)
        return real_hash(password)
    monkeypatch.setattr(accounts, "hash_password", simultaneous)
    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(store.reset_password, user["email"], code, password)
                   for password in ("first new password long enough", "second new password long enough")]
        assert sorted(f.result(timeout=10) for f in futures) == [False, True]


def multi_app(tmp_path, monkeypatch):
    from multiuser import create_multiuser_app
    monkeypatch.setenv("TRADER_MASTER_KEY", KEY)
    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-tests")
    sent = []
    app = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                              send_code=lambda *args: sent.append(args))
    return app, sent


def test_recovery_variants_share_account_limit_even_from_different_ips(tmp_path, monkeypatch):
    app, sent = multi_app(tmp_path, monkeypatch)
    store = app.state.accounts
    user = store.register("alice", "alice@example.test", "old password long enough", "invite", "invite")
    assert store.verify_email(user["email"], store.issue_verification(user["id"]))
    statuses = []
    with TestClient(app):
        for i, email in enumerate(["alice@example.test", " ALICE@example.test", "alice@example.test ", " alice@example.test "]):
            client = TestClient(app, client=(f"192.0.2.{i+1}", 1234))
            statuses.append(client.post("/api/auth/recovery/request", json={"email": email}).status_code)
    assert statuses == [200, 200, 200, 429]
    assert len(sent) == 3


def test_recovery_has_independent_ip_limit_across_email_addresses(tmp_path, monkeypatch):
    app, _ = multi_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        statuses = [client.post("/api/auth/recovery/request", json={"email": f"user{i}@example.test"}).status_code
                    for i in range(25)]
    assert 429 in statuses


def test_reset_endpoint_hashes_off_event_loop_with_bounded_admission(tmp_path, monkeypatch):
    import accounts
    import httpx
    app, _ = multi_app(tmp_path, monkeypatch)
    store = app.state.accounts
    user = store.register("alice", "alice@example.test", "old password long enough", "invite", "invite")
    assert store.verify_email(user["email"], store.issue_verification(user["id"]))
    code = store.issue_recovery(user["id"])
    entered = threading.Event()
    release = threading.Event()
    real_hash = accounts.hash_password
    def slow_hash(password):
        entered.set()
        release.wait(timeout=5)
        return real_hash(password)
    monkeypatch.setattr(accounts, "hash_password", slow_hash)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            pending = asyncio.create_task(client.post("/api/auth/recovery/reset", json={
                "email": user["email"], "code": code, "password": "new password long enough"}))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.005)
                started = time.monotonic()
                assert (await client.get("/api/auth/session")).status_code == 200
                assert time.monotonic() - started < .25
                assert not pending.done(), "Hashing blocked the event loop"
            finally:
                release.set()
                await pending
    asyncio.run(scenario())


def test_worker_capacity_stays_reserved_after_request_cancellation():
    from auth import BoundedAuthWork, AuthWorkBusy
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        release.wait(timeout=5)
    async def scenario():
        work = BoundedAuthWork(workers=1, maximum=1)
        task = asyncio.create_task(work.run(blocked))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(AuthWorkBusy):
                await work.run(lambda: None)
        finally:
            release.set()
            work.close()
    asyncio.run(scenario())


def test_verified_exchange_owner_is_unique_and_replacement_is_atomic(tmp_path):
    store, alice = account_store(tmp_path)
    bob = store.register("bob", "bob@example.test", "bob password long enough", "invite", "invite")
    store.set_credentials(alice["id"], {"token": "alice-original", "testnet": False, "account_id": "legacy"}, verified_exchange_id="42")
    store.set_credentials(bob["id"], {"token": "bob-original", "testnet": False}, verified_exchange_id="43")
    with pytest.raises(ValueError, match="already assigned"):
        store.set_credentials(bob["id"], {"token": "bob-replacement", "testnet": False}, verified_exchange_id={"id": 42})
    assert store.credentials(bob["id"])["token"] == "bob-original"
    store.set_credentials(alice["id"], {"token": "alice-rotated", "testnet": False}, verified_exchange_id="42")
    assert store.credentials(alice["id"])["account_id"] == "legacy"
    store.set_credentials(bob["id"], {"token": "testnet-key", "testnet": True}, verified_exchange_id="42")
    assert store.credentials(bob["id"])["testnet"] is True


def test_legacy_credential_fingerprint_prevents_duplicate_ownership(tmp_path):
    store, alice = account_store(tmp_path)
    bob = store.register("bob", "bob@example.test", "bob password long enough", "invite", "invite")
    store.set_credentials(alice["id"], {"token": "shared-token", "testnet": False})
    with pytest.raises(ValueError, match="already assigned"):
        store.set_credentials(bob["id"], {"token": "shared-token", "testnet": False})
    assert store.credentials(bob["id"]) == {}


def test_concurrent_identity_claims_have_exactly_one_owner(tmp_path):
    store, alice = account_store(tmp_path)
    bob = store.register("bob", "bob@example.test", "bob password long enough", "invite", "invite")
    def claim(user):
        try:
            store.set_credentials(user["id"], {"token": user["id"], "testnet": False}, verified_exchange_id="shared-id")
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(claim, [alice, bob])) == [False, True]


def test_profile_identity_requires_verified_stable_national_code(tmp_path):
    from accounts import Accounts
    store = Accounts(tmp_path / "accounts.sqlite3", KEY)
    profile = {"nationalCode": "0123456789", "verifications": {"identity": True}, "email": "old@example.test"}
    identity = store.profile_identity(profile)
    assert "0123456789" not in identity
    assert identity == store.profile_identity({**profile, "nationalCode": "۰۱۲۳۴۵۶۷۸۹", "email": "new@example.test"})
    assert identity != store.profile_identity({**profile, "nationalCode": "1234567890"})
    assert store.profile_identity({"id": 42}) == "42"
    for invalid in ({}, {"nationalCode": "0123456789"}, {**profile, "verifications": {"identity": False}}, {**profile, "nationalCode": "123"}):
        with pytest.raises(ValueError, match="identity"):
            store.profile_identity(invalid)


def test_multiuser_volume_lease_prevents_second_dashboard(tmp_path, monkeypatch):
    first, _ = multi_app(tmp_path, monkeypatch)
    second, _ = multi_app(tmp_path, monkeypatch)
    with TestClient(first) as client:
        assert client.get("/healthz").status_code == 200
        with pytest.raises(RuntimeError, match="Runtime is active"):
            with TestClient(second):
                pass
    with TestClient(second) as client:
        assert client.get("/healthz").json() == {"ok": True}
