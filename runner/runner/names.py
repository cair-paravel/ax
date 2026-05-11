from __future__ import annotations

import re

from fastapi import HTTPException


def sanitize_app_name(name: str) -> str:
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
        raise HTTPException(status_code=400, detail="Invalid app name (use a-z0-9 and hyphens, 2-63 chars)")
    return name
