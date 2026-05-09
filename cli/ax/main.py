from __future__ import annotations

import io
import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.progress import Progress, SpinnerColumn, TextColumn


app = typer.Typer(no_args_is_help=True)


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agentx"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class ClientConfig:
    base_url: str
    token: str


def _load_client_config() -> ClientConfig:
    if not CONFIG_PATH.exists():
        raise typer.Exit("Not logged in. Run: ax login <base_url> --token <token>")
    d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return ClientConfig(base_url=d["base_url"], token=d["token"])


def _save_client_config(cfg: ClientConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"base_url": cfg.base_url, "token": cfg.token}, indent=2) + "\n", encoding="utf-8")


def _read_ax_toml(path: Path) -> dict[str, Any]:
    ax_path = path / "ax.toml"
    if not ax_path.exists():
        raise typer.Exit("Missing ax.toml. Run: ax init")
    import tomllib

    return tomllib.loads(ax_path.read_text(encoding="utf-8"))


def _write_ax_toml(path: Path, name: str) -> None:
    ax_path = path / "ax.toml"
    if ax_path.exists():
        raise typer.Exit("ax.toml already exists")
    start = 'uv run uvicorn app:app --host 0.0.0.0 --port $PORT'
    if (path / "main.py").exists():
        start = 'uv run uvicorn main:app --host 0.0.0.0 --port $PORT'
    ax_path.write_text(
        "\n".join(
            [
                f'name = "{name}"',
                'type = "web"',
                f'start = "{start}"',
                "port = 8000",
                "",
                "[ingress]",
                'mode = "platform-path"',
                f'path = "/{name}"',
                "",
                "[env]",
                'ENV = "prod"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _load_ignore(root: Path) -> set[str]:
    ignore = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".DS_Store",
    }
    p = root / ".axignore"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ignore.add(line)
    return ignore


def _make_source_tar_gz(root: Path) -> bytes:
    ignore = _load_ignore(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            parts = rel.parts
            if any(part in ignore for part in parts):
                continue
            if p.is_dir():
                continue
            tf.add(p, arcname=str(rel))
    return buf.getvalue()


def _http(cfg: ClientConfig) -> httpx.Client:
    return httpx.Client(
        base_url=cfg.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {cfg.token}"},
        timeout=600.0,
    )


@app.command()
def login(base_url: str, token: str = typer.Option(..., "--token")) -> None:
    """Store runner URL + token."""
    _save_client_config(ClientConfig(base_url=base_url, token=token))
    typer.echo(f"Saved config to {CONFIG_PATH}")


@app.command()
def init(name: str | None = None, path: Path = typer.Option(Path("."), "--path")) -> None:
    """Create an ax.toml in the repo."""
    path = path.resolve()
    if name is None:
        name = path.name.lower().replace("_", "-")
    _write_ax_toml(path, name)
    typer.echo(f"Wrote {path / 'ax.toml'}")


@app.command()
def deploy(path: Path = typer.Option(Path("."), "--path")) -> None:
    """Package local code and deploy."""
    path = path.resolve()
    cfg = _load_client_config()
    ax = _read_ax_toml(path)

    env = ax.get("env", {})
    if not isinstance(env, dict):
        raise typer.Exit("[env] must be a table")

    ingress = ax.get("ingress")
    if ingress is not None and not isinstance(ingress, dict):
        raise typer.Exit("[ingress] must be a table")

    payload = {
        "name": ax["name"],
        "type": ax.get("type", "web"),
        "start": ax["start"],
        "port": ax.get("port"),
        # back-compat: allow top-level `domains = [...]`
        "domains": ax.get("domains", []),
        "ingress": ingress,
        "env": {str(k): str(v) for k, v in env.items()},
    }

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("Packaging source...", total=None)
        data = _make_source_tar_gz(path)
        progress.update(task, description="Uploading + deploying (server build/run)...")

        with _http(cfg) as client:
            r = client.post(
                "v1/deploy",
                data={"config_json": json.dumps(payload)},
                files={"source": ("source.tar.gz", data, "application/gzip")},
            )
            if r.status_code >= 400:
                raise typer.Exit(f"Deploy failed ({r.status_code}): {r.text}")
            progress.update(task, description="Deploy completed.")
            typer.echo(r.text.rstrip())


@app.command("ps")
def ps_() -> None:
    """List apps."""
    cfg = _load_client_config()
    with _http(cfg) as client:
        r = client.get("v1/apps")
        if r.status_code >= 400:
            raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
        apps = r.json()
    for a in apps:
        doms = ",".join(a.get("domains") or [])
        ppath = a.get("platform_path") or ""
        typer.echo(f"{a['name']}\t{a.get('last_deploy')}\t{doms}\t{ppath}")


@app.command()
def logs(name: str, tail: int = typer.Option(200, "--tail")) -> None:
    """Fetch recent logs for an app."""
    cfg = _load_client_config()
    with _http(cfg) as client:
        r = client.get(f"v1/apps/{name}/logs", params={"tail": tail})
        if r.status_code >= 400:
            raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
        typer.echo(r.text.rstrip())


@app.command("rm")
def rm_(name: str) -> None:
    """Remove a deployed app."""
    cfg = _load_client_config()
    with _http(cfg) as client:
        r = client.delete(f"v1/apps/{name}")
        if r.status_code >= 400:
            raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
        typer.echo(r.text.rstrip())

