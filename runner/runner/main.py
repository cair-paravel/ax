from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field


DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", "/var/lib/ax")).resolve()
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
CADDY_APPS_DIR = Path(os.environ.get("CADDY_APPS_DIR", "/etc/caddy/apps"))
CADDY_CONFIG = Path(os.environ.get("CADDY_CONFIG", "/etc/caddy/Caddyfile"))
SYSTEMD_DIR = Path(os.environ.get("SYSTEMD_DIR", "/etc/systemd/system"))
UV_CACHE_DIR = Path(os.environ.get("UV_CACHE_DIR", "/var/cache/ax/uv")).resolve()
PLATFORM_BASE_DOMAIN = os.environ.get("PLATFORM_BASE_DOMAIN", "").strip()
PLATFORM_ROUTES_DIRNAME = "platform-routes"

APPS_ROOT = DATA_DIR / "apps"
PORTS_START = int(os.environ.get("AX_PORTS_START", "41000"))
PORTS_END = int(os.environ.get("AX_PORTS_END", "49999"))


def _require_token(authorization: str | None) -> None:
    if not RUNNER_TOKEN:
        raise HTTPException(status_code=500, detail="RUNNER_TOKEN not set")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != RUNNER_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def require_auth(request: Request) -> None:
    _require_token(request.headers.get("authorization"))


def _sanitize_app_name(name: str) -> str:
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
        raise HTTPException(status_code=400, detail="Invalid app name (use a-z0-9 and hyphens, 2-63 chars)")
    return name


class RuntimeConfig(BaseModel):
    backend: Literal["process"] = "process"
    python: str | None = None
    memory: str | None = None
    cpu: str | None = None


class DeployConfig(BaseModel):
    name: str
    type: str = Field(default="web")
    start: str
    port: int | None = None
    domains: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    ingress: dict[str, Any] | None = None
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


class IngressSpec(BaseModel):
    mode: Literal["custom-domain", "platform-subdomain", "platform-path"] = "custom-domain"
    domains: list[str] = Field(default_factory=list)
    subdomain: str | None = None
    path: str | None = None


class AppSummary(BaseModel):
    name: str
    last_deploy: str | None = None
    type: str = "web"
    port: int | None = None
    domains: list[str] = Field(default_factory=list)
    platform_path: str | None = None
    running: bool | None = None


app = FastAPI(title="ax-runner", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/apps", response_model=list[AppSummary])
def list_apps(_: Annotated[None, Depends(require_auth)]) -> list[AppSummary]:
    if not APPS_ROOT.exists():
        return []
    out: list[AppSummary] = []
    for p in sorted(APPS_ROOT.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        cfg = _read_app_config(name)
        meta = _read_app_meta(name)
        last_deploy = meta.get("last_deploy")
        port = _meta_port(meta)
        out.append(
            AppSummary(
                name=name,
                last_deploy=last_deploy,
                type=cfg.type if cfg else "web",
                port=port or (cfg.port if cfg else None),
                domains=_effective_domains(cfg)[0] if cfg else [],
                platform_path=_effective_domains(cfg)[1] if cfg else None,
                running=_service_running(name),
            )
        )
    return out


@app.get("/v1/apps/{name}/logs", response_class=PlainTextResponse)
def app_logs(
    name: str,
    _: Annotated[None, Depends(require_auth)],
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> str:
    name = _sanitize_app_name(name)
    if not _unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found")
    res = _run(["journalctl", "-u", _unit_name(name), "-n", str(tail), "--no-pager", "--output=cat"], check=False)
    if res.returncode not in (0, 1):
        raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout.strip())
    return res.stdout


@app.delete("/v1/apps/{name}", response_class=PlainTextResponse)
def delete_app(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = _sanitize_app_name(name)
    if _unit_exists(name):
        _systemctl("disable", "--now", _unit_name(name), check=False)
    unit = _unit_path(name)
    if unit.exists():
        unit.unlink()
        _systemctl("daemon-reload", check=False)

    app_dir = APPS_ROOT / name
    if app_dir.exists():
        shutil.rmtree(app_dir)

    p = CADDY_APPS_DIR / f"app-{name}.caddy"
    if p.exists():
        p.unlink()

    _reconcile_caddy()
    return f"removed: {name}\n"


@app.post("/v1/apps/{name}/stop", response_class=PlainTextResponse)
def app_stop(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = _sanitize_app_name(name)
    if not _unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found")
    _systemctl("stop", _unit_name(name))
    return f"stopped: {name}\n"


@app.post("/v1/apps/{name}/start", response_class=PlainTextResponse)
def app_start(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = _sanitize_app_name(name)
    if not _unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found (deploy first)")
    if _service_running(name):
        return f"already running: {name}\n"
    _systemctl("start", _unit_name(name))
    return f"started: {name}\n"


@app.post("/v1/apps/{name}/restart", response_class=PlainTextResponse)
def app_restart(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = _sanitize_app_name(name)
    if not _unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found")
    _systemctl("restart", _unit_name(name))
    return f"restarted: {name}\n"


@app.post("/v1/deploy", response_class=PlainTextResponse)
async def deploy(
    _: Annotated[None, Depends(require_auth)],
    config_json: Annotated[str, Form(...)] = "",
    source: Annotated[UploadFile, File(...)] = None,  # type: ignore
) -> str:
    try:
        cfg = DeployConfig.model_validate_json(config_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid config_json: {e}") from e

    name = _sanitize_app_name(cfg.name)
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
            _safe_extract(tf, src_dir)
    except Exception as e:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Invalid tarball: {e}") from e

    pyproject = src_dir / "pyproject.toml"
    if not pyproject.exists():
        shutil.rmtree(release_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Missing pyproject.toml at repo root")

    port = _existing_or_allocate_port(name)
    env = {**cfg.env, "PORT": str(port), "UV_CACHE_DIR": str(UV_CACHE_DIR), "UV_PROJECT_ENVIRONMENT": str(src_dir / ".venv")}

    sync_cmd = ["uv", "sync", "--project", str(src_dir)]
    if (src_dir / "uv.lock").exists():
        sync_cmd.append("--frozen")
    if cfg.runtime.python:
        sync_cmd.extend(["--python", cfg.runtime.python])

    sync = _run(sync_cmd, env=env, check=False, timeout=1200)
    if sync.returncode != 0:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="uv sync failed:\n" + (sync.stdout + sync.stderr).strip())

    _write_current_symlink(app_dir, release_dir)
    _write_app_metadata(app_dir, cfg, build_id, port, release_dir)
    _write_systemd_unit(name, cfg, src_dir, port)

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", _unit_name(name))
    _systemctl("restart", _unit_name(name))

    _reconcile_caddy()

    return "\n".join(
        [
            f"synced: {name}",
            f"service: {_unit_name(name)}",
            f"release: {build_id}",
            f"port: {port}",
            f"domains: {', '.join(_effective_domains(cfg)[0]) if _effective_domains(cfg)[0] else '(none)'}",
            f"platform path: {_effective_domains(cfg)[1] or '(none)'}",
            "",
            "sync output:",
            (sync.stdout + sync.stderr).strip(),
        ]
    ).strip() + "\n"


def _run(
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


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", *args], check=check)


def _unit_name(name: str) -> str:
    return f"ax-{name}.service"


def _unit_path(name: str) -> Path:
    return SYSTEMD_DIR / _unit_name(name)


def _unit_exists(name: str) -> bool:
    return _unit_path(name).exists()


def _service_running(name: str) -> bool | None:
    if not _unit_exists(name):
        return False
    res = _systemctl("is-active", "--quiet", _unit_name(name), check=False)
    return res.returncode == 0


def _read_app_config(name: str) -> DeployConfig | None:
    p = APPS_ROOT / name / "config.json"
    if not p.exists():
        return None
    try:
        return DeployConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def _read_app_meta(name: str) -> dict[str, Any]:
    p = APPS_ROOT / name / "meta.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _meta_port(meta: dict[str, Any]) -> int | None:
    try:
        return int(meta["port"])
    except (KeyError, TypeError, ValueError):
        return None


def _write_current_symlink(app_dir: Path, release_dir: Path) -> None:
    current = app_dir / "current"
    tmp = app_dir / ".current.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(release_dir)
    tmp.replace(current)


def _write_app_metadata(app_dir: Path, cfg: DeployConfig, build_id: str, port: int, release_dir: Path) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "last_deploy.txt").write_text(build_id, encoding="utf-8")
    (app_dir / "config.json").write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    (app_dir / "meta.json").write_text(
        json.dumps({"last_deploy": build_id, "port": port, "release_dir": str(release_dir)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _existing_or_allocate_port(name: str) -> int:
    existing = _meta_port(_read_app_meta(name))
    if existing is not None and _port_available(existing, allow_in_use=True):
        return existing

    used = set()
    if APPS_ROOT.exists():
        for app_dir in APPS_ROOT.iterdir():
            if app_dir.is_dir():
                port = _meta_port(_read_app_meta(app_dir.name))
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


def _write_systemd_unit(name: str, cfg: DeployConfig, src_dir: Path, port: int) -> None:
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    env_lines = [_systemd_env(str(key), str(value)) for key, value in sorted(cfg.env.items())]
    env_lines.extend(
        [
            _systemd_env("PORT", str(port)),
            _systemd_env("UV_CACHE_DIR", str(UV_CACHE_DIR)),
            _systemd_env("UV_PROJECT_ENVIRONMENT", str(src_dir / ".venv")),
            _systemd_env("PATH", f"{src_dir / '.venv' / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        ]
    )

    resource_lines: list[str] = []
    if cfg.runtime.memory:
        resource_lines.append(f"MemoryMax={cfg.runtime.memory}")
    if cfg.runtime.cpu:
        resource_lines.append(f"CPUQuota={_cpu_quota(cfg.runtime.cpu)}")

    _unit_path(name).write_text(
        "\n".join(
            [
                "[Unit]",
                f"Description=ax app {name}",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={src_dir}",
                *env_lines,
                f"ExecStart=/bin/bash -lc {json.dumps('exec ' + cfg.start)}",
                "Restart=always",
                "RestartSec=2",
                "NoNewPrivileges=yes",
                "PrivateTmp=yes",
                "ProtectHome=yes",
                "ProtectSystem=full",
                *resource_lines,
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _systemd_env(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{key}={escaped}"'


def _cpu_quota(value: str) -> str:
    v = value.strip()
    if v.endswith("%"):
        return v
    try:
        cores = float(v)
    except ValueError:
        return v
    return f"{int(cores * 100)}%"


def _caddy_site(domains: list[str], port: int) -> str:
    doms = " ".join(domains)
    return f"""{doms} {{
  handle /_ax/health {{
    respond "ok" 200
  }}
  reverse_proxy 127.0.0.1:{port}
}}
"""


def _reload_caddy() -> None:
    validate = _run(["caddy", "validate", "--config", str(CADDY_CONFIG), "--adapter", "caddyfile"], check=False)
    if validate.returncode != 0:
        raise HTTPException(status_code=500, detail="caddy validate failed:\n" + (validate.stdout + validate.stderr).strip())
    reload_ = _run(["caddy", "reload", "--config", str(CADDY_CONFIG), "--adapter", "caddyfile"], check=False)
    if reload_.returncode != 0:
        restart = _systemctl("reload", "caddy", check=False)
        if restart.returncode != 0:
            raise HTTPException(status_code=500, detail="caddy reload failed:\n" + (reload_.stdout + reload_.stderr + restart.stdout + restart.stderr).strip())


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)


def _safe_extract(tf: tarfile.TarFile, dest_dir: Path) -> None:
    dest_dir = dest_dir.resolve()
    for member in tf.getmembers():
        member_path = (dest_dir / member.name).resolve()
        if not str(member_path).startswith(str(dest_dir) + os.sep):
            raise ValueError(f"Blocked path traversal in tar entry: {member.name}")
        if member.issym() or member.islnk():
            link_path = (member_path.parent / member.linkname).resolve()
            if not str(link_path).startswith(str(dest_dir) + os.sep):
                raise ValueError(f"Blocked unsafe link in tar entry: {member.name}")
    tf.extractall(path=dest_dir)


def _effective_ingress(cfg: DeployConfig) -> IngressSpec:
    if cfg.ingress is not None:
        return IngressSpec.model_validate(cfg.ingress)
    return IngressSpec(mode="custom-domain", domains=list(cfg.domains))


def _effective_domains(cfg: DeployConfig | None) -> tuple[list[str], str | None]:
    if cfg is None:
        return ([], None)
    ing = _effective_ingress(cfg)
    if ing.mode == "custom-domain":
        return (list(ing.domains), None)
    if ing.mode == "platform-subdomain":
        if not PLATFORM_BASE_DOMAIN:
            raise HTTPException(status_code=500, detail="PLATFORM_BASE_DOMAIN not set for platform-subdomain ingress")
        if not ing.subdomain:
            raise HTTPException(status_code=400, detail="ingress.subdomain required for platform-subdomain")
        return ([f"{ing.subdomain}.{PLATFORM_BASE_DOMAIN}"], None)
    if ing.mode == "platform-path":
        if not PLATFORM_BASE_DOMAIN:
            raise HTTPException(status_code=500, detail="PLATFORM_BASE_DOMAIN not set for platform-path ingress")
        if not ing.path or not ing.path.startswith("/"):
            raise HTTPException(status_code=400, detail="ingress.path must start with '/' for platform-path")
        return ([PLATFORM_BASE_DOMAIN], ing.path.rstrip("/"))
    return ([], None)


def _reconcile_caddy() -> None:
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
            cfg = _read_app_config(app_dir.name)
            port = _meta_port(_read_app_meta(app_dir.name))
            if cfg is None or port is None:
                continue
            domains, path = _effective_domains(cfg)
            ing = _effective_ingress(cfg)
            if ing.mode in ("custom-domain", "platform-subdomain"):
                if domains:
                    (CADDY_APPS_DIR / f"app-{app_dir.name}.caddy").write_text(_caddy_site(domains, port), encoding="utf-8")
            elif ing.mode == "platform-path" and path:
                platform_routes.append((app_dir.name, path, port))

    for app_name, path, port in sorted(platform_routes, key=lambda x: x[1]):
        (platform_routes_dir / f"{app_name}.caddy").write_text(
            "\n".join(
                [
                    f"handle {path}/_ax/health {{",
                    '  respond "ok" 200',
                    "}",
                    f"handle_path {path}/* {{",
                    f"  reverse_proxy 127.0.0.1:{port}",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    _reload_caddy()


if __name__ == "__main__":
    main()
