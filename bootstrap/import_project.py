# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "markdown", "markdownify", "beautifulsoup4"]
# ///
"""Import an existing Spec Kit repo into plane-flow: a Plane project wired to the pipeline.

    uv run bootstrap/import_project.py --ident SHOP --name "My Shop" \
        --repo-url https://github.com/you/shop.git            # clone into repos/shop (recommended)
        [--repo-path /path/to/checkout]                       # or use an existing checkout in place
        [--test-cmd "npm ci && npm test"] [--backend claude|fake]
        [--copy-from ~/projects/shop --copy .env.local]       # git-ignored files the tests/build need

What it does (the orchestrator must be running):
1. Clones the repo (or checks the given checkout): must have Spec Kit (.specify) and the Claude skills,
   a clean `main`.
2. Pre-registers the project's settings, then creates the Plane project; the orchestrator provisions it
   (columns, workers, setup page, 📜 card) and adopts the repo instead of creating one.
3. Adopts the repo's ratified constitution: publishes it as the locked 📜 Constitution page and moves the
   📜 card to Done, so feature work is unblocked immediately.
4. Adds every existing feature (specs/*) as a card with its Spec / Plan / Tasks pages: finished features in
   Done, unfinished ones in Backlog with a note (resuming them inside the pipeline isn't supported yet).
Your own working copy is never modified when --repo-url is used.
"""
import argparse
import hashlib
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))
import provision  # noqa: E402
import settings  # noqa: E402
from plane import Plane, md_to_html, text_of  # noqa: E402

S = settings.load()
CFG = json.loads((ROOT / ".secrets" / "plane.json").read_text())
CFG["base_url"], CFG["public_url"] = S["plane_api_url"], S["plane_public_url"]
ADMIN = {"X-API-Key": CFG["admin"]["token"]}
CONST_FILE = ".specify/memory/constitution.md"


def die(msg: str) -> None:
    sys.exit(f"ERROR: {msg}")


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if p.returncode != 0:
        die(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def wait(pred, timeout: float):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(2)
    return None


def feature_info(d: Path) -> dict:
    spec = (d / "spec.md").read_text() if (d / "spec.md").exists() else ""
    m = re.search(r"^#\s*Feature Specification:\s*(.+)$", spec, re.M)
    tasks = (d / "tasks.md").read_text() if (d / "tasks.md").exists() else ""
    done, total = len(re.findall(r"^\s*- \[[xX]\]", tasks, re.M)), len(re.findall(r"^\s*- \[[ xX]\]", tasks, re.M))
    return {"dir": d.name, "title": (m.group(1).strip() if m else d.name), "done": done, "total": total,
            "finished": total > 0 and done == total}


def main() -> None:
    ap = argparse.ArgumentParser(description="Import an existing Spec Kit repo into plane-flow.")
    ap.add_argument("--ident", required=True, help="Plane project identifier, e.g. SHOP")
    ap.add_argument("--name", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo-url")
    src.add_argument("--repo-path")
    ap.add_argument("--test-cmd", help="shell command Verify runs (exit 0 = pass)")
    ap.add_argument("--backend")
    ap.add_argument("--copy-from", help="folder to copy git-ignored files from (with --copy)")
    ap.add_argument("--copy", action="append", default=[], help="file to copy into the repo, e.g. .env.local")
    args = ap.parse_args()
    ident = args.ident.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", ident):
        die("--ident must be 2-12 letters/digits, starting with a letter")

    # 0. preflight
    try:
        httpx.get(f"http://127.0.0.1:{S['port']}/", timeout=5).raise_for_status()
    except Exception:
        die("the orchestrator isn't running (./flowctl start)")
    ws = httpx.Client(base_url=f"{S['plane_api_url']}/api/v1/workspaces/{CFG['workspace_slug']}", headers=ADMIN, timeout=30)
    if any(p["identifier"] == ident for p in ws.get("/projects/").json()["results"]):
        die(f"a Plane project with identifier {ident} already exists")

    # 1. repository
    if args.repo_url:
        rel = f"repos/{ident.lower()}"
        repo = ROOT / rel
        if repo.exists():
            die(f"{rel} already exists; remove it or pick another identifier")
        print(f"cloning {args.repo_url} -> {rel}")
        subprocess.run(["git", "clone", "-q", args.repo_url, str(repo)], check=True)
    else:
        repo = Path(args.repo_path).expanduser().resolve()
        rel = str(repo)
        if not (repo / ".git").exists():
            die(f"{repo} is not a git checkout")
    if git(repo, "branch", "--show-current") != "main":
        git(repo, "checkout", "-q", "main")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        die(f"{repo} has uncommitted changes on main; commit or stash them first")
    if not (repo / ".specify").is_dir():
        die("no .specify/ folder: this repo doesn't use Spec Kit (run `specify init --here` and commit first)")
    if not list((repo / ".claude/skills").glob("speckit-*")):
        die("Spec Kit's Claude skills are missing: run `specify integration install claude` and commit")
    for name in args.copy:
        if not args.copy_from:
            die("--copy needs --copy-from")
        s = Path(args.copy_from).expanduser() / name
        if s.exists():
            shutil.copy2(s, repo / name)
            print(f"copied {name} into the repo (git-ignored files are not committed)")
        else:
            print(f"warning: {s} not found, skipped")

    cfile = repo / CONST_FILE
    ctext = cfile.read_text() if cfile.exists() else ""
    m = re.search(r"\*\*Version\*\*:\s*([0-9]+\.[0-9]+\.[0-9]+)", ctext)
    placeholders = re.findall(r"\[[A-Z][A-Z0-9_]{2,}\]", re.sub(r"<!--.*?-->", "", ctext, flags=re.S))
    cversion = m.group(1) if m and not placeholders else None
    features = [feature_info(d) for d in sorted((repo / "specs").glob("*/")) if (d / "spec.md").exists()] \
        if (repo / "specs").exists() else []
    print(f"constitution: {('v' + cversion) if cversion else 'not ratified (template) — will be drafted in Plane'}")
    for f in features:
        print(f"feature {f['dir']}: {f['title']} — tasks {f['done']}/{f['total']}")

    # 2. pre-register settings, create the Plane project, wait for provisioning
    pending = {"repo": rel, "worktrees": f"worktrees/{ident.lower()}"}
    if args.test_cmd:
        pending["test_cmd"] = ["sh", "-c", args.test_cmd]
    if args.backend:
        pending["backend"] = args.backend
    reg = provision.load_registry()
    reg[f"pending:{ident}"] = pending
    provision.save_registry(reg)
    r = ws.post("/projects/", json={"name": args.name, "identifier": ident})
    if r.status_code != 201:
        reg = provision.load_registry()
        reg.pop(f"pending:{ident}", None)
        provision.save_registry(reg)
        die(f"Plane refused the project: {r.text[:300]}")
    pid = r.json()["id"]
    print(f"created Plane project {ident}; waiting for the orchestrator to provision it")
    entry = wait(lambda: (lambda e: e if e.get("setup_page") and e.get("constitution", {}).get("card_id") else None)(
        provision.load_registry().get(pid, {})), 120)
    if not entry:
        die("the orchestrator didn't provision the project within 2 minutes (see ./flowctl logs)")
    bot = Plane(CFG, pid, entry["states"])
    db = sqlite3.connect(ROOT / "orchestrator" / "flow.db")

    def page(worker: str, name: str, md: str, issue_id: str, artifact: str | None = None, lock: bool = False) -> str:
        """Publish a page and link it from the card. With `artifact`, register it so the orchestrator syncs edits."""
        html = md_to_html(md)
        page_id = bot.create_page(worker, name, "<p></p>")
        bot.set_page(worker, page_id, html, lock=lock)
        if artifact:
            db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?)", (issue_id, artifact, page_id,
                                                                       hashlib.sha256(text_of(html).encode()).hexdigest()))
            db.commit()
        bot.link(worker, issue_id, name, bot.page_url(page_id))
        return page_id

    # 3. adopt the ratified constitution
    card = entry["constitution"]["card_id"]
    if cversion:
        page_id = page("constitution-agent", f"📜 Constitution — {args.name}", ctext, card, "constitution", lock=True)
        reg = provision.load_registry()
        reg[pid]["constitution"].update({"version": cversion, "page_id": page_id, "ratified_at": time.time(),
                                         "imported": True})
        provision.save_registry(reg)
        bot.comment("flow-bot", card, f"📥 Imported the repository's ratified constitution **v{cversion}** from "
                                      f"`{CONST_FILE}`. The 📜 Constitution page is locked; to amend it, move this "
                                      f"card from Done back to **Specify 🤖**.")
        bot.move("flow-bot", card, "Done")
    # 4. existing features as cards with reference pages
    for f in features:
        state = "Done" if f["finished"] else "Backlog"
        desc = (f"Imported from `specs/{f['dir']}`. Tasks {f['done']}/{f['total']} done when imported."
                + ("" if f["finished"] else " Finishing an imported feature inside the pipeline isn't supported "
                   "yet: finish it outside, or create new cards for the remaining work."))
        cid = bot.create_issue("flow-bot", f"{f['title']} (imported {f['dir'].split('-')[0]})", desc, "Backlog")["id"]
        for art, title in (("spec.md", "Spec"), ("plan.md", "Plan"), ("tasks.md", "Tasks")):
            fp = repo / "specs" / f["dir"] / art
            if fp.exists():
                page("flow-bot", f"{ident} · {f['dir']} · {title} (imported)", fp.read_text(), cid)
        if state == "Done":
            bot.move("flow-bot", cid, "Done")
        print(f"card for {f['dir']} -> {state}")

    # the orchestrator keeps project settings in memory: ask it to reload this one project (no restart)
    r = httpx.post(f"http://127.0.0.1:{S['port']}/admin/reload/{ident}",
                   headers={"X-Plane-Flow-Token": CFG["webhook"]["secret"]}, timeout=60)
    print(f"\norchestrator reloaded {ident}: {r.json().get('result', r.text[:200])}")
    print(f"Plane: {S['plane_public_url']}/{CFG['workspace_slug']}/projects/{pid}/issues/")
    if args.test_cmd:
        print(f"Verify will run: {shlex.join(['sh', '-c', args.test_cmd])}")


if __name__ == "__main__":
    main()
