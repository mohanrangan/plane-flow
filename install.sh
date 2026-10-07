#!/usr/bin/env bash
# plane-flow installer — from a fresh `git clone` to a running Plane + orchestrator. Safe to re-run.
#
#   ./install.sh --admin-email you@example.com                      # new Plane, managed by plane-flow
#   ./install.sh --admin-email you@example.com --plane-existing /opt/plane-app   # reuse an installed Plane
#
# Options (all optional except --admin-email on first run):
#   --admin-email EMAIL       Plane instance admin (also the human approver)
#   --plane-existing DIR      use a Plane already installed in DIR (its docker-compose.yml); not started/stopped by plane-flow
#   --plane-version VER       Plane release to download for a new install (default v1.4.2, the tested version)
#   --http-port N             Plane web port for a new install (default 8080)
#   --orch-port N             orchestrator port (default 8787)
#   --public-url URL          URL people's browsers use for Plane (default http://localhost:<http-port>)
#   --compose-project NAME    docker compose project name for Plane (default plane)
#   --backend claude|fake     workspace default agent backend (default claude; fake = no AI, for testing)
#   --workspace-slug SLUG     Plane workspace (default ai-flow)
#   --systemd                 Linux: install and start the orchestrator as a systemd service (uses sudo)
#   --no-start                configure only; don't start anything
#   --no-selftest             skip the final self-test
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)

ADMIN_EMAIL=""; PLANE_EXISTING=""; PLANE_VERSION="v1.4.2"; HTTP_PORT=8080; ORCH_PORT=8787; PUBLIC_URL=""
COMPOSE_PROJECT="plane"; BACKEND=""; WS_SLUG=""; SYSTEMD=false; START=true; SELFTEST=true
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-email) ADMIN_EMAIL=$2; shift 2 ;;
    --plane-existing) PLANE_EXISTING=$(cd "$2" && pwd); shift 2 ;;
    --plane-version) PLANE_VERSION=$2; shift 2 ;;
    --http-port) HTTP_PORT=$2; shift 2 ;;
    --orch-port) ORCH_PORT=$2; shift 2 ;;
    --public-url) PUBLIC_URL=$2; shift 2 ;;
    --compose-project) COMPOSE_PROJECT=$2; shift 2 ;;
    --backend) BACKEND=$2; shift 2 ;;
    --workspace-slug) WS_SLUG=$2; shift 2 ;;
    --systemd) SYSTEMD=true; shift ;;
    --no-start) START=false; shift ;;
    --no-selftest) SELFTEST=false; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)"; exit 2 ;;
  esac
done
OS=$(uname)
say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. prerequisites
say "Checking prerequisites ($OS)"
missing=()
for t in git curl python3 openssl docker uv; do command -v $t >/dev/null || missing+=("$t"); done
[ ${#missing[@]} -eq 0 ] || die "missing: ${missing[*]}
  macOS: brew install ${missing[*]/docker/colima docker docker-compose}   (then: colima start)
  Linux: install Docker Engine + compose plugin (docs.docker.com/engine/install), then:
         curl -LsSf https://astral.sh/uv/install.sh | sh"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 must be 3.11 or newer"
if [ "$OS" = Darwin ] && command -v colima >/dev/null && ! colima status >/dev/null 2>&1; then
  echo "starting Colima (Docker VM)"; colima start
fi
docker info >/dev/null 2>&1 || die "Docker is installed but not reachable (Linux: is your user in the 'docker' group? macOS: colima start)"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
if ! command -v specify >/dev/null; then
  echo "installing the Spec Kit CLI (specify) with uv"
  uv tool install specify-cli --from git+https://github.com/github/spec-kit.git
  export PATH="$HOME/.local/bin:$PATH"
fi
specify --version >/dev/null 2>&1 || die "specify is installed but not on PATH (add ~/.local/bin to PATH)"
EFFECTIVE_BACKEND=${BACKEND:-$(python3 orchestrator/settings.py get backend)}
if [ "$EFFECTIVE_BACKEND" = claude ]; then
  command -v claude >/dev/null || die "the Claude Code CLI is required for backend 'claude' (or use --backend fake to test)"
  echo "claude $(claude --version 2>/dev/null | head -1) — make sure it is logged in for this user"
  echo "  (headless servers: run 'claude setup-token' or set ANTHROPIC_API_KEY)"
fi
echo "ok: git curl python3 openssl docker compose uv specify${EFFECTIVE_BACKEND:+ (backend: $EFFECTIVE_BACKEND)}"

# ---------------------------------------------------------------- 2. orchestrator/config.json
say "Writing orchestrator/config.json"
PUBLIC_URL=${PUBLIC_URL:-http://localhost:$HTTP_PORT}
PLANE_DIR=${PLANE_EXISTING:-$ROOT/plane}
MANAGE=$([ -n "$PLANE_EXISTING" ] && echo false || echo true)
python3 - "$ORCH_PORT" "$HTTP_PORT" "$PUBLIC_URL" "$PLANE_DIR" "$COMPOSE_PROJECT" "$MANAGE" "$BACKEND" "$WS_SLUG" <<'PY'
import json, sys, pathlib
port, http, public, pdir, proj, manage, backend, slug = sys.argv[1:]
p = pathlib.Path("orchestrator/config.json")
c = json.loads(p.read_text()) if p.exists() else {}
c.update({"port": int(port), "plane_api_url": f"http://localhost:{http}", "plane_public_url": public,
          "dashboard_url": f"http://localhost:{port}", "plane_compose_dir": pdir,
          "plane_compose_project": proj, "manage_plane": manage == "true"})
if backend: c["backend"] = backend
if slug: c["workspace_slug"] = slug
p.write_text(json.dumps(c, indent=2) + "\n")
print(json.dumps(c, indent=2))
PY
WEBHOOK_URL=$(python3 orchestrator/settings.py get webhook_url)

# ---------------------------------------------------------------- 3. Plane
if [ -z "$PLANE_EXISTING" ]; then
  say "Plane $PLANE_VERSION in $PLANE_DIR (compose project '$COMPOSE_PROJECT', port $HTTP_PORT)"
  mkdir -p "$PLANE_DIR"
  REL="https://github.com/makeplane/plane/releases/download/$PLANE_VERSION"
  [ -f "$PLANE_DIR/docker-compose.yml" ] || curl -fsSL -o "$PLANE_DIR/docker-compose.yml" "$REL/docker-compose.yml"
  [ -f "$PLANE_DIR/variables.env" ] || curl -fsSL -o "$PLANE_DIR/variables.env" "$REL/variables.env"
  if [ ! -f "$PLANE_DIR/.env" ]; then
    DOMAIN=$(python3 -c "import sys,urllib.parse as u; p=u.urlparse(sys.argv[1]); print(p.netloc)" "$PUBLIC_URL")
    sed -e "s|^APP_DOMAIN=.*|APP_DOMAIN=$DOMAIN|" \
        -e "s|^APP_RELEASE=.*|APP_RELEASE=$PLANE_VERSION|" \
        -e "s|^LISTEN_HTTP_PORT=.*|LISTEN_HTTP_PORT=$HTTP_PORT|" \
        -e "s|^LISTEN_HTTPS_PORT=.*|LISTEN_HTTPS_PORT=$((HTTP_PORT + 363))|" \
        -e "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" \
        -e "s|^LIVE_SERVER_SECRET_KEY=.*|LIVE_SERVER_SECRET_KEY=$(openssl rand -hex 32)|" \
        -e "s|^API_KEY_RATE_LIMIT=.*|API_KEY_RATE_LIMIT=600/minute|" \
        -e "s|^WEBHOOK_ALLOWED_HOSTS=.*|WEBHOOK_ALLOWED_HOSTS=host.docker.internal|" \
        -e "s|^WEBHOOK_ALLOWED_IPS=.*|WEBHOOK_ALLOWED_IPS=172.16.0.0/12,192.168.5.0/24,192.168.64.0/24|" \
        "$PLANE_DIR/variables.env" > "$PLANE_DIR/.env"
    chmod 600 "$PLANE_DIR/.env"
    echo "wrote $PLANE_DIR/.env (generated secrets, webhook allow-lists)"
  fi
  if [ "$OS" != Darwin ] && [ ! -f "$PLANE_DIR/docker-compose.override.yml" ]; then
    cp deploy/linux/docker-compose.override.yml "$PLANE_DIR/"
    echo "pinned Plane's Docker network (deploy/linux/docker-compose.override.yml)"
  fi
  $START && (cd "$PLANE_DIR" && docker compose -p "$COMPOSE_PROJECT" up -d)
else
  say "Using existing Plane in $PLANE_EXISTING"
  ENVF=$(ls "$PLANE_EXISTING"/.env "$PLANE_EXISTING"/plane.env 2>/dev/null | head -1 || true)
  ok=true
  grep -qE '^WEBHOOK_ALLOWED_IPS=.*172\.16\.0\.0/12' "${ENVF:-/dev/null}" || ok=false
  [ "$OS" = Darwin ] || [ -f "$PLANE_EXISTING/docker-compose.override.yml" ] || ok=false
  if ! $ok; then
    echo "Plane needs two changes so its webhooks can reach plane-flow (then restart Plane):"
    echo "  1. in ${ENVF:-its env file}: WEBHOOK_ALLOWED_IPS=172.16.0.0/12   and   API_KEY_RATE_LIMIT=600/minute"
    [ "$OS" = Darwin ] || echo "  2. cp $ROOT/deploy/linux/docker-compose.override.yml $PLANE_EXISTING/"
    echo "  then: cd $PLANE_EXISTING && docker compose down && docker compose up -d   (data is kept)"
    die "re-run install.sh after making these changes"
  fi
fi

$START || { echo "configured; not started (--no-start)"; exit 0; }
say "Waiting for Plane at http://localhost:$HTTP_PORT"
for i in $(seq 1 90); do
  curl -fs "http://localhost:$HTTP_PORT/api/instances/" >/dev/null 2>&1 && { echo "Plane is up"; break; }
  [ "$i" = 90 ] && die "Plane did not come up within 7.5 minutes (check: docker compose -p $COMPOSE_PROJECT logs)"
  sleep 5
done

# ---------------------------------------------------------------- 4. bootstrap
say "Bootstrapping Plane (admin, workspace, worker accounts, webhook -> $WEBHOOK_URL)"
if [ -z "$ADMIN_EMAIL" ] && [ ! -f .secrets/plane.json ]; then die "--admin-email is required on first run"; fi
uv run --quiet bootstrap/bootstrap.py ${ADMIN_EMAIL:+--admin-email "$ADMIN_EMAIL"}

# ---------------------------------------------------------------- 5. orchestrator
say "Starting the orchestrator"
if $SYSTEMD && [ "$OS" = Linux ]; then
  UNIT=$(mktemp)
  sed -e "s|__USER__|$(id -un)|" -e "s|__DIR__|$ROOT|" -e "s|__PATH__|$PATH|" -e "s|__UV__|$(command -v uv)|" \
      deploy/linux/plane-flow.service > "$UNIT"
  sudo cp "$UNIT" /etc/systemd/system/plane-flow.service && rm -f "$UNIT"
  sudo systemctl daemon-reload && sudo systemctl enable --now plane-flow
  sudo systemctl restart plane-flow
else
  ./flowctl restart >/dev/null
fi
for i in $(seq 1 30); do
  curl -fs "http://127.0.0.1:$ORCH_PORT/" >/dev/null 2>&1 && { echo "orchestrator is up"; break; }
  [ "$i" = 30 ] && die "orchestrator did not start (see: ./flowctl logs)"
  sleep 2
done

# ---------------------------------------------------------------- 6. self-test + summary
if $SELFTEST; then say "Self-test"; ./flowctl selftest || die "self-test failed (details above)"; fi
say "Done"
echo "Plane:      $PUBLIC_URL   (admin login: .secrets/plane.json)"
echo "Dashboard:  http://localhost:$ORCH_PORT/dashboard"
echo "Next:       create a project in Plane — it is set up automatically (see HANDOFF.md §5)"
