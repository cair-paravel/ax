from __future__ import annotations

import io
import shutil
import tarfile
import tempfile

from fastapi import HTTPException, UploadFile

from runner.apps import existing_or_allocate_port, write_app_metadata, write_current_symlink
from runner.archive import safe_extract
from runner.caddy import reconcile_caddy
from runner.commands import run, systemctl
from runner.config import APPS_ROOT, CADDY_APPS_DIR, UV_CACHE_DIR, UV_BIN
from runner.ingress import effective_domains
from runner.models import DeployConfig
from runner.systemd import unit_name, write_systemd_unit


async def deploy_source(cfg: DeployConfig, source: UploadFile, name: str) -> str:
    if cfg.runtime.backend != "process":
        raise HTTPException(status_code=400, detail="Only runtime.backend='process' is supported")

    APPS_ROOT.mkdir(parents=True, exist_ok=True)
    CADDY_APPS_DIR.mkdir(parents=True, exist_ok=True)
    UV_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    app_dir = APPS_ROOT / name
    releases_dir = app_dir / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    build_id = next(tempfile._get_candidate_names())
    release_dir = releases_dir / build_id
    src_dir = release_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    raw = await source.read()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            safe_extract(tf, src_dir)
    except Exception as e:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Invalid tarball: {e}") from e

    if not (src_dir / "pyproject.toml").exists():
        shutil.rmtree(release_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Missing pyproject.toml at repo root")

    port = existing_or_allocate_port(name)
    env = {**cfg.env, "PORT": str(port), "UV_CACHE_DIR": str(UV_CACHE_DIR), "UV_PROJECT_ENVIRONMENT": str(src_dir / ".venv")}

    sync_cmd = [UV_BIN, "sync", "--project", str(src_dir)]
    if (src_dir / "uv.lock").exists():
        sync_cmd.append("--frozen")
    if cfg.runtime.python:
        sync_cmd.extend(["--python", cfg.runtime.python])

    sync = run(sync_cmd, env=env, check=False, timeout=1200)
    if sync.returncode != 0:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="uv sync failed:\n" + (sync.stdout + sync.stderr).strip())

    write_current_symlink(app_dir, release_dir)
    write_app_metadata(app_dir, cfg, build_id, port, release_dir)
    write_systemd_unit(name, cfg, src_dir, port)

    systemctl("daemon-reload")
    systemctl("enable", "--now", unit_name(name))
    systemctl("restart", unit_name(name))

    reconcile_caddy()

    domains, platform_path = effective_domains(cfg)
    return "\n".join(
        [
            f"synced: {name}",
            f"service: {unit_name(name)}",
            f"release: {build_id}",
            f"port: {port}",
            f"domains: {', '.join(domains) if domains else '(none)'}",
            f"platform path: {platform_path or '(none)'}",
            "",
            "sync output:",
            (sync.stdout + sync.stderr).strip(),
        ]
    ).strip() + "\n"
