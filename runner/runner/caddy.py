from __future__ import annotations

import shutil

from fastapi import HTTPException

from runner.apps import meta_port, read_app_config, read_app_meta
from runner.commands import run, systemctl
from runner.config import APPS_ROOT, CADDY_APPS_DIR, CADDY_CONFIG, PLATFORM_ROUTES_DIRNAME
from runner.ingress import effective_domains, effective_ingress


def reconcile_caddy() -> None:
    CADDY_APPS_DIR.mkdir(parents=True, exist_ok=True)
    for p in CADDY_APPS_DIR.glob("app-*.caddy"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    legacy = CADDY_APPS_DIR / "platform-path.caddy"
    if legacy.exists():
        legacy.unlink()
    platform_routes_dir = CADDY_APPS_DIR / PLATFORM_ROUTES_DIRNAME
    if platform_routes_dir.exists():
        shutil.rmtree(platform_routes_dir)
    platform_routes_dir.mkdir(parents=True, exist_ok=True)

    platform_routes: list[tuple[str, str, int]] = []

    if APPS_ROOT.exists():
        for app_dir in sorted(APPS_ROOT.iterdir()):
            if not app_dir.is_dir():
                continue
            cfg = read_app_config(app_dir.name)
            port = meta_port(read_app_meta(app_dir.name))
            if cfg is None or port is None:
                continue
            domains, path = effective_domains(cfg)
            ing = effective_ingress(cfg)
            if ing.mode in ("custom-domain", "platform-subdomain"):
                if domains:
                    (CADDY_APPS_DIR / f"app-{app_dir.name}.caddy").write_text(_caddy_site(domains, port), encoding="utf-8")
            elif ing.mode == "platform-path" and path:
                platform_routes.append((app_dir.name, path, port))

    for app_name, path, port in sorted(platform_routes, key=lambda x: x[1]):
        (platform_routes_dir / f"{app_name}.caddy").write_text(
            "\n".join(
                [
                    f"handle_path {path}/* {{",
                    f"  reverse_proxy 127.0.0.1:{port}",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    reload_caddy()


def _caddy_site(domains: list[str], port: int) -> str:
    doms = " ".join(domains)
    return f"""{doms} {{
  reverse_proxy 127.0.0.1:{port}
}}
"""


def reload_caddy() -> None:
    validate = run(["caddy", "validate", "--config", str(CADDY_CONFIG), "--adapter", "caddyfile"], check=False)
    if validate.returncode != 0:
        raise HTTPException(status_code=500, detail="caddy validate failed:\n" + (validate.stdout + validate.stderr).strip())
    reload_ = run(["caddy", "reload", "--config", str(CADDY_CONFIG), "--adapter", "caddyfile"], check=False)
    if reload_.returncode != 0:
        restart = systemctl("reload", "caddy", check=False)
        if restart.returncode != 0:
            raise HTTPException(status_code=500, detail="caddy reload failed:\n" + (reload_.stdout + reload_.stderr + restart.stdout + restart.stderr).strip())
