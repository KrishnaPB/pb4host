"""Rule-based, explainable lead scoring (§10). Rules live in versioned YAML.

Conditions are evaluated by a restricted expression evaluator (comparisons, boolean logic,
attribute access on read-only namespaces, list literals). No calls, no builtins, no eval().
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Organization, PerformanceSnapshot, ScoringRuleVersion, Website
from ..util import php_is_eol, utcnow, wp_releases_behind

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "scoring" / "v1.yaml"


class Namespace:
    """Attribute access returning None for unknown names (so rules never raise on gaps)."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Any:  # only called when attribute is missing
        return None

    def as_dict(self) -> dict[str, Any]:
        return {k: (v.as_dict() if isinstance(v, Namespace) else v) for k, v in self.__dict__.items()}


_CONSTANT_NAMES = {"none": None, "null": None, "None": None, "true": True, "false": False,
                   "True": True, "False": False}
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Name, ast.Attribute, ast.Constant, ast.List, ast.Tuple, ast.Load,
)


class RuleSyntaxError(ValueError):
    pass


def compile_condition(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise RuleSyntaxError(f"invalid condition {expr!r}: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleSyntaxError(
                f"disallowed syntax {type(node).__name__} in condition {expr!r}"
            )
    return tree


def _eval(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTANT_NAMES:
            return _CONSTANT_NAMES[node.id]
        return ctx.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _eval(node.value, ctx)
        if base is None:
            return None
        if isinstance(base, dict):
            return base.get(node.attr)
        return getattr(base, node.attr, None)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, ctx) for e in node.elts]
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand if operand is not None else None
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_eval(v, ctx)) for v in node.values)
        return any(bool(_eval(v, ctx)) for v in node.values)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            right = _eval(comparator, ctx)
            try:
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Is):
                    ok = left is right
                elif isinstance(op, ast.IsNot):
                    ok = left is not right
                elif isinstance(op, ast.In):
                    ok = right is not None and left in right
                elif isinstance(op, ast.NotIn):
                    ok = right is None or left not in right
                elif left is None or right is None:
                    ok = False  # ordered comparison with a gap is never true
                elif isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                else:
                    ok = left >= right
            except TypeError:
                ok = False
            if not ok:
                return False
            left = right
        return True
    raise RuleSyntaxError(f"unsupported node {type(node).__name__}")


def evaluate(expr: str | ast.Expression, ctx: dict[str, Any]) -> bool:
    tree = compile_condition(expr) if isinstance(expr, str) else expr
    return bool(_eval(tree, ctx))


@dataclass
class Rule:
    id: str
    when: ast.Expression
    when_text: str
    points: int
    label: str = ""


@dataclass
class DisqualifyRule:
    id: str
    when: ast.Expression
    reason: str
    tracks: list[str]


@dataclass
class RuleSet:
    version_label: str
    checksum: str
    yaml_text: str
    thresholds: dict[str, int]
    tracks: dict[str, list[Rule]]
    disqualify: list[DisqualifyRule] = field(default_factory=list)

    def band(self, total: int) -> str:
        if total >= self.thresholds.get("hot", 70):
            return "hot"
        if total >= self.thresholds.get("warm", 40):
            return "warm"
        return "cold"


def load_rules(path: Path | None = None) -> RuleSet:
    settings = get_settings()
    path = path or settings.scoring_rules_path or DEFAULT_RULES_PATH
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    tracks: dict[str, list[Rule]] = {}
    for track, body in (data.get("tracks") or {}).items():
        rules: list[Rule] = []
        for raw in body.get("rules") or []:
            rules.append(
                Rule(
                    id=str(raw["id"]),
                    when=compile_condition(str(raw["when"])),
                    when_text=str(raw["when"]),
                    points=int(raw.get("points", 0)),
                    label=str(raw.get("label", raw["id"])),
                )
            )
        tracks[track] = rules
    disqualify = [
        DisqualifyRule(
            id=str(raw["id"]),
            when=compile_condition(str(raw["when"])),
            reason=str(raw.get("reason", raw["id"])),
            tracks=list(raw.get("tracks") or list(tracks.keys())),
        )
        for raw in data.get("disqualify") or []
    ]
    return RuleSet(
        version_label=str(data.get("version", "1")),
        checksum=checksum,
        yaml_text=text,
        thresholds={k: int(v) for k, v in (data.get("thresholds") or {}).items()},
        tracks=tracks,
        disqualify=disqualify,
    )


def ensure_rule_version(session: Session, rules: RuleSet | None = None) -> ScoringRuleVersion:
    rules = rules or load_rules()
    row = session.scalar(select(ScoringRuleVersion).where(ScoringRuleVersion.checksum == rules.checksum))
    if row is None:
        row = ScoringRuleVersion(
            version_label=rules.version_label, checksum=rules.checksum, yaml_text=rules.yaml_text
        )
        session.add(row)
        session.flush()
    return row


def build_context(
    org: Organization,
    website: Website | None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = now or utcnow()
    site_ns = Namespace()
    perf_ns = Namespace(mobile=Namespace(), desktop=Namespace())
    if website is not None:
        tls_days_left = None
        if website.tls_expires_at:
            tls_days_left = (website.tls_expires_at - now).days
        site_ns = Namespace(
            cms=website.cms,
            cms_version=website.cms_version,
            is_woocommerce=website.is_woocommerce,
            theme=website.theme,
            page_builder=website.page_builder,
            caching_layer=website.caching_layer,
            cdn=website.cdn,
            managed_host=website.managed_host,
            php_version=website.php_version,
            php_eol=php_is_eol(website.php_version, settings.php_min_supported),
            host_class=website.host_class,
            host_provider=website.host_provider,
            mail_provider=website.mail_provider,
            error_state=website.error_state or "none",
            error_confirmed=website.error_confirmed_at is not None,
            tls_valid=website.tls_valid,
            tls_days_left=tls_days_left,
            http_status=website.http_status,
            wp_releases_behind=wp_releases_behind(
                website.cms_version, settings.latest_wordpress_version
            )
            if website.cms == "wordpress"
            else None,
        )
        for strategy in ("mobile", "desktop"):
            lab: PerformanceSnapshot | None = website.latest_performance(strategy)
            fld: PerformanceSnapshot | None = website.latest_field_performance(strategy)
            if lab is not None or fld is not None:
                setattr(
                    perf_ns,
                    strategy,
                    Namespace(
                        lh_performance=lab.lh_performance if lab else None,
                        lcp_ms=lab.lcp_ms if lab else None,
                        tbt_ms=lab.tbt_ms if lab else None,
                        cls=lab.cls if lab else None,
                        ttfb_ms=lab.ttfb_ms if lab else None,
                        field_lcp_p75=fld.field_lcp_p75 if fld else None,
                        field_inp_p75=fld.field_inp_p75 if fld else None,
                        field_cls_p75=fld.field_cls_p75 if fld else None,
                        field_available=fld is not None,
                        field_source=fld.source if fld else None,
                    ),
                )
    org_ns = Namespace(
        track=org.track,
        country=org.country,
        has_website=bool(org.website_url),
        phones_count=len(org.phones or []),
        emails_count=len(org.emails or []),
        status=org.status,
    )
    return {"website": site_ns, "perf": perf_ns, "org": org_ns, "signals": list(org.signals or [])}


@dataclass
class ScoreResult:
    total: int
    band: str
    track_used: str
    breakdown: list[dict[str, Any]]
    disqualified_reason: str | None
    version: str


def score_context(rules: RuleSet, ctx: dict[str, Any], track: str) -> ScoreResult:
    track_used = track if track in rules.tracks else "smb"
    for dq in rules.disqualify:
        if track_used in dq.tracks and evaluate(dq.when, ctx):
            return ScoreResult(0, "cold", track_used, [], dq.reason, rules.checksum)
    total = 0
    breakdown: list[dict[str, Any]] = []
    for rule in rules.tracks.get(track_used, []):
        matched = evaluate(rule.when, ctx)
        if matched:
            total += rule.points
            breakdown.append({"id": rule.id, "label": rule.label, "points": rule.points})
    total = max(0, min(100, total))
    return ScoreResult(total, rules.band(total), track_used, breakdown, None, rules.checksum)


def score_organization(
    session: Session,
    org: Organization,
    rules: RuleSet | None = None,
    settings: Settings | None = None,
) -> ScoreResult:
    rules = rules or load_rules()
    ensure_rule_version(session, rules)
    website = org.primary_website
    ctx = build_context(org, website, settings)
    result = score_context(rules, ctx, org.track)
    org.score_total = result.total
    org.score_band = result.band
    org.scoring_version = result.version
    org.score_breakdown = {
        "track_used": result.track_used,
        "rules": result.breakdown,
        "disqualified_reason": result.disqualified_reason,
        "scored_at": utcnow().isoformat(timespec="seconds"),
    }
    if result.disqualified_reason and org.status in {"new", "reviewing"}:
        org.status = "disqualified"
        org.disqualify_reason = result.disqualified_reason
    elif not result.disqualified_reason and org.status == "disqualified" and org.disqualify_reason:
        # Re-qualify automatically only when the previous disqualification came from scoring
        if org.disqualify_reason in {dq.reason for dq in rules.disqualify}:
            org.status = "new"
            org.disqualify_reason = None
    session.flush()
    return result
