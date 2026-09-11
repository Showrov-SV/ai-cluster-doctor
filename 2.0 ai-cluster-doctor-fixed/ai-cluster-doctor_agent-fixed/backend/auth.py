"""
auth.py
JWT authentication + RBAC for the Host API, plus a separate lightweight
API-key scheme for Agents (Agents don't log in as users - they're
machines, not people, so a shared secret is the right-shaped credential).

Two independent credential types:
- User JWTs (POST /api/auth/login) protect the dashboard/API for human
  operators, with roles: "admin" (can rename/delete devices) and "viewer"
  (read-only).
- Agent API key - a single shared secret Agents send via the X-API-Key
  header on /api/agent/telemetry, so random devices on the LAN can't
  inject fake telemetry.

Both secrets (JWT signing key, agent API key) are auto-generated on first
run and persisted via db.py's settings table, so a fresh install needs
zero manual configuration - though both can be overridden via env vars
(JWT_SECRET, CLUSTER_DOCTOR_AGENT_API_KEY) for deployments that want to
pin them explicitly (e.g. multiple Host instances sharing one database).
"""

from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

import db

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECONDS = 24 * 60 * 60  # 24h

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _get_or_create_secret(setting_key: str, env_var: str) -> str:
    """Env var wins (for pinning across multiple Host instances);
    otherwise reuse/generate one via the settings table so it stays
    stable across restarts of a single Host."""
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val
    existing = db.get_setting(setting_key)
    if existing:
        return existing
    generated = secrets.token_hex(32)
    db.set_setting(setting_key, generated)
    return generated


def get_jwt_secret() -> str:
    return _get_or_create_secret("jwt_secret", "JWT_SECRET")


def get_agent_api_key() -> str:
    return _get_or_create_secret("agent_api_key", "CLUSTER_DOCTOR_AGENT_API_KEY")


# ---- password hashing (pbkdf2-hmac-sha256; no extra native/C-extension deps) ----

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, digest_hex = stored_hash.split(":")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(candidate.hex(), digest_hex)


# ---- JWT ----

def create_access_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + ACCESS_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        ) from exc


async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_access_token(token)
    return {"username": payload["sub"], "role": payload["role"]}


def require_role(*allowed_roles: str):
    """Dependency factory: Depends(require_role("admin"))"""

    async def _dependency(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _dependency


async def verify_agent_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, get_agent_api_key()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing agent API key")
