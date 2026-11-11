import io
import json
import html
import uuid
import logging
import zipfile
from urllib.parse import quote
from pathlib import Path
from typing import Dict, Any, Literal, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from syda.schemas import validate_schema
from syda.scenarios import ScenarioEngine

from .auth import require_authenticated
from ..database import get_db
from ..models.entities import JobRecord, ScenarioRecord
from ..provider_settings import resolve_provider
from ..job_queue import DATA_DIR, publish_job, output_directory, cancel_job
from ..services.generation import (
    _synthesize_relational_tables, _LocalTableGenerator, _scenario_schemas,
    _referential_integrity, _constraint_compliance, _temporal_consistency,
    _evaluation_metrics, _privacy_leakage_metric,
)
from ..scenario_definition import (
    scenario_definition as _scenario_definition,
    scenario_validation_message,
)
from ..models.user import User
from ..models.scenario import (
    GenerateRequest,
    JobStatusResponse,
    JobStats,
    ScenarioConfiguration,
    CostEstimate,
    ViolationRecord,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Generation"])

DATA_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/generate", response_model=JobStatusResponse)
async def start_generation(
    req: GenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """
    Accepts scenario configuration with validated syda schemas,
    persists the initial job, and dispatches generation.
    """
    job_id = str(uuid.uuid4())
    if req.scenario_id and not db.query(ScenarioRecord).filter(ScenarioRecord.id == req.scenario_id, ScenarioRecord.user_id == user.id).first():
        raise HTTPException(status_code=404, detail="Scenario not found")

    try:
        schemas = _scenario_schemas(req.scenario)
        _scenario_definition(req.scenario, schemas)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=scenario_validation_message(error)) from error
    try:
        selected = resolve_provider(db, user.id)
        snapshot = {key: selected.get(key) for key in ("id", "model", "base_url")}
        snapshot["reference_data"] = req.reference_data
        new_job = JobRecord(
            id=job_id,
            user_id=user.id,
            status="queued",
            progress=0,
            current_stage="Waiting for a generation worker",
            scenario=req.scenario.model_dump(by_alias=True),
            scenario_id=req.scenario_id,
            provider_snapshot=snapshot,
        )
        db.add(new_job)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        logger.exception("Could not persist generation job %s", job_id)
        raise HTTPException(
            status_code=503,
            detail="The generation database is unavailable. Restart the backend or check DATABASE_URL.",
        ) from error

    # Commit before publishing. The dispatcher recovers unpublished jobs.
    publish_job(job_id, db=db)

    return JobStatusResponse(
        job_id=job_id,
        status="queued",
        progress=0,
        current_stage="Waiting for a generation worker",
    )


@router.post("/estimate", response_model=CostEstimate)
async def estimate_generation(req: GenerateRequest, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    """Estimate model usage before generation using schema size and generation mode."""
    schemas = _scenario_schemas(req.scenario)
    try:
        definition = _scenario_definition(req.scenario, schemas)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=scenario_validation_message(error)) from error
    summary = ScenarioEngine.summarize(definition, req.scenario.record_count)
    field_counts = {
        table: len(fields)
        for table, fields in summary.enrichment_fields.items()
        if fields
    }
    rows_per_table = max(
        (summary.table_row_counts[table] for table in field_counts),
        default=0,
    )
    schema_tokens = max(100, len(json.dumps(schemas)) // 4)
    direct_mode = rows_per_table <= 500
    estimated_input_tokens = schema_tokens * len(field_counts)
    estimated_output_tokens = (
        sum(
            summary.table_row_counts[table] * max(4, field_count) * 6
            for table, field_count in field_counts.items()
        )
        if direct_mode
        else sum(field_counts.values()) * 180
    )

    selection = resolve_provider(db, user.id)
    if selection["id"] == "offline":
        return CostEstimate(
            provider="Offline",
            model="Local schema synthesizer",
            estimatedInputTokens=0,
            estimatedOutputTokens=0,
            estimatedCostUsd=0.0,
            note="Local generation selected; no model API calls are estimated.",
        )
    provider, model = selection["name"], selection["model"]

    try:
        from syda.run_report import _estimate_cost

        cost = round(_estimate_cost(model, estimated_input_tokens, estimated_output_tokens), 6)
    except Exception:
        cost = 0.0

    return CostEstimate(
        provider=provider,
        model=model,
        estimatedInputTokens=estimated_input_tokens,
        estimatedOutputTokens=estimated_output_tokens,
        estimatedCostUsd=cost if cost > 0 else None,
        note="Estimate only; actual usage depends on model output and retries.",
    )


@router.get("/status/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    """Poll status and measured metrics for an active or completed generation job."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    stats = JobStats.model_validate(job.stats) if job.stats else None

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        current_stage=job.current_stage,
        stats=stats,
        download_url=job.download_url,
    )


@router.get("/jobs")
async def list_jobs(db: Session = Depends(get_db), user: User = Depends(require_authenticated), limit: int = 50):
    """Return the persisted history of synthetic generation jobs."""
    jobs = db.query(JobRecord).filter(JobRecord.user_id == user.id).order_by(JobRecord.created_at.desc()).limit(limit).all()
    return [_job_payload(job) for job in jobs]


def _job_payload(job: JobRecord) -> Dict[str, Any]:
    payload = job.to_dict()
    job_dir = output_directory(job, DATA_DIR)
    payload["filesAvailable"] = job.status == "complete" and job_dir.exists() and any(job_dir.glob("*.csv"))
    return payload


def _documents(job_dir: Path) -> list[Path]:
    """Linked documents rendered under <job_dir>/<table>/, in document order."""
    files = [path for path in job_dir.glob("*/document_*") if path.suffix in (".pdf", ".html")]
    return sorted(files, key=lambda path: (path.parent.name, len(path.name), path.name))


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    """Return one persisted run with its scenario snapshot and results."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_payload(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_generation_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Cancel an active or queued generation run."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("complete", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel job with status '{job.status}'")
    cancelled = cancel_job(job_id, user_id=user.id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Could not cancel job")
    return {"jobId": job_id, "status": "cancelled", "message": "Generation cancelled successfully"}


@router.get("/preview/{job_id}")
async def preview_generated_dataset(
    job_id: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    table: Optional[str] = Query(None, description="Return only this table"),
    column: Optional[str] = Query(None, description="With value: keep rows where column equals value"),
    value: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Return one page of every generated table, or of one table filtered by column = value."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete":
        raise HTTPException(status_code=409, detail="Dataset preview is not ready")

    job_dir = output_directory(job, DATA_DIR)
    csv_files = sorted(job_dir.glob("*.csv")) if job_dir.exists() else []
    if not csv_files:
        raise HTTPException(status_code=404, detail="Generated dataset files are unavailable")

    stats = JobStats.model_validate(job.stats) if job.stats else None
    row_counts_by_name = {
        name.lower(): count for name, count in (stats.table_row_counts.items() if stats else [])
    }
    if table is not None:
        csv_files = [path for path in csv_files if path.stem.lower() == table.lower()]
        if not csv_files:
            raise HTTPException(status_code=404, detail=f"Table '{table}' was not generated")
    filtering = table is not None and column is not None and value is not None
    tables = {}
    for path in csv_files:
        row_count = row_counts_by_name.get(path.stem.lower(), 0)
        if filtering:
            # ponytail: reads the whole table to filter; index the CSVs if tables outgrow memory.
            full = pd.read_csv(path, dtype={column: str})
            if column not in full.columns:
                raise HTTPException(status_code=400, detail=f"Unknown column '{column}'")
            matches = full[full[column] == value]
            row_count = len(matches)
            raw_frame = matches.iloc[offset:offset + limit]
        else:
            raw_frame = pd.read_csv(path, skiprows=range(1, offset + 1), nrows=limit)
        frame = raw_frame.astype(object).where(pd.notna(raw_frame), None)
        tables[path.stem] = {
            "columns": frame.columns.tolist(),
            "rows": frame.to_dict(orient="records"),
            "rowCount": row_count,
        }
    documents = [
        {"table": path.parent.name, "name": path.name,
         "url": f"/api/documents/{job_id}/{path.parent.name}/{path.name}"}
        for path in _documents(job_dir)
    ]
    return {"jobId": job_id, "tables": tables, "documents": documents}


@router.get("/documents/{job_id}/{table}/{name}")
async def get_document(
    job_id: str,
    table: str,
    name: str,
    download: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Serve one linked document inline for preview, or as an attachment."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job or job.status != "complete":
        raise HTTPException(status_code=404, detail="Document not found")
    path = output_directory(job, DATA_DIR) / table / name
    if path not in _documents(output_directory(job, DATA_DIR)):
        raise HTTPException(status_code=404, detail="Document not found")
    headers = {"X-Content-Type-Options": "nosniff"}
    if path.suffix == ".html":
        # Rendered from user/LLM content: never run it with the app's origin.
        headers["Content-Security-Policy"] = "sandbox"
    return FileResponse(
        path,
        media_type="application/pdf" if path.suffix == ".pdf" else "text/html",
        filename=f"{table}_{name}",
        content_disposition_type="attachment" if download else "inline",
        headers=headers,
    )


def _completed_evaluation_stats(job_id: str, db: Session, user: User) -> JobStats:
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in {"complete", "failed"} or not job.stats:
        raise HTTPException(status_code=409, detail="Evaluation report is not ready")
    return JobStats.model_validate(job.stats)


def _evaluation_report_payload(job_id: str, stats: JobStats) -> Dict[str, Any]:
    payload = {
        "schemaVersion": "1.0",
        "jobId": job_id,
        "result": stats.evaluation_result,
        "recordsGenerated": stats.records_generated,
        "pathCounts": stats.path_counts,
        "metrics": [metric.model_dump(by_alias=True, exclude_none=True) for metric in stats.evaluation_metrics],
        "violations": [violation.model_dump(by_alias=True) for violation in stats.violations],
    }
    if stats.judge_audit is not None:
        payload["judgeAudit"] = stats.judge_audit.model_dump(by_alias=True)
    return payload


def _format_report_number(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.6g}"


def _evaluation_report_html(
    job_id: str,
    stats: JobStats,
    theme: Literal["light", "dark"] = "light",
) -> str:
    scorecards = "".join(
        f'<article class="card"><h3>{html.escape(metric.label)}</h3>'
        f'<span class="badge {metric.status}">{html.escape(metric.status.replace("_", " ").title())}</span>'
        f'<strong>{html.escape(metric.value)}</strong><p>{html.escape(metric.detail)}</p>'
        f'<small>{metric.violations:,} violations</small></article>'
        for metric in stats.evaluation_metrics
    )
    status_counts = {
        status: sum(metric.status == status for metric in stats.evaluation_metrics)
        for status in ("pass", "fail", "not_evaluated")
    }
    chart = "".join(
        f'<div class="chart"><span>{html.escape(status.replace("_", " ").title())}</span>'
        f'<progress max="{max(1, len(stats.evaluation_metrics))}" value="{count}"></progress><strong>{count}</strong></div>'
        for status, count in status_counts.items()
    )
    statistical = next((metric for metric in stats.evaluation_metrics if metric.key == "statistical_similarity"), None)
    field_rows = ""
    if statistical and statistical.field_results:
        field_rows = "".join(
            "<tr>"
            f"<td>{html.escape(field.table)}</td><td>{html.escape(field.column)}</td><td>{html.escape(field.kind)}</td>"
            f"<td>{field.score:.6g}</td><td>{_format_report_number(field.ks_statistic)}</td>"
            f"<td>{_format_report_number(field.wasserstein_distance)}</td>"
            f"<td>{_format_report_number(field.chi_square_statistic)}</td></tr>"
            for field in statistical.field_results
        )
    if statistical is not None and statistical.field_results is None:
        statistical_section = '<p>Per-field statistical details were not recorded for this run.</p>'
    elif field_rows:
        statistical_section = (
            '<table><thead><tr><th>Table</th><th>Field</th><th>Type</th><th>Similarity score</th>'
            '<th>KS statistic</th><th>Wasserstein distance</th><th>Chi-squared statistic</th></tr></thead>'
            f'<tbody>{field_rows}</tbody></table>'
        )
    else:
        statistical_section = '<p>No comparable numeric or categorical fields were evaluated.</p>'
    violation_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.table)}</td><td>{html.escape(str(item.row))}</td>"
        f"<td>{html.escape(item.rule)}</td><td>{html.escape(item.error)}</td></tr>"
        for item in stats.violations
    )
    violation_section = (
        '<table><thead><tr><th>Table</th><th>Row</th><th>Rule</th><th>Error</th></tr></thead>'
        f'<tbody>{violation_rows}</tbody></table>'
        if violation_rows else '<p>No causal or semantic rule violations detected.</p>'
    )
    if stats.judge_audit is not None:
        audit = stats.judge_audit
        anomaly_items = "".join(
            f"<li>{html.escape(anomaly)}</li>" for anomaly in audit.flagged_anomalies
        )
        anomaly_section = (
            f"<ol>{anomaly_items}</ol>"
            if anomaly_items else "<p>No anomalies were flagged in the sampled workflows.</p>"
        )
        judge_section = (
            '<section><h2>AI Judge Audit</h2>'
            f'<span class="badge {audit.verdict}">{html.escape(audit.verdict.title())}</span>'
            f'<p class="judge-score">{audit.score:.2f} / 1.00</p>'
            f'<p class="judge-reasoning">{html.escape(audit.reasoning)}</p>'
            f'<h3>Flagged anomalies ({len(audit.flagged_anomalies)})</h3>{anomaly_section}</section>'
        )
    else:
        judge_section = (
            "<section><h2>AI Judge Audit</h2>"
            "<p>An AI Judge audit was not recorded for this run. Existing deterministic "
            "evaluation results remain available below.</p></section>"
        )
    return f'''<!doctype html><html lang="en" data-theme="{theme}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Syda evaluation {html.escape(job_id)}</title>
<style>
:root{{color-scheme:light;--page:#f8fafc;--surface:#fff;--surface-muted:#f1f5f9;--text:#172033;--muted:#526078;--border:#dbe3ee;--pass:#15803d;--pass-bg:rgba(34,197,94,.12);--pass-border:rgba(21,128,61,.35);--fail:#b91c1c;--fail-bg:rgba(239,68,68,.12);--fail-border:rgba(185,28,28,.35);--warning:#b45309;--warning-bg:rgba(245,158,11,.14);--warning-border:rgba(180,83,9,.35);--neutral:#526078;--neutral-bg:rgba(100,116,139,.12);--neutral-border:rgba(82,96,120,.3)}}
:root[data-theme="dark"]{{color-scheme:dark;--page:#0b1117;--surface:#121a22;--surface-muted:#1b2530;--text:#e5edf5;--muted:#a7b4c4;--border:#2d3a48;--pass:#4ade80;--pass-bg:rgba(74,222,128,.13);--pass-border:rgba(74,222,128,.35);--fail:#f87171;--fail-bg:rgba(248,113,113,.13);--fail-border:rgba(248,113,113,.35);--warning:#fbbf24;--warning-bg:rgba(251,191,36,.14);--warning-border:rgba(251,191,36,.35);--neutral:#a7b4c4;--neutral-bg:rgba(167,180,196,.12);--neutral-border:rgba(167,180,196,.3)}}
*{{box-sizing:border-box}}body{{font:14px system-ui;margin:0;background:var(--page);color:var(--text)}}main{{max-width:1100px;margin:auto;padding:32px}}section,header,.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px}}section{{margin-top:18px}}p,small{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}}.card h3{{margin:0 0 8px}}.card strong{{display:block;font-size:1.4rem;margin:12px 0}}.badge{{float:right;display:inline-flex;width:max-content;align-items:center;justify-content:center;padding:3px 8px;border:1px solid var(--neutral-border);border-radius:999px;background:var(--neutral-bg);color:var(--neutral);line-height:1.2;white-space:nowrap}}.badge.pass{{border-color:var(--pass-border);background:var(--pass-bg);color:var(--pass)}}.badge.fail{{border-color:var(--fail-border);background:var(--fail-bg);color:var(--fail)}}.badge.warning{{border-color:var(--warning-border);background:var(--warning-bg);color:var(--warning)}}.badge.not_evaluated{{border-color:var(--neutral-border);background:var(--neutral-bg);color:var(--neutral)}}.judge-score,.judge-reasoning{{color:var(--text)}}.judge-score{{font-size:1.8rem;font-weight:700;margin:12px 0}}.chart{{display:grid;grid-template-columns:140px 1fr 40px;gap:12px;margin:10px 0}}progress{{width:100%;accent-color:var(--pass)}}table{{border-collapse:collapse;width:100%;overflow:auto}}th,td{{border:1px solid var(--border);padding:8px;text-align:left}}th{{background:var(--surface-muted)}}@media(max-width:640px){{main{{padding:16px}}.chart{{grid-template-columns:1fr 36px}}.chart progress{{grid-column:1/-1}}}}
</style></head>
<body><main><header><h1>Syda evaluation report</h1><p>Job {html.escape(job_id)} · Result: {html.escape(stats.evaluation_result.upper())}</p></header>
{judge_section}
<section><h2>Metric scorecards</h2><div class="cards">{scorecards}</div></section>
<section><h2>Quality status chart</h2>{chart}</section>
<section><h2>Statistical similarity details</h2>{statistical_section}</section>
<section><h2>Causal &amp; Semantic Rule Violations ({len(stats.violations)})</h2>{violation_section}</section>
</main></body></html>'''


@router.get("/jobs/{job_id}/evaluation")
async def get_evaluation_report_json(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    stats = _completed_evaluation_stats(job_id, db, user)
    return JSONResponse(_evaluation_report_payload(job_id, stats))


@router.get("/jobs/{job_id}/evaluation-report")
async def view_evaluation_report_html(
    job_id: str,
    theme: Literal["light", "dark"] = Query("light"),
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    stats = _completed_evaluation_stats(job_id, db, user)
    return HTMLResponse(
        _evaluation_report_html(job_id, stats, theme),
        headers={"Content-Disposition": f'inline; filename="evaluation_{job_id}.html"'},
    )


@router.get("/evaluation/{job_id}/report")
async def download_evaluation_report(
    job_id: str,
    format: str = Query("json", pattern="^(json|html)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Download the structured evaluation report as JSON or HTML."""
    stats = _completed_evaluation_stats(job_id, db, user)
    report = _evaluation_report_payload(job_id, stats)
    if format == "json":
        return JSONResponse(
            report,
            headers={"Content-Disposition": f'attachment; filename="evaluation_{job_id}.json"'},
        )
    return HTMLResponse(
        _evaluation_report_html(job_id, stats),
        headers={"Content-Disposition": f'attachment; filename="evaluation_{job_id}.html"'},
    )


@router.get("/download/{job_id}")
async def download_generated_dataset(
    job_id: str,
    table: Optional[str] = Query(None, description="Optional specific table name to download"),
    format: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """
    Streams generated dataset(s). If multiple relational tables were generated,
    returns a zip archive containing all tables, or single table CSV if specified.
    """
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete":
        raise HTTPException(status_code=409, detail="Dataset is not ready for download")

    job_dir = output_directory(job, DATA_DIR)
    scenario = ScenarioConfiguration.model_validate(job.scenario)
    title_slug = scenario.title.lower().replace(" ", "_")

    csv_files = list(job_dir.glob("*.csv")) if job_dir.exists() else []
    documents = _documents(job_dir)

    # If specific table requested or only 1 table generated
    if table:
        if Path(table).name != table:
            raise HTTPException(status_code=400, detail="Invalid table name")
        target_file = next(
            (path for path in csv_files if path.stem.lower() == table.lower()),
            None,
        )
        if target_file is not None and format == "csv":
            return FileResponse(
                target_file,
                media_type="text/csv",
                filename=f"{table.lower()}_{job_id}.csv",
            )
        if target_file is not None:
            raw_frame = pd.read_csv(target_file)
            records = raw_frame.astype(object).where(pd.notna(raw_frame), None).to_dict(orient="records")
            return JSONResponse(
                records,
                headers={"Content-Disposition": f'attachment; filename="{table.lower()}_{job_id}.json"'},
            )
        raise HTTPException(status_code=404, detail=f"Table '{table}' was not generated")

    if len(csv_files) == 1 and format == "csv" and not documents:
        return FileResponse(
            csv_files[0],
            media_type="text/csv",
            filename=f"{csv_files[0].stem}_{job_id}.csv",
        )

    if csv_files and format == "json":
        if len(csv_files) == 1 and not documents:
            raw_frame = pd.read_csv(csv_files[0])
            records = raw_frame.astype(object).where(pd.notna(raw_frame), None).to_dict(orient="records")
            return JSONResponse(
                records,
                headers={"Content-Disposition": f'attachment; filename="{csv_files[0].stem}_{job_id}.json"'},
            )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in csv_files:
                raw_frame = pd.read_csv(path)
                records = raw_frame.astype(object).where(pd.notna(raw_frame), None).to_dict(orient="records")
                zf.writestr(f"{path.stem}.json", json.dumps(records, default=str))
            for path in documents:
                zf.write(path, arcname=f"{path.parent.name}/{path.name}")
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(f'{title_slug}_{job_id}_json.zip', safe='')}"},
        )

    if len(csv_files) > 1 or documents:
        # Stream zip archive with all tables
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in csv_files:
                zf.write(f, arcname=f.name)
            for path in documents:
                zf.write(path, arcname=f"{path.parent.name}/{path.name}")
        buf.seek(0)

        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(f'{title_slug}_{job_id}.zip', safe='')}"},
        )

    raise HTTPException(status_code=404, detail="Generated dataset files are unavailable")


@router.post("/validate")
async def validate_scenario(req: GenerateRequest):
    """
    Validates scenario constraints and field definitions using syda.schemas.validate_schema.
    """
    scenario = req.scenario
    warnings = []
    schema_errors = []
    evaluated_tables = 0

    if scenario.record_count < 10:
        warnings.append("Scenario instance count is unusually low for statistical validation.")
    if len(scenario.workflow) < 2:
        warnings.append("Workflow should contain at least two sequential steps for causal ordering.")

    # Validate each table schema using syda's native schema validator
    if scenario.schemas:
        for table_name, schema_dict in scenario.schemas.items():
            evaluated_tables += 1
            try:
                validate_schema(schema_dict)
            except Exception as e:
                schema_errors.append(f"Table '{table_name}': {str(e)}")

        try:
            _scenario_definition(scenario, scenario.schemas)
        except Exception as error:
            schema_errors.append(f"Scenario workflow: {scenario_validation_message(error)}")

    return {
        "valid": len(schema_errors) == 0,
        "executable_checks_count": len(scenario.checks),
        "guidance_rules_count": len(scenario.rules),
        "workflow_steps_count": len(scenario.workflow),
        "evaluated_tables_count": evaluated_tables,
        "warnings": warnings,
        "schema_errors": schema_errors,
    }
