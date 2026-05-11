## ax

Single-tenant deploy platform for `uv` Python projects.

`ax` deploys Python apps as normal Linux processes instead of Docker containers:

- `runner/`: FastAPI API server that receives deploys, creates per-release virtualenvs with `uv`, writes systemd units, and updates Caddy routes.
- `cli/`: `ax` CLI to package local code and call the runner.
- `infra/`: Caddy config for the runner API and deployed app routes.

The default runtime is optimized for low overhead:

- one systemd service per deployed app
- one `.venv` per app release, so dependency versions do not conflict
- one shared `UV_CACHE_DIR`, so downloads/builds are reused across apps
- Caddy reverse-proxies to each app on an allocated localhost port

This is intended for a single trusted owner running their own apps. Keep Docker or another stronger isolation backend for untrusted multi-tenant code.

### Quickstart: CLI Dev

```bash
cd cli
uv sync
uv run ax --help
```

Or install the CLI globally:

```bash
uv tool install -e ./cli
```

### Server Bootstrap

Prereqs on the server:

- Linux with systemd
- `uv`
- `caddy`
- ports `80` and `443` open
- DNS for `PLATFORM_BASE_DOMAIN` pointing to the server, or use the server IP

Recommended token flow:

```bash
ax generate
```

On the server:

```bash
git clone <this repo>
cd ax
export PLATFORM_BASE_DOMAIN=apps.example.com
export RUNNER_TOKEN='<same token as ax generate>'
sudo -E ./setup.sh
```

`setup.sh` writes:

- `infra/.env` for repo-local record of `PLATFORM_BASE_DOMAIN` and `RUNNER_TOKEN`
- `/etc/ax/runner.env` for the runner service
- `/etc/systemd/system/ax-runner.service`
- `/etc/systemd/system/caddy.service.d/ax.conf`
- `/etc/caddy/Caddyfile`

It also creates:

- `/var/lib/ax/apps`
- `/var/cache/ax/uv`
- `/etc/caddy/apps`

Then it starts/restarts `caddy` and `ax-runner`.

### Deploying

On your laptop:

```bash
ax login apps.example.com
ax init
ax deploy
ax ps
ax logs myapi --tail 300
ax restart myapi
ax stop myapi
ax start myapi
ax rm myapi
```

For `localhost` and raw IPs the CLI uses `http://`; for normal hostnames it uses `https://`.

### `ax.toml`

Minimal web app:

```toml
name = "myapi"
type = "web"
start = "uvicorn app:app --host 127.0.0.1 --port $PORT"
port = 8000

[ingress]
mode = "platform-path"
path = "/myapi"

[runtime]
backend = "process"
python = "3.12"
memory = "512M"
cpu = "1"

[env]
ENV = "prod"
```

`port` is kept for app compatibility, but the process runtime allocates a private localhost port per app and exposes it as `$PORT`.

Ingress modes:

```toml
[ingress]
mode = "platform-path"
path = "/myapi"
```

```toml
[ingress]
mode = "platform-subdomain"
subdomain = "myapi"
```

```toml
[ingress]
mode = "custom-domain"
domains = ["api.example.com"]
```

### Runtime Model

For each deploy, the runner:

1. extracts the source into `/var/lib/ax/apps/<name>/releases/<id>/src`
2. runs `uv sync --project <src>` using `UV_CACHE_DIR=/var/cache/ax/uv`
3. atomically updates `/var/lib/ax/apps/<name>/current`
4. writes `/etc/systemd/system/ax-<name>.service`
5. restarts the app service
6. writes Caddy route snippets under `/etc/caddy/apps`
7. reloads Caddy

Each app gets an isolated virtualenv:

```text
/var/lib/ax/apps/api-a/current/src/.venv
/var/lib/ax/apps/api-b/current/src/.venv
```

All apps share the cache:

```text
/var/cache/ax/uv
```

This means conflicting dependency versions are fine across apps, while common packages are downloaded/built once.

### Operational Notes

Runner service:

```bash
systemctl status ax-runner
journalctl -u ax-runner -f
```

App service:

```bash
systemctl status ax-myapi
journalctl -u ax-myapi -f
```

Caddy:

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
```
