from __future__ import annotations

import os
import subprocess

from fastapi import HTTPException


def run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int | None = 120,
) -> subprocess.CompletedProcess[str]:
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, env=proc_env, timeout=timeout)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"Missing command: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise HTTPException(status_code=500, detail=f"Command timed out: {' '.join(cmd)}") from e
    if check and res.returncode != 0:
        raise HTTPException(status_code=500, detail=(res.stderr or res.stdout or f"Command failed: {' '.join(cmd)}").strip())
    return res


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["systemctl", *args], check=check)
