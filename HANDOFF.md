# plane-flow — handoff

Status as of 2026-10-06. Read this together with `README.md` (what each feature does) — this file
is about **where things stand, how to publish the repo, how to connect other Spec Kit / SDLC projects,
and how to move to Linux**.

---

## 1. What this is, in one paragraph

A self-hosted **Plane** board is the control surface for a **GitHub Spec Kit** pipeline run by headless
**Claude Code CLI** workers. Moving a card into a 🤖 column sends a Plane webhook to the **orchestrator**
(`orchestrator/flow.py`, one Python process), which runs the right worker in that card's own git worktree,
publishes the result as an editable Plane page, comments under the worker's own Plane identity, and hands
the card to the next column. 👤 columns are human gates. Every Plane project is provisioned automatically
(columns, worker accounts, a Spec Kit repo, a constitution card, metrics page). Nothing polls.

```
Browser ──► Plane (Docker) ──webhook──► orchestrator (flow.py) ──► claude -p "/speckit-…" in worktree
   ▲                                          │                         │
   └────── pages, comments, card moves ◄──────┴──── Plane API ◄─────────┘ files + git commits
```

## 2. Local state (not in git)

Everything specific to one machine stays out of the repo:
- `LOCAL.md` — **git-ignored notes for this installation**: what's running, which projects and cards exist,
  spend so far, pilot-project notes. Create your own; never commit it.
- `.secrets/plane.json` (logins, tokens, webhook secret), `plane/.env` (Plane secrets),
  `orchestrator/projects.json` (project registry), `orchestrator/flow.db` (run history), `repos/`, `worktrees/`.

Run / stop / inspect: `./flowctl start | stop [all] | restart | status | logs`.
Plane: http://localhost:8080 · orchestrator + dashboard: http://localhost:8787 (`/dashboard`).
Tested with Plane v1.4.2 CE, Spec Kit 1.0.8.dev0, Claude Code 2.1.x.

## 3. Repo map

```
install.sh                 installer: prerequisites, config, Plane (new or existing), bootstrap, start, self-test
flowctl                    start | stop [all] | restart | status | logs | selftest [--full]
LICENSE                    MIT
plane/                     Plane compose files + .env for a managed install (downloaded/generated, ignored)
deploy/linux/              systemd unit + pinned-network override for Plane
bootstrap/bootstrap.py     Plane setup: instance admin, workspace, worker accounts + tokens, webhook
bootstrap/selftest.py      end-to-end self-test (fake backend, throw-away project)
bootstrap/drive.py         acts as the human from the terminal (create/move/comment/answer)
orchestrator/
  flow.py                  THE BRAIN: phases, guard, gates, commands, prompts, dashboard routes, startup
  provision.py             per-project setup; constitution card; Standards baseline; column order
  settings.py              settings + macOS/Linux defaults; config.example.json documents every key
  plane.py                 Plane client (public API with tokens + web API with sessions for pages)
  backends.py              ClaudeBackend (working), CopilotBackend (stub), FakeBackend (tests, no AI)
  metrics.py               roll-ups for the Metrics page and dashboard
  dashboard.html, overview.html   live dashboard (server-sent events)
  config.json, projects.json, flow.db, flow.log   local settings, registry, run history, log (ignored)
repos/<ident>/             one git repo per project (ignored)
worktrees/<ident>/<card>/  one working folder per card (ignored)
```

Where to start reading the code: `flow.py` top → `PHASES` / `HUMAN_MOVES` tables → `handle()` →
`run_phase()` → the gates (`clarify_spec`, `run_analyze`, `run_verify`, `ratify`).

## 4. Publish to GitHub

The repo is prepared for publishing: no toy leftovers, Plane's own files are downloaded by `install.sh`
instead of being committed (Plane is AGPL-3.0), MIT `LICENSE`, local/secret files git-ignored, and committed
files contain no personal details.

**Always publish from fresh history.** The local development history contains a personal email address
and machine-specific notes, so the public repo starts from a single clean commit:
```sh
gh auth status                                   # logged in as <github-user>
git config user.email "<id>+<github-user>@users.noreply.github.com"   # id: gh api user --jq .id
git checkout --orphan public && git add -A && git commit -m "Initial public release"
gh repo create <github-user>/plane-flow --public --description "Plane board + Spec Kit AI pipeline"
git remote add origin https://github.com/<github-user>/plane-flow.git
git push -u origin public:main
```
Afterwards keep developing on `public` so the old history is never pushed. Before any push, this must print
nothing: `git ls-files | xargs grep -nE "plane_api_[0-9a-f]{16}|plane_wh_[0-9a-f]{16}|\"password\": \"[^\".]"`.

## 5. Connecting another Spec Kit / SDLC project (today, on this install)

There are two cases. **A** works fully today. **B** works today with manual steps; the planned
`import_project.py` (see §8) will automate it.

### A. A brand-new project
1. In Plane: **Projects → Add project**, pick a short identifier (e.g. `SHOP`).
2. Within seconds it is provisioned: pipeline columns, workers as members, repo `repos/shop` (Spec Kit
   installed, Claude integration), 🤖 Pipeline setup page, 📊 AI Metrics page, 📜 constitution card.
3. **Set the Verify command if it isn't Python** — stop the orchestrator, edit that project's `test_cmd`
   in `orchestrator/projects.json`, start again (examples in step B5).
4. Ratify the constitution: write rules on the 📜 card (description or `/note`), move it to
   **Specify 🤖**, review the 📜 Constitution page, move the card to **Done**.
5. Create feature cards and move them to **Specify 🤖**.

### B. An existing repo that already uses Spec Kit

**Use the import command** (the orchestrator must be running; your own working copy is not modified):
```sh
uv run bootstrap/import_project.py --ident SHOP --name "My Shop" \
    --repo-url https://github.com/<you>/<repo>.git \
    --test-cmd 'cp ../../../repos/shop/.env.local . 2>/dev/null; npm ci && npm run lint && npm test' \
    --copy-from ~/projects/<repo> --copy .env.local
```
It clones into `repos/<ident>`, creates and provisions the Plane project, adopts the ratified constitution
(locked 📜 page, card Done — or leaves the 📜 card for drafting if the constitution is still a template), and
adds existing features as cards with their Spec/Plan/Tasks pages (finished → Done, unfinished → Backlog with
a note). `--repo-path` uses an existing checkout in place instead of cloning. Before importing, commit/merge
and push the repo's `main`. The manual steps below are what the command automates.


**Prepare the repo (in your own working copy)**
1. Commit everything, merge finished feature branches into `main`, push to GitHub. The pipeline always
   branches from and merges into `main`, and refuses to work with uncommitted changes.
2. Make sure the Claude integration is installed: `.claude/skills/speckit-*` must exist. If the repo was set
   up for another agent: `specify integration install claude`, commit, push.
3. Note the constitution version: `grep "\*\*Version\*\*" .specify/memory/constitution.md`.

**Connect it** (order matters — clone *before* creating the Plane project, so provisioning adopts the clone
instead of creating an empty repo)

4. Clone into plane-flow, named after the identifier in lower case:
   `git clone https://github.com/<you>/<repo>.git ~/projects/plane-flow/repos/<ident>`
   Copy any git-ignored files the build/tests need (e.g. `.env.local`) into that clone.
5. Create the Plane project with that identifier (e.g. `SHOP`). Provisioning sees the existing `.git` and
   does not re-initialise it. Then stop the orchestrator and set `test_cmd` in `orchestrator/projects.json`:
   - Node / Next.js: `["sh","-c","npm ci --no-audit --no-fund >/dev/null && npm run lint && npm run typecheck && npm test"]`
   - Python (pytest): the default.
   - Anything else: any command that exits 0 on success.
   Card worktrees contain only tracked files, so the command must install dependencies itself (slow for
   big Node projects; the planned per-project "worktree setup" will link a shared `node_modules` instead).
   Untracked files (e.g. `.env.local`) are not in worktrees either: copy them in from the test command,
   e.g. `cp ../../../repos/<ident>/.env.local . 2>/dev/null;` at the start of the `sh -c` string.
6. **Adopt the existing constitution instead of generating a new one.** With the orchestrator stopped, set
   that project's `"constitution": {"card_id": …, "page_id": null, "version": "<x.y.z>"}` in
   `projects.json` (keep `card_id`). Start the orchestrator. Feature work is now unblocked. To also get the
   locked 📜 Constitution page in Plane: move the 📜 card **Backlog → Specify 🤖** — constitution-agent treats
   it as an amendment of the existing constitution (it also folds in the Standards baseline; empty the
   Standards baseline page first if you don't want that), then **Done** to ratify (version bumps).
7. Existing features stay in the repo (`specs/001-…`) as history; they are not on the board. New features
   are cards; their folders are named `specs/<ident>-<n>-<slug>`.
8. **test-agent's brief assumes Python command-line tools** (`tester_prompt()` in `flow.py`). For a web app,
   edit that prompt (e.g. "vitest for logic, Playwright for pages; put tests in `tests/acceptance/`") until the
   per-project testing brief exists. Also make sure the project's test runner picks up `tests/acceptance/`.
9. **Sync back to GitHub.** On Done the pipeline merges into the clone's `main` locally and does **not**
   push. After accepting a card: `cd ~/projects/plane-flow/repos/<ident> && git push`, then `git pull` in your
   own working copy. (Planned: push / open a PR automatically on Done.)
10. Smoke test: one small card through Specify → … → Acceptance; check Verify ran your real test command.

### Using a different coding agent (Copilot, Gemini, Codex, …)
`orchestrator/backends.py` isolates the agent. A backend = how to run it headless, how to name a Spec Kit
step (`/speckit-plan` vs `/speckit.plan`), how to read result/cost. Steps: install that agent's CLI and log
in; `specify integration install <agent>` in each project repo; add/verify the backend class; set
`"backend"` in `config.json`. Only Claude is tested; `CopilotBackend` is an unverified stub.

## 6. macOS and Linux (implemented and tested)

Same code on both; `install.sh` does the platform-specific parts. Verified on macOS (Colima) and on an
Ubuntu 24.04 VM standing in for a Linux server (§6.4).

### 6.1 Why the two platforms differ
| | macOS | Linux |
|---|---|---|
| Docker | runs inside a VM (Colima / Docker Desktop) | native Docker Engine |
| How Plane's containers reach the orchestrator | `host.docker.internal` → forwarded by Colima to the Mac's `127.0.0.1` | Plane's network pinned to `172.30.0.0/24`; the orchestrator also listens on its gateway `172.30.0.1` |
| Orchestrator listens on | `127.0.0.1` | `127.0.0.1` + `172.30.0.1` (never the LAN) |
| Webhook URL | `http://host.docker.internal:8787/webhook` | `http://172.30.0.1:8787/webhook` |
| Runs as | `./flowctl start` (background process) | systemd service (`./install.sh --systemd`) |

The "Linux way" can't be used on a Mac: the gateway address lives inside the Docker VM, not on the Mac.
Running the orchestrator itself in a container would make both identical (and isolate agents), at the cost
of a heavier image — see backlog.

### 6.2 Settings (`orchestrator/config.json`)
Every key is optional; `orchestrator/config.example.json` documents them and `orchestrator/settings.py` holds
the platform defaults: `listen`, `webhook_url`, `plane_api_url` (how this host calls Plane),
`plane_public_url` (what browsers open — links in comments/pages), `dashboard_url`, `manage_plane`
(`false` for a Plane installed separately), `plane_compose_dir`, `plane_compose_project`, `backend`, limits,
styles. The orchestrator re-points the Plane webhook to `webhook_url` at every start.

### 6.3 Install on a Linux server
1. Docker Engine + compose plugin (`sudo usermod -aG docker $USER`), Python 3.11+, `uv`, `git`.
2. Claude Code CLI; headless login with `claude setup-token` (or `ANTHROPIC_API_KEY`) as the same user.
3. `git clone https://github.com/<github-user>/plane-flow.git && cd plane-flow`
4. New Plane: `./install.sh --admin-email you@example.com --public-url http://<server>:8080 --systemd`
   Existing Plane: add `--plane-existing /path/to/plane-app`; the installer tells you the two edits it needs
   (`WEBHOOK_ALLOWED_IPS=172.16.0.0/12`, `API_KEY_RATE_LIMIT=600/minute`, plus copying
   `deploy/linux/docker-compose.override.yml` next to Plane's compose file) and stops until they're done.
   If that Plane already has an admin, first create `.secrets/plane.json` with
   `{"admin": {"email": "...", "password": "..."}}` so bootstrap signs in.
5. Dashboard from your laptop: `ssh -L 8787:localhost:8787 <server>` → http://localhost:8787/dashboard.

### 6.4 How it was tested (repeat this before relying on a change)
- **Fake backend + self-test**: `./flowctl selftest --full` creates a throw-away project on the fake backend
  (no AI cost), drives a card through every gate, then deletes it. 26/26 on macOS.
- **Fresh clone on macOS**: clone to a temp folder, `./install.sh --http-port 8090 --orch-port 8797
  --compose-project pftest --backend fake` → a second, independent instance; full self-test; re-run install
  (idempotent); `docker compose -p pftest down -v` to remove it.
- **Linux**: a second Colima VM is a real Ubuntu host with native Docker and no Mac forwarding:
  `colima start --profile linuxtest --cpu 4 --memory 6`, then inside `colima ssh --profile linuxtest`:
  install uv, `git clone`, `./install.sh --admin-email admin@example.com --http-port 8180 --orch-port 8887
  --backend fake --systemd`. Checks passed: full self-test 27/27; listens only on 127.0.0.1 and 172.30.0.1;
  Plane's worker container reaches the gateway, also after `docker compose down/up`; a switched-off webhook
  is re-enabled on restart; after a VM reboot Plane and the systemd service come back and the self-test
  passes. Afterwards: `colima delete --profile linuxtest` (and `docker context use colima` if the Docker
  context switched). Found and fixed this way: `specify init` refusing to run without the agent CLI installed.

## 7. Rolling out: one pilot project, then every project

1. **Pick a pilot**: a real project with Spec Kit already in use, a working test command, and work you can
   afford to have go through review gates (not a deadline project).
2. **Prepare it** (§5B steps 1–3) and **connect it** (§5B steps 4–9).
3. **Run 2–3 real features end to end**, including at least one with open questions and one rework.
   Watch: does Verify run the real tests? Do Analyze findings make sense under its constitution? Cost per
   feature (📊 AI Metrics)?
4. **Fix what the pilot exposes** in plane-flow (usually: test command, worktree setup, test-agent brief),
   commit, and only then connect the next project.
5. **Roll out** project by project with the same checklist; once `import_project.py` exists, connecting a
   project is one command.

## 8. Backlog (in the order I'd do it)
1. **Resume an imported half-done feature** inside the pipeline (map the card to its existing `specs/NNN-…`
   folder, start at Analyze → Implement). `import_project.py` already covers everything else (§5B).
2. **More per-project settings** in `projects.json` (`test_cmd` and `backend` exist): `worktree_setup`
   (link shared dependencies, copy `.env` files), `feature_naming` (`sequential` → `003-…` vs card key).
3. **Per-project testing brief / domain playbook** as a Plane page that test-agent reads (replaces the
   Python-only `tester_prompt`).
4. **Push / open PR on Done** so the clone and your working copy stay in sync via GitHub.
5. **Diagrams to agents**: download images pasted in pages and `.drawio`/`.drawio.png` card attachments into
   `specs/<feature>/diagrams/`, tell workers to follow them, Analyze/review check conformance. First test:
   does Plane keep PNG bytes intact on upload (needed for `.drawio.png`)?
6. **Copilot (and other) backends** — install, verify flags, `/fleet`, cost reporting.
7. **CI**: a GitHub Actions job running `install.sh --backend fake` + `flowctl selftest --full` on Ubuntu.
8. Orchestrator in a container (uniform networking, agent isolation); rename the constitution's draft step
   (it reuses "Specify 🤖"); bug-fix track; Jira mirror; metrics from recorded events instead of reading
   Plane history (needed at 100s of projects); per-job sandboxing.

## 9. Gotchas learned the hard way
- Plane's web UI reports a state change as `state_id`, the API as `state` — handle both.
- Plane **switches a webhook off** after ~20 minutes of failed deliveries (orchestrator down). Startup
  re-enables it and resumes stranded cards; events during the outage are otherwise lost.
- Plane may deliver the same event twice → deliveries are de-duplicated; guard corrections are matched for 60 s.
- Pages are edited through Plane's collaborative editor: write content via the live server's
  `/live/convert-document/` so it shows in the editor; update in place, don't recreate.
- Plane **stores any text colour but only displays its palette** (gray, peach, pink, orange, green,
  light-blue, dark-blue, purple). Custom hex colours silently render black.
- Sign-in is rate-limited (10/min per IP): sessions are cached and shared across projects.
- Plane assigns its own state `sequence` on create — provisioning re-asserts column order every start.
- Locked pages reject edits even from the owner's API calls — unlock, write, re-lock.
- Spec Kit's constitution keeps old placeholder names in an HTML comment (Sync Impact Report) — ignore
  comments when checking for unfilled placeholders.
- Strict constitutions make the Analyze gate dig deeper each round: expect reworks and cost
  (a small word-counter feature under a strict test constitution took 19 runs / ≈$19).
- Headless Chrome screenshots of the dashboard hang (the live event stream never "finishes") — force a cutoff.

## 10. Picking this up with an AI assistant
Open a Claude Code session in the plane-flow folder; `CLAUDE.md` points it here (and to `LOCAL.md` if present). Useful first prompts:
- "Run the §6.4 checks after any change to settings, networking or install."
- "Import my next project with bootstrap/import_project.py (§5B) and run one small card through Specify."
