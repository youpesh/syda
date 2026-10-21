import io
import json
import html
import uuid
import logging
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional

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
from ..job_queue import DATA_DIR, publish_job, output_directory
from ..services.generation import (
    _synthesize_relational_tables, _LocalTableGenerator, _scenario_schemas,
    _referential_integrity, _constraint_compliance, _temporal_consistency,
    _evaluation_metrics,
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


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    """Return one persisted run with its scenario snapshot and results."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_payload(job)


@router.get("/preview/{job_id}")
async def preview_generated_dataset(
    job_id: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Return one page of every generated table for browsing in the UI."""
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
    tables = {}
    for path in csv_files:
        raw_frame = pd.read_csv(path, skiprows=range(1, offset + 1), nrows=limit)
        frame = raw_frame.astype(object).where(pd.notna(raw_frame), None)
        tables[path.stem] = {
            "columns": frame.columns.tolist(),
            "rows": frame.to_dict(orient="records"),
            "rowCount": row_counts_by_name.get(path.stem.lower(), 0),
        }
    return {"jobId": job_id, "tables": tables}


@router.get("/evaluation/{job_id}/report")
async def download_evaluation_report(
    job_id: str,
    format: str = Query("json", pattern="^(json|html)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Download the structured evaluation report as JSON or HTML."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete" or not job.stats:
        raise HTTPException(status_code=409, detail="Evaluation report is not ready")
    stats = JobStats.model_validate(job.stats)
    report = {
        "jobId": job_id,
        "result": stats.evaluation_result,
        "recordsGenerated": stats.records_generated,
        "pathCounts": stats.path_counts,
        "metrics": [metric.model_dump(by_alias=True) for metric in stats.evaluation_metrics],
    }
    if format == "json":
        return JSONResponse(
            report,
            headers={"Content-Disposition": f'attachment; filename="evaluation_{job_id}.json"'},
        )

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(metric.label)}</td>"
        f"<td>{html.escape(metric.status.replace('_', ' ').title())}</td>"
        f"<td>{html.escape(metric.value)}</td>"
        f"<td>{metric.violations}</td>"
        f"<td>{html.escape(metric.detail)}</td>"
        "</tr>"
        for metric in stats.evaluation_metrics
    )
    document = f"""<!doctype html><html><head><meta charset="utf-8"><title>Syda evaluation {html.escape(job_id)}</title>
<style>body{{font:14px system-ui;margin:40px;color:#18181b}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d4d4d8;padding:8px;text-align:left}}th{{background:#f4f4f5}}</style>
</head><body><h1>Syda evaluation report</h1><p>Job {html.escape(job_id)} · Result: {html.escape(stats.evaluation_result.upper())}</p>
<table><thead><tr><th>Metric</th><th>Status</th><th>Value</th><th>Violations</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table></body></html>"""
    return HTMLResponse(
        document,
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

    if len(csv_files) == 1 and format == "csv":
        return FileResponse(
            csv_files[0],
            media_type="text/csv",
            filename=f"{csv_files[0].stem}_{job_id}.csv",
        )

    if csv_files and format == "json":
        if len(csv_files) == 1:
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
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={title_slug}_{job_id}_json.zip"},
        )

    if len(csv_files) > 1:
        # Stream zip archive with all tables
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in csv_files:
                zf.write(f, arcname=f.name)
        buf.seek(0)

        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={title_slug}_{job_id}.zip"},
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
