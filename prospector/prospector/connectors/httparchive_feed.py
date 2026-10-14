"""Monthly CrUX + HTTP Archive discovery feed (plan §4.3 / §9 `httparchive_feed`).

One BigQuery query per month lists WordPress origins in a country with poor real-user mobile
LCP. Cost control is fail-closed: a dry run estimates the bytes scanned and the query only runs
when the estimate is under `httparchive_max_bytes` (default 100 GB ≈ US$0.63 on-demand).

The BigQuery client library is optional (`pip install pb4host-prospector[bigquery]`); a
`FeedBackend` can be injected for tests or for running the SQL elsewhere and loading the CSV.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .base import Candidate, Connector, ConnectorError, register

SQL_PATH = Path(__file__).parent / "sql" / "httparchive_wordpress_poor_cwv.sql"
BYTES_PER_TB = 1024**4
ON_DEMAND_USD_PER_TB = 6.25


class FeedBackend(Protocol):
    def dry_run_bytes(self, sql: str, params: dict[str, Any]) -> int: ...

    def run(self, sql: str, params: dict[str, Any]) -> Iterable[Mapping[str, Any]]: ...


class GoogleBigQueryBackend:
    """Thin wrapper around google-cloud-bigquery, imported lazily."""

    def __init__(self, project: str | None, credentials_file: Path | None = None) -> None:
        try:
            from google.cloud import bigquery  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ConnectorError("install the 'bigquery' extra: pip install 'pb4host-prospector[bigquery]'") from exc
        self._bq = bigquery
        if credentials_file:
            self.client = bigquery.Client.from_service_account_json(str(credentials_file), project=project)
        else:
            self.client = bigquery.Client(project=project)

    def _config(self, params: dict[str, Any], dry_run: bool):
        bq = self._bq
        typed = []
        for name, value in params.items():
            if isinstance(value, date):
                typed.append(bq.ScalarQueryParameter(name, "DATE", value))
            elif isinstance(value, int):
                typed.append(bq.ScalarQueryParameter(name, "INT64", value))
            else:
                typed.append(bq.ScalarQueryParameter(name, "STRING", str(value)))
        return bq.QueryJobConfig(query_parameters=typed, dry_run=dry_run, use_query_cache=not dry_run)

    def dry_run_bytes(self, sql: str, params: dict[str, Any]) -> int:
        job = self.client.query(sql, job_config=self._config(params, dry_run=True))
        return int(job.total_bytes_processed or 0)

    def run(self, sql: str, params: dict[str, Any]) -> Iterable[Mapping[str, Any]]:
        job = self.client.query(sql, job_config=self._config(params, dry_run=False))
        for row in job.result():
            yield dict(row.items())


@dataclass
class FeedRow:
    origin: str
    lcp_p75: int | None = None
    inp_p75: int | None = None
    cls_p75: float | None = None
    rank: int | None = None


@dataclass
class FeedResult:
    month: str
    country: str
    rows: list[FeedRow] = field(default_factory=list)
    bytes_estimated: int = 0
    bytes_budget: int = 0
    cost_estimate_usd: float = 0.0

    def candidates(self) -> list[Candidate]:
        out = []
        for row in self.rows:
            out.append(Candidate(
                provider="httparchive_feed", external_id=row.origin, track="smb", persist={},
                transient={"website": row.origin.rstrip("/") + "/", "lcp_p75": row.lcp_p75, "inp_p75": row.inp_p75,
                           "cls_p75": row.cls_p75, "rank": row.rank, "month": self.month, "country": self.country},
            ))
        return out


def month_params(month: str) -> tuple[date, int]:
    try:
        year, mon = (int(p) for p in month.split("-", 1))
        first = date(year, mon, 1)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"month must be YYYY-MM, got {month!r}") from exc
    return first, year * 100 + mon


def _num(raw: Mapping[str, Any], key: str, cast=int):
    value = raw.get(key)
    try:
        return cast(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_rows(rows: Iterable[Mapping[str, Any]]) -> list[FeedRow]:
    out: list[FeedRow] = []
    for raw in rows:
        origin = str(raw.get("origin") or "").strip()
        if not origin.startswith(("http://", "https://")):
            continue
        out.append(FeedRow(origin=origin, lcp_p75=_num(raw, "lcp_p75"), inp_p75=_num(raw, "inp_p75"),
                           cls_p75=_num(raw, "cls_p75", float), rank=_num(raw, "rank")))
    return out


@register
class HttpArchiveFeedConnector(Connector):
    name = "httparchive_feed"
    terms_url = "https://developer.chrome.com/docs/crux/bigquery"  # CrUX & HTTP Archive datasets (CC BY 4.0)
    requires_terms_review = True
    default_daily_cap = 2

    def __init__(self, settings=None, backend: FeedBackend | None = None) -> None:
        super().__init__(settings)
        self._backend = backend

    @property
    def backend(self) -> FeedBackend:
        if self._backend is None:
            self._backend = GoogleBigQueryBackend(self.settings.bigquery_project, self.settings.bigquery_credentials_file)
        return self._backend

    def sql(self) -> str:
        return SQL_PATH.read_text(encoding="utf-8")

    def fetch(self, session, *, month: str, country: str | None = None, limit: int | None = None,
              lcp_threshold_ms: int = 4000) -> FeedResult:
        country = (country or self.settings.httparchive_default_country).lower()
        limit = int(limit or self.settings.httparchive_default_limit)
        first, yyyymm = month_params(month)
        params = {"month": first, "yyyymm": yyyymm, "country": country, "lcp_threshold_ms": lcp_threshold_ms, "limit": limit}
        budget = int(self.settings.httparchive_max_bytes)
        self.reserve_calls(session, 1)
        sql = self.sql()
        estimate = int(self.backend.dry_run_bytes(sql, params))
        result = FeedResult(month=month, country=country, bytes_estimated=estimate, bytes_budget=budget,
                            cost_estimate_usd=round(estimate / BYTES_PER_TB * ON_DEMAND_USD_PER_TB, 4))
        if estimate > budget:
            self.record_error(session, f"dry run {estimate / 1024**3:.1f} GB exceeds budget {budget / 1024**3:.1f} GB")
            raise ConnectorError(
                f"HTTP Archive feed refused: dry run estimates {estimate / 1024**3:.1f} GB, budget is "
                f"{budget / 1024**3:.1f} GB (raise HTTPARCHIVE_MAX_BYTES deliberately if this is expected)"
            )
        result.rows = parse_rows(self.backend.run(sql, params))
        return result
