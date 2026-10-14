from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    # Must happen before any prospector module reads settings.
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
    os.environ["SUPPRESSION_SALT"] = "test-salt"
    os.environ["CRAWLER_MIN_INTERVAL_S"] = "0"
    os.environ["LOG_LEVEL"] = "WARNING"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    from prospector.config import get_settings, reset_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRAWLER_MIN_INTERVAL_S", "0")
    reset_settings()
    s = get_settings()
    yield s
    reset_settings()


@pytest.fixture()
def engine(settings):
    from prospector import db as dbmod
    from prospector.connectors.base import ensure_connector_states
    from prospector.core.scoring import ensure_rule_version

    eng = dbmod.init_engine(settings)
    dbmod.create_all()
    with dbmod.session_scope() as s:
        for state in ensure_connector_states(s):
            state.terms_reviewed_at = state.terms_reviewed_at or __import__("prospector.util", fromlist=["utcnow"]).utcnow()
        ensure_rule_version(s)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    from prospector.db import session_factory

    s = session_factory()()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture()
def owner(session):
    from prospector.api.security import hash_password
    from prospector.models import User

    user = User(email="owner@test.example", name="Owner", role="owner", password_hash=hash_password("owner-password-12345"))
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def client(engine, owner):
    from fastapi.testclient import TestClient

    from prospector.api.app import create_app

    app = create_app()
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture()
def logged_in(client):
    import re

    r = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/login", data={"email": "owner@test.example", "password": "owner-password-12345", "csrf_token": token, "next": "/"}, follow_redirects=True)
    assert r.status_code == 200
    token = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    client.headers["X-CSRF-Token"] = token
    return client


def html_fixture(name: str) -> str:
    return (FIXTURES / "html" / name).read_text(encoding="utf-8")


def json_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "json" / name).read_text(encoding="utf-8"))


class FixtureSite:
    """Serve fixture HTML for a fake domain through an httpx MockTransport."""

    def __init__(self, pages: dict[str, tuple[int, str, dict | None]], robots: str = "User-agent: *\nAllow: /\n"):
        self.pages = pages
        self.robots = robots
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path or "/"
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots, headers={"content-type": "text/plain"})
        if path in self.pages:
            status, body, headers = self.pages[path]
            h = {"content-type": "text/html; charset=utf-8"}
            h.update(headers or {})
            return httpx.Response(status, text=body, headers=h)
        return httpx.Response(404, text="<html><body>Not found</body></html>", headers={"content-type": "text/html"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=True)


@pytest.fixture()
def wp_site():
    return FixtureSite({
        "/": (200, html_fixture("wp_home.html"), {"server": "LiteSpeed", "x-powered-by": "PHP/7.4.33", "link": "<https://coastalbakes.example/wp-json/>; rel=\"https://api.w.org/\""}),
        "/contact-us/": (200, html_fixture("wp_contact.html"), None),
        "/about/": (200, "<html><head><title>About</title></head><body><h1>About Coastal Bakes</h1></body></html>", None),
    })
