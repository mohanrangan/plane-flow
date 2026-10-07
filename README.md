# plane-flow

Local reference setup: Plane (self-hosted, Docker) as the visual control surface for a
Spec Kit development pipeline run by AI workers. Triggered by Plane webhooks, no polling.

## Feature summary
What this project adds on top of a stock Plane install and stock Spec Kit:

**Control surface (Plane)**
- Board columns *are* the pipeline: 9 pipeline columns between Backlog and Done; 🤖 columns run
  AI workers, 👤 columns are human approval gates. Everything is driven from the browser.
- Webhook-driven, no polling. The orchestrator re-enables the webhook if Plane switches it off
  after failed deliveries, and resumes cards stranded in a 🤖 column after a restart.
- Nine pipeline identities as real Plane accounts (`spec-agent`, `plan-agent`, `tasks-agent`, `dev-agent`,
  `test-agent`, `review-agent`, `constitution-agent`, `flow-bot`, plus `human-editor` for synced edits):
  every comment, page and git commit shows who did it.
- Spec Kit artifacts are published as editable Plane pages; human edits are pulled back into the
  repo before the next step.
- Transition guard: invalid moves are reverted with an explanation; Done only from Acceptance;
  no moves while a worker is running.
- Comment commands: `/note`, `/answer`, `/rework`, `/retry`. Plain comments are for people and
  never reach an agent. Open questions are highlighted (red italics by default).

**Pipeline**
- Specify → (clarify) → Plan → Tasks → **Analyze gate** → Implement ∥ **test-agent** →
  **Verify gate** → Code Review → Acceptance → merge to `main`.
- Clarify: material ambiguity becomes a question for you, not a guess; planning is blocked until
  every question is answered.
- Analyze gate: spec/plan/tasks consistency and constitution compliance checked before any code is
  written; any CRITICAL finding sends the card back.
- test-agent writes adversarial black-box tests in parallel with dev-agent, on a branch cut before
  the code exists. Verify runs the whole suite itself; failures loop back to dev-agent (max 2 rounds).
- Per-card git worktrees: cards run in parallel; agent fan-out (sub-agents) allowed in Implement.

**Governance**
- Per-project constitution card and locked 📜 Constitution page, drafted by `constitution-agent`
  from your snippet, ratified by you (Done), versioned and amendable. Feature work is blocked until
  it is ratified; every step follows the ratified version on `main`.
- Workspace-wide baseline constitution in the **Standards** project, included in every project's.

**Multi-project**
- Every Plane project is provisioned automatically on creation (or at startup): columns in order,
  workers as members, a Spec Kit repo at `repos/<identifier>`, setup page, constitution card,
  metrics page.

**Visibility and cost**
- Per-run metrics (cost, tokens, duration, turns, sub-agents) recorded for every agent run.
- 📊 AI Metrics page per project in Plane (locked, refreshed after every run and move).
- Live dashboard on localhost: all-projects overview and per-project view, pushed updates.

**Backend**
- Agents run as headless Claude Code CLI (`claude -p`) on the logged-in account; no API key.
  A Copilot CLI backend is stubbed behind the same interface (unverified).

## Spec Kit: what was changed and what wasn't
- **No Spec Kit files were modified.** Each project repo gets a stock install
  (`specify init --here --integration claude --script sh`, Spec Kit 1.0.8.dev0): skills in
  `.claude/skills/speckit-*/`, templates in `.specify/templates/`, scripts in `.specify/scripts/bash/`.
- **Everything pipeline-specific is layered on through the prompts and code in `orchestrator/flow.py`:**
  - Every run is told it is unattended: never wait for input, don't create branches or commit
    (the orchestrator owns git), finish with a short summary.
  - `speckit-specify` is told to leave `[NEEDS CLARIFICATION]` markers for material questions instead
    of assuming, and to respect the constitution.
  - Feature directories are named `specs/<card-key>-<slug>` (e.g. `specs/clab-2-word-counter-cli`) and
    passed via `SPECIFY_FEATURE_DIRECTORY`, instead of Spec Kit's sequential `001-…` numbering.
    One branch + worktree per card, created by the orchestrator.
  - `speckit-clarify` (normally interactive) runs in two halves: questions are posted on the card;
    your `/answer`s are handed to the skill pre-answered when the card moves to Plan.
  - `speckit-analyze` (read-only) must end with `CRITICAL_COUNT: n`; the orchestrator writes the report
    to `analysis.md` and enforces the gate.
  - `speckit-constitution` gets your snippet, `/note`s and the Standards baseline, and must leave no
    placeholders; the orchestrator validates and ratifies.
  - Plan reworks must keep `research.md`, `data-model.md`, `quickstart.md` and `contracts/` in sync.
  - `/note`s are appended to the next step's prompt with the rule that they may not change the spec.
- **Not Spec Kit:** test-agent's adversarial testing brief, review-agent's code review, the Verify gate,
  guard, comment commands, pages, metrics and dashboard are all this project's code.
- **Spec Kit features not used yet:** `speckit-checklist` (specify still writes its own requirements
  checklist), `speckit-converge`, `speckit-taskstoissues`, Spec Kit extensions/hooks.

## Known limitations / next steps
- Images and diagrams in pages don't reach the agents yet (planned: `.drawio.png`/`.drawio` support).
- Copilot backend unverified; the constitution's draft step reuses the "Specify" column name.
- The verify test command defaults to Python/pytest for every new project; set `test_cmd` (and `backend`)
  per project in `orchestrator/projects.json`. Existing Spec Kit repos: `bootstrap/import_project.py` (HANDOFF §5B).
- Metrics refresh reads each card's history from Plane; fine at small scale, needs event logging at 100s of projects.
- Strict constitutions make the Analyze gate dig deep: expect rework rounds and higher cost.
- Single machine: localhost only, the agents use your personal Claude login, no sandbox per job.

## Install (macOS or Linux)
Prerequisites: Docker (macOS: Colima or Docker Desktop; Linux: Docker Engine + compose plugin), `git`,
`curl`, `openssl`, Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and the
[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) logged in for the user that runs it
(headless servers: `claude setup-token`). The installer adds the Spec Kit CLI if it's missing.

    git clone https://github.com/<github-user>/plane-flow.git && cd plane-flow
    ./install.sh --admin-email you@example.com          # downloads + starts Plane, sets everything up
    ./install.sh --admin-email you@example.com --plane-existing /path/to/plane   # reuse an installed Plane
    ./install.sh --help                                  # ports, public URL, systemd, fake backend, ...

`install.sh` is safe to re-run. It ends with a self-test. Then open Plane (admin login is in
`.secrets/plane.json`), create a project, and it is set up automatically.

## Run
    ./flowctl start | stop [all] | restart | status | logs [N]
    ./flowctl selftest           # plumbing checks, no AI (~1 min)
    ./flowctl selftest --full    # + a card through every gate on the fake backend (~3 min)

Plane: http://localhost:8080 · dashboard: http://localhost:8787/dashboard (`/dashboard/<IDENTIFIER>` per project).
Settings: `orchestrator/config.json` (written by install.sh, git-ignored; every key is documented in
`orchestrator/config.example.json`, defaults in `orchestrator/settings.py`).

**macOS vs Linux.** Same code; only defaults differ. On macOS Docker runs in a VM and Colima forwards
`host.docker.internal` to the Mac's localhost, so the orchestrator listens on `127.0.0.1`. On Linux, Plane's
Docker network is pinned to `172.30.0.0/24` (`deploy/linux/docker-compose.override.yml`) and the orchestrator
also listens on its gateway `172.30.0.1`; webhooks go there. Neither is reachable from the network. On Linux
`./install.sh --systemd` installs `deploy/linux/plane-flow.service` so it starts at boot.

## Projects
Every Plane project in the workspace is AI-managed. When a project is created (or found at
startup), the orchestrator provisions it within seconds: pipeline columns, the worker accounts as
members, a Spec Kit repo at `repos/<identifier>`, a **🤖 Pipeline setup** page and a
**📊 AI Metrics** page. Default columns that already hold cards are kept, never deleted.
The registry of projects (repo, worktrees, test command, column ids) is `orchestrator/projects.json`.
New repos are only ever created under `repos/`.

## Constitution (per project)
Each project has a **📜 Project constitution** card and a **📜 Constitution** page: the rules every
feature is specified, planned, built and checked against (`.specify/memory/constitution.md`).

1. Write the rules you want in the card's description (a short snippet is fine).
2. Move the card to **Specify 🤖**: on this card it runs `constitution-agent`, which expands the snippet
   into testable MUST rules and includes the workspace baseline (the **Standards** project's
   📜 Baseline constitution page).
3. Review/edit the page in **Spec Review 👤** (or `/rework <changes>`), then move to **Done** to ratify:
   it is validated (no template placeholders, has a version), merged into `main`, and the page locked.
4. Amend: add the change to the description and move the card from Done back to **Specify 🤖**.

Feature work in a project is blocked until its constitution is ratified. Features always follow the
ratified version on `main` (synced into their branch at each step; the version shows on every comment).

**Clarify:** spec-agent asks instead of guessing on material ambiguity (`[NEEDS CLARIFICATION]`).
Questions are posted on the card; answer with `/answer` on the card or by editing the Spec page. Moving to
Plan with unanswered questions is blocked; answers are folded into the spec before planning.

**Analyze gate:** after Tasks, `speckit-analyze` checks spec, plan and tasks for consistency and for
constitution conflicts (always CRITICAL). Any critical finding stops the card before code is written.
Strict constitutions make this gate thorough and can take several rework rounds.

## How it works
Board columns are the pipeline. Moving a card into a 🤖 column sends a webhook to the
orchestrator, which runs that worker headlessly in the card's own git worktree.

| Column | Worker | Step | Output |
|---|---|---|---|
| Specify 🤖 | spec-agent | speckit-specify | Spec page |
| Spec Review 👤 | you | answer open questions, edit the page, move on | |
| Plan 🤖 | spec-agent, then plan-agent | speckit-clarify (folds in your answers), speckit-plan | Plan page |
| Plan Review 👤 | you | | |
| Tasks 🤖 | tasks-agent | speckit-tasks | Tasks page, auto-advances |
| Analyze 🤖 | review-agent | speckit-analyze (read-only) | 0 critical → Implement; any critical → back to Plan Review |
| Implement 🤖 | dev-agent **∥ test-agent** | speckit-implement ∥ independent tests | code + `tests/acceptance/`, Test Plan page |
| Verify 🤖 | test-agent | orchestrator runs the full suite | pass → Code Review; fail → back to Implement (max 2 rounds) |
| Code Review 🤖 | review-agent | review vs spec | Review page |
| Acceptance 👤 | you | move to Done to merge to main | |

- Each worker is its own Plane account; comments, pages and commits carry its name.
  `flow-bot` is the pipeline's own voice (guard corrections, resumes).
- Pages are the editable surface: edits made in Plane are pulled into the repo
  (committed as `human-editor`) before the next phase runs.
- Comments: **a slash talks to the pipeline; plain comments are for people** and never reach an agent
  (a card gets a one-time hint the first time someone comments plainly).
  - `/note <text>`: add to the brief for the card's next run, in any column (including Backlog). Notes
    steer *how* a step is done; agents decline notes that would change *what* is built (change the spec
    instead) and record the notes they applied in the document they produce.
  - `/answer 1: … 2: …`: answer the spec's open questions in Spec Review. Moving to Plan is blocked until
    every numbered question is answered (checked before any agent runs); `/answer 2: use your judgement`
    explicitly delegates.
  - `/rework <feedback>` in a 👤 column re-runs the previous worker now; `/retry` in a 🤖 column re-runs a
    failed step.
- Open questions are shown in the colour set by `styles.question_color` in `orchestrator/config.json`
  (workspace-wide), on the card and on the Spec page. It must be one of Plane's display colours —
  gray, peach, pink, orange, green, light-blue, dark-blue, purple (`peach` displays as red); Plane
  stores other values but shows them uncoloured. `styles.question_italic: true` also italicises them.
  The styling is applied by `styled_question()` in `orchestrator/flow.py`.

## Parallelism
- **Across cards:** every card has its own worktree (`worktrees/`), so cards run at the same
  time (`max_parallel` agent processes in `orchestrator/config.json`).
- **Within a phase:** test-agent works beside dev-agent on a branch cut before any code
  exists, so its tests are written blind to the implementation; merged before Verify.
- **Within a worker (fan-out):** phases marked `fanout` let the worker split its own task
  across sub-agents (Claude Code subagents; Copilot CLI `/fleet`). Count is in the run metrics.

## Guard rails
Plane CE cannot restrict state changes, so the orchestrator enforces `HUMAN_MOVES` in
`flow.py`: invalid moves are put back with a comment listing valid next steps; Done is only
accepted from Acceptance; a card can't be moved while its worker runs.

On start the orchestrator re-enables the webhook (Plane switches it off after ~20 minutes of
failed deliveries) and resumes any card left in a 🤖 column.

## Dashboard
- `/dashboard`: all projects — totals, workers running anywhere, a sortable/filterable project table,
  activity across projects.
- `/dashboard/<ID>`: one project — workers running now (with parallel workers grouped), tiles,
  cumulative cost chart, cost by worker, cards, activity. Read-only; links into Plane.
- Updates are pushed by the orchestrator (server-sent events) as runs start/finish and cards move.

## Layout
- `install.sh` – installer; `flowctl` – start/stop/status/logs/selftest
- `plane/` – Plane's compose files and `.env` for a managed install (downloaded/generated, not in git)
- `bootstrap/bootstrap.py` – Plane setup (idempotent: admin, workspace, worker accounts, webhook);
  `selftest.py` – end-to-end self-test; `drive.py` – act as the human from the CLI
- `orchestrator/flow.py` – webhook receiver, pipeline, dashboard; `provision.py` – project setup;
  `plane.py` – Plane client; `metrics.py` – roll-ups; `backends.py` – agent backends (claude, copilot stub,
  fake); `settings.py` – settings + platform defaults; `config.example.json` – every setting documented;
  `config.json` and `projects.json` – local settings and project registry (generated, not in git)
- `deploy/linux/` – systemd unit and the pinned-network override for Plane
- `repos/<identifier>/` – one repo per project, always on `main`; card work happens in `worktrees/<identifier>/`

## Agents: Claude Code or GitHub Copilot CLI
Per project (`"backend"` in `orchestrator/projects.json`, `--backend` at import) or workspace-wide
(`config.json`). Provisioning installs the matching Spec Kit skills. Claude is tested end to end. Copilot is
wired per its programmatic docs (`copilot -p … --allow-all-tools --no-ask-user --output-format json`,
`--fleet` for parallel subagents in Implement) but must be verified where Copilot is installed:
`./flowctl selftest --agent copilot`. Steps: HANDOFF §5 "Agents".
