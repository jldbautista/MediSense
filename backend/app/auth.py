"""Login check for the dashboard and editor endpoints (Supabase Auth)."""

import os
import time
from functools import lru_cache

from fastapi import Header, HTTPException
from supabase import Client, create_client

from . import db  # noqa: F401  (loads backend/.env before the client is created)

CACHE_SECONDS = 60
_cache: dict[str, tuple[float, dict]] = {}


@lru_cache
def _auth_client() -> Client:
    """Separate client just for checking tokens, so the database client
    used by the endpoints is never touched by auth calls."""
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def require_user(authorization: str | None = Header(default=None)) -> dict:
    """Return {"id", "email"} for a valid token, else raise 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Not logged in")
    token = authorization.split(" ", 1)[1].strip()

    now = time.monotonic()
    hit = _cache.get(token)
    if hit and hit[0] > now:
        return hit[1]

    try:
        res = _auth_client().auth.get_user(token)
    except Exception as err:
        # Supabase answered "bad token" (has an HTTP status) -> 401.
        # No status means couldn't reach Supabase -> 503, not "logged out".
        if getattr(err, "status", None) is not None:
            raise _unauthorized("Session expired or invalid. Log in again.")
        raise HTTPException(status_code=503, detail=f"Could not check login: {err}")

    user = getattr(res, "user", None)
    if user is None:
        raise _unauthorized("Session expired or invalid. Log in again.")

    info = {"id": user.id, "email": user.email}
    if len(_cache) > 100:  # tiny cleanup; only one user in practice
        _cache.clear()
    _cache[token] = (now + CACHE_SECONDS, info)
    return info