"""Command-line entry points: `prospector <command>`."""

from __future__ import annotations

import getpass
import json
import logging
import sys
from pathlib import Path

import typer
from sqlalchemy import select

from . import __version__
from .config import get_settings
from .db import create_all, init_engine, session_factory
from .logging_setup import configure_logging

app = typer.Typer(help="PB4HOST Prospector — organisation-first prospecting with provenance.", no_args_is_help=True)
log = logging.getLogger("prospector.cli")


def _boot():
    settings = get_settings()
    configure_logging(settings.log_level)
    init_engine(settings)
    return settings


def _alembic_config(settings):
    """alembic.ini sits next to the package in a checkout, or in the working directory of a container."""
    from alembic.config import Config

    candidates = [Path.cwd() / "alembic.ini", Path(__file__).resolve().parent.parent / "alembic.ini"]
    ini = next((c for c in candidates if c.exists()), None)
    if ini is None:
        raise FileNotFoundError("alembic.ini not found in the working directory or next to the package")
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


@app.callback()
def _version_cb(version: bool = typer.Option(False, "--version", help="Show version and exit.")):
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("init-db")
def init_db(alembic: bool = typer.Option(True, help="Stamp the Alembic head after creating tables.")):
    """Create all tables (SQLite dev) and seed connector states."""
    settings = _boot()
    from .connectors.base import ensure_connector_states
    from .core.scoring import ensure_rule_version

    create_all()
    with session_factory()() as session:
        ensure_connector_states(session)
        ensure_rule_version(session)
        session.commit()
    if alembic:
        try:
            from alembic import command

            command.stamp(_alembic_config(settings), "head")
        except Exception as exc:
            log.warning("alembic stamp skipped: %s", exc)
    typer.echo(f"database ready at {settings.database_url}")


@app.command("migrate")
def migrate():
    """Run Alembic migrations to head (PostgreSQL production)."""
    settings = _boot()
    from alembic import command

    command.upgrade(_alembic_config(settings), "head")
    from .connectors.base import ensure_connector_states
    from .core.scoring import ensure_rule_version

    with session_factory()() as session:
        ensure_connector_states(session)
        ensure_rule_version(session)
        session.commit()
    typer.echo("migrations applied")


@app.command("create-user")
def create_user(
    email: str = typer.Option(..., prompt=True),
    name: str = typer.Option("", prompt=True),
    role: str = typer.Option("owner", prompt=True),
    password: str = typer.Option(None, help="Omit to be prompted securely."),
):
    """Create a login. The first user should be an owner."""
    _boot()
    from .api.security import hash_password
    from .models import ROLES, User

    if role not in ROLES:
        typer.echo(f"role must be one of {ROLES}", err=True)
        raise typer.Exit(1)
    if not password:
        password = getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        typer.echo("password too short", err=True)
        raise typer.Exit(1)
    with session_factory()() as session:
        if session.scalar(select(User).where(User.email == email.lower())):
            typer.echo("user exists", err=True)
            raise typer.Exit(1)
        session.add(User(email=email.lower(), name=name, role=role, password_hash=hash_password(password)))
        session.commit()
    typer.echo(f"created {role} {email.lower()}")


@app.command("serve")
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    """Run the web app with uvicorn."""
    import uvicorn

    uvicorn.run("prospector.api.app:create_app", host=host, port=port, reload=reload, factory=True)


@app.command("worker")
def worker(once: bool = typer.Option(False, help="Drain the queue once and exit.")):
    """Run the background worker."""
    _boot()
    from .jobs.queue import run_pending
    from .jobs.worker import Worker

    if once:
        n = run_pending(session_factory())
        typer.echo(f"executed {n} job(s)")
        return
    Worker().run_forever()


@app.command("run")
def run_discovery(
    query: str = typer.Argument(..., help='e.g. "web design agency"'),
    location: str = typer.Option("", help='e.g. "Mangaluru, Karnataka"'),
    track: str = typer.Option("smb"),
    pages: int = typer.Option(1, min=1),
    wait: bool = typer.Option(False, help="Process the queue inline until idle (no worker needed)."),
):
    """Queue a Google Places discovery run (needs GOOGLE_MAPS_API_KEY and a reviewed connector)."""
    _boot()
    from .api.routers.runs import create_run
    from .jobs.queue import run_pending

    with session_factory()() as session:
        run = create_run(session, query=query, location=location, track=track, max_pages=pages, region_code=None, user=None)
        session.commit()
        typer.echo(f"queued run #{run.id}")
    if wait:
        n = run_pending(session_factory())
        typer.echo(f"processed {n} job(s)")


@app.command("spike")
def spike(
    urls_file: Path = typer.Argument(..., exists=True, readable=True, help="Text file with one website URL per line."),
    track: str = typer.Option("unknown", help="smb, agency or unknown (lets agency signals promote the track)."),
    pagespeed: bool = typer.Option(False, help="Also run PageSpeed (needs terms review + quota)."),
    out: Path = typer.Option(Path("spike-report.json")),
):
    """Week-2 validation spike: probe a list of URLs, score them, and write a precision report."""
    settings = _boot()
    from .core.organizations import ensure_website, resolve_organization
    from .jobs.queue import run_pending
    from .jobs.tasks import enqueue_enrichment
    from .models import Organization

    urls = [u.strip() for u in urls_file.read_text().splitlines() if u.strip() and not u.startswith("#")]
    if not pagespeed:
        settings.psi_strategies = []  # type: ignore[assignment]
    ids: list[int] = []
    with session_factory()() as session:
        for url in urls:
            resolved = resolve_organization(session, website=url, track=track)
            ensure_website(session, resolved.organization, url)
            enqueue_enrichment(session, resolved.organization.id, url, reason="spike")
            ids.append(resolved.organization.id)
        session.commit()
    typer.echo(f"queued {len(ids)} probes; processing inline …")
    n = run_pending(session_factory())
    typer.echo(f"processed {n} job(s)")
    report = []
    with session_factory()() as session:
        for org_id in ids:
            org = session.get(Organization, org_id)
            site = org.primary_website if org else None
            report.append({
                "organization_id": org_id, "domain": org.domain if org else None, "name": org.label if org else None,
                "cms": site.cms if site else None, "cms_version": site.cms_version if site else None,
                "woocommerce": site.is_woocommerce if site else None, "page_builder": site.page_builder if site else None,
                "caching": site.caching_layer if site else None, "cdn": site.cdn if site else None,
                "error_state": site.error_state if site else None, "host_provider": site.host_provider if site else None,
                "phones": org.phones if org else [], "emails": org.emails if org else [],
                "score": org.score_total if org else None, "band": org.score_band if org else None,
                "signals": org.signals if org else [],
                "human_review": {"cms_correct": None, "contact_correct": None, "note": ""},
            })
    out.write_text(json.dumps(report, indent=2))
    typer.echo(f"wrote {out} — fill in human_review for the precision check (target ≥90% CMS, ≥85% contact facts)")


@app.command("retention")
def retention(dry_run: bool = typer.Option(False)):
    """Run the retention policy once and print the report."""
    _boot()
    from .compliance.retention import run_retention

    with session_factory()() as session:
        report = run_retention(session)
        if dry_run:
            session.rollback()
        else:
            session.commit()
    typer.echo(json.dumps(report, indent=2, default=str))


@app.command("demo-seed")
def demo_seed(count: int = typer.Option(12, min=1, max=60)):
    """Insert clearly-labelled synthetic organisations (.example domains) to explore the UI offline."""
    _boot()
    from .demo import seed_demo

    with session_factory()() as session:
        created = seed_demo(session, count=count)
        session.commit()
    typer.echo(f"seeded {created} demo organisations (names prefixed 'Demo:', domains under .example)")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
