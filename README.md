## agentx (MVP)

Single-tenant deploy platform for `uv` Python projects.

### What’s in here

- `infra/`: Docker Compose + Caddy reverse proxy
- `runner/`: API server that builds/runs apps as Docker containers
- `cli/`: `ax` CLI to push local code to the runner

### Quickstart (local)

Prereqs: Docker Desktop (or Docker Engine + Compose plugin)

If `docker compose` fails, start your Docker runtime first (Docker Desktop / OrbStack / Colima).

1. Start the platform:

```bash
docker compose -f infra/docker-compose.yml up --build
```

2. In another terminal, install the CLI (editable):

```bash
cd cli
python -m venv .venv && source .venv/bin/activate
pip install -U pip
pip install -e .
```

3. Login and deploy a project from its repo directory:

```bash
ax login http://localhost:8080 --token local-dev-token
ax init
ax deploy
```

The runner listens at `http://localhost:8080`.

### Deploying to Hetzner (notes)

- Provision an Ubuntu box
- Install Docker + Compose plugin
- Open ports `80` and `443` (and optionally `8080` if you don’t put runner behind Caddy yet)
- Point DNS (and optionally wildcard DNS) to the server IP
- Run:

```bash
git clone <this repo>
cd agentx
docker compose -f infra/docker-compose.yml up --build -d
```

### `ax.toml` reference

Minimal web app:

```toml
name = "myapi"
type = "web"
start = "uv run uvicorn myapi.app:app --host 0.0.0.0 --port $PORT"
port = 8000
domains = ["myapi.apps.yourdomain.com", "api.example.com"]

[env]
ENV = "prod"
```


