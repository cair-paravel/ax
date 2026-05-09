from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field


DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", "/data")).resolve()
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
CADDY_CONTAINER_NAME = os.environ.get("CADDY_CONTAINER_NAME", "agentx-caddy")
CADDY_APPS_DIR = Path(os.environ.get("CADDY_APPS_DIR", "/etc/caddy/apps"))

AGENTX_NETWORK = os.environ.get("AGENTX_NETWORK", "agentx")
APPS_ROOT = DATA_DIR / "apps"
BUILDS_ROOT = DATA_DIR / "builds"


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


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n{p.stdout}")
    return p.stdout


def _sanitize_app_name(name: str) -> str:
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
        raise HTTPException(status_code=400, detail="Invalid app name (use a-z0-9 and hyphens, 2-63 chars)")
    return name


class DeployConfig(BaseModel):
    name: str
    type: str = Field(default="web")  # web|worker|job (MVP treats all as container)
    start: str
    port: int | None = None
    domains: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class AppSummary(BaseModel):
    name: str
    last_deploy: str | None = None
    type: str = "web"
    port: int | None = None
    domains: list[str] = Field(default_factory=list)


app = FastAPI(title="agentx-runner", version="0.1.0")


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
        last_deploy = (p / "last_deploy.txt").read_text(encoding="utf-8").strip() if (p / "last_deploy.txt").exists() else None
        out.append(
            AppSummary(
                name=name,
                last_deploy=last_deploy,
                type=cfg.type if cfg else "web",
                port=cfg.port if cfg else None,
                domains=cfg.domains if cfg else [],
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
    container = f"ax-{name}"
    if not _container_exists(container):
        raise HTTPException(status_code=404, detail="Container not found")
    try:
        return _run(["docker", "logs", "--tail", str(tail), container])
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


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
    APPS_ROOT.mkdir(parents=True, exist_ok=True)
    BUILDS_ROOT.mkdir(parents=True, exist_ok=True)
    CADDY_APPS_DIR.mkdir(parents=True, exist_ok=True)

    app_dir = APPS_ROOT / name
    app_dir.mkdir(parents=True, exist_ok=True)

    build_id = next(tempfile._get_candidate_names())
    build_dir = BUILDS_ROOT / f"{name}-{build_id}"
    build_dir.mkdir(parents=True, exist_ok=True)

    src_dir = build_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    raw = await source.read()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            _safe_extract(tf, src_dir)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid tarball: {e}") from e

    pyproject = src_dir / "pyproject.toml"
    if not pyproject.exists():
        raise HTTPException(status_code=400, detail="Missing pyproject.toml at repo root (MVP expects repo-root pyproject)")

    dockerfile = build_dir / "Dockerfile"
    dockerfile.write_text(_dockerfile_template(cfg), encoding="utf-8")

    image_tag = f"agentx/{name}:{build_id}"

    # Build image
    build_out = _run(
        ["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(src_dir)],
        cwd=build_dir,
    )

    # Stop & remove existing container if present
    _run(["docker", "rm", "-f", f"ax-{name}"], cwd=build_dir) if _container_exists(f"ax-{name}") else None

    # Run container
    env_args: list[str] = []
    for k, v in sorted(cfg.env.items()):
        env_args += ["-e", f"{k}={v}"]

    # PORT mapping: internal only (Caddy reverse-proxy uses docker network, not host ports)
    internal_port = str(cfg.port or 8000)
    env_args += ["-e", f"PORT={internal_port}"]

    run_cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        f"ax-{name}",
        "--restart",
        "unless-stopped",
        "--network",
        AGENTX_NETWORK,
        *env_args,
        image_tag,
    ]
    run_out = _run(run_cmd, cwd=build_dir)

    # Write Caddy snippet if domains exist
    if cfg.domains:
        snippet_path = CADDY_APPS_DIR / f"{name}.caddy"
        snippet_path.write_text(_caddy_snippet(cfg.domains, f"ax-{name}", internal_port), encoding="utf-8")
        _reload_caddy()

    # Persist last deploy metadata
    (app_dir / "last_deploy.txt").write_text(build_id, encoding="utf-8")
    (app_dir / "config.json").write_text(cfg.model_dump_json(indent=2), encoding="utf-8")

    return "\n".join(
        [
            f"built: {image_tag}",
            f"container: ax-{name}",
            f"domains: {', '.join(cfg.domains) if cfg.domains else '(none)'}",
            "",
            "build output:",
            build_out.strip(),
            "",
            "run output:",
            run_out.strip(),
        ]
    ).strip() + "\n"


def _dockerfile_template(cfg: DeployConfig) -> str:
    # Simple buildpack-like Dockerfile. Uses uv inside container.
    # For MVP we keep it single-stage and assume runtime deps can compile.
    start = cfg.start.replace('"', '\\"')
    return f"""\
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    UV_SYSTEM_PYTHON=1 \\
    UV_CACHE_DIR=/uv-cache

RUN apt-get update && apt-get install -y --no-install-recommends \\
      ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app
COPY . /app

RUN if [ -f uv.lock ]; then uv sync --frozen; else uv sync; fi

EXPOSE {cfg.port or 8000}
CMD ["bash", "-lc", "{start}"]
"""


def _caddy_snippet(domains: list[str], container_name: str, port: str) -> str:
    doms = " ".join(domains)
    return f"""{doms} {{
  reverse_proxy {container_name}:{port}
}}
"""


def _container_exists(name: str) -> bool:
    p = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0


def _read_app_config(name: str) -> DeployConfig | None:
    p = (APPS_ROOT / name / "config.json")
    if not p.exists():
        return None
    try:
        return DeployConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def _reload_caddy() -> None:
    # Reload by exec'ing inside the Caddy container. Runner has docker socket access.
    _run(
        [
            "docker",
            "exec",
            CADDY_CONTAINER_NAME,
            "caddy",
            "reload",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ]
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)


def _safe_extract(tf: tarfile.TarFile, dest_dir: Path) -> None:
    dest_dir = dest_dir.resolve()
    for member in tf.getmembers():
        member_path = (dest_dir / member.name).resolve()
        if not str(member_path).startswith(str(dest_dir) + os.sep):
            raise ValueError(f"Blocked path traversal in tar entry: {member.name}")
    tf.extractall(path=dest_dir)

