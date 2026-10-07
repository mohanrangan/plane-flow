# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""One-time Plane setup for plane-flow: instance admin, workspace, worker accounts + API tokens, webhook.

Idempotent: safe to re-run (existing accounts are signed in, existing objects reused). Creates no
projects — every Plane project is provisioned by the orchestrator when it appears.

    uv run bootstrap/bootstrap.py --admin-email you@example.com
        [--plane-url http://localhost:8080] [--webhook-url http://172.30.0.1:8787/webhook]
        [--workspace-name "AI Flow"] [--workspace-slug ai-flow]

Defaults come from orchestrator/config.json (see orchestrator/settings.py). Writes everything the
orchestrator needs to .secrets/plane.json (chmod 600). If the Plane instance already has an admin,
create .secrets/plane.json first with {"admin": {"email": ..., "password": ...}} so it signs in.
"""
import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))
import settings  # noqa: E402

SECRETS = ROOT / ".secrets"
OUT = SECRETS / "plane.json"

WORKERS = {
    "spec-agent": "Writes the feature spec (speckit specify)",
    "plan-agent": "Writes the technical plan (speckit plan)",
    "tasks-agent": "Breaks the plan into tasks (speckit tasks)",
    "dev-agent": "Implements the tasks (speckit implement)",
    "test-agent": "Writes independent adversarial tests; runs the verification gate",
    "review-agent": "Reviews the implementation against the spec",
    "flow-bot": "The pipeline itself: guards transitions, explains reverted moves",
    "constitution-agent": "Drafts and amends each project's constitution (speckit constitution)",
}


def pw() -> str:
    return secrets.token_urlsafe(24) + "aA1!"


class PlaneSession:
    def __init__(self, base: str):
        self.base = base

    def form_auth(self, path: str, data: dict) -> httpx.Client:
        c = httpx.Client(base_url=self.base, follow_redirects=False, timeout=30)
        token = c.get("/auth/get-csrf-token/").json()["csrf_token"]
        r = c.post(path, data={"csrfmiddlewaretoken": token, **data})
        loc = r.headers.get("location", "")
        if r.status_code not in (301, 302) or "error_code" in loc:
            raise RuntimeError(f"{path} failed: {r.status_code} {loc or r.text[:300]}")
        c.headers["X-CSRFToken"] = c.cookies.get("csrftoken", token)
        return c

    def sign_up_or_in(self, email: str, password: str) -> httpx.Client:
        try:
            return self.form_auth("/auth/sign-up/", {"email": email, "password": password})
        except RuntimeError:
            return self.form_auth("/auth/sign-in/", {"email": email, "password": password})


def api_container(project: str | None) -> str:
    """Plane's API container, found by compose labels so any install folder / project name works."""
    flt = ["--filter", "label=com.docker.compose.service=api"]
    if project:
        flt += ["--filter", f"label=com.docker.compose.project={project}"]
    out = subprocess.run(["docker", "ps", *flt, "--format", "{{.Names}}\t{{.Image}}"],
                         text=True, capture_output=True).stdout
    names = [line.split("\t")[0] for line in out.splitlines() if "plane-backend" in line]
    if len(names) != 1:
        sys.exit(f"expected exactly one running Plane API container, found: {names or 'none'} "
                 f"(set plane_compose_project in orchestrator/config.json to choose)")
    return names[0]


def django_shell(container: str, code: str) -> str:
    r = subprocess.run(["docker", "exec", "-i", container, "python", "manage.py", "shell"],
                       input=code, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:])
    return r.stdout


def main() -> None:
    s = settings.load()
    ap = argparse.ArgumentParser(description="Set up Plane for plane-flow (idempotent).")
    ap.add_argument("--admin-email")
    ap.add_argument("admin_email_pos", nargs="?", help=argparse.SUPPRESS)  # old positional form
    ap.add_argument("--plane-url", default=s["plane_api_url"])
    ap.add_argument("--webhook-url", default=s["webhook_url"])
    ap.add_argument("--workspace-name", default=s["workspace_name"])
    ap.add_argument("--workspace-slug", default=s["workspace_slug"])
    ap.add_argument("--compose-project", default=s["plane_compose_project"])
    args = ap.parse_args()
    base = args.plane_url.rstrip("/")
    plane = PlaneSession(base)

    SECRETS.mkdir(exist_ok=True)
    SECRETS.chmod(0o700)
    cfg = json.loads(OUT.read_text()) if OUT.exists() else {}
    admin_email = args.admin_email or args.admin_email_pos or cfg.get("admin", {}).get("email")
    if not admin_email:
        sys.exit("--admin-email is required on first run")
    cfg["base_url"], cfg["workspace_slug"] = base, args.workspace_slug
    cfg.setdefault("admin", {"email": admin_email, "password": pw()})
    cfg.setdefault("workers", {})
    for name in WORKERS:
        cfg["workers"].setdefault(name, {"email": f"{name}@agents.example.com", "password": pw()})
    OUT.write_text(json.dumps(cfg, indent=2))
    OUT.chmod(0o600)

    # 1. instance admin (first run) or plain sign-in (re-run / existing instance)
    admin = cfg["admin"]
    if not httpx.get(f"{base}/api/instances/").json()["instance"]["is_setup_done"]:
        plane.form_auth("/api/instances/admins/sign-up/", {
            "email": admin["email"], "password": admin["password"],
            "first_name": "Admin", "last_name": "", "company_name": "plane-flow",
            "is_telemetry_enabled": "False",
        })
        print("instance admin created")
    a = plane.form_auth("/auth/sign-in/", admin)

    # 2. workspace
    slug = args.workspace_slug
    if a.get(f"/api/workspaces/{slug}/").status_code != 200:
        a.post("/api/workspaces/", json={"name": args.workspace_name, "slug": slug,
                                         "organization_size": "Just myself"}).raise_for_status()
        print(f"workspace {slug} created")

    # 3. worker accounts through the real sign-up flow
    for w in cfg["workers"].values():
        if "id" not in w:
            plane.sign_up_or_in(w["email"], w["password"])
    print(f"{len(cfg['workers'])} worker accounts ready")

    # 4. names, workspace membership and API tokens (no public API for these in Plane CE).
    #    Project membership is added per project by the orchestrator's provisioning.
    out = django_shell(api_container(args.compose_project), f"""
import json
from plane.db.models import User, Profile, Workspace, WorkspaceMember, APIToken
ws = Workspace.objects.get(slug={slug!r})
people = {json.dumps({**{n: w['email'] for n, w in cfg['workers'].items()}, 'admin': admin['email']})}
tokens = {{}}
for name, email in people.items():
    u = User.objects.get(email=email)
    if name != 'admin':
        u.first_name, u.last_name, u.display_name = name, '', name
        u.save()
        WorkspaceMember.objects.get_or_create(workspace=ws, member=u, defaults={{'role': 15}})
    Profile.objects.filter(user=u).update(is_onboarded=True, is_tour_completed=True, last_workspace_id=ws.id,
        onboarding_step={{'profile_complete': True, 'workspace_create': True, 'workspace_invite': True, 'workspace_join': True}})
    t, _ = APIToken.objects.get_or_create(user=u, workspace=ws, label='plane-flow',
        defaults={{'user_type': 0 if name == 'admin' else 1, 'allowed_rate_limit': '600/min'}})
    tokens[name] = {{'id': str(u.id), 'token': t.token}}
print('JSON:' + json.dumps(tokens))
""")
    tokens = json.loads(out.split("JSON:", 1)[1].strip().splitlines()[0])
    admin.update(tokens.pop("admin"))
    for name, t in tokens.items():
        cfg["workers"][name].update(t)

    # 5. webhook -> orchestrator (the orchestrator re-points it at startup if settings change)
    events = {"project": True, "issue": True, "issue_comment": True, "cycle": False, "module": False}
    hooks = a.get(f"/api/workspaces/{slug}/webhooks/").json()
    known = cfg.get("webhook", {}).get("id")
    hook = next((h for h in hooks if h["id"] == known or h["url"] == args.webhook_url), None)
    if hook is None:
        r = a.post(f"/api/workspaces/{slug}/webhooks/", json={"url": args.webhook_url, **events})
        if r.status_code >= 400:
            sys.exit(f"webhook create failed: {r.status_code} {r.text}\n"
                     "Plane blocks private addresses unless WEBHOOK_ALLOWED_HOSTS/IPS in its .env allow them.")
        hook = r.json()
        print(f"webhook created -> {args.webhook_url}")
    elif hook["url"] != args.webhook_url:
        a.patch(f"/api/workspaces/{slug}/webhooks/{hook['id']}/", json={"url": args.webhook_url, **events})
        print(f"webhook re-pointed -> {args.webhook_url}")
    secret = hook.get("secret_key") or cfg.get("webhook", {}).get("secret")
    cfg["webhook"] = {"id": hook["id"], "url": args.webhook_url, "secret": secret}

    OUT.write_text(json.dumps(cfg, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
