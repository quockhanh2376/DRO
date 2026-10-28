"""Local single-admin login, password hashing, session, and CSRF helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.dependencies import get_session
from app.db.models import AdminCredentialRecord
from app.db.repositories import add_audit_event

auth_router = APIRouter(tags=["authentication"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "web" / "templates")

# Pre-computed dummy hash to ensure constant-time response for invalid usernames/passwords
_DUMMY_SALT = bytes.fromhex("00112233445566778899aabbccddeeff")
_DUMMY_DIGEST = hashlib.scrypt(b"timing_dummy_seed", salt=_DUMMY_SALT, n=2**15, r=8, p=1,
                               maxmem=64 * 1024 * 1024, dklen=32)
_DUMMY_HASH = f"scrypt${_DUMMY_SALT.hex()}${_DUMMY_DIGEST.hex()}"

_VERIFIED_CONFIGURED_CREDENTIAL: tuple[str, str, str] | None = None


def reset_auth_cache() -> None:
    global _VERIFIED_CONFIGURED_CREDENTIAL
    _VERIFIED_CONFIGURED_CREDENTIAL = None


class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: float = 60.0,
                 lockout_seconds: float = 900.0) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_rate_limited(self, ip: str, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        with self._lock:
            lockout_until = self._lockouts.get(ip)
            if lockout_until is not None:
                if current_time < lockout_until:
                    return True
                del self._lockouts[ip]
            return False

    def record_failure(self, ip: str, now: float | None = None) -> bool:
        """Record a failure. Returns True if now locked out."""
        current_time = time.time() if now is None else now
        with self._lock:
            window_start = current_time - self.window_seconds
            history = [t for t in self._attempts.get(ip, []) if t >= window_start]
            history.append(current_time)
            self._attempts[ip] = history
            if len(history) >= self.max_attempts:
                self._lockouts[ip] = current_time + self.lockout_seconds
                self._attempts.pop(ip, None)
                return True
            return False

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)
            self._lockouts.pop(ip, None)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()
            self._lockouts.clear()


login_rate_limiter = LoginRateLimiter()


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = _hash_password(password, bytes.fromhex(salt)).split("$")[-1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _csrf_token(request: Request) -> str:
    return request.session.setdefault("csrf", secrets.token_urlsafe(32))


def verify_csrf(request: Request, supplied: str | None) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(status_code=401, detail="Authentication required")


async def protect_mutation(request: Request) -> None:
    require_admin(request)
    token = request.headers.get("x-csrf-token")
    if token is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        token = str(form.get("csrf_token", ""))
    verify_csrf(request, token)


async def protect_destructive(request: Request) -> None:
    await protect_mutation(request)
    if request.headers.get("x-confirm-action") != "confirm":
        raise HTTPException(status_code=400, detail="Explicit action confirmation is required")


def verify_login(session: Session, username: str, password: str) -> bool:
    global _VERIFIED_CONFIGURED_CREDENTIAL
    configured_password = os.getenv("DRO_ADMIN_PASSWORD", "")
    configured_user = os.getenv("DRO_ADMIN_USER", "admin")
    has_valid_config = len(configured_password) >= 12
    user_matches = has_valid_config and hmac.compare_digest(username, configured_user)

    if not has_valid_config:
        _check_password(password, _DUMMY_HASH)
        return False

    credential = session.get(AdminCredentialRecord, 1)
    if credential is None:
        credential = AdminCredentialRecord(id=1, username=configured_user,
                                           password_hash=_hash_password(configured_password))
        session.add(credential)
        session.flush()
        _VERIFIED_CONFIGURED_CREDENTIAL = (configured_user, configured_password, credential.password_hash)
    else:
        cached = _VERIFIED_CONFIGURED_CREDENTIAL
        if cached != (configured_user, configured_password, credential.password_hash):
            if (credential.username != configured_user
                    or not _check_password(configured_password, credential.password_hash)):
                credential.username = configured_user
                credential.password_hash = _hash_password(configured_password)
                session.flush()
            _VERIFIED_CONFIGURED_CREDENTIAL = (configured_user, configured_password, credential.password_hash)

    if user_matches and hmac.compare_digest(credential.username, username):
        return _check_password(password, credential.password_hash)

    _check_password(password, _DUMMY_HASH)
    return False


@auth_router.get("/login", response_class=HTMLResponse, name="login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "csrf_token": _csrf_token(request), "error": None,
    })


@auth_router.post("/login", response_class=HTMLResponse, name="login_submit")
async def login_submit(request: Request, session: Session = Depends(get_session)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if login_rate_limiter.is_rate_limited(client_ip):
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "csrf_token": _csrf_token(request),
            "error": "Too many failed login attempts. Please try again later.",
        }, status_code=429)

    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    username, password = str(form.get("username", "")), str(form.get("password", ""))

    valid = await run_in_threadpool(verify_login, session, username, password)
    if not valid:
        login_rate_limiter.record_failure(client_ip)
        add_audit_event(session, "login_failed", None, {"ip": client_ip, "username": username})
        session.commit()
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "csrf_token": _csrf_token(request),
            "error": "Invalid username or password",
        }, status_code=401)

    login_rate_limiter.record_success(client_ip)
    session.commit()
    request.session.clear()
    request.session["admin"] = username
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@auth_router.post("/logout", name="logout", dependencies=[Depends(protect_mutation)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
