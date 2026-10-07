# /// script
# dependencies = ["httpx", "markdown"]
# ///
"""Act as the human in Plane from the command line (same API calls the web UI makes).

  drive.py project NAME IDENT                 create a project (the pipeline sets it up)
  drive.py card IDENT "title" "description"   create a card in Backlog
  drive.py describe IDENT-N "markdown"        append to a card's description
  drive.py move IDENT-N "State name"          move a card (like dragging it)
  drive.py comment IDENT-N "text"             comment on a card
  drive.py wait IDENT-N "text" [secs]         wait until a comment containing text appears
  drive.py show IDENT-N [width]               state, assignee and comment trail
  drive.py page IDENT "title fragment"        print a page's text
  drive.py edit IDENT "title fragment" OLD NEW   edit a page's text as the human
"""
import json
import pathlib
import re
import sys
import time

import httpx
import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / ".secrets/plane.json").read_text())
BASE, SLUG = CFG["base_url"], CFG["workspace_slug"]
H = {"X-API-Key": CFG["admin"]["token"]}
NAMES = {w["id"]: n for n, w in CFG["workers"].items()} | {CFG["admin"]["id"]: "HUMAN"}


def reg():
    return json.loads((ROOT / "orchestrator/projects.json").read_text())


def project(ident):
    pid, e = next((k, v) for k, v in reg().items() if v["identifier"] == ident)
    return pid, e, httpx.Client(base_url=f"{BASE}/api/v1/workspaces/{SLUG}/projects/{pid}", headers=H, timeout=30)


def card(key):
    ident, n = key.rsplit("-", 1)
    pid, e, a = project(ident)
    item = next(i for i in a.get("/work-items/", params={"per_page": 100}).json()["results"]
                if i["sequence_id"] == int(n))
    return e, a, item


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def session():
    c = httpx.Client(base_url=BASE, timeout=30)
    t = c.get("/auth/get-csrf-token/").json()["csrf_token"]
    c.post("/auth/sign-in/", data={"csrfmiddlewaretoken": t, "email": CFG["admin"]["email"],
                                   "password": CFG["admin"]["password"]})
    c.headers["X-CSRFToken"] = c.cookies.get("csrftoken", t)
    return c


def find_page(ident, frag):
    pid, _, _ = project(ident)
    c = session()
    base = f"/api/workspaces/{SLUG}/projects/{pid}/pages/"
    p = next(p for p in c.get(base).json() if frag in p["name"] and not p.get("archived_at"))
    return c, base, p


cmd, *args = sys.argv[1:]
if cmd == "project":
    a = httpx.Client(base_url=f"{BASE}/api/v1/workspaces/{SLUG}", headers=H, timeout=30)
    r = a.post("/projects/", json={"name": args[0], "identifier": args[1]})
    print("project", r.status_code)
    for _ in range(45):
        if any(v["identifier"] == args[1] and v.get("setup_page") for v in reg().values()):
            print("provisioned")
            break
        time.sleep(2)
elif cmd == "card":
    pid, e, a = project(args[0])
    i = a.post("/work-items/", json={"name": args[1], "description_html": markdown.markdown(args[2])}).json()
    print(f"created {args[0]}-{i['sequence_id']}")
elif cmd == "describe":
    e, a, i = card(args[0])
    html = (i.get("description_html") or "") + markdown.markdown(args[1])
    print("describe", a.patch(f"/work-items/{i['id']}/", json={"description_html": html}).status_code)
elif cmd == "move":
    e, a, i = card(args[0])
    print("move", a.patch(f"/work-items/{i['id']}/", json={"state": e["states"][args[1]]}).status_code)
elif cmd == "comment":
    e, a, i = card(args[0])
    print("comment", a.post(f"/work-items/{i['id']}/comments/", json={"comment_html": f"<p>{args[1]}</p>"}).status_code)
elif cmd == "wait":
    e, a, i = card(args[0])
    deadline = time.time() + (int(args[2]) if len(args) > 2 else 900)
    seen = len(a.get(f"/work-items/{i['id']}/comments/").json()["results"]) if args[1].startswith("+") else 0
    needle = args[1].lstrip("+")
    while time.time() < deadline:
        cs = sorted(a.get(f"/work-items/{i['id']}/comments/").json()["results"], key=lambda c: c["created_at"])
        if any(needle in text(c["comment_html"]) for c in cs[seen:]):
            print("seen:", needle)
            break
        time.sleep(10)
    else:
        print("TIMEOUT waiting for", needle)
elif cmd == "show":
    e, a, i = card(args[0])
    states = {v: k for k, v in e["states"].items()}
    width = int(args[1]) if len(args) > 1 else 200
    print(f"{args[0]} [{states.get(i['state'])}] assignees={[NAMES.get(x, x) for x in i['assignees']]}")
    for c in sorted(a.get(f"/work-items/{i['id']}/comments/").json()["results"], key=lambda c: c["created_at"]):
        print(f"  {c['created_at'][11:19]} {NAMES.get(c['actor'], c['actor']):18} {text(c['comment_html'])[:width]}")
elif cmd == "page":
    c, base, p = find_page(args[0], args[1])
    print(f"== {p['name']} (locked={p['is_locked']}, owner={NAMES.get(p['owned_by'])})")
    print(text(c.get(f"{base}{p['id']}/").json()["description_html"]))
elif cmd == "edit":
    c, base, p = find_page(args[0], args[1])
    html = c.get(f"{base}{p['id']}/").json()["description_html"]
    assert args[2] in html, "text to replace not found on the page"
    r = c.patch(f"{base}{p['id']}/description/", json={"description_html": html.replace(args[2], args[3])})
    print("edit", r.status_code, r.text[:120])
