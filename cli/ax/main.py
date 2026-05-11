from __future__ import annotations

import io
import ipaddress
import json
import os
import secrets
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable

import httpx
import typer
from rich.progress import Progress, SpinnerColumn, TextColumn


app = typer.Typer(no_args_is_help=True)


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_DIR = CONFIG_HOME / "ax"
CONFIG_PATH = CONFIG_DIR / "config.json"
RUNNER_TOKEN_PATH = CONFIG_DIR / "runner-token"


@dataclass
class ClientConfig:
    base_url: str
    token: str


def _load_client_config() -> ClientConfig:
    if not CONFIG_PATH.exists():
        raise typer.Exit(
            "Not logged in. Run: ax login <hostname-or-ip> (after ax generate), or ax login <hostname-or-ip> --token …"
        )
    d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return ClientConfig(base_url=d["base_url"], token=d["token"])


def _save_client_config(cfg: ClientConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"base_url": cfg.base_url, "token": cfg.token}, indent=2) + "\n", encoding="utf-8")


def _read_saved_runner_token() -> str | None:
    if not RUNNER_TOKEN_PATH.exists():
        return None
    t = RUNNER_TOKEN_PATH.read_text(encoding="utf-8").strip()
    return t or None


def _runner_api_url_from_base(domain: str) -> str:
    """Same host as setup/login: https://<host> (http for localhost / *.localhost); IP → http://<ip>."""
    d = domain.strip()
    if not d:
        raise typer.Exit("Domain is empty.")
    d = d.removeprefix("https://").removeprefix("http://").rstrip("/")
    host = d.split("/")[0].split("?")[0].strip()
    if not host:
        raise typer.Exit("Domain is empty.")

    ip_test = host.strip("[]")
    try:
        parsed = ipaddress.ip_address(ip_test)
    except ValueError:
        parsed = None
    if parsed is not None:
        if isinstance(parsed, ipaddress.IPv6Address):
            return f"http://[{parsed.compressed}]"
        return f"http://{parsed.compressed}"

    hl = host.lower()
    use_http = hl == "localhost" or hl.endswith(".localhost")
    scheme = "http" if use_http else "https"
    return f"{scheme}://{host}"


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
    start = 'uvicorn app:app --host 127.0.0.1 --port $PORT'
    if (path / "main.py").exists():
        start = 'uvicorn main:app --host 127.0.0.1 --port $PORT'
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
                "[runtime]",
                'backend = "process"',
                "# python = \"3.12\"",
                "# memory = \"512M\"",
                "# cpu = \"1\"",
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


def _runner_call(cfg: ClientConfig, fn: Callable[[httpx.Client], Any]) -> Any:
    """Run ``fn(client)`` with a short-lived client; turn DNS/connect failures into a clear message."""
    try:
        with _http(cfg) as client:
            return fn(client)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise typer.Exit(
            f"Cannot reach runner at {cfg.base_url}\n"
            f"  ({e})\n"
            "Usually the hostname does not resolve from this machine. Use the same host you set as PLATFORM_BASE_DOMAIN on the server (any domain or IP you control):\n"
            "  ax login apps.example.com\n"
            "  ax login 203.0.113.10\n"
            "DNS: A/AAAA for that host → your server. "
            f"Saved URL is in {CONFIG_PATH}"
        ) from e


@app.command()
def generate(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing runner-token"),
) -> None:
    """Generate RUNNER_TOKEN, save to ~/.config/ax/runner-token, then use the same value on the server (setup.sh) and run ax login."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if RUNNER_TOKEN_PATH.exists() and not force:
        raise typer.Exit(
            f"{RUNNER_TOKEN_PATH} already exists. Use --force to generate a new token "
            "(you must update the server infra/.env to match)."
        )
    token = secrets.token_hex(32)
    RUNNER_TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    try:
        RUNNER_TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    typer.echo(f"Saved runner token to {RUNNER_TOKEN_PATH} (mode 600)\n")
    typer.echo("On the server (same token for RUNNER_TOKEN):")
    typer.echo("  • Interactive: ./setup.sh — paste the token when asked")
    typer.echo("  • Non-interactive: export RUNNER_TOKEN='…' && export PLATFORM_BASE_DOMAIN='your.domain' && ./setup.sh")
    typer.echo("\nToken (copy to server or export RUNNER_TOKEN):\n")
    typer.echo(token)
    typer.echo("\nThen on this machine (DNS: point your runner host at the server):")
    typer.echo("  ax login YOUR_RUNNER_HOST")
    typer.echo("  example: ax login apps.example.com   (same value as PLATFORM_BASE_DOMAIN on the server)")
    typer.echo("  (omit --token; it reads the same file)")


@app.command()
def login(
    base_domain: Annotated[
        str,
        typer.Argument(
            help="Your runner host: same as PLATFORM_BASE_DOMAIN on the server (any hostname or public IP), e.g. apps.example.com, localhost, 203.0.113.10.",
        ),
    ],
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Bearer token; defaults to ~/.config/ax/runner-token from ax generate",
    ),
) -> None:
    """Save runner API URL (from hostname or IP) and token."""
    if token is None:
        token = _read_saved_runner_token()
    if not token:
        raise typer.Exit(
            "No token. Run `ax generate` first, or pass --token …"
        )
    base_url = _runner_api_url_from_base(base_domain)
    _save_client_config(ClientConfig(base_url=base_url, token=token))
    typer.echo(f"Runner API: {base_url}")
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

    runtime = ax.get("runtime", {"backend": "process"})
    if not isinstance(runtime, dict):
        raise typer.Exit("[runtime] must be a table")

    payload = {
        "name": ax["name"],
        "type": ax.get("type", "web"),
        "start": ax["start"],
        "port": ax.get("port"),
        # back-compat: allow top-level `domains = [...]`
        "domains": ax.get("domains", []),
        "ingress": ingress,
        "runtime": runtime,
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

        r = _runner_call(
            cfg,
            lambda c: c.post(
                "v1/deploy",
                data={"config_json": json.dumps(payload)},
                files={"source": ("source.tar.gz", data, "application/gzip")},
            ),
        )
        if r.status_code >= 400:
            raise typer.Exit(f"Deploy failed ({r.status_code}): {r.text}")
        progress.update(task, description="Deploy completed.")
        typer.echo(r.text.rstrip())


@app.command("ps")
def ps_() -> None:
    """List apps."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.get("v1/apps"))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    apps = r.json()
    for a in apps:
        doms = ",".join(a.get("domains") or [])
        ppath = a.get("platform_path") or ""
        run = a.get("running")
        run_s = "?" if run is None else ("up" if run else "down")
        typer.echo(f"{a['name']}\t{run_s}\t{a.get('last_deploy')}\t{doms}\t{ppath}")


@app.command()
def logs(name: str, tail: int = typer.Option(200, "--tail")) -> None:
    """Fetch recent logs for an app."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.get(f"v1/apps/{name}/logs", params={"tail": tail}))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    typer.echo(r.text.rstrip())


@app.command("rm")
def rm_(name: str) -> None:
    """Remove a deployed app."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.delete(f"v1/apps/{name}"))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    typer.echo(r.text.rstrip())


@app.command()
def start(name: str) -> None:
    """Start a stopped app service."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.post(f"v1/apps/{name}/start"))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    typer.echo(r.text.rstrip())


@app.command()
def stop(name: str) -> None:
    """Stop a running app service (does not remove the app)."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.post(f"v1/apps/{name}/stop"))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    typer.echo(r.text.rstrip())


@app.command()
def restart(name: str) -> None:
    """Restart an app service."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.post(f"v1/apps/{name}/restart"))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    typer.echo(r.text.rstrip())


@app.command()
def health() -> None:
    """Check runner health through the authenticated API."""
    cfg = _load_client_config()
    r = _runner_call(cfg, lambda c: c.get("v1/health"))
    if r.status_code >= 400:
        raise typer.Exit(f"Request failed ({r.status_code}): {r.text}")
    data = r.json()
    typer.echo(f"{data.get('status', 'unknown')}\tapps={data.get('apps', 0)}")
