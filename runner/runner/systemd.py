from __future__ import annotations

import json
from pathlib import Path

from runner.commands import systemctl
from runner.config import SYSTEMD_DIR, UV_CACHE_DIR
from runner.models import DeployConfig


def unit_name(name: str) -> str:
    return f"ax-{name}.service"


def unit_path(name: str) -> Path:
    return SYSTEMD_DIR / unit_name(name)


def unit_exists(name: str) -> bool:
    return unit_path(name).exists()


def service_running(name: str) -> bool | None:
    if not unit_exists(name):
        return False
    res = systemctl("is-active", "--quiet", unit_name(name), check=False)
    return res.returncode == 0


def write_systemd_unit(name: str, cfg: DeployConfig, src_dir: Path, port: int) -> None:
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

    unit_path(name).write_text(
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
