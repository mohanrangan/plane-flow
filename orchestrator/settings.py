"""Settings shared by the orchestrator, bootstrap, install.sh and flowctl.

`orchestrator/config.json` (git-ignored, written by install.sh) overrides the defaults below; every
key is optional. Defaults depend on the platform because of how Plane's containers reach this host:

- macOS: Docker runs inside a VM (Colima). `host.docker.internal` inside containers is forwarded to
  the Mac's localhost, so the orchestrator listens on 127.0.0.1 only.
- Linux: containers can't reach the host's 127.0.0.1. Plane's Docker network is pinned to a fixed
  range (deploy/linux/docker-compose.override.yml) and the orchestrator also listens on that network's
  gateway address, which only the host and Plane's containers can reach.

Command line (used by flowctl / install.sh):  python3 orchestrator/settings.py get <key>
                                              python3 orchestrator/settings.py dump
"""
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "orchestrator" / "config.json"
IS_MAC = platform.system() == "Darwin"


def load() -> dict:
    user = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    port = user.get("port", 8787)
    gateway = user.get("plane_network_gateway", "172.30.0.1")
    api_url = user.get("plane_api_url", "http://localhost:8080").rstrip("/")
    defaults = {
        "backend": "claude",                 # claude | copilot | fake (fake = no AI, for tests)
        "port": port,
        "listen": ["127.0.0.1"] if IS_MAC else ["127.0.0.1", gateway],
        "webhook_url": (f"http://host.docker.internal:{port}/webhook" if IS_MAC
                        else f"http://{gateway}:{port}/webhook"),
        "plane_api_url": api_url,            # how this host calls Plane
        "plane_public_url": api_url,         # what people's browsers open (links in comments/pages)
        "dashboard_url": f"http://localhost:{port}",
        "plane_network_gateway": gateway,
        "manage_plane": True,                # flowctl starts/stops Plane's containers
        "plane_compose_dir": "plane",        # relative to the repo root, or absolute
        "plane_compose_project": "plane",    # docker compose project name
        "workspace_name": "AI Flow",
        "workspace_slug": "ai-flow",
        "max_parallel": 4,
        "max_fix_loops": 2,
        "models": {},
        "styles": {"question_color": "peach", "question_italic": True},
    }
    merged = {**defaults, **user}
    merged["plane_public_url"] = merged["plane_public_url"].rstrip("/")
    return merged


def compose_dir(s: dict) -> Path:
    p = Path(s["plane_compose_dir"])
    return p if p.is_absolute() else ROOT / p


if __name__ == "__main__":
    s = load()
    if len(sys.argv) >= 3 and sys.argv[1] == "get":
        v = s.get(sys.argv[2], "")
        if sys.argv[2] == "plane_compose_dir":
            v = str(compose_dir(s))
        print(" ".join(map(str, v)) if isinstance(v, list) else str(v).lower() if isinstance(v, bool) else v)
    elif len(sys.argv) >= 2 and sys.argv[1] == "dump":
        print(json.dumps(s, indent=2))
    else:
        sys.exit("usage: settings.py get <key> | dump")
