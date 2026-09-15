"""Minimal GitHub REST client. Stdlib only; honours GITHUB_API_URL for GHES."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class GitHubApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API HTTP {status}: {message}")
        self.status = status


class GitHubClient:
    def __init__(self, token: str, api_url: str | None = None):
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "paradime-dinoai-action",
        }

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> tuple[dict | list, dict]:
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self._headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return (json.loads(raw) if raw else {}), dict(resp.headers)
        except urllib.error.HTTPError as e:
            raise GitHubApiError(e.code, e.read().decode(errors="replace")[:1000]) from e

    def get(self, path: str, params: dict | None = None) -> dict | list:
        return self._request("GET", path, params=params)[0]

    def post(self, path: str, body: dict) -> dict | list:
        return self._request("POST", path, body=body)[0]

    def paginate(self, path: str, *, per_page: int = 100, max_items: int = 1000) -> list[dict]:
        items: list[dict] = []
        page = 1
        while len(items) < max_items:
            batch, _ = self._request("GET", path, params={"per_page": per_page, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return items[:max_items]

    # --- pull requests -------------------------------------------------------------------

    def get_pull(self, repo: str, number: int) -> dict:
        return self.get(f"/repos/{repo}/pulls/{number}")  # type: ignore[return-value]

    def list_pull_files(self, repo: str, number: int, *, max_files: int = 300) -> list[dict]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/files", max_items=max_files)

    def list_issue_comments(self, repo: str, number: int) -> list[dict]:
        return self.paginate(f"/repos/{repo}/issues/{number}/comments", max_items=200)

    def list_review_comments(self, repo: str, number: int) -> list[dict]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/comments", max_items=300)

    def list_reviews(self, repo: str, number: int) -> list[dict]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/reviews", max_items=200)

    def create_review(self, repo: str, number: int, *, commit_id: str, body: str, event: str, comments: list[dict]) -> dict:
        payload = {"commit_id": commit_id, "body": body, "event": event, "comments": comments}
        return self.post(f"/repos/{repo}/pulls/{number}/reviews", payload)  # type: ignore[return-value]

    def file_exists(self, repo: str, path: str, ref: str) -> bool:
        try:
            self.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
            return True
        except GitHubApiError as e:
            if e.status == 404:
                return False
            raise

    def create_issue_comment(self, repo: str, number: int, body: str) -> dict:
        return self.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})  # type: ignore[return-value]
