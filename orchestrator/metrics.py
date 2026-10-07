"""Per-project roll-up of AI worker activity, published as a locked Plane page.

Cost and tokens come from the orchestrator's run log; time-in-column comes from
Plane's own activity history, so lead time and human wait cover every card.
"""
import time
from collections import defaultdict
from datetime import datetime

from plane import md_to_html

HUMAN_MARK, AI_MARK = "👤", "🤖"
CLOSED = {"Done", "Cancelled"}


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _bar(value: float, top: float, width: int = 20) -> str:
    return "█" * max(1, round(width * value / top)) if top and value else ""


def card_timeline(plane, worker: str, issue_id: str, now: float) -> dict:
    """Seconds spent in human vs AI columns, plus created/done times, from Plane's history."""
    r = plane.api(worker).get(f"/work-items/{issue_id}/activities/", params={"per_page": 100})
    acts = sorted(r.json().get("results", []), key=lambda a: a["created_at"]) if r.status_code == 200 else []
    created = next((_ts(a["created_at"]) for a in acts if a.get("verb") == "created"), None)
    moves = [(_ts(a["created_at"]), a.get("old_value") or "", a.get("new_value") or "")
             for a in acts if a.get("field") == "state"]
    human = ai = 0.0
    done_at = None
    for i, (t, _old, new) in enumerate(moves):
        end = moves[i + 1][0] if i + 1 < len(moves) else now
        if HUMAN_MARK in new:
            human += end - t
        elif AI_MARK in new:
            ai += end - t
        done_at = t if new == "Done" else (None if new not in CLOSED else done_at)
    return {"created": created, "done": done_at, "human_s": human, "ai_column_s": ai}


def compute(plane, q, project: str, worker: str) -> dict:
    now = time.time()
    runs = q("SELECT ts, issue, phase, worker, backend, ok, duration_s, cost_usd, tokens_in, tokens_out, "
             "COALESCE(subagents, 0) FROM runs WHERE project=? ORDER BY ts", project)
    states = plane.state_name
    cards = []
    for d in plane.api(worker).get("/work-items/", params={"per_page": 100}).json()["results"]:
        key = f"{project}-{d['sequence_id']}"
        mine = [r for r in runs if r[1] == key]
        tl = card_timeline(plane, worker, d["id"], now)
        cards.append({
            "key": key, "id": d["id"], "name": d["name"], "state": states.get(d["state"], "?"),
            "runs": sum(1 for r in mine if r[4] != "test-runner"),
            "cost": sum(r[7] for r in mine), "agent_s": sum(r[6] for r in mine if r[4] != "test-runner"),
            "rework": sum(1 for r in mine if "rework" in r[2]),
            "verify_runs": sum(1 for r in mine if r[2] == "verify"),
            "verify_fails": sum(1 for r in mine if r[2] == "verify" and not r[5]),
            "first_run": mine[0][0] if mine else None, **tl,
        })
    cards.sort(key=lambda c: (c["first_run"] is None, c["first_run"] or 0, c["key"]))

    total = sum(r[7] for r in runs)
    week = sum(r[7] for r in runs if r[0] >= now - 7 * 86400)
    agent_runs = [r for r in runs if r[4] != "test-runner"]
    features = [c for c in cards if not c["name"].startswith("📜")]  # the constitution card isn't a feature
    delivered = [c for c in features if c["state"] == "Done" and c["runs"]]
    in_flight = [c for c in features if c["state"] not in CLOSED | {"Backlog"}]
    first_pass = [c for c in delivered if not c["rework"] and not c["verify_fails"]]
    leads = [c["done"] - c["created"] for c in delivered if c["done"] and c["created"]]
    waits = [c["human_s"] for c in delivered]

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0

    workers = defaultdict(list)
    for r in runs:
        workers[r[3] + (" (verify gate)" if r[4] == "test-runner" else "")].append(r)
    running, series = 0.0, []
    for r in agent_runs:
        running += r[7]
        series.append({"ts": r[0], "cumulative": round(running, 4), "cost": r[7], "card": r[1],
                       "worker": r[3], "phase": r[2]})
    return {
        "project": project, "generated": now,
        "headline": {
            "cumulative_cost": total, "cost_7d": week, "delivered": len(delivered), "in_flight": len(in_flight),
            "avg_cost_per_feature": avg([c["cost"] for c in delivered]),
            "first_pass": len(first_pass), "first_pass_rate": len(first_pass) / len(delivered) if delivered else None,
            "avg_lead_s": avg(leads) if leads else None, "avg_human_wait_s": avg(waits) if waits else None,
            "agent_runs": len(agent_runs), "failed_runs": sum(1 for r in agent_runs if not r[5]),
            "agent_s": sum(r[6] for r in agent_runs),
            "tokens_in": sum(r[8] for r in runs), "tokens_out": sum(r[9] for r in runs),
        },
        "series": series,
        "cards": [c for c in cards if c["runs"] or c["state"] not in CLOSED | {"Backlog"}],
        "workers": sorted(({"worker": w, "runs": len(rs), "failed": sum(1 for r in rs if not r[5]),
                            "cost": sum(r[7] for r in rs), "avg_cost": avg([r[7] for r in rs]),
                            "avg_s": avg([r[6] for r in rs]), "subagents": sum(r[10] for r in rs)}
                           for w, rs in workers.items()), key=lambda w: -w["cost"]),
    }


def build(m: dict, dashboard_url: str | None = None) -> str:
    """Render the computed metrics as the Plane Metrics page."""
    h = m["headline"]
    total = h["cumulative_cost"]
    out = [
        f"_Auto-generated by flow-bot · updated {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"refreshes after every run and card move · read-only._"
        + (f" **[Open the live dashboard]({dashboard_url})** (on this Mac)." if dashboard_url else ""),
        "## Headline",
        "| Metric | Value |", "|---|---|",
        f"| **Cumulative AI cost** | **${total:.2f}** |",
        f"| Last 7 days | ${h['cost_7d']:.2f} |",
        f"| Features delivered (Done) | {h['delivered']} |",
        f"| In flight | {h['in_flight']} |",
        f"| Avg cost per delivered feature | ${h['avg_cost_per_feature']:.2f} |",
        f"| First-pass rate (no rework, no failed verify) | "
        f"{100 * (h['first_pass_rate'] or 0):.0f}% ({h['first_pass']}/{h['delivered']}) |",
        f"| Avg lead time (created → Done) | {_dur(h['avg_lead_s']) if h['avg_lead_s'] else '–'} |",
        f"| Avg time waiting on humans (👤 columns) | "
        f"{_dur(h['avg_human_wait_s']) if h['avg_human_wait_s'] is not None else '–'} |",
        f"| Agent runs / failed | {h['agent_runs']} / {h['failed_runs']} |",
        f"| Agent working time | {_dur(h['agent_s'])} |",
        f"| Tokens in / out | {h['tokens_in'] / 1e6:.2f}M / {h['tokens_out'] / 1e3:.0f}k |",
        "",
        "## Cumulative cost by day",
        "| Day | Runs | Cost | Cumulative | |", "|---|---|---|---|---|",
    ]
    by_day = defaultdict(lambda: [0, 0.0])
    for r in m["series"]:
        day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        by_day[day][0] += 1
        by_day[day][1] += r["cost"]
    running = 0.0
    for day, (n, cost) in sorted(by_day.items()):
        running += cost
        out.append(f"| {day} | {n} | ${cost:.2f} | ${running:.2f} | {_bar(running, total)} |")

    out += ["", "## Cards",
            "| Card | State | Agent runs | Rework | Verify (fails) | Cost | Cumulative | Agent time | "
            "Waiting on humans | Lead time |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    running = 0.0
    for c in m["cards"]:
        running += c["cost"]
        lead = _dur(c["done"] - c["created"]) if c["done"] and c["created"] else "–"
        out.append(f"| {c['key']} {c['name'][:50]} | {c['state']} | {c['runs']} | {c['rework']} | "
                   f"{c['verify_runs']} ({c['verify_fails']}) | ${c['cost']:.2f} | ${running:.2f} | "
                   f"{_dur(c['agent_s'])} | {_dur(c['human_s'])} | {lead} |")

    out += ["", "## Workers",
            "| Worker | Runs | Failed | Cost | Avg cost / run | Avg duration | Sub-agents |",
            "|---|---|---|---|---|---|---|"]
    for w in m["workers"]:
        out.append(f"| {w['worker']} | {w['runs']} | {w['failed']} | ${w['cost']:.2f} | ${w['avg_cost']:.2f} | "
                   f"{_dur(w['avg_s'])} | {w['subagents']} |")

    out += ["", "## Notes",
            "- **Cost** is the API-price equivalent reported by the agent CLI per run. It is real spend on API "
            "billing; on a subscription it is a usage measure, not a charge.",
            "- **Waiting on humans** is time spent in 👤 columns, from Plane's history. Time in 🤖 columns beyond "
            "agent working time is queueing or pipeline downtime.",
            "- The verify gate runs the test suite without a model, so it costs nothing."]
    return md_to_html("\n".join(out))


def card_total(q, key: str) -> str:
    (cost, runs, secs), = q("SELECT COALESCE(SUM(cost_usd),0), COUNT(*), COALESCE(SUM(duration_s),0) FROM runs "
                            "WHERE issue=? AND backend != 'test-runner'", key)
    return f"{key} total: ${cost:.2f} across {runs} agent runs, {_dur(secs)} agent time."
