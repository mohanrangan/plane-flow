"""Thin Plane client. Every call is made as a specific worker so that comments,
pages and state changes are attributed to that worker, never to a human."""
import re
import threading

import httpx
import markdown as md
from bs4 import BeautifulSoup
from markdownify import markdownify


def md_to_html(text: str) -> str:
    return md.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])


def html_to_md(html: str) -> str:
    return markdownify(html, heading_style="ATX", bullets="-", escape_asterisks=False,
                       escape_underscores=False, escape_misc=False).strip() + "\n"


def text_of(html: str) -> str:
    """Whitespace-insensitive text, used to tell real edits from editor re-serialisation."""
    return re.sub(r"\s+", "", BeautifulSoup(html or "", "html.parser").get_text())


class Plane:
    """Client for one Plane project. Identities are the workers plus "admin" (used only for setup)."""

    # Signed-in sessions are shared by every project: Plane rate-limits sign-ins.
    _web: dict[str, httpx.Client] = {}
    _web_lock = threading.Lock()

    def __init__(self, cfg: dict, project_id: str, states: dict[str, str]):
        self.cfg = cfg
        self.base = cfg["base_url"]
        self.slug, self.pid = cfg["workspace_slug"], project_id
        self.states = states
        self.state_name = {v: k for k, v in states.items()}
        self._api: dict[str, httpx.Client] = {}

    def _identity(self, worker: str) -> dict:
        return self.cfg["admin"] if worker == "admin" else self.cfg["workers"][worker]

    # -- public API (token auth) -------------------------------------------
    def api(self, worker: str) -> httpx.Client:
        if worker not in self._api:
            self._api[worker] = httpx.Client(
                base_url=f"{self.base}/api/v1/workspaces/{self.slug}/projects/{self.pid}",
                headers={"X-API-Key": self._identity(worker)["token"]}, timeout=30)
        return self._api[worker]

    def issue(self, worker: str, issue_id: str) -> dict:
        r = self.api(worker).get(f"/work-items/{issue_id}/")
        r.raise_for_status()
        return r.json()

    def update_issue(self, worker: str, issue_id: str, **fields) -> None:
        self.api(worker).patch(f"/work-items/{issue_id}/", json=fields).raise_for_status()

    def move(self, worker: str, issue_id: str, state: str, assignee: str | None = None) -> None:
        fields = {"state": self.states[state]}
        if assignee:
            fields["assignees"] = [assignee]
        self.update_issue(worker, issue_id, **fields)

    def comment(self, worker: str, issue_id: str, markdown_text: str) -> None:
        self.api(worker).post(f"/work-items/{issue_id}/comments/",
                              json={"comment_html": md_to_html(markdown_text)}).raise_for_status()

    def create_issue(self, worker: str, name: str, description_md: str, state: str) -> dict:
        r = self.api(worker).post("/work-items/", json={"name": name, "description_html": md_to_html(description_md),
                                                        "state": self.states[state]})
        r.raise_for_status()
        return r.json()

    def comments(self, worker: str, issue_id: str) -> list[dict]:
        r = self.api(worker).get(f"/work-items/{issue_id}/comments/", params={"per_page": 100})
        r.raise_for_status()
        return sorted(r.json()["results"], key=lambda c: c["created_at"])

    def lock_page(self, worker: str, page_id: str, locked: bool) -> None:
        c = self.web(worker)
        (c.post if locked else c.delete)(f"{self._pages()}{page_id}/lock/")

    def link(self, worker: str, issue_id: str, title: str, url: str) -> None:
        self.api(worker).post(f"/work-items/{issue_id}/links/", json={"title": title, "url": url})

    # -- web app API (session auth) — pages are not in the CE public API ------
    def web(self, worker: str) -> httpx.Client:
        with Plane._web_lock:
            c = Plane._web.get(worker)
            if c and c.get("/api/users/me/").status_code == 200:
                return c
            w = self._identity(worker)
            c = httpx.Client(base_url=self.base, timeout=30)
            token = c.get("/auth/get-csrf-token/").json()["csrf_token"]
            c.post("/auth/sign-in/", data={"csrfmiddlewaretoken": token,
                                           "email": w["email"], "password": w["password"]})
            c.headers["X-CSRFToken"] = c.cookies.get("csrftoken", token)
            Plane._web[worker] = c
            return c

    def _pages(self) -> str:
        return f"/api/workspaces/{self.slug}/projects/{self.pid}/pages/"

    def page_url(self, page_id: str) -> str:
        return f"{self.cfg.get('public_url', self.base)}/{self.slug}/projects/{self.pid}/pages/{page_id}"

    def create_page(self, worker: str, name: str, html: str) -> str:
        r = self.web(worker).post(self._pages(), json={"name": name, "access": 0, "description_html": html})
        r.raise_for_status()
        return r.json()["id"]

    def page_html(self, worker: str, page_id: str) -> str | None:
        r = self.web(worker).get(f"{self._pages()}{page_id}/")
        return r.json().get("description_html") if r.status_code == 200 else None

    def set_page(self, worker: str, page_id: str, html: str, lock: bool = False) -> None:
        """Replace a page's content in place, in the editor's own format, so the URL never changes."""
        c = self.web(worker)
        conv = c.post("/live/convert-document/", json={"description_html": html, "variant": "document"})
        conv.raise_for_status()
        c.delete(f"{self._pages()}{page_id}/lock/")
        r = c.patch(f"{self._pages()}{page_id}/description/",
                    json={"description_html": html, "description_binary": conv.json()["description_binary"]})
        r.raise_for_status()
        if lock:
            c.post(f"{self._pages()}{page_id}/lock/")

    def page_exists(self, worker: str, page_id: str) -> bool:
        r = self.web(worker).get(f"{self._pages()}{page_id}/")
        return r.status_code == 200 and not r.json().get("archived_at")

    def project_name(self, worker: str) -> str:
        return self.api(worker).get("/").json().get("name", self.pid)

    def delete_page(self, worker: str, page_id: str) -> None:
        c = self.web(worker)
        c.post(f"{self._pages()}{page_id}/archive/")
        c.delete(f"{self._pages()}{page_id}/")
