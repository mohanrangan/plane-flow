# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "markdown", "markdownify", "beautifulsoup4"]
# ///
"""End-to-end self-test of a running plane-flow install. No AI is used: the test project runs on the
fake backend, whatever the workspace default is.

    ./flowctl selftest            plumbing: orchestrator reachable, webhook configured, a new project is
                                  provisioned via Plane's webhook, the guard reverts bad moves, feature
                                  work is blocked until a constitution is ratified          (~1 min)
    ./flowctl selftest --full     + constitution draft/ratify, a card through every gate (questions,
                                  clarify, plan, tasks, analyze, implement ∥ tests, verify, review),
                                  then Done → merged to main                                 (~3 min)
    --agent copilot|claude        run the test project on a REAL agent instead of the fake backend and add an
                                  agent check (one constitution draft, costs a little); with --full the
                                  whole card runs on that agent too
    --keep                        leave the test project in Plane for inspection

Exit code 0 only if every check passed.
"""
import argparse
import json
import random
import shutil
import string
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))
import provision  # noqa: E402
import settings  # noqa: E402

S = settings.load()
CFG = json.loads((ROOT / ".secrets" / "plane.json").read_text())
BASE, SLUG = S["plane_api_url"], CFG["workspace_slug"]
ADMIN = {"X-API-Key": CFG["admin"]["token"]}
WS = httpx.Client(base_url=f"{BASE}/api/v1/workspaces/{SLUG}", headers=ADMIN, timeout=30)
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((ok, what))
    print(("  PASS  " if ok else "  FAIL  ") + what, flush=True)
    return ok


def wait(pred, timeout: float, every: float = 2.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    return None


def text(html: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


class Proj:
    def __init__(self, pid: str):
        self.pid = pid
        self.api = httpx.Client(base_url=f"{BASE}/api/v1/workspaces/{SLUG}/projects/{pid}", headers=ADMIN, timeout=30)

    @property
    def entry(self) -> dict:
        return provision.load_registry().get(self.pid, {})

    def state_id(self, name: str) -> str:
        return self.entry["states"][name]

    def card(self, name: str, desc: str = "") -> str:
        r = self.api.post("/work-items/", json={"name": name, "description_html": f"<p>{desc}</p>"})
        r.raise_for_status()
        return r.json()["id"]

    def move(self, cid: str, state: str) -> None:
        self.api.patch(f"/work-items/{cid}/", json={"state": self.state_id(state)}).raise_for_status()

    def comment(self, cid: str, body: str) -> None:
        self.api.post(f"/work-items/{cid}/comments/", json={"comment_html": f"<p>{body}</p>"}).raise_for_status()

    def state(self, cid: str) -> str:
        sid = self.api.get(f"/work-items/{cid}/").json()["state"]
        return {v: k for k, v in self.entry["states"].items()}.get(sid, "?")

    def seen(self, cid: str, needle: str) -> bool:
        cs = self.api.get(f"/work-items/{cid}/comments/", params={"per_page": 100}).json()["results"]
        return any(needle.lower() in text(c["comment_html"]).lower() for c in cs)

    def await_comment(self, cid: str, needle: str, timeout: float) -> bool:
        return bool(wait(lambda: self.seen(cid, needle), timeout))

    def await_state(self, cid: str, state: str, timeout: float) -> bool:
        return bool(wait(lambda: self.state(cid) == state, timeout))


AGENT = "fake"
SKILLS = {"claude": ".claude/skills", "copilot": ".github/skills", "fake": ".claude/skills"}


def plumbing() -> Proj | None:
    print("orchestrator and webhook")
    for addr in S["listen"]:
        try:
            ok = httpx.get(f"http://{addr}:{S['port']}/", timeout=5).json().get("ok")
        except Exception:
            ok = False
        check(bool(ok), f"orchestrator answers on {addr}:{S['port']}")
    web = httpx.Client(base_url=BASE, timeout=30)
    tok = web.get("/auth/get-csrf-token/").json()["csrf_token"]
    web.post("/auth/sign-in/", data={"csrfmiddlewaretoken": tok, "email": CFG["admin"]["email"],
                                     "password": CFG["admin"]["password"]})
    hook = web.get(f"/api/workspaces/{SLUG}/webhooks/{CFG['webhook']['id']}/").json()
    check(bool(hook.get("is_active")), "webhook is active")
    check(hook.get("url") == S["webhook_url"], f"webhook points at {S['webhook_url']} (is {hook.get('url')})")
    check(all(hook.get(k) for k in ("project", "issue", "issue_comment")), "webhook sends project, issue, comment events")

    print("provisioning a new project via Plane's webhook")
    ident = "ST" + "".join(random.choices(string.ascii_uppercase, k=4))
    reg = provision.load_registry()
    reg[f"pending:{ident}"] = {"backend": AGENT}
    provision.save_registry(reg)
    r = WS.post("/projects/", json={"name": f"Selftest {ident}", "identifier": ident})   # no special characters
    if not check(r.status_code == 201, f"created Plane project {ident}" + ("" if r.status_code == 201 else f": {r.text[:200]}")):
        reg = provision.load_registry()
        reg.pop(f"pending:{ident}", None)
        provision.save_registry(reg)
        return None
    p = Proj(r.json()["id"])
    t0 = time.time()
    ready = wait(lambda: p.entry.get("setup_page") and p.entry.get("constitution", {}).get("card_id"), 90)
    check(bool(ready), f"provisioned by the orchestrator in {time.time() - t0:.0f}s (columns, workers, repo, setup page, 📜 card)")
    if not ready:
        return p
    p.ident = ident
    check(p.entry.get("backend") == AGENT, f"test project runs on the {AGENT} backend"
          + (" (no AI cost)" if AGENT == "fake" else ""))
    check((ROOT / p.entry["repo"] / ".specify").exists(), f"Spec Kit repo created at {p.entry['repo']}")
    check(bool(list((ROOT / p.entry["repo"] / SKILLS[AGENT]).glob("speckit-*"))),
          f"Spec Kit skills for {AGENT} are installed ({SKILLS[AGENT]})")

    print("guard rails")
    cid = p.card("selftest guard card")
    p.move(cid, "Done")
    check(p.await_comment(cid, "can only be set to Done", 40) and p.await_state(cid, "Backlog", 20),
          "Backlog → Done is reverted with an explanation")
    p.move(cid, "Specify 🤖")
    check(p.await_comment(cid, "no ratified constitution", 40) and p.await_state(cid, "Backlog", 20),
          "feature work is blocked until the constitution is ratified")
    return p


def agent_check(p: Proj) -> None:
    print(f"real agent: {AGENT}")
    const = p.entry["constitution"]["card_id"]
    p.move(const, "Specify 🤖")
    ok = p.await_comment(const, "ready: version", 900)
    if not ok and p.seen(const, "failed"):
        print("    the agent run failed; see the comment on the card and ./flowctl logs")
    check(ok, f"{AGENT} drafted a constitution through the Spec Kit skill (one headless agent run)")
    path = ROOT / p.entry["repo"] / ".specify/memory/constitution.md"
    wt = ROOT / p.entry["worktrees"]
    drafts = list(wt.glob("*/.specify/memory/constitution.md"))
    check(any("**Version**" in d.read_text() for d in drafts) or path.exists(), "the draft is in the card's working folder")


def full(p: Proj) -> None:
    print("constitution")
    const = p.entry["constitution"]["card_id"]
    p.move(const, "Specify 🤖")
    check(p.await_comment(const, "ready: version", 120), "constitution-agent drafts the constitution")
    p.move(const, "Done")
    check(p.await_comment(const, "ratified and merged", 90), "moving the card to Done ratifies it")
    path = ROOT / p.entry["repo"] / ".specify/memory/constitution.md"
    check(path.exists() and "**Version**: 1.0.0" in path.read_text(), "ratified constitution v1.0.0 is on main")

    print("a feature through every gate")
    cid = p.card("selftest feature", "Print a greeting. FAKE_QUESTION")
    p.move(cid, "Specify 🤖")
    check(p.await_comment(cid, "open question", 120), "spec-agent asks an open question instead of guessing")
    p.move(cid, "Plan 🤖")
    check(p.await_comment(cid, "still unanswered", 60) and p.await_state(cid, "Spec Review 👤", 20),
          "moving on with an unanswered question is blocked")
    p.comment(cid, "/answer 1: plain text")
    check(p.await_comment(cid, "recorded as an answer", 40), "/answer is acknowledged")
    p.move(cid, "Plan 🤖")
    check(p.await_state(cid, "Plan Review 👤", 180), "clarify + plan run, card returns to Plan Review")
    p.move(cid, "Tasks 🤖")
    check(p.await_state(cid, "Acceptance 👤", 400), "tasks → analyze → implement ∥ tests → verify → review reach Acceptance")
    for needle, what in (("analyze passed", "Analyze gate passed"), ("acceptance tests complete", "test-agent ran in parallel"),
                         ("verify passed", "Verify gate ran the real test command and passed"),
                         ("code review complete", "review-agent approved")):
        check(p.seen(cid, needle), what)
    p.move(cid, "Done")
    check(p.await_comment(cid, "🏁 Merged", 60), "Done merges the card's branch into main")
    check((ROOT / p.entry["repo"] / "fakeapp/__init__.py").exists(), "the implementation is on main")


def cleanup(p: Proj) -> None:
    print("cleanup")
    ident, repo, wts = p.ident, p.entry.get("repo"), p.entry.get("worktrees")
    r = WS.delete(f"/projects/{p.pid}/")
    check(r.status_code in (200, 204), f"deleted Plane project {ident}")
    gone = wait(lambda: p.pid not in provision.load_registry(), 30)
    if not gone:  # older Plane builds may not send a project-deleted webhook
        reg = provision.load_registry()
        reg.pop(p.pid, None)
        provision.save_registry(reg)
    check(True, "orchestrator no longer serves it" if gone else "registry entry removed by the self-test")
    for rel in (repo, wts):
        if rel and rel.startswith(("repos/st", "worktrees/st")):   # only the self-test's own folders
            shutil.rmtree(ROOT / rel, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--agent", choices=["claude", "copilot"])
    args = ap.parse_args()
    global AGENT
    AGENT = args.agent or "fake"
    p = plumbing()
    if p is not None and getattr(p, "ident", None) and args.agent and not args.full:
        agent_check(p)
    if p is not None and getattr(p, "ident", None) and args.full:
        full(p)
    if p is not None and getattr(p, "ident", None) and not args.keep:
        cleanup(p)
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (": FAILED → " + "; ".join(failed) if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
