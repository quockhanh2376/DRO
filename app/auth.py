"""Local single-admin login, password hashing, session, and CSRF helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.dependencies import get_session
from app.db.models import AdminCredentialRecord

auth_router = APIRouter(tags=["authentication"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "web" / "templates")


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
    configured_password = os.getenv("DRO_ADMIN_PASSWORD", "")
    configured_user = os.getenv("DRO_ADMIN_USER", "admin")
    if len(configured_password) < 12 or not hmac.compare_digest(username, configured_user):
        return False
    credential = session.get(AdminCredentialRecord, 1)
    if credential is None:
        credential = AdminCredentialRecord(id=1, username=configured_user,
                                           password_hash=_hash_password(configured_password))
        session.add(credential)
        session.flush()
    elif (credential.username != configured_user
          or not _check_password(configured_password, credential.password_hash)):
        credential.username = configured_user
        credential.password_hash = _hash_password(configured_password)
        session.flush()
    return hmac.compare_digest(credential.username, username) and _check_password(
        password, credential.password_hash)


@auth_router.get("/login", response_class=HTMLResponse, name="login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "csrf_token": _csrf_token(request), "error": None,
    })


@auth_router.post("/login", response_class=HTMLResponse, name="login_submit")
async def login_submit(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token", "")))
    username, password = str(form.get("username", "")), str(form.get("password", ""))
    if not verify_login(session, username, password):
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "csrf_token": _csrf_token(request),
            "error": "Invalid username or password",
        }, status_code=401)
    request.session.clear()
    request.session["admin"] = username
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@auth_router.post("/logout", name="logout", dependencies=[Depends(protect_mutation)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
