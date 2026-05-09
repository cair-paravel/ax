## ax

Single-tenant deploy platform for `uv` Python projects.

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

With compose default `PLATFORM_BASE_DOMAIN=localhost`, log in with the **base** domain; the CLI uses **`http://runner.localhost`** automatically:

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

- **`ax login <base>`** resolves the runner API automatically: **hostname** → `https://runner.<domain>` (or `http://runner.*.localhost` for local); **public IP** (v4/v6 literal) → `http://<ip>` using the **`/v1/*`** routes on port **80** in Caddy (no `runner.<ip>` in DNS/TLS).
- In production, prefer **`ax generate`**, the same token on the server (`setup.sh` or `infra/.env`), then **`ax login <base-domain>`** with no `--token` (it reads `~/.config/ax/runner-token`). See **Server bootstrap** below.

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

3. **DNS:** add **`runner`** as an **A** (and **AAAA** if you use IPv6) record to the **same** IP as your apex (e.g. `runner.example.com` → server).

4. On your laptop (token is read automatically from `~/.config/ax/runner-token` when you omit `--token`):

```bash
ax login <base-domain>
```

Examples: `ax login example.com` → `https://runner.example.com` · `ax login 49.12.245.83` → `http://49.12.245.83`

5. Deploy apps with `ax init` / `ax deploy` as usual.

---

- Provision an Ubuntu box
- Install Docker + Compose plugin
- Open ports `80` and `443`
- Point DNS (apex + **`runner`** subdomain) to the server IP

The setup script writes **`infra/.env`** (`PLATFORM_BASE_DOMAIN`, `RUNNER_TOKEN`, mode `600`) and runs `docker compose up --build -d`.

**Non-interactive** (e.g. cloud-init) — use the token from `ax generate` (or any secret):

```bash
export PLATFORM_BASE_DOMAIN=example.com
export RUNNER_TOKEN='<same token as ax generate>'
./setup.sh
```

Or copy `infra/.env.example` → `infra/.env`, edit values, then:

```bash
docker compose -f infra/docker-compose.yml up --build -d
```

You can still pass an explicit token: `ax login example.com --token …`

#### Base domain

Set `PLATFORM_BASE_DOMAIN` in **`infra/.env`** (or via the setup script): a **hostname** (e.g. `example.com`) or a **public IPv4** (no `https://`). Caddy serves:

- **`https://<hostname>`** — platform apps + `/_ax/health` (TLS may not work for a raw IP apex)
- **`https://runner.<hostname>`** — runner API for the CLI when DNS has `runner` → server
- **`http://<PLATFORM_BASE_DOMAIN>/v1/...`** — runner API on port **80** (used when the base is an **IP**, and as an HTTP fallback for hostnames)

`./setup.sh` regenerates **`infra/caddy-runner-subdomain.caddy`** (HTTPS `runner.*` block is **skipped** for numeric IPs).

Defaults remain `localhost` / `local-dev-token` if `.env` is absent (local dev only).

#### Security defaults

- Only expose ports `80` and `443` publicly.
- The runner container is **not** published on the host; Caddy terminates TLS and reverse-proxies to it on **`runner.<domain>`**.

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


