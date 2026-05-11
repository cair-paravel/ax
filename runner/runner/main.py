from __future__ import annotations

import shutil
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse

from runner.apps import meta_port, read_app_config, read_app_meta
from runner.auth import require_auth
from runner.caddy import reconcile_caddy
from runner.commands import run, systemctl
from runner.config import APPS_ROOT, CADDY_APPS_DIR
from runner.deploy import deploy_source
from runner.ingress import effective_domains
from runner.models import AppSummary, DeployConfig
from runner.names import sanitize_app_name
from runner.systemd import service_running, unit_exists, unit_name, unit_path


app = FastAPI(title="ax-runner", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/health")
def api_health(_: Annotated[None, Depends(require_auth)]) -> dict[str, object]:
    return {
        "status": "ok",
        "apps": len(list(APPS_ROOT.iterdir())) if APPS_ROOT.exists() else 0,
    }


@app.get("/v1/apps", response_model=list[AppSummary])
def list_apps(_: Annotated[None, Depends(require_auth)]) -> list[AppSummary]:
    if not APPS_ROOT.exists():
        return []
    out: list[AppSummary] = []
    for p in sorted(APPS_ROOT.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        cfg = read_app_config(name)
        meta = read_app_meta(name)
        port = meta_port(meta)
        out.append(
            AppSummary(
                name=name,
                last_deploy=meta.get("last_deploy"),
                type=cfg.type if cfg else "web",
                port=port or (cfg.port if cfg else None),
                domains=effective_domains(cfg)[0] if cfg else [],
                platform_path=effective_domains(cfg)[1] if cfg else None,
                running=service_running(name),
            )
        )
    return out


@app.get("/v1/apps/{name}/logs", response_class=PlainTextResponse)
def app_logs(
    name: str,
    _: Annotated[None, Depends(require_auth)],
    tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> str:
    name = sanitize_app_name(name)
    if not unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found")
    res = run(["journalctl", "-u", unit_name(name), "-n", str(tail), "--no-pager", "--output=cat"], check=False)
    if res.returncode not in (0, 1):
        raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout.strip())
    return res.stdout


@app.delete("/v1/apps/{name}", response_class=PlainTextResponse)
def delete_app(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = sanitize_app_name(name)
    if unit_exists(name):
        systemctl("disable", "--now", unit_name(name), check=False)
    unit = unit_path(name)
    if unit.exists():
        unit.unlink()
        systemctl("daemon-reload", check=False)

    app_dir = APPS_ROOT / name
    if app_dir.exists():
        shutil.rmtree(app_dir)

    p = CADDY_APPS_DIR / f"app-{name}.caddy"
    if p.exists():
        p.unlink()

    reconcile_caddy()
    return f"removed: {name}\n"


@app.post("/v1/apps/{name}/stop", response_class=PlainTextResponse)
def app_stop(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = sanitize_app_name(name)
    if not unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found")
    systemctl("stop", unit_name(name))
    return f"stopped: {name}\n"


@app.post("/v1/apps/{name}/start", response_class=PlainTextResponse)
def app_start(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = sanitize_app_name(name)
    if not unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found (deploy first)")
    if service_running(name):
        return f"already running: {name}\n"
    systemctl("start", unit_name(name))
    return f"started: {name}\n"


@app.post("/v1/apps/{name}/restart", response_class=PlainTextResponse)
def app_restart(name: str, _: Annotated[None, Depends(require_auth)]) -> str:
    name = sanitize_app_name(name)
    if not unit_exists(name):
        raise HTTPException(status_code=404, detail="Service not found")
    systemctl("restart", unit_name(name))
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

    name = sanitize_app_name(cfg.name)
    return await deploy_source(cfg, source, name)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
