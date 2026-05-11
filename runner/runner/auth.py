from __future__ import annotations

from fastapi import HTTPException, Request

from runner.config import RUNNER_TOKEN


def require_auth(request: Request) -> None:
    authorization = request.headers.get("authorization")
    if not RUNNER_TOKEN:
        raise HTTPException(status_code=500, detail="RUNNER_TOKEN not set")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != RUNNER_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
