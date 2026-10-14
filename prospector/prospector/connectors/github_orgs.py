"""GitHub organisation discovery for the agency track (plan §9 `github_orgs`).

Flow per run page:
1. `GET /search/users?q=type:org location:"<city>" language:PHP` → organisation logins;
2. `GET /orgs/{login}` → name, website (`blog`), location, html_url;
3. `GET /orgs/{login}/repos?sort=pushed&per_page=30` → how many repos look like WordPress work.

Only organisation-level facts are kept (name, website, location, GitHub URL, WordPress repo
count). We never call `/users/{login}` for members, never read commit e-mails, and GitHub data
is used for discovery only — outreach goes to the business through its own website (GitHub
Acceptable Use Policies §7: no spamming or unsolicited e-mail from GitHub information).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import Candidate, Connector, ConnectorError, register

API = "https://api.github.com"
WP_MARKERS = re.compile(r"\b(wordpress|woocommerce|wp-[a-z]|gutenberg|elementor|wpcs|bedrock)\b", re.I)


@dataclass
class GithubOrg:
    login: str
    name: str | None
    website: str | None
    location: str | None
    html_url: str
    public_repos: int = 0
    wordpress_repos: int = 0
    recent_repos: int = 0


@dataclass
class GithubPage:
    candidates: list[Candidate] = field(default_factory=list)
    raw_count: int = 0
    without_website: int = 0
    total_count: int = 0
    calls: int = 0
    has_more: bool = False


def count_wordpress_repos(repos: list[dict[str, Any]]) -> int:
    hits = 0
    for repo in repos:
        haystack = " ".join([repo.get("name") or "", repo.get("description") or "", " ".join(repo.get("topics") or [])])
        if WP_MARKERS.search(haystack):
            hits += 1
    return hits


def city_from_location(location: str | None) -> str | None:
    if not location:
        return None
    first = location.split(",")[0].strip()
    return first[:120] or None


@register
class GithubOrgsConnector(Connector):
    name = "github_orgs"
    terms_url = "https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies"
    requires_terms_review = True
    default_daily_cap = 300

    def __init__(self, settings=None, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                       "User-Agent": self.settings.crawler_user_agent}
            if self.settings.github_token:
                headers["Authorization"] = f"Bearer {self.settings.github_token}"
            self._client = httpx.Client(timeout=20.0, headers=headers)
        return self._client

    def _get(self, session, path: str, params: dict[str, Any] | None = None) -> Any:
        self.reserve_calls(session, 1)
        try:
            resp = self.client.get(API + path, params=params)
        except httpx.HTTPError as exc:
            self.record_error(session, f"transport: {exc}")
            raise ConnectorError(f"GitHub request failed: {exc}") from exc
        if resp.status_code in {403, 429} and resp.headers.get("x-ratelimit-remaining") == "0":
            self.record_error(session, "rate limited")
            raise ConnectorError("GitHub rate limit reached; the run stops and can be resumed later")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            self.record_error(session, f"HTTP {resp.status_code}: {resp.text[:200]}")
            raise ConnectorError(f"GitHub HTTP {resp.status_code} for {path}")
        return resp.json()

    def search_page(self, session, *, location: str, page: int = 1, per_page: int = 30, language: str | None = "PHP",
                    extra_query: str | None = None) -> GithubPage:
        q = f'type:org location:"{location}"'
        if language:
            q += f" language:{language}"
        if extra_query:
            q += f" {extra_query}"
        data = self._get(session, "/search/users", {"q": q, "per_page": per_page, "page": page})
        out = GithubPage(calls=1)
        if not data:
            return out
        items = data.get("items") or []
        out.total_count = int(data.get("total_count") or 0)
        out.raw_count = len(items)
        out.has_more = page * per_page < out.total_count
        for item in items:
            login = item.get("login")
            if not login:
                continue
            profile = self._get(session, f"/orgs/{login}")
            out.calls += 1
            if not profile:
                continue
            website = (profile.get("blog") or "").strip() or None
            if website and not website.lower().startswith(("http://", "https://")):
                website = "https://" + website
            if not website:
                out.without_website += 1
                continue
            repos = self._get(session, f"/orgs/{login}/repos", {"sort": "pushed", "per_page": 30}) or []
            out.calls += 1
            org = GithubOrg(login=login, name=profile.get("name"), website=website, location=profile.get("location"),
                            html_url=profile.get("html_url") or f"https://github.com/{login}", public_repos=int(profile.get("public_repos") or 0),
                            wordpress_repos=count_wordpress_repos(repos), recent_repos=len(repos))
            out.candidates.append(Candidate(
                provider=self.name, external_id=login, track="agency",
                persist={},  # nothing persisted from GitHub except facts applied with provenance by the task
                transient={"website": website, "name": org.name, "city": city_from_location(org.location),
                           "location": org.location, "html_url": org.html_url, "wordpress_repos": org.wordpress_repos,
                           "public_repos": org.public_repos},
            ))
        return out
