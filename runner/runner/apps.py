from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from runner.config import APPS_ROOT, PORTS_END, PORTS_START
from runner.models import DeployConfig


def read_app_config(name: str) -> DeployConfig | None:
    p = APPS_ROOT / name / "config.json"
    if not p.exists():
        return None
    try:
        return DeployConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def read_app_meta(name: str) -> dict[str, Any]:
    p = APPS_ROOT / name / "meta.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def meta_port(meta: dict[str, Any]) -> int | None:
    try:
        return int(meta["port"])
    except (KeyError, TypeError, ValueError):
        return None


def write_current_symlink(app_dir: Path, release_dir: Path) -> None:
    current = app_dir / "current"
    tmp = app_dir / ".current.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(release_dir)
    tmp.replace(current)


def write_app_metadata(app_dir: Path, cfg: DeployConfig, build_id: str, port: int, release_dir: Path) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "last_deploy.txt").write_text(build_id, encoding="utf-8")
    (app_dir / "config.json").write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    (app_dir / "meta.json").write_text(
        json.dumps({"last_deploy": build_id, "port": port, "release_dir": str(release_dir)}, indent=2) + "\n",
        encoding="utf-8",
    )


def existing_or_allocate_port(name: str) -> int:
    existing = meta_port(read_app_meta(name))
    if existing is not None and _port_available(existing, allow_in_use=True):
        return existing

    used = set()
    if APPS_ROOT.exists():
        for app_dir in APPS_ROOT.iterdir():
            if app_dir.is_dir():
                port = meta_port(read_app_meta(app_dir.name))
                if port is not None:
                    used.add(port)

    for port in range(PORTS_START, PORTS_END + 1):
        if port not in used and _port_available(port):
            return port
    raise HTTPException(status_code=500, detail=f"No free ports in {PORTS_START}-{PORTS_END}")


def _port_available(port: int, *, allow_in_use: bool = False) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return allow_in_use
