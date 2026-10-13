"""Single-operator, restart-revoked sessions for the dashboard."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import math
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

COOKIE = "dashboard_session"
PUBLIC = {"/api/auth/login", "/api/auth/session"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthWorkBusy(RuntimeError):
    pass


class BoundedAuthWork:
    """Bound running and queued authentication work outside the live executor."""

    def __init__(self, workers=2, maximum=4):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dashboard-auth")
        self.maximum = maximum
        self.pending = 0

    async def run(self, function, *args):
        if self.pending >= self.maximum:
            raise AuthWorkBusy("Authentication service busy; try again shortly")
        self.pending += 1
        try:
            future = asyncio.get_running_loop().run_in_executor(self.executor, partial(function, *args))
        except BaseException:
            self.pending -= 1
            raise
        def finished(_):
            self.pending -= 1
        future.add_done_callback(finished)
        # A disconnected caller must not release the capacity of a still-running
        # hash, otherwise repeated cancellations could fill an unbounded queue.
        return await asyncio.shield(future)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        name, n, r, p, salt, expected = encoded.split("$")
        if (name, n, r, p) != ("scrypt", "16384", "8", "1"):
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt_bytes, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError, UnicodeError):
        return False


@dataclass
class Session:
    csrf_token: str
    expires: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class AuthState:
    def __init__(self, password_hash=None, allowed_hosts=None, secure_cookie=None,
                 session_seconds=None, login_attempts=5):
        self.password_hash = os.getenv("DASHBOARD_PASSWORD_HASH", "") if password_hash is None else password_hash
        self.allowed_hosts = allowed_hosts if allowed_hosts is not None else {
            h.strip().lower() for h in os.getenv("DASHBOARD_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",") if h.strip()
        }
        self.secure_cookie = (os.getenv("DASHBOARD_SECURE_COOKIE", "false").lower() == "true"
                              if secure_cookie is None else secure_cookie)
        self.session_seconds = (float(os.getenv("DASHBOARD_SESSION_SECONDS", "43200"))
                                if session_seconds is None else session_seconds)
        if not math.isfinite(self.session_seconds) or self.session_seconds <= 0:
            raise ValueError("DASHBOARD_SESSION_SECONDS must be finite and positive")
        self.login_attempts = login_attempts
        # Single operator: one global bucket also covers spoofed IPs and proxies.
        self.attempts = deque()
        self.sessions: dict[str, Session] = {}

    def session(self, token):
        session = self.sessions.get(token)
        if session and (session.expires <= time.monotonic() or session.revoked.is_set()):
            session.revoked.set()
            self.sessions.pop(token, None)
            return None
        return session

    def create_session(self):
        for token in list(self.sessions):
            self.session(token)
        while len(self.sessions) >= 20:
            self.sessions.pop(next(iter(self.sessions))).revoked.set()
        token = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(32), time.monotonic() + self.session_seconds)
        self.sessions[token] = session
        return token, session


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        state = scope["app"].state.auth
        conn = HTTPConnection(scope)
        socket = scope["type"] == "websocket"

        async def reject(status, message):
            if socket:
                await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            else:
                await JSONResponse({"error": message}, status_code=status,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)

        try:
            authority = urlsplit("//" + conn.headers.get("host", ""))
            if (len(conn.headers.getlist("host")) != 1 or authority.hostname not in state.allowed_hosts
                    or authority.username or authority.password or authority.path):
                return await reject(400, "Invalid host")
            origin = conn.headers.get("origin")
            expected_scheme = "https" if state.secure_cookie else "http"
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme != expected_scheme or parsed.hostname != authority.hostname
                        or (parsed.port or (443 if parsed.scheme == "https" else 80)) !=
                        (authority.port or (443 if expected_scheme == "https" else 80))
                        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
                    return await reject(403, "Invalid origin")
            elif socket:
                return await reject(403, "Origin required")
        except ValueError:
            return await reject(400, "Invalid host or origin")

        path = scope["path"]
        protected = path.startswith("/api/") or socket or path in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
        session = state.session(conn.cookies.get(COOKIE))
        if protected and path not in PUBLIC:
            if not session:
                return await reject(401, "Authentication required")
            if not socket and scope["method"] not in SAFE_METHODS:
                csrf = conn.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(csrf.encode(), session.csrf_token.encode()):
                    return await reject(403, "Invalid CSRF token")

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                                (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")])
                message = {**message, "headers": headers}
            await send(message)

        if not socket:
            return await self.app(scope, receive, guarded_send)
        worker = asyncio.create_task(self.app(scope, receive, send))
        revoked = asyncio.create_task(session.revoked.wait())
        try:
            done, _ = await asyncio.wait({worker, revoked},
                                        timeout=max(0, session.expires - time.monotonic()),
                                        return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                await worker
            else:
                session.revoked.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await send({"type": "websocket.close", "code": 4401})
        finally:
            worker.cancel()
            revoked.cancel()
            await asyncio.gather(worker, revoked, return_exceptions=True)


def install_auth(app: FastAPI, state: AuthState | None = None):
    app.state.auth = state or AuthState()
    app.add_middleware(AuthMiddleware)

    @app.exception_handler(RequestValidationError)
    async def safe_credential_validation(request: Request, exc: RequestValidationError):
        # Pydantic normally echoes invalid values, including credential payloads.
        if request.url.path == "/api/settings/credentials":
            return JSONResponse({"error": "Invalid credential request"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.get("/api/auth/session")
    async def session_status(request: Request):
        state = request.app.state.auth
        session = state.session(request.cookies.get(COOKIE))
        return {"authenticated": bool(session), "setup_required": not bool(state.password_hash),
                "csrf_token": session.csrf_token if session else None, "multiuser": False}

    @app.post("/api/auth/login")
    async def login(request: Request):
        state = request.app.state.auth
        now = time.monotonic()
        while state.attempts and state.attempts[0] <= now - 60:
            state.attempts.popleft()
        if len(state.attempts) >= state.login_attempts:
            return JSONResponse({"error": "Too many login attempts; try again shortly"}, 429,
                                headers={"Retry-After": "60"})
        state.attempts.append(now)
        # Parse manually so validation responses never echo password input.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"error": "Authentication failed"}, 401)
        try:
            import json
            payload = json.loads(body)
            password = payload.get("password", "") if isinstance(payload, dict) else ""
        except (ValueError, UnicodeError):
            password = ""
        if not state.password_hash or not await asyncio.to_thread(verify_password, password, state.password_hash):
            return JSONResponse({"error": "Authentication failed"}, 401)
        old = state.sessions.pop(request.cookies.get(COOKIE), None)
        if old:
            old.revoked.set()
        token, session = state.create_session()
        response = JSONResponse({"authenticated": True, "csrf_token": session.csrf_token})
        response.set_cookie(COOKIE, token, httponly=True, secure=state.secure_cookie, samesite="strict",
                            max_age=max(1, int(state.session_seconds)), path="/")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        session = request.app.state.auth.sessions.pop(request.cookies.get(COOKIE), None)
        if session:
            session.revoked.set()
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", secure=request.app.state.auth.secure_cookie,
                               httponly=True, samesite="strict")
        return response
