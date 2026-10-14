from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...connectors.csv_import import FIELDS, PRESETS, CsvImportConnector, read_csv
from ...connectors.linkedin_connections import LinkedInConnectionsConnector, parse_connections_csv
from ...jobs.tasks import enqueue_enrichment
from ...models import LAWFUL_BASES, ImportBatch, User
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role

router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("")
def list_imports(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(50)).all()
    return render(request, "imports/list.html", {"batches": batches, "presets": PRESETS, "fields": FIELDS, "lawful_bases": LAWFUL_BASES})


@router.post("", dependencies=[Depends(csrf_protect)])
async def upload(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form("generic"),
    track: str = Form("unknown"),
    lawful_basis: str = Form(""),
    notice_status: str = Form("pending"),
    mapping_json: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "file too large")
    rows = read_csv(content)
    if not rows:
        flash(request, "The file has no data rows.", "error")
        return redirect("/imports")
    mapping = dict(PRESETS.get(kind, PRESETS["generic"]))
    if mapping_json.strip():
        try:
            mapping.update({k: v for k, v in json.loads(mapping_json).items() if k in FIELDS and v})
        except json.JSONDecodeError:
            flash(request, "Column mapping must be valid JSON.", "error")
            return redirect("/imports")
    basis = lawful_basis if lawful_basis in LAWFUL_BASES else None
    batch = ImportBatch(kind=kind, filename=file.filename or "upload.csv", track=track if track in {"smb", "agency", "unknown"} else "unknown",
                        column_mapping=mapping, lawful_basis=basis, imported_by=user.id)
    db.add(batch)
    db.flush()
    importer = CsvImportConnector()
    to_probe: list[tuple[int, str]] = []
    summary = importer.import_rows(
        db, batch, rows, mapping, lawful_basis=basis, notice_status=notice_status, actor=user,
        on_website=lambda org_id, url: to_probe.append((org_id, url)),
    )
    for org_id, url in to_probe:
        enqueue_enrichment(db, org_id, url, reason=f"import{batch.id}")
    audit.log(db, "import.create", actor=user, entity_type="import_batch", entity_id=batch.id, kind=kind,
              rows=summary.rows, ok=summary.ok, errors=len(summary.errors), persons=summary.persons_created)
    flash(request, f"Imported {summary.ok}/{summary.rows} rows: {summary.organizations_created} new organisations, "
                   f"{summary.organizations_matched} matched, {summary.persons_created} contacts. "
                   f"{len(to_probe)} websites queued for enrichment.", "success")
    return redirect(f"/imports/{batch.id}")


@router.post("/linkedin", dependencies=[Depends(csrf_protect)])
async def upload_linkedin_connections(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("owner")),
):
    """Owner seat only: the owner's own LinkedIn Connections.csv (plan §4.1 route 2)."""
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "file too large")
    try:
        rows = parse_connections_csv(content)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/imports")
    batch = ImportBatch(kind="linkedin_connections", filename=file.filename or "Connections.csv", track="unknown",
                        column_mapping={}, lawful_basis="legitimate_interest", imported_by=user.id)
    db.add(batch)
    db.flush()
    summary = LinkedInConnectionsConnector().import_rows(db, batch, rows, actor=user)
    flash(request, f"Connections import: {summary.rows} rows, {summary.created} new, {summary.updated} updated, "
                   f"{summary.linked} linked to organisations, {summary.unlinked} waiting for a match.", "success")
    return redirect("/people")


@router.get("/{batch_id}")
def batch_detail(batch_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404)
    return render(request, "imports/detail.html", {"batch": batch})
