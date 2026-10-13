"""Invitation-only multi-user entry point for the trading dashboard."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection

from accounts import Accounts, canonical_email
from auth import COOKIE, SAFE_METHODS, BoundedAuthWork, AuthWorkBusy
from settings import ROOT, data_dir, load_environment
from runtime_lease import FileLease


def _send_code(email: str, code: str, purpose: str):
    host = os.getenv("SMTP_HOST", "")
    sender = os.getenv("SMTP_FROM", "")
    if not host or not sender:
        raise RuntimeError("SMTP is not configured")
    port = int(os.getenv("SMTP_PORT", "587"))
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = "Trading dashboard email code"
    message.set_content(f"Your {purpose} code is {code}. It expires in 10 minutes. If you did not request this, ignore this email.")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as client:
            if os.getenv("SMTP_USERNAME"):
                client.login(os.environ["SMTP_USERNAME"], os.environ.get("SMTP_PASSWORD", ""))
            client.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as client:
            client.starttls(context=ssl.create_default_context())
            if os.getenv("SMTP_USERNAME"):
                client.login(os.environ["SMTP_USERNAME"], os.environ.get("SMTP_PASSWORD", ""))
            client.send_message(message)


@dataclass
class UserSession:
    user_id: str
    username: str
    csrf_token: str
    expires: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class SessionRegistry:
    def __init__(self, lifetime: float):
        if not math.isfinite(lifetime) or lifetime <= 0:
            raise ValueError("DASHBOARD_SESSION_SECONDS must be finite and positive")
        self.lifetime = lifetime
        self.sessions = {}
        self.attempts = {}

    def get(self, token):
        session = self.sessions.get(token)
        if session and (session.expires <= time.monotonic() or session.revoked.is_set()):
            self.sessions.pop(token, None)
            session.revoked.set()
            return None
        return session

    def create(self, user):
        for existing in list(self.sessions):
            self.get(existing)
        token = secrets.token_urlsafe(32)
        session = UserSession(user["id"], user["username"], secrets.token_urlsafe(32),
                              time.monotonic() + self.lifetime)
        self.sessions[token] = session
        return token, session

    def revoke_user(self, user_id):
        for token, session in list(self.sessions.items()):
            if session.user_id == user_id:
                self.sessions.pop(token)
                session.revoked.set()

    def limited(self, key, max_attempts=5, period=60):
        now = time.monotonic()
        bucket = self.attempts.setdefault(key, deque())
        while bucket and bucket[0] <= now - period:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            return True
        bucket.append(now)
        if len(self.attempts) > 10000:
            self.attempts = {name: values for name, values in self.attempts.items()
                             if values and values[-1] > now - 3600}
        return False


class AccountCredentials:
    def __init__(self, accounts, user_id):
        self.accounts, self.user_id = accounts, user_id

    def get(self):
        return self.accounts.credentials(self.user_id)

    def profile_identity(self, profile):
        return self.accounts.profile_identity(profile)

    def set(self, values, verified_exchange_id=None):
        self.accounts.set_credentials(self.user_id, values, verified_exchange_id=verified_exchange_id)


class OptimizerSlots:
    def __init__(self, maximum):
        self.maximum = max(1, int(maximum))
        self.active = set()

    def try_start(self, user_id):
        if user_id in self.active or len(self.active) >= self.maximum:
            return False
        self.active.add(user_id)
        return True

    def finish(self, user_id):
        self.active.discard(user_id)


class UserOptimizerSlots:
    def __init__(self, slots, user_id):
        self.slots, self.user_id = slots, user_id

    def try_start(self):
        return self.slots.try_start(self.user_id)

    def finish(self):
        self.slots.finish(self.user_id)


class TenantRegistry:
    def __init__(self, accounts, root, offline):
        self.accounts, self.root, self.offline = accounts, Path(root), offline
        self.tenants = {}
        self.lock = asyncio.Lock()
        self.optimizer_slots = OptimizerSlots(os.getenv("TRADER_MAX_OPTIMIZER_JOBS", "2"))

    async def get(self, user_id):
        if user_id in self.tenants:
            return self.tenants[user_id][0]
        async with self.lock:
            if user_id in self.tenants:
                return self.tenants[user_id][0]
            from storage import Store
            from bot_manager import BotManager
            from server import create_app
            directory = self.root / "users" / user_id
            credentials = AccountCredentials(self.accounts, user_id)
            manager = BotManager(store=Store(directory / "trader.sqlite3"), legacy_dir=directory,
                                 account_id=credentials.get().get("account_id", user_id),
                                 testnet_factory=lambda: bool(credentials.get().get("testnet")))
            app = create_app(manager=manager, offline=self.offline, credential_store=credentials,
                             security=False,
                             optimizer_slots=UserOptimizerSlots(self.optimizer_slots, user_id))
            context = app.router.lifespan_context(app)
            await context.__aenter__()
            self.tenants[user_id] = (app, context)
            return app

    async def shutdown(self):
        for app, context in list(self.tenants.values()):
            await context.__aexit__(None, None, None)
        self.tenants.clear()


class TenantDispatch:
    def __init__(self, registry):
        self.registry = registry

    async def __call__(self, scope, receive, send):
        user_id = scope.get("user_id")
        if not user_id:
            await JSONResponse({"error": "Authentication required"}, status_code=401)(scope, receive, send)
            return
        tenant = await self.registry.get(user_id)
        await tenant(scope, receive, send)


class MultiAuthMiddleware:
    PUBLIC = {"/api/auth/session", "/api/auth/login", "/api/auth/register",
              "/api/auth/register/verify", "/api/auth/recovery/request",
              "/api/auth/recovery/reset"}

    def __init__(self, app, sessions, allowed_hosts, secure_cookie):
        self.app, self.sessions = app, sessions
        self.allowed_hosts, self.secure_cookie = allowed_hosts, secure_cookie

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        socket = scope["type"] == "websocket"

        async def reject(status, message):
            if socket:
                await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            else:
                await JSONResponse({"error": message}, status_code=status,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)

        try:
            host = conn.headers.get("host", "")
            authority = urlsplit("//" + host)
            if (len(conn.headers.getlist("host")) != 1 or authority.hostname not in self.allowed_hosts
                    or authority.username or authority.password or authority.path):
                return await reject(400, "Invalid host")
            origin = conn.headers.get("origin")
            scheme = "https" if self.secure_cookie else "http"
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme != scheme or parsed.hostname != authority.hostname
                        or (parsed.port or (443 if scheme == "https" else 80)) !=
                        (authority.port or (443 if scheme == "https" else 80))
                        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
                    return await reject(403, "Invalid origin")
            elif socket:
                return await reject(403, "Origin required")
        except ValueError:
            return await reject(400, "Invalid host or origin")

        path = scope["path"]
        protected = path.startswith("/api/") or socket or path in {"/docs", "/redoc", "/openapi.json"}
        session = self.sessions.get(conn.cookies.get(COOKIE))
        if protected and path not in self.PUBLIC:
            if not session:
                return await reject(401, "Authentication required")
            if not socket and scope["method"] not in SAFE_METHODS:
                csrf = conn.headers.get("x-csrf-token", "")
                try:
                    valid = hmac.compare_digest(csrf.encode("ascii"), session.csrf_token.encode("ascii"))
                except UnicodeError:
                    valid = False
                if not valid:
                    return await reject(403, "Invalid CSRF token")
        if session:
            scope["user_id"] = session.user_id
            scope["user_name"] = session.username
        async def guarded_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                                (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")])
                message = {**message, "headers": headers}
            await send(message)

        if socket and session:
            async def close_on_expiry():
                try:
                    await asyncio.wait_for(session.revoked.wait(),
                                           timeout=max(0, session.expires - time.monotonic()))
                except TimeoutError:
                    session.revoked.set()
                try:
                    await send({"type": "websocket.close", "code": 4401})
                except RuntimeError:
                    pass
            watcher = asyncio.create_task(close_on_expiry())
            try:
                return await self.app(scope, receive, send)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        await self.app(scope, receive, guarded_send)


async def _payload(request):
    body = await request.body()
    if len(body) > 8192:
        return None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def create_multiuser_app(*, data_root=None, offline=None, allowed_hosts=None, send_code=None):
    load_environment()
    root = Path(data_root) if data_root is not None else data_dir()
    master_key = os.getenv("TRADER_MASTER_KEY", "")
    if not master_key:
        raise RuntimeError("TRADER_MASTER_KEY is required for multi-user startup")
    if len(os.getenv("TRADER_INVITE_CODE", "")) < 16:
        raise RuntimeError("TRADER_INVITE_CODE must contain at least 16 characters")
    if send_code is None and (not os.getenv("SMTP_HOST") or not os.getenv("SMTP_FROM")):
        raise RuntimeError("SMTP_HOST and SMTP_FROM are required for multi-user startup")
    accounts = Accounts(root / "accounts.sqlite3", master_key)
    sessions = SessionRegistry(float(os.getenv("DASHBOARD_SESSION_SECONDS", "43200")))
    tenants = TenantRegistry(accounts, root, offline if offline is not None else
                             os.getenv("TRADER_OFFLINE", "false").lower() == "true")
    secure_cookie = os.getenv("DASHBOARD_SECURE_COOKIE", "false").lower() == "true"
    hosts = allowed_hosts or {h.strip().lower() for h in
                              os.getenv("DASHBOARD_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",") if h.strip()}
    sender = send_code or _send_code
    auth_work = BoundedAuthWork()
    volume_lease = FileLease(root / ".dashboard.lock")

    @asynccontextmanager
    async def lifespan(app):
        volume_lease.acquire()
        try:
            yield
        finally:
            try:
                await tenants.shutdown()
            finally:
                auth_work.close()
                volume_lease.release()

    app = FastAPI(title="Trading Bot Dashboard", lifespan=lifespan)
    app.add_middleware(MultiAuthMiddleware, sessions=sessions, allowed_hosts=hosts, secure_cookie=secure_cookie)
    app.state.accounts = accounts
    app.state.sessions = sessions
    app.state.tenants = tenants
    app.state.auth_work = auth_work

    @app.exception_handler(AuthWorkBusy)
    async def auth_busy(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=503, headers={"Retry-After": "1"})
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def root_page():
        return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/auth/session")
    async def session_status(request: Request):
        session = sessions.get(request.cookies.get(COOKIE))
        return {"authenticated": bool(session), "setup_required": False,
                "multiuser": True,
                "username": session.username if session else None,
                "csrf_token": session.csrf_token if session else None}

    @app.post("/api/auth/register", status_code=201)
    async def register(request: Request):
        payload = await _payload(request)
        if payload is None:
            return JSONResponse({"error": "Invalid registration request"}, status_code=400)
        username = payload.get("username", "")
        if (sessions.limited("registration:" + str(request.client.host), 500, 3600)
                or sessions.limited("registration-name:" + str(username).lower(), 5, 3600)):
            return JSONResponse({"error": "Too many requests; try again later"}, status_code=429)
        try:
            user = await auth_work.run(accounts.register, payload.get("username", ""),
                                           payload.get("email", ""), payload.get("password", ""),
                                           payload.get("invitation_code", ""), os.getenv("TRADER_INVITE_CODE", ""))
        except PermissionError:
            return JSONResponse({"error": "Invalid invitation code"}, status_code=403)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        code = await auth_work.run(accounts.issue_verification, user["id"])
        try:
            await auth_work.run(sender, user["email"], code, "registration")
        except Exception:
            with accounts.connect() as conn:
                conn.execute("DELETE FROM users WHERE id=? AND verified=0", (user["id"],))
            return JSONResponse({"error": "Could not send confirmation email"}, status_code=503)
        return {"ok": True, "message": "Enter the confirmation code sent to your email"}

    @app.post("/api/auth/register/verify")
    async def verify_registration(request: Request):
        payload = await _payload(request)
        if not payload or not await auth_work.run(accounts.verify_email, canonical_email(payload.get("email", "")), payload.get("code", "")):
            return JSONResponse({"error": "Invalid or expired confirmation code"}, status_code=400)
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login(request: Request):
        payload = await _payload(request)
        username = payload.get("username", "") if payload else ""
        key = "login:" + str(request.client.host) + ":" + str(username).lower()
        if sessions.limited(key):
            return JSONResponse({"error": "Too many login attempts; try again shortly"}, status_code=429)
        user = await auth_work.run(accounts.login, username, payload.get("password", "") if payload else "")
        if not user:
            return JSONResponse({"error": "Authentication failed"}, status_code=401)
        old = sessions.sessions.pop(request.cookies.get(COOKIE), None)
        if old:
            old.revoked.set()
        token, session = sessions.create(user)
        response = JSONResponse({"authenticated": True, "username": user["username"],
                                 "csrf_token": session.csrf_token})
        response.set_cookie(COOKIE, token, httponly=True, secure=secure_cookie, samesite="strict",
                            max_age=max(1, int(sessions.lifetime)), path="/")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        session = sessions.sessions.pop(request.cookies.get(COOKIE), None)
        if session:
            session.revoked.set()
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", secure=secure_cookie, httponly=True, samesite="strict")
        return response

    @app.post("/api/auth/recovery/request")
    async def request_recovery(request: Request):
        payload = await _payload(request)
        email = canonical_email(payload.get("email", "") if payload else "")
        ip_limited = sessions.limited("recovery-ip:" + str(request.client.host), 20, 3600)
        account_limited = sessions.limited("recovery-email:" + email, 3, 3600)
        if ip_limited or account_limited:
            return JSONResponse({"error": "Too many requests; try again later"}, status_code=429)
        if send_code is None and (not os.getenv("SMTP_HOST") or not os.getenv("SMTP_FROM")):
            return JSONResponse({"error": "Recovery email service unavailable"}, status_code=503)
        user = await auth_work.run(accounts.by_email, email) if email else None
        if user and user["verified"]:
            code = await auth_work.run(accounts.issue_recovery, user["id"])
            try:
                await auth_work.run(sender, user["email"], code, "password recovery")
            except Exception:
                pass
        return {"ok": True, "message": "If the address is registered, a code has been sent"}

    @app.post("/api/auth/recovery/reset")
    async def reset_password(request: Request):
        payload = await _payload(request)
        if not payload:
            return JSONResponse({"error": "Invalid or expired recovery code"}, status_code=400)
        email = canonical_email(payload.get("email", ""))
        ip_limited = sessions.limited("reset-ip:" + str(request.client.host), 20, 60)
        account_limited = sessions.limited("reset-email:" + email, 5, 60)
        if ip_limited or account_limited:
            return JSONResponse({"error": "Too many requests; try again later"}, status_code=429)
        try:
            user = await auth_work.run(accounts.by_email, email)
            success = await auth_work.run(accounts.reset_password, email, payload.get("code", ""), payload.get("password", ""))
        except (ValueError, TypeError):
            success = False
            user = None
        if not success:
            return JSONResponse({"error": "Invalid or expired recovery code"}, status_code=400)
        sessions.revoke_user(user["id"])
        return {"ok": True}

    app.mount("/", TenantDispatch(tenants), name="tenant")
    return app


load_environment()
app = create_multiuser_app() if os.getenv("TRADER_MULTIUSER", "false").lower() == "true" else None
