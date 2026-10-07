"""Turn any Plane project into a pipeline project: columns, workers, repo, setup page.

Every project in the workspace is AI-managed. New projects get a fresh Spec Kit repo
at repos/<identifier>; nothing outside REPOS_ROOT is ever initialised.
"""
import json
import logging
import subprocess
import time
from pathlib import Path

from plane import Plane, md_to_html

log = logging.getLogger("flow")

ROOT = Path(__file__).resolve().parent.parent
REPOS_ROOT = ROOT / "repos"
REGISTRY = ROOT / "orchestrator" / "projects.json"

AI, HUMAN = "#3b82f6", "#f59e0b"
STATES = [  # name, group, colour, sequence (left-to-right board order)
    ("Specify 🤖", "started", AI, 30000),
    ("Spec Review 👤", "started", HUMAN, 31000),
    ("Plan 🤖", "started", AI, 32000),
    ("Plan Review 👤", "started", HUMAN, 33000),
    ("Tasks 🤖", "started", AI, 34000),
    ("Analyze 🤖", "started", AI, 34500),
    ("Implement 🤖", "started", AI, 35000),
    ("Verify 🤖", "started", AI, 35500),
    ("Code Review 🤖", "started", AI, 36000),
    ("Acceptance 👤", "started", HUMAN, 37000),
]
DROP_DEFAULT_STATES = {"Todo", "In Progress"}
STANDARDS = ("STD", "Standards")  # workspace-wide baseline lives in this project
CONSTITUTION_CARD = "📜 Project constitution"
BASELINE_PAGE = "📜 Baseline constitution (applies to every project)"
BASELINE_TEXT = """These principles apply to **every project** in this workspace. Each project's own constitution
must include them and may make them stricter, never weaker. Edit this page to change the baseline;
projects pick it up the next time their constitution is drafted or amended.

1. **Tested requirements.** Every functional requirement has at least one automated test, and the
   full test suite passes before code review.
2. **Clean failure.** Users never see an unhandled exception or traceback; errors are reported with a
   clear message and a non-zero exit code.
3. **Justified dependencies.** Every new third-party dependency is named and justified in the plan.
4. **No secrets in code.** Credentials, tokens and keys are never committed.
5. **Simplicity.** Choose the smallest design that meets the specification.
"""
CONSTITUTION_GUIDE = """This card governs the project's **constitution**: the rules every feature in this project is
specified, planned, built and checked against. Feature cards cannot start until it is ratified.

1. Write the rules you want for this project in this description (plain sentences are fine), or add them
   as `/note <rule>` comments. Plain comments are for people and are not sent to the agent.
2. Move this card to **Specify 🤖**: constitution-agent drafts the constitution, including the workspace
   baseline from the Standards project.
3. Review and edit the **📜 Constitution** page, or comment `/rework <changes>`.
4. Move this card to **Done** to ratify it. To amend later, move it from Done back to **Specify 🤖**.

Principles requested by the project owner:

"""
DEFAULT_TEST_CMD = ["uv", "run", "--quiet", "--no-project", "--with", "pytest", "--with-editable", ".",
                    "python", "-m", "pytest", "-q"]


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def projects_in(reg: dict) -> dict:
    """Registry entries for real Plane projects (skips pending:<IDENT> placeholders)."""
    return {k: v for k, v in reg.items() if not k.startswith("pending:")}


def save_registry(reg: dict) -> None:
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False))
    tmp.replace(REGISTRY)


def list_projects(cfg: dict) -> list[dict]:
    admin = Plane(cfg, "", {}).api("admin")
    r = admin.get(f"{cfg['base_url']}/api/v1/workspaces/{cfg['workspace_slug']}/projects/")
    r.raise_for_status()
    return [p for p in r.json()["results"] if not p.get("archived_at")]


def _run(cmd: list[str], cwd: Path) -> None:
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}…: {(p.stderr or p.stdout).strip()[-400:]}")


def ensure_states(admin) -> dict[str, str]:
    existing = {s["name"]: s for s in admin.get("/states/").json()["results"]}
    for name, group, color, seq in STATES:
        if name not in existing:
            r = admin.post("/states/", json={"name": name, "group": group, "color": color})
            r.raise_for_status()
            existing[name] = r.json()
        if existing[name].get("sequence") != seq:  # Plane assigns its own; keep pipeline order canonical
            admin.patch(f"/states/{existing[name]['id']}/", json={"sequence": seq})
            existing[name]["sequence"] = seq
    cards = admin.get("/work-items/", params={"per_page": 100}).json()["results"]
    used = {c["state"] for c in cards}
    for name in DROP_DEFAULT_STATES & existing.keys():
        if existing[name]["id"] not in used:  # never strand a card someone already put there
            admin.delete(f"/states/{existing[name]['id']}/")
            existing.pop(name)
    return {n: s["id"] for n, s in existing.items()}


def ensure_members(cfg: dict, admin) -> None:
    current = {m.get("member") or m.get("id") for m in _members(admin)}
    for w in cfg["workers"].values():
        if w["id"] not in current:
            r = admin.post("/members/", json={"member": w["id"], "role": 15})
            if r.status_code >= 400 and "already" not in r.text.lower():
                raise RuntimeError(f"adding {w['email']} failed: {r.status_code} {r.text[:200]}")


def _members(admin) -> list[dict]:
    r = admin.get("/members/")
    data = r.json() if r.status_code == 200 else []
    return data.get("results", data) if isinstance(data, dict) else data


def ensure_standards(cfg: dict) -> None:
    """Create the Standards project (holder of the baseline constitution) if it doesn't exist."""
    if any(p["identifier"] == STANDARDS[0] for p in list_projects(cfg)):
        return
    admin = Plane(cfg, "", {}).api("admin")
    r = admin.post(f"{cfg['base_url']}/api/v1/workspaces/{cfg['workspace_slug']}/projects/",
                   json={"identifier": STANDARDS[0], "name": STANDARDS[1],
                         "description": "Workspace-wide standards: the baseline constitution for every project."})
    r.raise_for_status()
    log.info("created the Standards project")


SKILLS_DIR = {"claude": ".claude/skills", "copilot": ".github/skills"}


def ensure_integration(path: Path, integration: str) -> bool:
    """Make sure the Spec Kit skills for this agent exist in the repo (adds them next to others, commits)."""
    if not (path / ".git").exists() or list((path / SKILLS_DIR.get(integration, ".x")).glob("speckit-*")):
        return False
    _run(["specify", "integration", "install", integration, "--force"], path)
    _run(["git", "add", "-A"], path)
    _run(["git", "-c", "user.name=flow-bot", "-c", "user.email=flow-bot@agents.example.com", "commit", "-q",
          "-m", f"Add Spec Kit {integration} integration (plane-flow)"], path)
    log.info("installed the Spec Kit %s integration in %s", integration, path)
    return True


def ensure_repo(path: Path, name: str, integration: str = "claude") -> bool:
    """Create a Spec Kit repo at `path` if there is none. Returns True if it was created."""
    if not path.resolve().is_relative_to(REPOS_ROOT.resolve()):
        raise RuntimeError(f"{path} is outside the allowed repos folder {REPOS_ROOT}")
    if (path / ".git").exists():
        return False
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], path)
    # --ignore-agent-tools: Spec Kit otherwise refuses when the agent CLI isn't installed yet (e.g. a fresh
    # server); plane-flow checks the agent CLI itself (install.sh) and the backend decides which agent runs.
    _run(["specify", "init", "--here", "--force", "--non-interactive", "--integration", integration,
          "--script", "sh", "--ignore-agent-tools"], path)
    (path / "README.md").write_text(f"# {name}\n\nRepository managed by the Plane → Spec Kit AI pipeline.\n")
    _run(["git", "add", "-A"], path)
    _run(["git", "-c", "user.name=flow-bot", "-c", "user.email=flow-bot@agents.example.com",
          "commit", "-q", "-m", "Initial commit: Spec Kit scaffold"], path)
    return True


def provision(cfg: dict, project_id: str, reg: dict) -> dict:
    """Idempotent: safe to run on a project that is already (partly) set up."""
    admin = Plane(cfg, project_id, {}).api("admin")
    proj = admin.get("/").json()
    ident = proj["identifier"]
    # A "pending:<IDENT>" entry lets settings (repo, test_cmd, backend, ...) be prepared before the Plane
    # project exists — used by import and the self-test. It is merged over the defaults on first sight.
    entry = reg.get(project_id) or {
        "identifier": ident, "name": proj["name"],
        "repo": f"repos/{ident.lower()}", "worktrees": f"worktrees/{ident.lower()}",
        "test_cmd": DEFAULT_TEST_CMD, "created": time.time(),
        **reg.pop(f"pending:{ident}", {}),
    }
    entry["identifier"], entry["name"] = ident, proj["name"]
    entry["states"] = ensure_states(admin)
    ensure_members(cfg, admin)
    backend = entry.get("backend") or cfg.get("default_backend", "claude")
    integration = "copilot" if backend == "copilot" else "claude"
    repo = Path(entry["repo"]) if Path(entry["repo"]).is_absolute() else ROOT / entry["repo"]
    created = ensure_repo(repo, proj["name"], integration) if entry["repo"].startswith("repos/") else False
    ensure_integration(repo, integration)
    reg[project_id] = entry
    save_registry(reg)
    bot = Plane(cfg, project_id, entry["states"])
    if ident == STANDARDS[0]:
        entry["role"] = "standards"
        if not entry.get("baseline_page"):
            entry["baseline_page"] = bot.create_page("flow-bot", BASELINE_PAGE, md_to_html(BASELINE_TEXT))
    elif not entry.get("constitution", {}).get("card_id"):
        card = bot.create_issue("flow-bot", CONSTITUTION_CARD, CONSTITUTION_GUIDE, "Backlog")
        entry["constitution"] = {"card_id": card["id"], "page_id": None, "version": None}
    save_registry(reg)
    if created or not entry.get("setup_page"):
        page_id = bot.create_page("flow-bot", "🤖 Pipeline setup", md_to_html(setup_text(entry, ROOT, cfg.get("dashboard_url", "http://localhost:8787"))))
        entry["setup_page"] = page_id
        save_registry(reg)
    log.info("provisioned %s (%s): repo %s", ident, proj["name"], entry["repo"])
    return entry


def setup_text(entry: dict, root: Path, dashboard: str = "http://localhost:8787") -> str:
    return f"""This project is managed by the AI pipeline. Set up automatically by **flow-bot**.

| | |
|---|---|
| Repository | `{root / entry['repo']}` |
| Work folders | `{root / entry['worktrees']}/<card>` |
| Verify runs | `{' '.join(entry['test_cmd'])}` |

**First:** ratify the project's rules on the **📜 Project constitution** card (move it to **Specify 🤖**,
review the draft, then move it to **Done**). Feature work is blocked until then.

**Then:** create a work item describing what you want, and move it to **Specify 🤖**.
Workers pick it up, post under their own names, and hand the card to you at the 👤 columns.

- A slash talks to the pipeline; plain comments are for people and never reach an agent.
- `/note <text>` adds to the brief for the card's next run (works in any column, including Backlog).
- `/answer 1: … 2: …` answers the spec's open questions in **Spec Review 👤**.
- `/rework <feedback>` in a 👤 column re-runs the previous worker with your feedback.
- `/retry` in a 🤖 column re-runs that step after a failure.
- Moves the pipeline doesn't allow are put back automatically, with an explanation.
- Cost and progress: the **📊 AI Metrics** page in this project, and the live dashboard on the host
  (`{dashboard}/dashboard/{entry['identifier']}`).
"""
