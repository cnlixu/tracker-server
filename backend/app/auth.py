"""Single-administrator authentication with signed session cookies."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from itsdangerous import BadData, URLSafeTimedSerializer

from .config import AuthSettings
from .schemas import AuthSessionResponse, LoginRequest


LOGGER = logging.getLogger(__name__)
SESSION_SALT = "tracker-admin-session-v1"
PASSWORD_HASHER = PasswordHasher()

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def get_auth_settings(request: Request) -> AuthSettings:
    """Return authentication settings loaded during application startup."""
    return request.app.state.auth_settings


AuthSettingsDependency = Annotated[AuthSettings, Depends(get_auth_settings)]


def verify_admin_password(password: str, password_hash: str) -> bool:
    """Verify an Argon2 password hash without exposing parsing failures."""
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def create_session_token(username: str, settings: AuthSettings) -> str:
    serializer = URLSafeTimedSerializer(settings.session_secret, salt=SESSION_SALT)
    return serializer.dumps({"username": username, "version": 1})


def read_session_token(token: str, settings: AuthSettings) -> str | None:
    serializer = URLSafeTimedSerializer(settings.session_secret, salt=SESSION_SALT)
    try:
        payload = serializer.loads(token, max_age=settings.session_max_age_seconds)
    except BadData:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    username = payload.get("username")
    if not isinstance(username, str):
        return None
    if not secrets.compare_digest(username, settings.admin_username):
        return None
    return username


def require_admin(
    request: Request,
    settings: AuthSettingsDependency,
) -> str:
    """Require a valid signed administrator session cookie."""
    token = request.cookies.get(settings.cookie_name)
    if token:
        username = read_session_token(token, settings)
        if username is not None:
            return username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
    )


AdminDependency = Annotated[str, Depends(require_admin)]


@router.post("/login", response_model=AuthSessionResponse)
async def login(
    credentials: LoginRequest,
    request: Request,
    response: Response,
    settings: AuthSettingsDependency,
) -> AuthSessionResponse:
    username_matches = secrets.compare_digest(
        credentials.username,
        settings.admin_username,
    )
    password_matches = verify_admin_password(
        credentials.password,
        settings.admin_password_hash,
    )
    if not (username_matches and password_matches):
        client = request.client.host if request.client else "unknown"
        LOGGER.warning("administrator login rejected client=%s", client)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        )

    response.set_cookie(
        key=settings.cookie_name,
        value=create_session_token(settings.admin_username, settings),
        max_age=settings.session_max_age_seconds,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return AuthSessionResponse(
        authenticated=True,
        username=settings.admin_username,
    )


@router.get("/session", response_model=AuthSessionResponse)
async def session(username: AdminDependency) -> AuthSessionResponse:
    return AuthSessionResponse(authenticated=True, username=username)


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_session(_username: AdminDependency) -> Response:
    """Small endpoint used by Nginx auth_request for protected static pages."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, settings: AuthSettingsDependency) -> Response:
    response.delete_cookie(
        key=settings.cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
