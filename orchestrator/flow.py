# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "markdown", "markdownify", "beautifulsoup4"]
# ///
"""Plane -> Spec Kit orchestrator.

Receives Plane webhooks (no polling). Every project in the workspace is AI-managed:
new projects are provisioned automatically (pipeline columns, workers, a Spec Kit
repo under repos/). Moving a work item into an AI column runs that phase's worker;
the worker publishes its artifact as a Plane page, comments under its own identity,
and hands the card to the next column.

Every card works in its own git worktree, so cards run in parallel, and a phase
can have a companion worker running beside it in a second worktree.
"""
import hashlib
import hmac
import json
import logging
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from backends import BACKENDS, Result  # noqa: E402
import metrics  # noqa: E402
import provision  # noqa: E402
import settings  # noqa: E402
from plane import Plane, html_to_md, md_to_html, text_of  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = settings.load()
CFG = json.loads((ROOT / ".secrets" / "plane.json").read_text())
CFG["base_url"] = SETTINGS["plane_api_url"]        # how this host calls Plane
CFG["public_url"] = SETTINGS["plane_public_url"]   # what people's browsers open
CFG["dashboard_url"] = SETTINGS["dashboard_url"]
CFG["default_backend"] = SETTINGS["backend"]
PORT = SETTINGS["port"]
MAX_FIX_LOOPS = SETTINGS["max_fix_loops"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("flow")

HEADLESS = (
    "\n\nYou are running unattended as part of an automated pipeline. Never ask the user "
    "questions or wait for input: make reasonable assumptions and record them in the artifact. "
    "Do not create or switch git branches and do not commit; the pipeline handles git. "
    "Finish with a summary of at most 8 short bullet points of what you produced."
)
ACCEPTANCE_DIR = "tests/acceptance"
CONSTITUTION_FILE = ".specify/memory/constitution.md"
NEEDS = re.compile(r"\[NEEDS CLARIFICATION:?\s*([^\]]*)\]", re.I)
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9_]{2,}\]")
# The spec phase may stop and ask: material ambiguity becomes a question for the human, not a guess.
ASK_INSTEAD = (
    "\n\nYou are running unattended: never wait for input. For details with a sensible default, assume and "
    "record the assumption. But where the answer materially changes scope, behaviour, or compliance with the "
    "project constitution, do NOT guess: leave a `[NEEDS CLARIFICATION: <one precise question>]` marker in the "
    "spec (at most 3). Do not create or switch git branches and do not commit. Finish with a summary of at most "
    "8 short bullet points."
)


@dataclass
class Phase:
    worker: str
    next_state: str
    skill: str | None = None      # Spec Kit skill to invoke
    artifact: str | None = None   # file in the feature dir published as a Plane page
    title: str = ""               # page title suffix
    needs: tuple = ()             # artifacts that must already exist
    human_next: bool = False      # next column is a human gate -> assign to the requester
    companion: str | None = None  # worker that runs in parallel in its own worktree
    fanout: bool = False          # let the worker split its own task across sub-agents


IMPLEMENT, VERIFY, ANALYZE, SPECIFY = "Implement 🤖", "Verify 🤖", "Analyze 🤖", "Specify 🤖"
PHASES = {
    "Specify 🤖": Phase("spec-agent", "Spec Review 👤", "speckit-specify", "spec.md", "Spec", human_next=True),
    "Plan 🤖": Phase("plan-agent", "Plan Review 👤", "speckit-plan", "plan.md", "Plan", ("spec.md",), True),
    "Tasks 🤖": Phase("tasks-agent", ANALYZE, "speckit-tasks", "tasks.md", "Tasks", ("spec.md", "plan.md")),
    IMPLEMENT: Phase("dev-agent", VERIFY, "speckit-implement", None, "", ("tasks.md",),
                     companion="test-agent", fanout=True),
    "Code Review 🤖": Phase("review-agent", "Acceptance 👤", None, "review.md", "Review", ("tasks.md",), True),
}
AFTER_VERIFY = "Code Review 🤖"
# Human gate -> the AI phase that produced what is being reviewed (for /rework).
REWORK = {"Spec Review 👤": "Specify 🤖", "Plan Review 👤": "Plan 🤖", "Acceptance 👤": IMPLEMENT}
WORKER_IDS = {w["id"] for w in CFG["workers"].values()}

# Plane CE cannot restrict state changes, so the pipeline enforces them: a move a
# human makes that is not listed here is put back, with a comment saying why.
# One table per track; workers' own hand-offs are always trusted.
GUARD = "flow-bot"
ENTRY, FINAL, CANCELLED = "Backlog", "Done", "Cancelled"
HUMAN_MOVES = {
    "feature": {
        "Backlog": {"Specify 🤖"},
        "Specify 🤖": {"Backlog"},
        "Spec Review 👤": {"Plan 🤖", "Specify 🤖", "Backlog"},
        "Plan 🤖": {"Spec Review 👤"},
        "Plan Review 👤": {"Tasks 🤖", "Plan 🤖", "Spec Review 👤"},
        "Tasks 🤖": {"Plan Review 👤"},
        ANALYZE: {"Plan Review 👤"},
        IMPLEMENT: {"Plan Review 👤"},
        VERIFY: {IMPLEMENT},
        "Code Review 🤖": {IMPLEMENT},
        "Acceptance 👤": {"Done", IMPLEMENT},
        "Done": set(),
        "Cancelled": {"Backlog"},
    },
    # The constitution card reuses Specify (draft) and Spec Review (review); Done ratifies it,
    # and moving it from Done back to Specify starts an amendment.
    "constitution": {
        "Backlog": {SPECIFY},
        SPECIFY: {"Backlog"},
        "Spec Review 👤": {"Done", SPECIFY},
        "Done": {SPECIFY},
        "Cancelled": {"Backlog"},
    },
}
START_STATES = {"feature": SPECIFY, "constitution": SPECIFY}  # where a card may be created directly
CONSTITUTION_WORKER = "constitution-agent"


def track_of(proj: "Project", data: dict) -> str:
    return "constitution" if data.get("id") and data["id"] == proj.constitution_card else "feature"


def allowed_moves(track: str, old: str) -> set:
    moves = set(HUMAN_MOVES[track].get(old, set()))
    return moves if old == FINAL else moves | {CANCELLED}


class Project:
    """One Plane project with its repo, worktrees, Plane client and dashboard links."""

    def __init__(self, pid: str, entry: dict):
        self.id, self.ident, self.name = pid, entry["identifier"], entry["name"]
        self.repo = (ROOT / entry["repo"]).resolve()     # stays on main; merges land here
        self.worktrees = ROOT / entry["worktrees"]       # one checkout per card branch
        self.test_cmd = entry["test_cmd"]
        self.states = entry["states"]
        self.plane = Plane(CFG, pid, entry["states"])
        self.backend = BACKENDS[entry.get("backend") or SETTINGS["backend"]]()   # per project, else workspace default
        self.role = entry.get("role", "pipeline")
        const = entry.get("constitution") or {}
        self.constitution_card, self.constitution_page = const.get("card_id"), const.get("page_id")
        self.ratified: str | None = const.get("version")      # ratified constitution version, if any
        self.metrics: dict | None = None
        base = f"{CFG['public_url']}/{CFG['workspace_slug']}"
        self.links = {"board": f"{base}/projects/{pid}/issues/", "browse": f"{base}/browse/",
                      "metrics_page": None, "dashboard": f"{CFG['dashboard_url']}/dashboard/{self.ident}"}


projects: dict[str, Project] = {}          # Plane project id -> Project
registry_lock = threading.Lock()


def by_ident(ident: str) -> Project | None:
    return next((p for p in projects.values() if p.ident == ident), None)


backend = BACKENDS[SETTINGS["backend"]]()
slots = threading.Semaphore(SETTINGS["max_parallel"])  # concurrent agent processes
git_lock = threading.Lock()                # worktree add/remove and merges touch shared git state
guard_lock = threading.Lock()
issue_locks: dict[str, threading.Lock] = {}
busy: dict[str, int] = {}                  # issue id -> jobs queued or running
reverting: dict[tuple[str, str], float] = {}  # (issue id, state id) -> expiry of the guard's own moves
pending_feedback: dict[str, str] = {}      # issue id -> verify failure output for dev-agent
live_runs: dict[str, dict] = {}            # what is running right now, for the dashboard
feed: deque = deque(maxlen=200)            # recent pipeline events, newest first
subscribers: list[tuple[queue.Queue, str | None]] = []  # open dashboard streams (queue, project or all)
metrics_dirty: set[str] = set()            # project ids whose metrics need a refresh
metrics_wake = threading.Event()


def emit(kind: str, text: str, project: str | None = None, **extra) -> None:
    """Push an event to every open dashboard (server-sent events; nothing polls)."""
    ev = {"ts": time.time(), "kind": kind, "text": text, "project": project, **extra}
    if kind != "metrics":
        feed.appendleft(ev)
    for sub, scope in list(subscribers):
        if scope is None or scope == project:
            sub.put(ev)


def mark_dirty(proj: "Project") -> None:
    metrics_dirty.add(proj.id)
    metrics_wake.set()


db_lock = threading.Lock()
db = sqlite3.connect(ROOT / "orchestrator" / "flow.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS pages(issue_id TEXT, artifact TEXT, page_id TEXT, text_hash TEXT,
    PRIMARY KEY(issue_id, artifact));
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts REAL, issue TEXT, phase TEXT, worker TEXT,
    backend TEXT, ok INTEGER, duration_s REAL, turns INTEGER, cost_usd REAL,
    tokens_in INTEGER, tokens_out INTEGER, session_id TEXT);
""")
_cols = [c[1] for c in db.execute("PRAGMA table_info(runs)")]
if "subagents" not in _cols:
    db.execute("ALTER TABLE runs ADD COLUMN subagents INTEGER DEFAULT 0")
if "project" not in _cols:
    db.execute("ALTER TABLE runs ADD COLUMN project TEXT")
    db.commit()


def q(sql: str, *args):
    with db_lock:
        rows = db.execute(sql, args).fetchall()
        db.commit()
        return rows


def record(item: "Item", label: str, worker: str, res: Result, engine: str | None = None) -> None:
    q("INSERT INTO runs(ts,issue,phase,worker,backend,ok,duration_s,turns,cost_usd,tokens_in,tokens_out,"
      "session_id,subagents,project) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", time.time(), item.key, label, worker,
      engine or item.proj.backend.name, int(res.ok), res.duration_s, res.turns, res.cost_usd, res.tokens_in,
      res.tokens_out, res.session_id, res.subagents, item.proj.ident)
    mark_dirty(item.proj)


def git(*args: str, cwd: Path, worker: str = "flow") -> str:
    p = subprocess.run(
        ["git", "-c", f"user.name={worker}", "-c", f"user.email={worker}@agents.example.com", *args],
        cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def commit(wt: Path, worker: str, message: str) -> bool:
    git("add", "-A", cwd=wt)
    if not git("status", "--porcelain", cwd=wt):
        return False
    git("commit", "-q", "-m", message, cwd=wt, worker=worker)
    return True


def add_worktree(proj: Project, path: Path, branch: str, base: str) -> None:
    """Check `branch` out at `path`, creating it from `base` if it does not exist yet."""
    with git_lock:
        git("worktree", "prune", cwd=proj.repo)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if git("branch", "--list", branch, cwd=proj.repo):
            git("worktree", "add", "-q", str(path), branch, cwd=proj.repo)
        else:
            git("worktree", "add", "-q", "-b", branch, str(path), base, cwd=proj.repo)


def drop_worktree(proj: Project, path: Path, branch: str | None = None) -> None:
    with git_lock:
        if path.exists():
            git("worktree", "remove", "--force", str(path), cwd=proj.repo)
        if branch and git("branch", "--list", branch, cwd=proj.repo):
            git("branch", "-q", "-D", branch, cwd=proj.repo)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "feature"


class Item:
    """A Plane work item bound to a git branch, a worktree and a Spec Kit feature directory."""

    def __init__(self, proj: Project, issue_id: str, worker: str):
        self.proj, self.plane = proj, proj.plane
        d = self.plane.issue(worker, issue_id)
        self.id, self.name = issue_id, d["name"]
        self.key = f"{proj.ident}-{d['sequence_id']}"
        self.description = html_to_md(d.get("description_html") or "").strip()
        self.state = self.plane.state_name.get(d["state"], "")
        # Hand-backs go to whoever created the card, or to the admin for cards the pipeline created itself.
        self.requester = d.get("created_by") if d.get("created_by") not in WORKER_IDS else CFG["admin"]["id"]
        self.branch = self.key.lower()
        self.wt = proj.worktrees / self.branch
        self.feature_rel = ""

    def open(self) -> "Item":
        add_worktree(self.proj, self.wt, self.branch, "main")
        specs = self.wt / "specs"
        existing = sorted(specs.glob(f"{self.branch}-*")) if specs.exists() else []
        self.feature_rel = f"specs/{existing[0].name}" if existing else f"specs/{self.branch}-{slugify(self.name)}"
        return self

    @property
    def feature_dir(self) -> Path:
        return self.wt / self.feature_rel

    def env(self) -> dict:
        return {"SPECIFY_FEATURE_DIRECTORY": self.feature_rel, "SPECIFY_FEATURE": Path(self.feature_rel).name}


def artifact_path(item: Item, artifact: str) -> Path:
    return item.wt / CONSTITUTION_FILE if artifact == "constitution" else item.feature_dir / artifact


def placeholders(path: Path) -> list[str]:
    """Unfilled template tokens, ignoring HTML comments (Spec Kit's Sync Impact Report names old tokens there)."""
    if not path.exists():
        return ["(no constitution file)"]
    return sorted(set(PLACEHOLDER.findall(re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S))))


def constitution_version(path: Path) -> str | None:
    m = re.search(r"\*\*Version\*\*:\s*([0-9]+\.[0-9]+\.[0-9]+)", path.read_text()) if path.exists() else None
    return m.group(1) if m else None


def sync_constitution(item: Item) -> str | None:
    """Feature work always follows the ratified constitution on main, even on branches cut earlier."""
    main_file, wt_file = item.proj.repo / CONSTITUTION_FILE, item.wt / CONSTITUTION_FILE
    if main_file.exists() and (not wt_file.exists() or wt_file.read_text() != main_file.read_text()):
        wt_file.parent.mkdir(parents=True, exist_ok=True)
        wt_file.write_text(main_file.read_text())
        commit(item.wt, "flow-bot", f"{item.key}: follow constitution v{constitution_version(main_file)} from main")
    return constitution_version(wt_file)


def open_questions(item: Item) -> list[str]:
    spec = item.feature_dir / "spec.md"
    return [q_.strip() for q_ in NEEDS.findall(spec.read_text())] if spec.exists() else []


# Comments: a slash talks to the pipeline, plain text talks to people (never sent to an agent).
COMMANDS = ("/note", "/answer", "/rework", "/retry")
PLAIN_HINT = ("💬 Plain comments are for people and aren't sent to the agents. Use `/note <text>` to add something "
              "to the brief for this card's next run, `/answer 1: …` to answer open questions, or "
              "`/rework <text>` in a 👤 column to redo the last step now.")


def comment_text(c: dict) -> str:
    return BeautifulSoup(c.get("comment_html") or "", "html.parser").get_text(" ").strip()


def human_commands(item: Item, prefix: str, since_worker: str | None = None) -> list[str]:
    """Bodies of the human's `prefix …` comments since a worker last completed a run on this card."""
    comments = item.plane.comments(GUARD, item.id)
    ids = {CFG["workers"][since_worker]["id"]} if since_worker else WORKER_IDS
    last = max((i for i, c in enumerate(comments) if c["actor"] in ids and "✅" in c["comment_html"]), default=-1)
    texts = [comment_text(c) for c in comments[last + 1:] if c["actor"] not in WORKER_IDS]
    return [t[len(prefix):].strip() for t in texts if t.lower().startswith(prefix) and t[len(prefix):].strip()]


def human_answers(item: Item) -> list[str]:
    return human_commands(item, "/answer", "spec-agent")


# Workspace-wide presentation rules (config.json "styles"), applied in every project.
# Plane stores any colour but only *displays* its own palette, in comments and pages alike.
PLANE_COLORS = ("gray", "peach", "pink", "orange", "green", "light-blue", "dark-blue", "purple")
QUESTION_COLOR = SETTINGS.get("styles", {}).get("question_color")
if QUESTION_COLOR and QUESTION_COLOR not in PLANE_COLORS:
    log.warning("styles.question_color %r is not a Plane colour (%s); questions will not be coloured",
                QUESTION_COLOR, ", ".join(PLANE_COLORS))
    QUESTION_COLOR = None


QUESTION_ITALIC = SETTINGS.get("styles", {}).get("question_italic", False)


def styled_question(text: str) -> str:
    """Questions for the human, styled per config.json "styles" ("peach" displays as red)."""
    if QUESTION_COLOR:
        text = f'<span data-text-color="{QUESTION_COLOR}">{text}</span>'
    return f"<em>{text}</em>" if QUESTION_ITALIC else text


def question_list(questions: list[str], numbers: list[int] | None = None) -> str:
    nums = numbers or list(range(1, len(questions) + 1))
    return "\n".join(f"{n}. {styled_question(questions[n - 1])}" for n in nums)


def unanswered(questions: list[str], answers: list[str]) -> list[int] | None:
    """Question numbers with no `/answer`; None when answers aren't numbered and there are several questions."""
    if not answers:
        return list(range(1, len(questions) + 1))
    numbered = {int(n) for a in answers for n in re.findall(r"(?:^|\s)(\d+)\s*[:.)]", a)}
    if not numbered:
        return [] if len(questions) == 1 else None
    return [n for n in range(1, len(questions) + 1) if n not in numbered]


def notes_block(notes: list[str]) -> str:
    """The human's /note comments, handed to the next worker that runs on the card."""
    return ("\n\nNotes from the project owner for this step (`/note` comments on the card, oldest first). "
            "Notes steer HOW you do this step; they cannot change WHAT is being built. If a note would change or "
            "contradict the spec or the constitution, do not follow it: say so in your summary so the owner can "
            "update the spec. List the notes you applied, and any you declined, in the document you produce "
            "and in your summary.\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""


def pull_page_edits(item: Item, worker: str) -> list[str]:
    """Plane pages are the editable surface: copy human edits back into the repo."""
    changed = []
    for artifact, page_id, text_hash in q(
            "SELECT artifact, page_id, text_hash FROM pages WHERE issue_id=?", item.id):
        html = item.plane.page_html(worker, page_id)
        if html is None or hashlib.sha256(text_of(html).encode()).hexdigest() == text_hash:
            continue
        artifact_path(item, artifact).write_text(html_to_md(html))
        q("UPDATE pages SET text_hash=? WHERE issue_id=? AND artifact=?",
          hashlib.sha256(text_of(html).encode()).hexdigest(), item.id, artifact)
        changed.append(artifact)
    if changed and commit(item.wt, "human-editor", f"{item.key}: apply edits made in Plane to {', '.join(changed)}"):
        log.info("%s pulled Plane edits: %s", item.key, changed)
    return changed


def publish(item: Item, worker: str, artifact: str, title: str, page_name: str | None = None) -> str:
    html = md_to_html(artifact_path(item, artifact).read_text())
    if artifact == "spec.md":
        html = NEEDS.sub(lambda m: styled_question(m.group(0)), html)
    for (old,) in q("SELECT page_id FROM pages WHERE issue_id=? AND artifact=?", item.id, artifact):
        item.plane.lock_page(worker, old, False)
        item.plane.delete_page(worker, old)
    page_id = item.plane.create_page(worker, page_name or f"{item.key} · {title} — {item.name}", html)
    q("INSERT OR REPLACE INTO pages VALUES(?,?,?,?)", item.id, artifact, page_id,
      hashlib.sha256(text_of(html).encode()).hexdigest())
    url = item.plane.page_url(page_id)
    item.plane.link(worker, item.id, f"{title} ({artifact})", url)
    return url


def build_prompt(phase: Phase, item: Item, feedback: str | None) -> str:
    f = item.feature_rel
    if feedback:
        if phase.artifact == "plan.md":  # the plan step also owns research, data model, quickstart and contracts
            return (f"Rework requested for feature `{f}` ({item.key}: {item.name}).\n\nFeedback:\n{feedback}\n\n"
                    f"Revise `{f}/plan.md` accordingly, keeping the Spec Kit template structure, and update every "
                    f"design document the plan step produced (`research.md`, `data-model.md`, `quickstart.md`, "
                    f"`contracts/`) so they all agree with the revised plan. Re-run the plan's Constitution Check "
                    f"against `{CONSTITUTION_FILE}`." + HEADLESS)
        if phase.artifact:
            return (f"Rework requested for feature `{f}` ({item.key}: {item.name}).\n\nFeedback:\n{feedback}\n\n"
                    f"Revise `{f}/{phase.artifact}` accordingly, keeping the Spec Kit template structure." + HEADLESS)
        return (f"Rework requested for feature `{f}` ({item.key}: {item.name}).\n\nFeedback:\n{feedback}\n\n"
                f"Fix the implementation, following `{f}/tasks.md` and `{f}/spec.md`, until the whole test suite "
                f"passes. Do NOT modify anything under `{ACCEPTANCE_DIR}/`: those are independent tests. If you "
                f"believe one of them contradicts the spec, leave it alone and say so in your summary." + HEADLESS)
    if phase.skill == "speckit-specify":
        return item.proj.backend.skill_prompt(phase.skill, (
            f"{item.name}\n\n{item.description}\n\n"
            f"SPECIFY_FEATURE_DIRECTORY is `{f}` — use exactly this directory. The project constitution is "
            f"`{CONSTITUTION_FILE}`: the spec must not require anything it forbids.") + ASK_INSTEAD)
    if phase.skill == "speckit-implement":
        return item.proj.backend.skill_prompt(phase.skill, (
            f"Feature directory: `{f}`. An independent tester is writing black-box tests in "
            f"`{ACCEPTANCE_DIR}/` in parallel; do not create or edit that directory.")) + HEADLESS
    if phase.skill:
        return item.proj.backend.skill_prompt(phase.skill, f"Feature directory: `{f}`.") + HEADLESS
    return (f"You are the code reviewer for feature `{f}` ({item.key}: {item.name}). Review the "
            f"implementation on the current branch (`git diff main...HEAD`) against `{f}/spec.md`, "
            f"`{f}/plan.md` and `{f}/tasks.md`. Do NOT modify any source code. "
            f"Write `{f}/review.md` with: a first line `Verdict: APPROVE` or `Verdict: CHANGES REQUESTED`, "
            f"each acceptance criterion with pass/fail, and concrete findings." + HEADLESS)


def tester_prompt(item: Item) -> str:
    f = item.feature_rel
    return (f"You are test-agent, an independent and adversarial tester for feature `{f}` ({item.key}: {item.name}). "
            f"The implementation is being written right now by someone else and you cannot see it. Using only "
            f"`{f}/spec.md`, `{f}/plan.md` and anything under `{f}/contracts/`, write black-box tests in "
            f"`{ACCEPTANCE_DIR}/` that (1) cover every acceptance scenario and functional requirement and "
            f"(2) try to break the feature: boundary values, malformed and hostile input, the edge cases the spec "
            f"lists and ones it forgot. Exercise only the public interface the plan and contracts define (for a CLI, "
            f"run it as a subprocess via `sys.executable -m <package>`). Where the spec is silent, assert no more "
            f"than that the program fails cleanly without a traceback. Do not write implementation code, and touch "
            f"nothing outside `{ACCEPTANCE_DIR}/` and `{f}/test-plan.md`. Write `{f}/test-plan.md`: a table mapping "
            f"each test to the requirement or risk it targets, marking which are adversarial." + HEADLESS)


class tracking:
    """Registers a running worker so the dashboard can show it live."""

    def __init__(self, item: Item, worker: str, label: str, branch: str, engine: str | None = None):
        self.id = uuid.uuid4().hex
        self.info = {"project": item.proj.ident, "card": item.key, "card_id": item.id, "title": item.name,
                     "worker": worker, "phase": label, "branch": branch, "backend": engine or item.proj.backend.name,
                     "started": time.time()}

    def __enter__(self):
        live_runs[self.id] = self.info
        emit("start", f"{self.info['worker']} started {self.info['phase']}", self.info["project"],
             card=self.info["card"], worker=self.info["worker"])

    def __exit__(self, *exc):
        live_runs.pop(self.id, None)


def stats(res: Result, branch: str, cver: str | None = None) -> str:
    fan = f" · {res.subagents} sub-agents" if res.subagents else ""
    con = f" · constitution v{cver}" if cver else ""
    return f"_{res.duration_s:.0f}s · {res.turns} turns{fan} · ${res.cost_usd:.2f} · branch `{branch}`{con}_"


def execute(worker: str, item: Item, wt: Path, label: str, prompt: str, must_exist: str | None = None,
            fanout: bool = False) -> Result:
    """Run one agent in one worktree: announce, run, record metrics, commit."""
    item.plane.comment(worker, item.id, f"▶️ **{worker}** started **{label}** (backend: {item.proj.backend.name}).")
    with slots:
        log.info("%s %s: %s started in %s", item.key, label, worker, wt.name)
        with tracking(item, worker, label, wt.name):
            res = item.proj.backend.run(prompt, str(wt), item.env(), SETTINGS["models"].get(worker), fanout)
    record(item, label, worker, res)
    emit("finish", f"{worker} {'finished' if res.ok else 'FAILED'} {label}", item.proj.ident, card=item.key,
         ok=res.ok, cost=res.cost_usd, duration_s=res.duration_s, worker=worker)
    if res.ok and must_exist and not (wt / must_exist).exists():
        res.ok, res.text = False, f"worker finished but `{must_exist}` was not produced.\n\n{res.text}"
    res.raw["committed"] = commit(wt, worker, f"{item.key}: {label} ({worker})")
    log.info("%s %s: %s finished ok=%s in %.0fs, %d sub-agents", item.key, label, worker, res.ok,
             res.duration_s, res.subagents)
    return res


def run_companion(item: Item, worker: str, out: dict) -> None:
    """The tester works on a branch cut before any implementation exists, so it cannot see the code."""
    branch = f"{item.branch}-tests"
    wt = item.proj.worktrees / branch
    try:
        drop_worktree(item.proj, wt, branch)
        add_worktree(item.proj, wt, branch, item.branch)
        out["wt"], out["branch"] = wt, branch
        out["res"] = execute(worker, item, wt, "acceptance tests", tester_prompt(item),
                             f"{item.feature_rel}/test-plan.md")
    except Exception as e:
        log.exception("%s companion crashed", item.key)
        out["error"] = str(e)


def finish_companion(item: Item, worker: str, out: dict) -> None:
    """Merge the tester's branch into the card's branch and report under the tester's name."""
    res: Result | None = out.get("res")
    if res is None or not res.ok:
        detail = out.get("error") or (res.text[-1200:] if res else "no result")
        item.plane.comment(worker, item.id, f"❌ **acceptance tests** failed, so verification will only run the "
                                            f"developer's own tests.\n\n```\n{detail}\n```")
        return
    try:
        with git_lock:
            git("merge", "--no-ff", "-q", "-m", f"{item.key}: merge independent acceptance tests",
                out["branch"], cwd=item.wt, worker=worker)
    except RuntimeError as e:
        subprocess.run(["git", "merge", "--abort"], cwd=item.wt, capture_output=True)
        item.plane.comment(worker, item.id, f"⚠️ Could not merge the acceptance tests into `{item.branch}`: `{e}`")
        return
    drop_worktree(item.proj, out["wt"], out["branch"])
    url = publish(item, worker, "test-plan.md", "Test Plan")
    item.plane.comment(worker, item.id, f"✅ **acceptance tests** complete, written without sight of the "
                                        f"implementation and merged into `{item.branch}`.\n\n{res.text.strip()}\n\n"
                                        f"📄 [Test Plan page]({url})\n\n{stats(res, item.branch)}")


def run_phase(proj: Project, issue_id: str, state: str, feedback: str | None = None, stay: bool = False) -> None:
    phase = PHASES[state]
    w = phase.worker
    item = Item(proj, issue_id, w)
    if state == IMPLEMENT and not feedback:
        feedback = pending_feedback.pop(issue_id, None)
    label = f"{state.rsplit(' ', 1)[0].lower()}{' (rework)' if feedback else ''}"
    try:
        item.open()
        cver = sync_constitution(item)
        missing = [a for a in phase.needs if not (item.feature_dir / a).exists()]
        if missing:
            proj.plane.comment(w, item.id, f"⛔ Can't run **{label}** yet: missing {', '.join(missing)}. "
                                           f"Run the earlier phases first.")
            return
        pulled = pull_page_edits(item, w)
        notes = human_commands(item, "/note")
        if state == "Plan 🤖" and not feedback and not clarify_spec(item, cver):
            return
        proj.plane.update_issue(w, item.id, assignees=[CFG["workers"][w]["id"]])

        side, side_thread = {}, None
        if phase.companion and not feedback:
            side_thread = threading.Thread(target=run_companion, args=(item, phase.companion, side))
            side_thread.start()
        res = execute(w, item, item.wt, label, build_prompt(phase, item, feedback) + notes_block(notes),
                      f"{item.feature_rel}/{phase.artifact}" if phase.artifact else None, phase.fanout)
        if side_thread:
            side_thread.join()
            finish_companion(item, phase.companion, side)

        if not res.ok:
            proj.plane.comment(w, item.id, f"❌ **{label}** failed.\n\n```\n{res.text[-1500:]}\n```\n\n"
                                           f"{stats(res, item.branch, cver)}\n\nFix the input, then comment "
                                           f"`/retry` on this card.")
            return
        body = f"✅ **{label}** complete.\n\n{res.text.strip()}\n\n"
        if pulled:
            body += f"Picked up your edits from Plane: {', '.join(pulled)}.\n\n"
        if phase.artifact:
            url = publish(item, w, phase.artifact, phase.title)
            body += f"📄 [{phase.title} page]({url}) — editable in Plane; edits are used by the next phase.\n\n"
        if not res.raw.get("committed"):
            body += "⚠️ No file changes were produced.\n\n"
        questions = open_questions(item) if state == SPECIFY else []
        if questions:
            body += (f"❓ **{len(questions)} open question(s) need your answer before planning:**\n\n"
                     + question_list(questions)
                     + "\n\nAnswer with `/answer 1: … 2: …` on this card (one comment can answer several), or "
                       "edit the Spec page. Your answers are folded into the spec when you move the card to "
                       "**Plan 🤖**; moving on with unanswered questions is blocked.\n\n")
        if not stay:
            body += (f"👉 Review, then move this card to the next column to continue "
                     f"(or comment `/rework <feedback>`).\n\n" if phase.human_next
                     else f"➡️ Handing over to **{phase.next_state}**.\n\n")
        proj.plane.comment(w, item.id, body + stats(res, item.branch, cver))
        if not stay:
            proj.plane.move(w, item.id, phase.next_state, item.requester if phase.human_next else None)
    except Exception as e:  # make the failure visible on the card
        log.exception("%s %s crashed", item.key, label)
        proj.plane.comment(w, item.id, f"❌ **{label}** crashed in the orchestrator: `{e}`")


def clarify_spec(item: Item, cver: str | None) -> bool:
    """Fold the human's answers into the spec before planning. False = card sent back to Spec Review."""
    questions, answers = open_questions(item), human_answers(item)
    if not questions and not answers:
        return True
    review = item.proj.states["Spec Review 👤"]
    missing = unanswered(questions, answers) if questions else []
    if missing:
        put_back(item.proj, item.id, review,
                 f"{len(missing)} of {len(questions)} open question(s) on the spec are still unanswered:\n\n"
                 + question_list(questions, missing)
                 + "\n\nAnswer with `/answer 1: …` on this card (or edit the Spec page), then move it to "
                   "**Plan 🤖** again. "
                   "Moved back to **Spec Review 👤**.", item.requester)
        return False
    w, f = "spec-agent", item.feature_rel
    item.plane.update_issue(w, item.id, assignees=[CFG["workers"][w]["id"]])
    prompt = item.proj.backend.skill_prompt("speckit-clarify", (
        f"Feature directory: `{f}`. You are running unattended, so the questions have already been put to the "
        f"product owner. Open questions in the spec: {questions or '(none marked)'}. The product owner's answers, "
        f"verbatim and in the order given: {answers}. Do not ask anything further. Apply the answers following "
        f"this skill's rules: record them in the spec's Clarifications section, update the affected requirements, "
        f"and remove every resolved [NEEDS CLARIFICATION] marker. If a question has no usable answer, leave its "
        f"marker in place.")) + HEADLESS
    res = execute(w, item, item.wt, "clarify", prompt, f"{f}/spec.md")
    left = open_questions(item)
    if not res.ok or left:
        put_back(item.proj, item.id, review,
                 (f"Clarification failed: `{res.text[-300:]}`" if not res.ok else
                  f"These questions are still open after applying your answers:\n\n" + question_list(left)) + "\n\nMoved back to **Spec Review 👤**.", item.requester)
        return False
    url = publish(item, w, "spec.md", "Spec")
    item.plane.comment(w, item.id, f"✅ **clarify** complete: your answers are now part of the spec.\n\n"
                                   f"{res.text.strip()}\n\n📄 [Spec page]({url})\n\n➡️ Continuing with "
                                   f"**Plan 🤖**.\n\n{stats(res, item.branch, cver)}")
    return True


ANALYZE_TAIL = (
    "\n\nYou are running unattended: do not ask whether to remediate and do not modify any files. Output the "
    "complete report. The final line of your reply must be exactly `CRITICAL_COUNT: <n>`, where n is the number "
    "of CRITICAL findings (every conflict with the constitution is CRITICAL)."
)


def run_analyze(proj: Project, issue_id: str) -> None:
    """Gate before any code is written: spec, plan and tasks are consistent and obey the constitution."""
    w = "review-agent"
    item = Item(proj, issue_id, w)
    try:
        item.open()
        cver = sync_constitution(item)
        if not (item.feature_dir / "tasks.md").exists():
            proj.plane.comment(w, item.id, "⛔ Can't analyze yet: missing tasks.md. Run the earlier phases first.")
            return
        proj.plane.update_issue(w, item.id, assignees=[CFG["workers"][w]["id"]])
        pull_page_edits(item, w)
        res = execute(w, item, item.wt, "analyze", item.proj.backend.skill_prompt(
            "speckit-analyze", f"Feature directory: `{item.feature_rel}`.") + ANALYZE_TAIL)
        m = re.search(r"CRITICAL_COUNT:\s*(\d+)", res.text)
        if not res.ok or not m:
            proj.plane.comment(w, item.id, f"❌ **analyze** failed or gave no verdict.\n\n```\n{res.text[-1200:]}\n```"
                                           f"\n\nComment `/retry` on this card.\n\n{stats(res, item.branch, cver)}")
            return
        report = re.sub(r"\n?CRITICAL_COUNT:\s*\d+\s*$", "", res.text.strip())
        (item.feature_dir / "analysis.md").write_text(report + "\n")
        commit(item.wt, w, f"{item.key}: analysis report ({w})")
        url = publish(item, w, "analysis.md", "Analysis")
        n = int(m.group(1))
        if n == 0:
            proj.plane.comment(w, item.id, f"✅ **analyze** passed: spec, plan and tasks are consistent and comply "
                                           f"with the constitution (0 critical findings).\n\n📄 [Analysis page]({url})"
                                           f"\n\n➡️ Handing over to **{IMPLEMENT}**.\n\n{stats(res, item.branch, cver)}")
            proj.plane.move(w, item.id, IMPLEMENT)
            return
        crit = [ln.strip() for ln in report.splitlines() if "CRITICAL" in ln.upper()][:8]
        proj.plane.comment(w, item.id, f"🛑 **analyze** found **{n} critical issue(s)**, so no code will be written "
                                       f"until they are resolved.\n\n" + "\n".join(f"- {c}" for c in crit)
                           + f"\n\n📄 [Full analysis]({url})\n\n👉 Options: `/rework <how to fix>` here to re-plan, "
                             f"edit the Spec/Plan pages and move the card to **Tasks 🤖**, or amend the "
                             f"constitution if the rule itself should change.\n\n{stats(res, item.branch, cver)}")
        proj.plane.move(w, item.id, "Plan Review 👤", item.requester)
    except Exception as e:
        log.exception("%s analyze crashed", item.key)
        proj.plane.comment(w, item.id, f"❌ **analyze** crashed in the orchestrator: `{e}`")


def baseline_text() -> str:
    std = next((p for p in projects.values() if p.role == "standards"), None)
    page = provision.load_registry().get(std.id, {}).get("baseline_page") if std else None
    html = std.plane.page_html(GUARD, page) if page else None
    return html_to_md(html) if html else ""


def update_constitution_record(proj: Project, **fields) -> None:
    with registry_lock:
        reg = provision.load_registry()
        reg[proj.id].setdefault("constitution", {}).update(fields)
        provision.save_registry(reg)


def run_constitution(proj: Project, issue_id: str, feedback: str | None = None, stay: bool = False) -> None:
    """constitution-agent drafts (or amends) the project's rules; a human ratifies them by moving the card to Done."""
    w = CONSTITUTION_WORKER
    item = Item(proj, issue_id, w)
    label = ("constitution amendment" if proj.ratified else "constitution draft") + (" (rework)" if feedback else "")
    try:
        item.open()
        proj.plane.update_issue(w, item.id, assignees=[CFG["workers"][w]["id"]])
        pull_page_edits(item, w)
        args = (f"Project: {proj.name} ({proj.ident}).\n\nThe project owner's requested principles and context "
                f"(from the constitution card):\n{item.description}\n\nWorkspace baseline: every principle below "
                f"MUST appear in this constitution. You may make them stricter, never weaker.\n{baseline_text()}\n\n"
                + (f"This amends the ratified constitution v{proj.ratified}: keep what still applies, bump the version "
                   f"following the skill's rules, and record the change in the Sync Impact Report."
                   if proj.ratified else "This is the first version: CONSTITUTION_VERSION is 1.0.0 and the ratification "
                                         "date is today.")
                + "\nWrite every principle as a testable MUST / MUST NOT rule. Leave no [PLACEHOLDER] tokens."
                + (f"\n\nRevision requested by the reviewer, applied to the current draft:\n{feedback}" if feedback else ""))
        res = execute(w, item, item.wt, label, item.proj.backend.skill_prompt("speckit-constitution", args) + HEADLESS
                      + notes_block(human_commands(item, "/note", w)), CONSTITUTION_FILE)
        path = item.wt / CONSTITUTION_FILE
        left = placeholders(path)
        if res.ok and left:
            res.ok, res.text = False, f"the draft still contains template placeholders: {sorted(set(left))[:6]}"
        if not res.ok:
            proj.plane.comment(w, item.id, f"❌ **{label}** failed.\n\n```\n{res.text[-1200:]}\n```\n\nComment "
                                           f"`/retry` on this card.\n\n{stats(res, item.branch)}")
            return
        url = publish(item, w, "constitution", "Constitution", f"📜 Constitution — {proj.name}")
        page_id = q("SELECT page_id FROM pages WHERE issue_id=? AND artifact='constitution'", item.id)[0][0]
        proj.constitution_page = page_id
        update_constitution_record(proj, page_id=page_id)
        version = constitution_version(path)
        body = (f"✅ **{label}** ready: version **{version}**.\n\n{res.text.strip()}\n\n📄 [Constitution page]({url})"
                f" — edit it directly if you want changes.\n\n")
        if not stay:
            body += ("👉 Review it, then move this card to **Done** to ratify it, or comment `/rework <changes>`. "
                     + (f"Until then the current v{proj.ratified} stays in force." if proj.ratified else
                        "Feature work in this project starts once it is ratified.") + "\n\n")
        proj.plane.comment(w, item.id, body + stats(res, item.branch))
        if not stay:
            proj.plane.move(w, item.id, "Spec Review 👤", item.requester)
    except Exception as e:
        log.exception("%s constitution crashed", item.key)
        proj.plane.comment(w, item.id, f"❌ **{label}** crashed in the orchestrator: `{e}`")


def ratify(proj: Project, issue_id: str) -> None:
    """Moving the constitution card to Done: validate, merge into main, lock the page, unblock features."""
    w = CONSTITUTION_WORKER
    item = Item(proj, issue_id, w)
    review = proj.states["Spec Review 👤"]
    try:
        item.open()
        pull_page_edits(item, w)
        path = item.wt / CONSTITUTION_FILE
        version = constitution_version(path)
        left = placeholders(path)
        if not version or left:
            problem = f"template placeholders remain: {sorted(set(left))[:6]}" if left else \
                "no `**Version**: x.y.z` line was found"
            put_back(proj, item.id, review, f"Can't ratify: {problem}. Fix the Constitution page (or `/rework`), "
                                            f"then move the card to **Done** again. Moved back to **Spec Review 👤**.",
                     item.requester)
            return
        with git_lock:
            git("merge", "--no-ff", "-q", "-m", f"{item.key}: ratify constitution v{version}", item.branch,
                cwd=proj.repo, worker=w)
        drop_worktree(proj, item.wt, item.branch)  # an amendment starts again from main
        if proj.constitution_page:
            proj.plane.lock_page(w, proj.constitution_page, True)
        previous, proj.ratified = proj.ratified, version
        update_constitution_record(proj, version=version, ratified_at=time.time())
        proj.plane.comment(w, item.id, f"📜 **Constitution v{version} ratified** and merged into `main`"
                                       + (f" (replacing v{previous})" if previous else "") + ". The page is now locked; "
                                       f"to change the rules, move this card back to **Specify 🤖**.\n\n"
                                       + ("Feature work in this project can start; every feature follows this version."
                                          if not previous else "Features in flight switch to it at their next step."))
        emit("setup", f"constitution v{version} ratified", proj.ident, card=item.key)
    except Exception as e:
        log.exception("%s ratify crashed", item.key)
        proj.plane.comment(w, item.id, f"❌ **ratify** crashed in the orchestrator: `{e}`")


def run_verify(proj: Project, issue_id: str) -> None:
    """Hard gate: the orchestrator itself runs the whole test suite; no agent's word is taken for it."""
    w = "test-agent"
    item = Item(proj, issue_id, w)
    try:
        item.open()
        proj.plane.update_issue(w, item.id, assignees=[CFG["workers"][w]["id"]])
        proj.plane.comment(w, item.id, f"▶️ **{w}** started **verify**: running the full test suite "
                                       f"(`{' '.join(proj.test_cmd)}`).")
        t0 = time.time()
        try:
            with tracking(item, w, "verify", item.wt.name, engine="test-runner"):
                p = subprocess.run(proj.test_cmd, cwd=item.wt, text=True, capture_output=True, timeout=900)
            ok, out = p.returncode == 0, (p.stdout + "\n" + p.stderr).strip()
        except subprocess.TimeoutExpired:
            ok, out = False, "test run timed out after 900s"
        git("clean", "-fdq", cwd=item.wt)  # drop build leftovers; everything real is committed
        res = Result(ok, out, time.time() - t0)
        record(item, "verify", w, res, engine="test-runner")
        emit("finish", f"{w} verify {'passed' if ok else 'FAILED'}", proj.ident, card=item.key, ok=ok, cost=0.0,
             duration_s=res.duration_s, worker=w)
        tail = out[-1800:]
        if ok:
            proj.plane.comment(w, item.id, f"✅ **verify** passed.\n\n```\n{tail[-600:]}\n```\n\n"
                                           f"➡️ Handing over to **{AFTER_VERIFY}**.\n\n_{res.duration_s:.0f}s_")
            proj.plane.move(w, item.id, AFTER_VERIFY)
            return
        history = [r[0] for r in q("SELECT ok FROM runs WHERE issue=? AND phase='verify' ORDER BY id", item.key)]
        fails = len(history) - (max(i for i, v in enumerate(history) if v) + 1 if any(history) else 0)
        if fails <= MAX_FIX_LOOPS:
            pending_feedback[issue_id] = f"The verification gate failed. Test output:\n\n{tail}"
            proj.plane.comment(w, item.id, f"❌ **verify** failed (round {fails} of {MAX_FIX_LOOPS}). "
                                           f"Sending back to **{IMPLEMENT}** with the failures.\n\n```\n{tail}\n```")
            proj.plane.move(w, item.id, IMPLEMENT)
        else:
            proj.plane.comment(w, item.id, f"🛑 **verify** still failing after {MAX_FIX_LOOPS} fix rounds; a human "
                                           f"needs to look. Comment `/retry` to re-run the tests, or move the card "
                                           f"to **{IMPLEMENT}**.\n\n```\n{tail}\n```")
            proj.plane.update_issue(w, item.id, assignees=[item.requester])
    except Exception as e:
        log.exception("%s verify crashed", item.key)
        proj.plane.comment(w, item.id, f"❌ **verify** crashed in the orchestrator: `{e}`")


def merge_done(proj: Project, issue_id: str) -> None:
    w = "dev-agent"
    item = Item(proj, issue_id, w)
    if not git("branch", "--list", item.branch, cwd=proj.repo):
        return
    try:
        with git_lock:
            git("merge", "--no-ff", "-q", "-m", f"{item.key}: {item.name}", item.branch, cwd=proj.repo, worker=w)
        drop_worktree(proj, item.wt)
        proj.plane.comment(w, item.id, f"🏁 Merged `{item.branch}` into `main`.\n\n"
                                       f"📊 {metrics.card_total(q, item.key)}")
    except RuntimeError as e:
        subprocess.run(["git", "merge", "--abort"], cwd=proj.repo, capture_output=True)
        proj.plane.comment(w, item.id, f"⚠️ Could not merge `{item.branch}` into `main`: `{e}`")


def enqueue(fn, proj: Project, issue_id: str, *args) -> None:
    """Run a job on its own thread. Jobs for one card are serialised; different cards run in parallel."""
    with guard_lock:
        busy[issue_id] = busy.get(issue_id, 0) + 1
        lock = issue_locks.setdefault(issue_id, threading.Lock())

    def job():
        try:
            with lock:
                fn(proj, issue_id, *args)
        except Exception:
            log.exception("job failed")
        finally:
            with guard_lock:
                busy[issue_id] -= 1
                if busy[issue_id] <= 0:
                    del busy[issue_id]

    threading.Thread(target=job, daemon=True).start()


def put_back(proj: Project, issue_id: str, state_id: str, reason: str, assignee: str | None = None) -> None:
    """Undo a move the pipeline does not allow, and say why on the card."""
    with guard_lock:
        reverting[(issue_id, state_id)] = time.time() + 60  # Plane may echo a move more than once
    proj.plane.update_issue(GUARD, issue_id, state=state_id, **({"assignees": [assignee]} if assignee else {}))
    proj.plane.comment(GUARD, issue_id, f"↩️ {reason}")


NO_CONSTITUTION = ("This project has no ratified constitution yet, so feature work can't start. Ratify the "
                   "**📜 Project constitution** card first: move it to **Specify 🤖**, review the draft, then move "
                   "it to **Done**.")


def start_state(proj: Project, issue_id: str, state: str) -> str | None:
    if issue_id == proj.constitution_card:
        if state != SPECIFY:
            return None
        enqueue(run_constitution, proj, issue_id)
        return f"queued constitution in {state}"
    if state == SPECIFY and not proj.ratified:
        put_back(proj, issue_id, proj.states[ENTRY], f"{NO_CONSTITUTION} Moved back to **{ENTRY}**.")
        return "blocked: no ratified constitution"
    if state == ANALYZE:
        enqueue(run_analyze, proj, issue_id)
    elif state in PHASES:
        enqueue(run_phase, proj, issue_id, state)
    elif state == VERIFY:
        enqueue(run_verify, proj, issue_id)
    else:
        return None
    return f"queued {state}"


def on_state_change(proj: Project, data: dict, act: dict, actor: str | None) -> str:
    issue_id, new_id, old_id = data["id"], act.get("new_value"), act.get("old_value")
    new, old = proj.plane.state_name.get(new_id, ""), proj.plane.state_name.get(old_id, "")
    with guard_lock:
        if reverting.get((issue_id, new_id), 0) > time.time():  # echo of the guard's own correction
            return "guard echo"
        reverting.pop((issue_id, new_id), None)
    human = actor not in WORKER_IDS
    if human:
        back_id, back = (old_id, old) if old else (proj.states[ENTRY], ENTRY)
        if busy.get(issue_id):
            put_back(proj, issue_id, back_id, f"A worker is still running on this card, so it can't be moved yet. "
                                              f"Moved back to **{back}**; try again once the worker has commented.")
            return f"blocked {old} -> {new}: busy"
        track = track_of(proj, data)
        ok = allowed_moves(track, old)
        if new not in ok:
            options = ", ".join(f"**{s}**" for s in sorted(ok)) or "none (this card is finished)"
            why = (f"A card can only be set to **{FINAL}** from **Acceptance 👤**, after review. Nothing was merged. "
                   if new == FINAL else f"**{old or '?'}** → **{new}** isn't a valid step in this pipeline. ")
            put_back(proj, issue_id, back_id, f"{why}Moved back to **{back}**. Valid next steps from here: {options}.")
            return f"blocked {old} -> {new}"
        if track == "feature" and not proj.ratified and new != SPECIFY and (new in PHASES or new in (VERIFY, ANALYZE)):
            put_back(proj, issue_id, back_id, f"{NO_CONSTITUTION} Moved back to **{back}**.")
            return f"blocked {old} -> {new}: no constitution"
    queued = start_state(proj, issue_id, new)
    if queued:
        return queued
    if new == FINAL and human:
        if track_of(proj, data) == "constitution":
            enqueue(ratify, proj, issue_id)
            return "queued ratification"
        enqueue(merge_done, proj, issue_id)
        return "queued merge"
    return f"moved {old} → {new}"


def on_created(proj: Project, data: dict, actor: str | None) -> str:
    """A card created directly in a pipeline column: start it, or send it to Backlog."""
    st = data.get("state")
    state = st.get("name", "") if isinstance(st, dict) else proj.plane.state_name.get(st, "")
    if actor in WORKER_IDS or state in (ENTRY, CANCELLED, ""):
        return "ignored"
    start = START_STATES[track_of(proj, data)]
    if state == start:
        return start_state(proj, data["id"], state)
    put_back(proj, data["id"], proj.states[ENTRY],
             f"New cards start in **{ENTRY}** (or directly in **{start}**), not **{state}**. Moved to **{ENTRY}**.")
    return f"blocked create in {state}"


def on_comment(proj: Project, data: dict) -> str:
    text = comment_text(data)
    cmd = text.lower().split(" ")[0] if text else ""
    issue_id = data["issue"]
    item = Item(proj, issue_id, GUARD)
    state, is_constitution = item.state, issue_id == proj.constitution_card
    if not text.startswith("/"):  # people talking: never sent to an agent; one hint per open card
        if text and state not in (FINAL, CANCELLED) and not any(
                c["actor"] in WORKER_IDS and "Plain comments are for people" in c["comment_html"]
                for c in proj.plane.comments(GUARD, issue_id)):
            proj.plane.comment(GUARD, issue_id, PLAIN_HINT)
            return "plain comment: hint posted"
        return "ignored"
    if cmd == "/note":
        if not text[5:].strip():
            proj.plane.comment(GUARD, issue_id, "`/note` needs text, e.g. `/note amounts are always in USD`.")
            return "empty note"
        when = ("once the current run finishes, at this card's next step" if busy.get(issue_id) else
                "the card is closed, so notes are no longer used" if state in (FINAL, CANCELLED) and not is_constitution
                else "when the constitution is next drafted (move the card to **Specify 🤖**)" if is_constitution
                else "when **spec-agent** writes the spec (move the card to **Specify 🤖**)" if state == ENTRY
                else "at the next step that runs on this card (moving it on, or `/rework`)")
        proj.plane.comment(GUARD, issue_id, f"📝 Noted for the next run: it will be used {when}.")
        return "note recorded"
    if cmd == "/answer":
        if not is_constitution and state == "Spec Review 👤" and item.wt.exists() and open_questions(item.open()):
            proj.plane.comment(GUARD, issue_id, "📝 Recorded as an answer. Answers are folded into the spec when you "
                                                "move this card to **Plan 🤖**.")
            return "answer recorded"
        proj.plane.comment(GUARD, issue_id, "`/answer` applies while a card in **Spec Review 👤** has open questions; "
                                            "this card has none right now. Use `/note` to add information instead.")
        return "answer not applicable"
    if cmd not in ("/rework", "/retry"):
        proj.plane.comment(GUARD, issue_id, f"Unknown command `{cmd}`. Commands: " + ", ".join(f"`{c}`" for c in COMMANDS))
        return f"unknown command {cmd}"
    if busy.get(issue_id):
        proj.plane.comment(GUARD, issue_id, f"`{cmd}` ignored: a worker is already running on this card.")
        return f"{cmd} ignored: busy"
    if cmd == "/rework" and issue_id == proj.constitution_card and state == "Spec Review 👤":
        enqueue(run_constitution, proj, issue_id, text[7:].strip() or "(no details)", True)
        return "queued constitution rework"
    if cmd == "/rework" and state in REWORK:
        target = REWORK[state]
        # Reworked code goes back through Verify and Code Review; reworked documents stay for re-review.
        enqueue(run_phase, proj, issue_id, target, text[7:].strip() or "(no details)", target != IMPLEMENT)
        return f"queued rework of {target}"
    if cmd == "/retry" and (queued := start_state(proj, issue_id, state)):
        return queued.replace("queued", "queued retry of")
    proj.plane.comment(GUARD, issue_id, "`/rework` applies in a 👤 review column; "
                                        "`/retry` applies in a 🤖 column after a failed run.")
    return f"{cmd} not applicable in {state}"


def onboard(project_id: str) -> Project | None:
    """Provision a project (idempotent) and start serving it."""
    with registry_lock:
        reg = provision.load_registry()
        try:
            entry = provision.provision(CFG, project_id, reg)
        except Exception as e:
            log.exception("provisioning %s failed", project_id)
            emit("setup", f"setting up a new project failed: {e}")
            return None
        proj = Project(project_id, entry)
        is_new = project_id not in projects
        projects[project_id] = proj
    if is_new:
        emit("setup", f"project {proj.ident} ({proj.name}) set up: repo {entry['repo']}", proj.ident)
    mark_dirty(proj)
    return proj


def offboard(project_id: str) -> str:
    """A project was deleted in Plane: stop serving it. Its repo and worktrees are left on disk."""
    with registry_lock:
        proj = projects.pop(project_id, None)
        reg = provision.load_registry()
        reg.pop(project_id, None)
        provision.save_registry(reg)
    ident = proj.ident if proj else project_id
    emit("setup", f"project {ident} deleted in Plane; no longer served (repo kept on disk)", ident)
    return f"project {ident} removed"


def handle(event: str, body: dict) -> str:
    data, act = body.get("data") or {}, body.get("activity") or {}
    actor = (act.get("actor") or {}).get("id")
    created = str(body.get("action", "")).startswith("create")
    if event == "project":
        if str(body.get("action", "")).startswith("delete") and data.get("id") in projects:
            return offboard(data["id"])
        if created or (data.get("id") and data["id"] not in projects):
            threading.Thread(target=onboard, args=(data["id"],), daemon=True).start()
            return f"setting up project {data.get('identifier')}"
        return "ignored"
    proj = projects.get(data.get("project"))
    if proj is None:
        if data.get("project"):  # a project we haven't set up yet (e.g. created while we were down)
            threading.Thread(target=onboard, args=(data["project"],), daemon=True).start()
            return "unknown project: setting it up; repeat the action once its setup page appears"
        return "ignored"
    if event == "issue" and created:
        return on_created(proj, data, actor)
    # The public API reports the field as "state", the web UI as "state_id".
    if event == "issue" and act.get("field") in ("state", "state_id"):
        return on_state_change(proj, data, act, actor)
    if event == "issue_comment" and created and actor not in WORKER_IDS:
        return on_comment(proj, data)
    return "ignored"


# ---------------------------------------------------------------- dashboard + metrics
DASHBOARD_HTML = (ROOT / "orchestrator" / "dashboard.html").read_text()
OVERVIEW_HTML = (ROOT / "orchestrator" / "overview.html").read_text()


def project_metrics(proj: Project) -> dict:
    if proj.metrics is None:
        proj.metrics = metrics.compute(proj.plane, q, proj.ident, GUARD)
    return proj.metrics


def live_for(ident: str | None) -> list[dict]:
    now = time.time()
    return sorted(({**r, "elapsed_s": now - r["started"]} for r in list(live_runs.values())
                   if ident is None or r["project"] == ident), key=lambda r: (r["project"], r["card"], r["started"]))


def summary(proj: Project) -> dict:
    return {**project_metrics(proj), "now": time.time(), "name": proj.name, "links": proj.links,
            "feed": [e for e in feed if e.get("project") == proj.ident][:25], "live": live_for(proj.ident)}


def overview() -> dict:
    rows = []
    for p in sorted(projects.values(), key=lambda p: p.ident):
        h = project_metrics(p)["headline"]
        rows.append({"ident": p.ident, "name": p.name, "links": p.links, **h,
                     "running": sum(1 for r in live_runs.values() if r["project"] == p.ident)})
    total = {k: sum(r[k] for r in rows) for k in ("cumulative_cost", "cost_7d", "delivered", "in_flight",
                                                   "agent_runs", "failed_runs", "running")}
    return {"now": time.time(), "projects": rows, "total": total, "live": live_for(None), "feed": list(feed)[:30]}


def refresh_metrics(proj: Project) -> None:
    """Rewrite the project's metrics page in place (one stable URL)."""
    rows = q("SELECT page_id FROM pages WHERE issue_id='__metrics__' AND artifact=?", proj.ident)
    page_id = rows[0][0] if rows and proj.plane.page_exists(GUARD, rows[0][0]) else None
    proj.metrics = metrics.compute(proj.plane, q, proj.ident, GUARD)
    html = metrics.build(proj.metrics, proj.links["dashboard"])
    if not page_id:
        page_id = proj.plane.create_page(GUARD, f"📊 AI Metrics — {proj.name}", "<p></p>")
        q("INSERT OR REPLACE INTO pages VALUES('__metrics__',?,?,'')", proj.ident, page_id)
        log.info("%s metrics page: %s", proj.ident, proj.plane.page_url(page_id))
    proj.plane.set_page(GUARD, page_id, html, lock=True)
    proj.links["metrics_page"] = proj.plane.page_url(page_id)
    emit("metrics", "metrics refreshed", proj.ident)


def metrics_loop() -> None:
    while True:
        metrics_wake.wait()
        time.sleep(5)  # coalesce bursts of events into one refresh per project
        metrics_wake.clear()
        while metrics_dirty:
            pid = metrics_dirty.pop()
            if pid in projects:
                try:
                    refresh_metrics(projects[pid])
                except Exception:
                    log.exception("metrics refresh failed for %s", projects[pid].ident)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload) -> None:
        raw = json.dumps(payload, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, text: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def _events(self, scope: str | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        sub: queue.Queue = queue.Queue()
        entry = (sub, scope)
        subscribers.append(entry)
        try:
            while True:
                try:
                    self.wfile.write(f"data: {json.dumps(sub.get(timeout=15))}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            subscribers.remove(entry)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        parts = path.strip("/").split("/")
        if path == "/dashboard":
            return self._html(OVERVIEW_HTML)
        if path == "/api/overview":
            return self._send(200, overview())
        if path == "/api/events":
            return self._events(None)
        if len(parts) >= 2 and parts[0] in ("dashboard", "api"):
            proj = by_ident(parts[1].upper())
            if not proj:
                return self._send(404, {"error": f"unknown project {parts[1]}",
                                        "projects": sorted(p.ident for p in projects.values())})
            if parts[0] == "dashboard":
                return self._html(DASHBOARD_HTML.replace("__PROJECT__", proj.ident))
            if parts[2:] == ["summary"]:
                return self._send(200, summary(proj))
            if parts[2:] == ["events"]:
                return self._events(proj.ident)
        if path == "/runs":
            cols = ["ts", "project", "issue", "phase", "worker", "backend", "ok", "duration_s", "turns",
                    "cost_usd", "tokens_in", "tokens_out", "subagents"]
            return self._send(200, [dict(zip(cols, r)) for r in q(f"SELECT {','.join(cols)} FROM runs ORDER BY id")])
        self._send(200, {"ok": True, "backend": backend.name, "cards_running": len(busy),
                         "backends": {p.ident: p.backend.name for p in projects.values()},
                         "projects": {p.ident: str(p.repo) for p in projects.values()},
                         "dashboard": f"http://localhost:{PORT}/dashboard"})

    def do_POST(self):
        if self.path.startswith("/admin/reload/"):
            # Local admin call (e.g. import_project.py): re-read one project's settings without a restart.
            if not hmac.compare_digest(self.headers.get("X-Plane-Flow-Token", ""), CFG["webhook"]["secret"]):
                return self._send(401, {"error": "bad token"})
            ident = self.path.rsplit("/", 1)[-1].upper()
            pid = next((k for k, v in provision.projects_in(provision.load_registry()).items()
                        if v.get("identifier") == ident), None)
            if not pid:
                return self._send(404, {"error": f"unknown project {ident}"})
            proj = onboard(pid)
            return self._send(200, {"result": f"reloaded {ident}" + (f", constitution v{proj.ratified}" if proj and proj.ratified else "")})
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        sig = self.headers.get("X-Plane-Signature", "")
        secret = CFG["webhook"]["secret"].encode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        # Plane signs json.dumps(payload); accept the raw bytes or that canonical form.
        candidates = (raw, json.dumps(body).encode())
        if not any(hmac.compare_digest(hmac.new(secret, c, hashlib.sha256).hexdigest(), sig) for c in candidates):
            log.warning("rejected delivery with bad signature")
            return self._send(401, {"error": "bad signature"})
        delivery = self.headers.get("X-Plane-Delivery", "")
        if q("SELECT 1 FROM deliveries WHERE id=?", delivery):
            return self._send(200, {"result": "duplicate"})
        q("INSERT INTO deliveries VALUES(?,?)", delivery, time.time())
        try:
            result = handle(self.headers.get("X-Plane-Event", ""), body)
        except Exception as e:
            log.exception("handler error")
            result = f"error: {e}"
        if result != "ignored":
            data = body.get("data") or {}
            proj = projects.get(data.get("project"))
            if proj:
                mark_dirty(proj)
                card = f"{proj.ident}-{data['sequence_id']}" if data.get("sequence_id") else None
                if result != "guard echo" and not result.startswith("queued"):
                    emit("move", result, proj.ident, card=card)
            log.info("%s %s -> %s", self.headers.get("X-Plane-Event"), body.get("action"), result)
        self._send(200, {"result": result})

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- startup
def ensure_webhook() -> None:
    """Keep the webhook on, pointed at this orchestrator, with all needed events.

    Plane switches a webhook off after ~20 minutes of failed deliveries, and the address changes when
    plane-flow moves (e.g. Mac -> Linux), so both are re-asserted at every start."""
    c = Plane(CFG, "", {}).web("admin")
    url = f"/api/workspaces/{CFG['workspace_slug']}/webhooks/{CFG['webhook']['id']}/"
    hook = c.get(url).json()
    want = {"is_active": True, "project": True, "issue": True, "issue_comment": True,
            "url": SETTINGS["webhook_url"]}
    changed = {k: v for k, v in want.items() if hook.get(k) != v}
    if changed:
        if not hook.get("is_active"):
            log.warning("webhook was deactivated by Plane (failed deliveries); re-enabling it")
        if "url" in changed:
            log.warning("webhook URL %s -> %s", hook.get("url"), SETTINGS["webhook_url"])
        c.patch(url, json=changed).raise_for_status()


def resume_stranded(proj: Project) -> None:
    """Cards in a 🤖 column have no job after a (re)start; their trigger may have been missed."""
    ai_states = {proj.states[s]: s for s in [*PHASES, VERIFY] if s in proj.states}
    r = proj.plane.api(GUARD).get("/work-items/", params={"per_page": 100})
    r.raise_for_status()
    for d in r.json()["results"]:
        state = ai_states.get(d["state"])
        if state and not busy.get(d["id"]):
            proj.plane.comment(GUARD, d["id"], f"🔁 The pipeline (re)started while this card was in **{state}**, so "
                                               f"its trigger may have been missed or interrupted. Resuming **{state}**.")
            start_state(proj, d["id"], state)
            log.info("resumed %s-%s in %s", proj.ident, d["sequence_id"], state)


if __name__ == "__main__":
    ensure_webhook()
    provision.ensure_standards(CFG)
    for p in provision.list_projects(CFG):  # also catches projects created while we were down
        onboard(p["id"])
    for proj in projects.values():
        if git("branch", "--show-current", cwd=proj.repo) != "main":
            git("checkout", "-q", "main", cwd=proj.repo)  # fails loudly on uncommitted work
    threading.Thread(target=metrics_loop, daemon=True).start()
    for proj in projects.values():
        resume_stranded(proj)
    # Listen addresses come from settings: 127.0.0.1 on macOS (Colima forwards Plane's webhooks to it),
    # plus the pinned Docker gateway on Linux. Never the LAN.
    ThreadingHTTPServer.daemon_threads = True
    servers = [ThreadingHTTPServer((addr, PORT), Handler) for addr in SETTINGS["listen"]]
    log.info("listening on %s, backend=%s, projects=%s, webhook=%s, dashboard=%s/dashboard",
             ", ".join(f"{a}:{PORT}" for a in SETTINGS["listen"]), backend.name,
             ",".join(sorted(p.ident for p in projects.values())), SETTINGS["webhook_url"], CFG["dashboard_url"])
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()
