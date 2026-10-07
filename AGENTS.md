# plane-flow

Plane board + GitHub Spec Kit pipeline run by headless AI workers (Claude Code or GitHub Copilot CLI).

(This file is for any coding assistant, e.g. GitHub Copilot; CLAUDE.md has the same content for Claude Code.)

- Start with `HANDOFF.md` (publishing, connecting projects, Linux move, backlog), then `LOCAL.md` if it
  exists (this machine's state and pilot notes — git-ignored, never commit or quote it in public files),
  then `README.md` (what each feature does).
- Never commit `LOCAL.md`, `.secrets/`, `plane/.env`, `orchestrator/projects.json`, `orchestrator/flow.*`,
  `repos/`, `worktrees/` — they hold credentials, generated state or project code.
- The orchestrator runs as a background process (`./flowctl status`). Restart it after code changes
  (`./flowctl restart`), and only when no card is mid-run (dashboard "Working now" is empty).
- Test changes against a lab project with `bootstrap/drive.py` (acts as the human in Plane), not on
  real projects. Agent runs cost money — say roughly how much before long test runs.
- Ask before pushing to GitHub or changing anything in a project repo outside `repos/`.
- Keep committed files generic: no personal names, emails, home paths or private project names.
