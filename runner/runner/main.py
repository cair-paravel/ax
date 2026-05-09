from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

import docker
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field


DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", "/data")).resolve()
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
CADDY_CONTAINER_NAME = os.environ.get("CADDY_CONTAINER_NAME", "agentx-caddy")
CADDY_APPS_DIR = Path(os.environ.get("CADDY_APPS_DIR", "/etc/caddy/apps"))
PLATFORM_BASE_DOMAIN = os.environ.get("PLATFORM_BASE_DOMAIN", "").strip()
PLATFORM_ROUTES_DIRNAME = "platform-routes"

AGENTX_NETWORK = os.environ.get("AGENTX_NETWORK", "agentx")
APPS_ROOT = DATA_DIR / "apps"
BUILDS_ROOT = DATA_DIR / "builds"

DOCKER = docker.from_env()

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


class DeployConfig(BaseModel):
    name: str
    type: str = Field(default="web")  # web|worker|job (MVP treats all as container)
    start: str
    port: int | None = None
    # Back-compat: older configs used top-level domains.
    domains: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    ingress: dict[str, Any] | None = None


class IngressSpec(BaseModel):
    mode: Literal["custom-domain", "platform-subdomain", "platform-path"] = "custom-domain"
    domains: list[str] = Field(default_factory=list)  # for custom-domain
    subdomain: str | None = None  # for platform-subdomain
    path: str | None = None  # for platform-path, like "/myapi"


class AppSummary(BaseModel):
    name: str
    last_deploy: str | None = None
    type: str = "web"
    port: int | None = None
    domains: list[str] = Field(default_factory=list)
    platform_path: str | None = None


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
                domains=_effective_domains(cfg)[0] if cfg else [],
                platform_path=_effective_domains(cfg)[1] if cfg else None,
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
        c = DOCKER.containers.get(container)
        out = c.logs(tail=tail)
        return out.decode("utf-8", errors="replace")
    except docker.errors.DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker error: {e}") from e


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

    # Docker requires the Dockerfile to be within the build context.
    dockerfile_name = "Dockerfile.agentx"
    dockerfile = src_dir / dockerfile_name
    dockerfile.write_text(_dockerfile_template(cfg), encoding="utf-8")

    image_tag = f"agentx/{name}:{build_id}"

    try:
        image, logs = DOCKER.images.build(
            path=str(src_dir),
            dockerfile=dockerfile_name,
            tag=image_tag,
            rm=True,
        )
        build_out = _format_build_logs(logs)
    except docker.errors.BuildError as e:
        raise HTTPException(status_code=500, detail=_format_build_error(e)) from e
    except docker.errors.DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker error: {e}") from e

    # Stop & remove existing container if present
    try:
        if _container_exists(f"ax-{name}"):
            DOCKER.containers.get(f"ax-{name}").remove(force=True)
    except docker.errors.DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker error: {e}") from e

    # Run container
    env_args: list[str] = []
    for k, v in sorted(cfg.env.items()):
        env_args += ["-e", f"{k}={v}"]

    # PORT mapping: internal only (Caddy reverse-proxy uses docker network, not host ports)
    internal_port = str(cfg.port or 8000)
    env_args += ["-e", f"PORT={internal_port}"]

    try:
        c = DOCKER.containers.run(
            image=image_tag,
            detach=True,
            name=f"ax-{name}",
            network=AGENTX_NETWORK,
            environment={**cfg.env, "PORT": internal_port},
            restart_policy={"Name": "unless-stopped"},
        )
        run_out = c.id
    except docker.errors.DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker error: {e}") from e

    # Persist last deploy metadata
    (app_dir / "last_deploy.txt").write_text(build_id, encoding="utf-8")
    (app_dir / "config.json").write_text(cfg.model_dump_json(indent=2), encoding="utf-8")

    _reconcile_caddy()

    return "\n".join(
        [
            f"built: {image_tag}",
            f"container: ax-{name}",
            f"domains: {', '.join(_effective_domains(cfg)[0]) if _effective_domains(cfg)[0] else '(none)'}",
            f"platform path: {_effective_domains(cfg)[1] or '(none)'}",
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


def _caddy_site(domains: list[str], container_name: str, port: str) -> str:
    doms = " ".join(domains)
    return f"""{doms} {{
  handle /_ax/health {{
    respond "ok" 200
  }}
  reverse_proxy {container_name}:{port}
}}
"""


def _container_exists(name: str) -> bool:
    try:
        DOCKER.containers.get(name)
        return True
    except docker.errors.NotFound:
        return False
    except docker.errors.DockerException:
        return False


def _read_app_config(name: str) -> DeployConfig | None:
    p = (APPS_ROOT / name / "config.json")
    if not p.exists():
        return None
    try:
        return DeployConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def _reload_caddy() -> None:
    try:
        caddy = DOCKER.containers.get(CADDY_CONTAINER_NAME)
        res = caddy.exec_run(
            [
                "caddy",
                "reload",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            ],
            stdout=True,
            stderr=True,
        )
        if res.exit_code != 0:
            out = (res.output or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"caddy reload failed: {out}")
    except docker.errors.DockerException as e:
        raise RuntimeError(f"Docker error reloading caddy: {e}") from e


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


def _effective_ingress(cfg: DeployConfig) -> IngressSpec:
    if cfg.ingress is not None:
        return IngressSpec.model_validate(cfg.ingress)
    # back-compat: treat top-level domains as custom-domain
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
    # Clean known generated files
    for p in CADDY_APPS_DIR.glob("app-*.caddy"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    # Cleanup from older versions.
    legacy = CADDY_APPS_DIR / "platform-path.caddy"
    if legacy.exists():
        legacy.unlink()
    platform_routes_dir = CADDY_APPS_DIR / PLATFORM_ROUTES_DIRNAME
    if platform_routes_dir.exists():
        shutil.rmtree(platform_routes_dir)
    platform_routes_dir.mkdir(parents=True, exist_ok=True)

    platform_routes: list[tuple[str, str, str, str]] = []  # (app_name, path, container, port)

    if APPS_ROOT.exists():
        for app_dir in sorted(APPS_ROOT.iterdir()):
            if not app_dir.is_dir():
                continue
            cfg = _read_app_config(app_dir.name)
            if cfg is None:
                continue
            domains, path = _effective_domains(cfg)
            container = f"ax-{app_dir.name}"
            port = str(cfg.port or 8000)

            ing = _effective_ingress(cfg)
            if ing.mode in ("custom-domain", "platform-subdomain"):
                if domains:
                    (CADDY_APPS_DIR / f"app-{app_dir.name}.caddy").write_text(
                        _caddy_site(domains, container, port),
                        encoding="utf-8",
                    )
            elif ing.mode == "platform-path":
                if path:
                    platform_routes.append((app_dir.name, path, container, port))

    # For platform-path, we only generate route snippets (no site blocks), because
    # the base Caddyfile already defines {$PLATFORM_BASE_DOMAIN} { ... }.
    for app_name, path, container, port in sorted(platform_routes, key=lambda x: x[1]):
        (platform_routes_dir / f"{app_name}.caddy").write_text(
            "\n".join(
                [
                    f"handle {path}/_ax/health {{",
                    '  respond "ok" 200',
                    "}",
                    f"handle_path {path}/* {{",
                    f"  reverse_proxy {container}:{port}",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    _reload_caddy()


def _format_build_logs(logs: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for entry in logs:
        if "stream" in entry and entry["stream"]:
            chunks.append(str(entry["stream"]))
        elif "status" in entry and entry["status"]:
            chunks.append(str(entry["status"]) + ("\n" if not str(entry["status"]).endswith("\n") else ""))
        elif "error" in entry and entry["error"]:
            chunks.append(str(entry["error"]) + ("\n" if not str(entry["error"]).endswith("\n") else ""))
    return "".join(chunks).strip()


def _format_build_error(e: docker.errors.BuildError) -> str:
    details = ""
    try:
        details = _format_build_logs(list(e.build_log or []))  # type: ignore[arg-type]
    except Exception:
        details = ""
    msg = str(e)
    if details:
        return msg + "\n" + details
    return msg


if __name__ == "__main__":
    main()

