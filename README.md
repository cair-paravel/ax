## ax

Single-tenant deploy platform for `uv` Python projects.

Each stack uses **your** hostname or public IP as **`PLATFORM_BASE_DOMAIN`** (the same value for `./setup.sh` on the server and **`ax login`** on your laptop). There is no vendor-specific domain; pick any DNS name you control or use the server’s IP.

### What’s in here

- `infra/`: Docker Compose + Caddy reverse proxy
- `runner/`: API server that builds/runs apps as Docker containers (the runner image uses **`uv sync --frozen`** + `uv run`; lockfile is `runner/uv.lock`)
- `cli/`: `ax` CLI to push local code to the runner (`cli/uv.lock` + **`uv sync` / `uv run ax`** for local dev)

### Quickstart (local)

Prereqs: Docker Desktop (or Docker Engine + Compose plugin)

If `docker compose` fails, start your Docker runtime first (Docker Desktop / OrbStack / Colima).

1. Start the platform:

```bash
docker compose -f infra/docker-compose.yml up --build
```

2. Install the CLI with `uv` (pick one):

**Global tool (typical):**

```bash
cd /path/to/ax
uv tool install -e ./cli
```

**From a venv in `cli/` (hacking on the CLI):**

```bash
cd /path/to/ax/cli
uv sync
uv run ax --help
```

3. Login and deploy a project from its repo directory:

With compose default `PLATFORM_BASE_DOMAIN=localhost`, the runner API is on the **same host** (`http://localhost` — no TLS on local):

```bash
ax login localhost --token local-dev-token
ax init
ax deploy
ax ps
ax start myapi
ax stop myapi
ax restart myapi
ax logs myapi --tail 300
ax rm myapi
```

Notes:

- **`ax login <host>`** must match **`PLATFORM_BASE_DOMAIN`** on the server (the hostname or IP where Caddy serves **`/v1/*`** and **`/health`**). Non-localhost hostnames use **`https://<host>`**; **`localhost`** / **`*.localhost`** use **`http://`**; **public IP** uses **`http://<ip>`**.
- In production, prefer **`ax generate`**, the same token on the server (`setup.sh` or `infra/.env`), then **`ax login <same-host-as-PLATFORM_BASE_DOMAIN>`** with no `--token`. See **Server bootstrap** below.

### Deploying to Hetzner (notes)

**Server bootstrap (recommended DX)**

1. On your laptop (CLI installed): create a token and store it locally:

```bash
ax generate
```

2. On the server: clone the repo, set the **same** `RUNNER_TOKEN` (paste when prompted, or export it), and run setup:

```bash
git clone <this repo>
cd ax
./setup.sh
```

3. **DNS:** point **`PLATFORM_BASE_DOMAIN`** at the server (e.g. **`apps.example.com`** A/AAAA → your server). That same host serves the runner API (`/v1/...`), platform health (`/_ax/health`), and path-based apps (`/myapi/...`). IP-only bases skip DNS and use **`http://<ip>`** for the CLI.

4. On your laptop (token is read automatically from `~/.config/ax/runner-token` when you omit `--token`):

```bash
ax login <same-host-as-PLATFORM_BASE_DOMAIN>
```

Example: server has `PLATFORM_BASE_DOMAIN=apps.example.com` → run **`ax login apps.example.com`** (CLI uses `https://apps.example.com`). For a raw IP base, use **`ax login 203.0.113.10`** → `http://203.0.113.10`.

5. Deploy apps with `ax init` / `ax deploy` as usual.

---

- Provision an Ubuntu box
- Install Docker + Compose plugin
- Open ports `80` and `443`
- Point DNS for **`PLATFORM_BASE_DOMAIN`** (your chosen runner/app host) to the server IP

The setup script writes **`infra/.env`** (`PLATFORM_BASE_DOMAIN`, `RUNNER_TOKEN`, mode `600`) and runs `docker compose up --build -d`.

**Non-interactive** (e.g. cloud-init) — use the token from `ax generate` (or any secret):

```bash
export PLATFORM_BASE_DOMAIN=apps.example.com
export RUNNER_TOKEN='<same token as ax generate>'
./setup.sh
```

Or copy `infra/.env.example` → `infra/.env`, edit values, then:

```bash
docker compose -f infra/docker-compose.yml up --build -d
```

You can still pass an explicit token: `ax login apps.example.com --token …`

#### `PLATFORM_BASE_DOMAIN` (runner + apps host)

Set it in **`infra/.env`** (or via `./setup.sh`): the **hostname or IP** where Caddy listens for this stack (no `https://`). On that host, Caddy serves:

- **`/v1/*`** and **`/health`** → runner API (for **`ax deploy`**, **`ax ps`**, etc.)
- **`/_ax/health`** → platform liveness
- **`/<app-path>/...`** → path-based apps (from `ax.toml` ingress)

TLS on **:443** for real hostnames; **`http://<host>/v1/*`** on **:80** as well (helps before certs exist and for IP-only bases).

Defaults remain `localhost` / `local-dev-token` if `.env` is absent (local dev only).

#### Security defaults

- Only expose ports `80` and `443` publicly.
- The runner container is **not** published on the host; Caddy reverse-proxies **`/v1`** and **`/health`** to it on **`PLATFORM_BASE_DOMAIN`**.

#### Upgrading an existing host

After pulling changes, recycle the stack so Compose picks up renamed services (`ax-caddy`, `ax-runner`, Docker network `ax`):

```bash
docker compose -f infra/docker-compose.yml down
docker compose -f infra/docker-compose.yml up --build -d
```

If old containers still conflict, remove them with `docker rm -f <name>` using the names shown in `docker ps -a`.

### `ax.toml` reference

Minimal web app:

```toml
name = "myapi"
type = "web"
start = "uv run uvicorn myapi.app:app --host 0.0.0.0 --port $PORT"
port = 8000

[ingress]
# Choose one:
# mode = "platform-path"         # https://<platform-base-domain>/myapi/*
# path = "/myapi"
#
# mode = "platform-subdomain"    # https://myapi.<platform-base-domain>/*
# subdomain = "myapi"
#
# mode = "custom-domain"         # https://api.example.com/*
# domains = ["api.example.com"]

[env]
ENV = "prod"
```

### Updating the CLI when you pull changes

If installed via `uv tool`:

```bash
uv tool list
uv tool upgrade ax
```


