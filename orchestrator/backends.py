"""Agent backends. A backend runs one Spec Kit phase headlessly in a repo.

Two kinds of parallelism exist, and the pipeline uses both:
- across workers: the orchestrator runs separate agent processes in separate git
  worktrees at the same time. Backend-agnostic; nothing here is involved.
- within a worker ("fan-out"): one agent splits its own task across sub-agents.
  Each backend expresses this its own way (Claude Code subagents, Copilot CLI
  /fleet); the pipeline only says `fanout=True` for phases where it is useful.

To switch to GitHub Copilot CLI: install its Spec Kit integration in the target
repo (`specify integration install copilot`), verify CopilotBackend below against
the installed CLI, and set "backend": "copilot" in config.json.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field

TIMEOUT_S = 45 * 60

# Spec Kit marks independent tasks with [P] in tasks.md; both backends get this cue.
FANOUT_HINT = (
    "\n\nTasks marked [P] in tasks.md are independent of each other. Where it is faster, "
    "run independent work in parallel using sub-agents, then integrate and check the results yourself."
)


@dataclass
class Result:
    ok: bool
    text: str
    duration_s: float = 0.0
    turns: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    session_id: str = ""
    subagents: int = 0          # sub-agents the worker launched (in-agent fan-out)
    raw: dict = field(default_factory=dict)


def _clean_env(extra: dict) -> dict:
    # Don't leak a parent agent session's markers into the worker process.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    env.update(extra)
    return env


class ClaudeBackend:
    """Claude Code CLI in print mode. Uses the logged-in CLI account; no API key."""

    name = "claude"
    tools = "Bash,Read,Write,Edit,Glob,Grep,Skill,TodoWrite"
    subagent_tools = ("Agent", "Task")  # the subagent tool's current and older name

    def skill_prompt(self, skill: str, args: str) -> str:
        return f"/{skill} {args}"

    def run(self, prompt: str, cwd: str, env: dict, model: str | None = None,
            fanout: bool = False) -> Result:
        tools = self.tools + ("," + ",".join(self.subagent_tools) if fanout else "")
        cmd = ["claude", "-p", prompt + (FANOUT_HINT if fanout else ""),
               "--output-format", "stream-json", "--verbose",
               "--permission-mode", "acceptEdits", "--allowedTools", tools]
        if model:
            cmd += ["--model", model]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=cwd, env=_clean_env(env), text=True,
                               capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return Result(False, f"timed out after {TIMEOUT_S}s", time.time() - t0)
        dur = time.time() - t0
        final, subagents = None, 0
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                final = ev
            elif ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
                subagents += sum(1 for b in ev.get("message", {}).get("content", [])
                                 if b.get("type") == "tool_use" and b.get("name") in self.subagent_tools)
        if final is None:
            return Result(False, (p.stderr or p.stdout)[-1500:], dur)
        u = final.get("usage", {})
        return Result(
            ok=p.returncode == 0 and not final.get("is_error"),
            text=final.get("result") or "",
            duration_s=dur,
            turns=final.get("num_turns", 0),
            cost_usd=final.get("total_cost_usd", 0.0) or 0.0,
            tokens_in=u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0),
            tokens_out=u.get("output_tokens", 0),
            session_id=final.get("session_id", ""),
            subagents=subagents,
            raw=final,
        )


class CopilotBackend:
    """GitHub Copilot CLI. NOT YET VERIFIED: Copilot CLI is not installed on this machine.

    Before switching, check against the installed CLI: the headless flags, the
    `/fleet` invocation for fan-out, the Spec Kit prompt names its integration
    installs, and whether it can report usage/cost for metrics.
    """

    name = "copilot"

    def skill_prompt(self, skill: str, args: str) -> str:
        # Spec Kit's copilot integration exposes dotted prompt names.
        return f"/{skill.replace('speckit-', 'speckit.')} {args}"

    def run(self, prompt: str, cwd: str, env: dict, model: str | None = None,
            fanout: bool = False) -> Result:
        if fanout:  # Copilot's own fan-out decides how to split the work
            prompt = "/fleet " + prompt + FANOUT_HINT
        cmd = ["copilot", "-p", prompt, "--allow-all-tools"]
        if model:
            cmd += ["--model", model]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=cwd, env=_clean_env(env), text=True,
                               capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return Result(False, f"timed out after {TIMEOUT_S}s", time.time() - t0)
        return Result(p.returncode == 0, (p.stdout or p.stderr)[-4000:], time.time() - t0)


class FakeBackend:
    """No AI: writes small canned Spec Kit artifacts so the whole pipeline (webhooks, provisioning, gates,
    pages, git, Verify) can be tested at zero cost — `"backend": "fake"` in config.json, or `flowctl selftest`.

    Markers in a card's description steer it: FAKE_QUESTION makes the spec ask one question;
    FAKE_CRITICAL makes Analyze report a critical finding."""

    name = "fake"

    def skill_prompt(self, skill: str, args: str) -> str:
        return f"/{skill} {args}"

    def run(self, prompt: str, cwd: str, env: dict, model: str | None = None, fanout: bool = False) -> Result:
        import re
        from pathlib import Path
        t0 = time.time()
        root, fdir = Path(cwd), Path(cwd) / env.get("SPECIFY_FEATURE_DIRECTORY", "specs/fake")
        fdir.mkdir(parents=True, exist_ok=True)
        head = prompt.lstrip()
        say = "fake run"

        def write(rel: str, text: str) -> None:
            f = root / rel if not rel.startswith("specs/") else root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

        if head.startswith("/speckit-constitution"):
            path = root / ".specify/memory/constitution.md"
            old = path.read_text() if path.exists() else ""
            m = re.search(r"\*\*Version\*\*:\s*(\d+)\.(\d+)\.(\d+)", old)
            ver = f"{m.group(1)}.{int(m.group(2)) + 1}.0" if m else "1.0.0"
            today = time.strftime("%Y-%m-%d")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# Fake Project Constitution\n\n## Core Principles\n\n### I. Tested\nEvery requirement "
                            f"MUST have a test.\n\n## Governance\nAmend through the constitution card.\n\n"
                            f"**Version**: {ver} | **Ratified**: {today} | **Last Amended**: {today}\n")
            say = f"constitution v{ver}"
        elif head.startswith("/speckit-specify"):
            q = "\n- **FR-002**: [NEEDS CLARIFICATION: Which output format, plain or JSON?]" if "FAKE_QUESTION" in prompt else ""
            crit = "\n\nFAKE_CRITICAL" if "FAKE_CRITICAL" in prompt else ""
            write(str(fdir / "spec.md"), f"# Feature Specification\n\n## Requirements\n\n- **FR-001**: The tool MUST "
                                         f"print a greeting.{q}{crit}\n")
            say = "spec written" + (" with one open question" if q else "")
        elif head.startswith("/speckit-clarify"):
            spec = fdir / "spec.md"
            text = re.sub(r"\[NEEDS CLARIFICATION:[^\]]*\]", "Plain text output.", spec.read_text())
            spec.write_text(text + "\n## Clarifications\n\n- Q: output format → A: from the owner's answer\n")
            say = "answers applied"
        elif head.startswith("/speckit-plan") or ("Rework requested" in head and "plan.md" in head):
            for name in ("plan.md", "research.md", "data-model.md", "quickstart.md", "contracts/cli.md"):
                write(str(fdir / name), f"# {name}\n\nFake {name} for the self-test.\n")
            say = "plan written"
        elif head.startswith("/speckit-tasks"):
            write(str(fdir / "tasks.md"), "# Tasks\n\n- [ ] T001 Create fakeapp package\n- [ ] T002 Add a unit test\n")
            say = "tasks written"
        elif head.startswith("/speckit-analyze"):
            spec = (fdir / "spec.md").read_text() if (fdir / "spec.md").exists() else ""
            n = 1 if "FAKE_CRITICAL" in spec else 0
            return Result(True, ("| C1 | Constitution | CRITICAL | spec.md | fake critical finding |\n" if n else
                                 "No issues found.\n") + f"CRITICAL_COUNT: {n}", time.time() - t0, 1)
        elif head.startswith("/speckit-implement") or ("Rework requested" in head and "implementation" in head):
            write("pyproject.toml", '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = '
                                    '"setuptools.build_meta"\n\n[project]\nname = "fakeapp"\nversion = "0.1.0"\n\n'
                                    '[tool.setuptools]\npackages = ["fakeapp"]\n')
            write("fakeapp/__init__.py", 'def greet(name: str) -> str:\n    """Greet someone."""\n    return f"Hello, {name}!"\n')
            write("tests/test_fakeapp.py", "from fakeapp import greet\n\n\ndef test_greet():\n    assert greet('a') == 'Hello, a!'\n")
            tasks = fdir / "tasks.md"
            if tasks.exists():
                tasks.write_text(tasks.read_text().replace("- [ ]", "- [X]"))
            say = "implemented"
        elif "You are test-agent" in head:
            write("tests/acceptance/test_acceptance.py", "from fakeapp import greet\n\n\ndef test_acceptance():\n"
                                                         "    assert greet('x').startswith('Hello')\n")
            write(str(fdir / "test-plan.md"), "# Test plan\n\n| Test | Requirement |\n|---|---|\n| test_acceptance | FR-001 |\n")
            say = "acceptance tests written"
        elif "You are the code reviewer" in head:
            write(str(fdir / "review.md"), "Verdict: APPROVE\n\nFake review: all acceptance criteria pass.\n")
            say = "Verdict: APPROVE"
        elif "Rework requested" in head:
            m = re.search(r"Revise `([^`]+)`", head)
            if m and (root / m.group(1)).exists():
                (root / m.group(1)).write_text((root / m.group(1)).read_text() + "\n\nRevised after rework.\n")
            say = "reworked"
        time.sleep(1)
        return Result(True, f"- (fake backend) {say}", time.time() - t0, 1)


BACKENDS = {"claude": ClaudeBackend, "copilot": CopilotBackend, "fake": FakeBackend}
