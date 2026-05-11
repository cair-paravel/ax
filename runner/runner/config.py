from __future__ import annotations

import os
from pathlib import Path


DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", "/var/lib/ax")).resolve()
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
CADDY_APPS_DIR = Path(os.environ.get("CADDY_APPS_DIR", "/etc/caddy/apps"))
CADDY_CONFIG = Path(os.environ.get("CADDY_CONFIG", "/etc/caddy/Caddyfile"))
SYSTEMD_DIR = Path(os.environ.get("SYSTEMD_DIR", "/etc/systemd/system"))
UV_CACHE_DIR = Path(os.environ.get("UV_CACHE_DIR", "/var/cache/ax/uv")).resolve()
UV_PYTHON_INSTALL_DIR = Path(os.environ.get("UV_PYTHON_INSTALL_DIR", "/var/lib/ax/python")).resolve()
UV_BIN = os.environ.get("UV_BIN", "uv")
PLATFORM_BASE_DOMAIN = os.environ.get("PLATFORM_BASE_DOMAIN", "").strip()
PLATFORM_ROUTES_DIRNAME = "platform-routes"

APPS_ROOT = DATA_DIR / "apps"
PORTS_START = int(os.environ.get("AX_PORTS_START", "41000"))
PORTS_END = int(os.environ.get("AX_PORTS_END", "49999"))
