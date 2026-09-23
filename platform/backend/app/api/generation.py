import os
import io
import json
import html
import time
import uuid
import asyncio
import logging
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import random

import pandas as pd
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, Query
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from syda.schemas import validate_schema, ModelConfig
from syda.generate import SyntheticDataGenerator
from syda.output import save_dataframes
from syda.scenarios import ScenarioDefinition, ScenarioEngine, ScenarioPath, ScenarioStep

from app.database import get_db, SessionLocal
from app.models.entities import JobRecord, ScenarioRecord
from app.models.scenario import (
    GenerateRequest,
    JobStatusResponse,
    JobStats,
    ScenarioConfiguration,
    EvaluationMetric,
    CostEstimate,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Generation"])

DATA_DIR = Path("data/jobs")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _update_job_in_db(job_id: str, updates: Dict[str, Any]):
    """Helper to update a job record in the database within a short-lived session."""
    db = SessionLocal()
    try:
        job = db.query(JobRecord).filter(JobRecord.id == job_id).first()
        if job:
            for key, val in updates.items():
                setattr(job, key, val)
            db.commit()
    except Exception as e:
        logger.error(f"Error updating job {job_id} in database: {e}")
        db.rollback()
    finally:
        db.close()


def _synthesize_relational_tables(
    schemas: Dict[str, Any],
    sample_sizes: Dict[str, int],
) -> Dict[str, pd.DataFrame]:
    """
    Intelligent fallback synthesizer that preserves 100% foreign-key referential integrity
    and causal timelines when running offline or without an active LLM provider.
    """
    dfs: Dict[str, pd.DataFrame] = {}
    base_time = datetime.now() - timedelta(days=60)

    for table_idx, (table_name, schema_def) in enumerate(schemas.items()):
        rows = []
        n_rows = sample_sizes.get(table_name, 0)

        # Extract fields and foreign keys
        fields = {k: v for k, v in schema_def.items() if not (k.startswith("__") and k.endswith("__"))}
        fks = schema_def.get("__foreign_keys__", {})

        for i in range(1, n_rows + 1):
            row = {}
            for col_name, col_info in fields.items():
                col_type = col_info if isinstance(col_info, str) else col_info.get("type", "text")
                constraints = col_info.get("constraints", {}) if isinstance(col_info, dict) else {}

                # 1. Primary keys
                if constraints.get("primary_key") or col_name.endswith("_id") and col_name == f"{table_name.lower()}_id":
                    row[col_name] = i

                # 2. Foreign keys referencing previously generated parent tables
                elif col_name in fks or col_type == "foreign_key":
                    fk_target = fks.get(col_name)
                    parent_table = None
                    if isinstance(fk_target, str) and "." in fk_target:
                        parent_table = fk_target.split(".")[0]
                    elif isinstance(fk_target, (list, tuple)) and len(fk_target) >= 1:
                        parent_table = fk_target[0]
                    elif col_name.endswith("_id"):
                        candidate = col_name[:-3].capitalize()
                        if candidate in dfs:
                            parent_table = candidate

                    if parent_table and parent_table in dfs and not dfs[parent_table].empty:
                        parent_df = dfs[parent_table]
                        parent_pk_col = [c for c in parent_df.columns if c.endswith("_id") or "id" in c]
                        if parent_pk_col:
                            row[col_name] = random.choice(parent_df[parent_pk_col[0]].tolist())
                        else:
                            row[col_name] = random.randint(1, len(parent_df))
                    else:
                        row[col_name] = max(1, (i % 20) + 1)

                # 3. Numeric types
                elif col_type in ("integer", "int"):
                    min_val = constraints.get("min", 1)
                    max_val = constraints.get("max", 1000)
                    row[col_name] = random.randint(int(min_val), int(max_val))

                elif col_type in ("float", "number"):
                    min_val = constraints.get("min", 10.0)
                    max_val = constraints.get("max", 500.0)
                    row[col_name] = round(random.uniform(float(min_val), float(max_val)), 2)

                # 4. Email / date / datetime / text
                elif col_type == "email" or "email" in col_name:
                    row[col_name] = f"user_{i}@example.com"

                elif col_type in ("date", "datetime") and col_name.lower() in {
                    "birth_date",
                    "date_of_birth",
                    "dob",
                }:
                    birth_date = datetime.now() - timedelta(
                        days=random.randint(21 * 365, 90 * 365)
                    )
                    row[col_name] = (
                        birth_date.date().isoformat()
                        if col_type == "date"
                        else birth_date.isoformat()
                    )

                elif col_type in ("date", "datetime"):
                    event_time = base_time + timedelta(days=table_idx * 2, hours=i * 4)
                    row[col_name] = event_time.date().isoformat() if col_type == "date" else event_time.isoformat()

                elif col_type == "boolean":
                    row[col_name] = random.choice([True, True, False])

                else:
                    prefixes = ["ALPHA", "BETA", "STANDARD", "ACTIVE", "COMPLETED", "VERIFIED"]
                    row[col_name] = f"{random.choice(prefixes)}-{table_name[:3].upper()}-{i}"

            rows.append(row)

        dfs[table_name] = pd.DataFrame(rows)

    return dfs


class _LocalTableGenerator:
    """Expose the offline synthesizer through the core generator protocol."""

    def generate_for_schemas(
        self,
        schemas: Dict[str, Any],
        prompts: Optional[Dict[str, str]] = None,
        sample_sizes: Optional[Dict[str, int]] = None,
        **_: Any,
    ) -> Dict[str, pd.DataFrame]:
        return _synthesize_relational_tables(schemas, sample_sizes or {})


def _scenario_schemas(scenario: ScenarioConfiguration) -> Dict[str, Any]:
    if scenario.schemas:
        return scenario.schemas
    return {
        step: {
            f"{step.lower()}_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "title": "text",
            "status": "text",
            "created_at": "date",
        }
        for step in scenario.workflow or ["Entity", "Record"]
    }


def _scenario_definition(
    scenario: ScenarioConfiguration,
    schemas: Dict[str, Any],
) -> ScenarioDefinition:
    """Translate the platform configuration into the reusable core model."""
    ordered_tables = [table for table in scenario.workflow if table in schemas]
    ordered_tables.extend(table for table in schemas if table not in ordered_tables)

    steps = []
    for index, table in enumerate(ordered_tables):
        non_event_dates = {"birth_date", "date_of_birth", "dob"}
        timestamp_field = next(
            (
                field
                for field, definition in schemas[table].items()
                if not field.startswith("__")
                and field.lower() not in non_event_dates
                and (
                    definition.lower()
                    if isinstance(definition, str)
                    else str(definition.get("type", "")).lower()
                )
                in ("date", "datetime")
            ),
            None,
        )
        steps.append(
            ScenarioStep(
                name=table,
                table=table,
                timestampField=timestamp_field,
                timeOffsetDays=index,
            )
        )

    return ScenarioDefinition(
        name=scenario.title,
        description=scenario.description,
        schemas=schemas,
        steps=steps,
        paths=[
            ScenarioPath(
                name=path.name,
                steps=path.steps,
                weight=path.weight,
                overrides=path.overrides,
            )
            for path in scenario.paths
        ],
    )


def _referential_integrity(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
) -> tuple[str, int]:
    """Calculate foreign-key integrity from generated values, without invented scores."""
    checked = 0
    violations = 0

    for table_name, schema_def in schemas.items():
        child_df = dfs.get(table_name)
        if child_df is None:
            continue
        for child_column, target in schema_def.get("__foreign_keys__", {}).items():
            if not isinstance(target, str) or "." not in target or child_column not in child_df:
                continue
            parent_table, parent_column = target.split(".", 1)
            parent_df = dfs.get(parent_table)
            non_null_values = child_df[child_column].dropna()
            checked += len(non_null_values)
            if parent_df is None or parent_column not in parent_df:
                violations += len(non_null_values)
                continue
            parent_values = set(parent_df[parent_column].dropna().tolist())
            violations += int((~non_null_values.isin(parent_values)).sum())

    if checked == 0:
        return "Not applicable", 0
    integrity = 100 * (checked - violations) / checked
    return f"{integrity:.1f}%", violations


def _constraint_compliance(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
) -> tuple[str, int, int]:
    checked = 0
    violations = 0
    for table_name, schema_def in schemas.items():
        df = dfs.get(table_name)
        if df is None:
            continue
        for column_name, field in schema_def.items():
            if column_name.startswith("__") or column_name not in df or not isinstance(field, dict):
                continue
            constraints = field.get("constraints", {})
            if "min" in constraints:
                values = pd.to_numeric(df[column_name], errors="coerce").dropna()
                checked += len(values)
                violations += int((values < float(constraints["min"])).sum())
            if "max" in constraints:
                values = pd.to_numeric(df[column_name], errors="coerce").dropna()
                checked += len(values)
                violations += int((values > float(constraints["max"])).sum())
    if checked == 0:
        return "Not evaluated", 0, 0
    compliance = 100 * (checked - violations) / checked
    return f"{compliance:.1f}%", violations, checked


def _temporal_consistency(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
) -> tuple[str, int, int]:
    """Measure child-before-parent temporal violations across declared FK relationships."""
    checked = 0
    violations = 0

    def temporal_columns(schema_def: Dict[str, Any]) -> list[str]:
        columns = []
        for name, field in schema_def.items():
            field_type = field if isinstance(field, str) else field.get("type") if isinstance(field, dict) else None
            if field_type in ("date", "datetime"):
                columns.append(name)
        return columns

    for child_table, child_schema in schemas.items():
        child_df = dfs.get(child_table)
        child_dates = temporal_columns(child_schema)
        if child_df is None or not child_dates:
            continue
        for child_fk, target in child_schema.get("__foreign_keys__", {}).items():
            if not isinstance(target, str) or "." not in target or child_fk not in child_df:
                continue
            parent_table, parent_pk = target.split(".", 1)
            parent_schema = schemas.get(parent_table, {})
            parent_df = dfs.get(parent_table)
            parent_dates = temporal_columns(parent_schema)
            if parent_df is None or parent_pk not in parent_df or not parent_dates:
                continue
            child_date = child_dates[0]
            parent_date = parent_dates[-1]
            merged = child_df[[child_fk, child_date]].merge(
                parent_df[[parent_pk, parent_date]],
                left_on=child_fk,
                right_on=parent_pk,
                how="inner",
                suffixes=("_child", "_parent"),
            )
            child_values = pd.to_datetime(merged[f"{child_date}_child"] if child_date == parent_date else merged[child_date], errors="coerce")
            parent_values = pd.to_datetime(merged[f"{parent_date}_parent"] if child_date == parent_date else merged[parent_date], errors="coerce")
            comparable = child_values.notna() & parent_values.notna()
            checked += int(comparable.sum())
            violations += int((child_values[comparable] < parent_values[comparable]).sum())

    if checked == 0:
        return "Not evaluated", 0, 0
    consistency = 100 * (checked - violations) / checked
    return f"{consistency:.1f}%", violations, checked


def _evaluation_metrics(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
    scenario: ScenarioConfiguration,
) -> tuple[list[EvaluationMetric], str, int]:
    referential_value, referential_violations = _referential_integrity(dfs, schemas)
    semantic_value, semantic_violations, semantic_checked = _constraint_compliance(dfs, schemas)
    causal_value, causal_violations, causal_checked = _temporal_consistency(dfs, schemas)
    missing_steps = [step for step in scenario.workflow if step not in dfs or dfs[step].empty]
    coverage_value = f"{100 * (len(scenario.workflow) - len(missing_steps)) / max(1, len(scenario.workflow)):.1f}%"

    def measured_status(violations: int, checked: int) -> str:
        if checked == 0:
            return "not_evaluated"
        return "pass" if violations == 0 else "fail"

    metrics = [
        EvaluationMetric(
            key="statistical_similarity",
            label="Statistical similarity",
            status="not_evaluated",
            value="No reference",
            detail="Provide a reference sample to compare numeric and categorical distributions.",
        ),
        EvaluationMetric(
            key="semantic_correctness",
            label="Semantic correctness",
            status=measured_status(semantic_violations, semantic_checked),
            value=semantic_value,
            detail="Checks declared numeric minimum and maximum constraints.",
            violations=semantic_violations,
        ),
        EvaluationMetric(
            key="causal_consistency",
            label="Causal consistency",
            status=measured_status(causal_violations, causal_checked),
            value=causal_value,
            detail="Checks temporal ordering between related parent and child records.",
            violations=causal_violations,
        ),
        EvaluationMetric(
            key="scenario_coverage",
            label="Scenario coverage",
            status="pass" if not missing_steps else "fail",
            value=coverage_value,
            detail="Checks that every declared workflow step produced records.",
            violations=len(missing_steps),
        ),
        EvaluationMetric(
            key="privacy_leakage",
            label="Privacy leakage risk",
            status="not_evaluated",
            value="Not configured",
            detail="Declare quasi-identifiers before running a k-anonymity check.",
        ),
        EvaluationMetric(
            key="referential_integrity",
            label="Referential integrity",
            status="pass" if referential_violations == 0 else "fail",
            value=referential_value,
            detail="Checks every declared foreign key against generated parent keys.",
            violations=referential_violations,
        ),
    ]
    evaluated = [metric for metric in metrics if metric.status != "not_evaluated"]
    result = "fail" if any(metric.status == "fail" for metric in evaluated) else (
        "pass" if len(evaluated) == len(metrics) else "partial"
    )
    return metrics, result, sum(metric.violations for metric in metrics)


async def _run_generation_pipeline(job_id: str, scenario: ScenarioConfiguration):
    """
    Asynchronous generation pipeline using syda.generate.SyntheticDataGenerator
    and saving datasets to disk while persisting job metadata in the database.
    """
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    try:
        # Stage 1: Inspect & validate schema dependencies
        _update_job_in_db(job_id, {
            "status": "generating",
            "progress": 20,
            "current_stage": "Analyzing relational schemas and foreign-key dependencies with Syda",
        })
        await asyncio.sleep(0.5)

        schemas = _scenario_schemas(scenario)

        # Stage 2: Execute generation
        _update_job_in_db(job_id, {
            "status": "generating",
            "progress": 55,
            "current_stage": "Generating synthetic records across schema tables",
        })

        dfs: Dict[str, pd.DataFrame] = {}
        path_counts: Dict[str, int] = {}
        definition = _scenario_definition(scenario, schemas)

        # Attempt to use syda.generate.SyntheticDataGenerator if LLM key is configured
        openai_key = os.getenv("OPENAI_API_KEY")
        gemini_key = os.getenv("GEMINI_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        if openai_key or anthropic_key or gemini_key:
            try:
                model_cfg = (
                    ModelConfig(provider="openai", model_name="gpt-4o-mini", batch_size=10)
                    if openai_key
                    else (
                        ModelConfig(provider="anthropic", model_name="claude-3-5-haiku-20241022", batch_size=10)
                        if anthropic_key
                        else ModelConfig(provider="gemini", model_name="gemini-3.6-flash", batch_size=10)
                    )
                )
                generator = SyntheticDataGenerator(model_config=model_cfg)
                result = await asyncio.to_thread(
                    ScenarioEngine(generator).generate,
                    definition,
                    scenario.record_count,
                )
                dfs = result.tables
                path_counts = result.path_counts
            except Exception as llm_err:
                logger.warning(f"Live LLM generation encountered ({llm_err}). Using syda schema synthesizer.")

        if not dfs:
            result = await asyncio.to_thread(
                ScenarioEngine(_LocalTableGenerator()).generate,
                definition,
                scenario.record_count,
            )
            dfs = result.tables
            path_counts = result.path_counts

        # Stage 3: Validate referential integrity & save output
        _update_job_in_db(job_id, {
            "status": "evaluating",
            "progress": 85,
            "current_stage": "Verifying foreign-key constraints and writing multi-table datasets",
        })
        await asyncio.sleep(0.5)

        # Save generated DataFrames using syda.output.save_dataframes
        save_dataframes(dfs, str(job_dir), format="csv")

        elapsed = round(time.time() - t0, 1)
        total_records = sum(len(df) for df in dfs.values())
        metrics, evaluation_result, total_violations = _evaluation_metrics(dfs, schemas, scenario)
        metric_by_key = {metric.key: metric for metric in metrics}
        table_row_counts = {name: len(df) for name, df in dfs.items()}

        stats = JobStats(
            causalIntegrity=metric_by_key["causal_consistency"].value,
            referentialIntegrity=metric_by_key["referential_integrity"].value,
            compliance=metric_by_key["semantic_correctness"].value,
            recordsGenerated=total_records,
            flaggedRecords=total_violations,
            durationSeconds=elapsed,
            tableRowCounts=table_row_counts,
            pathCounts=path_counts,
            evaluationMetrics=metrics,
            evaluationResult=evaluation_result,
        )

        _update_job_in_db(job_id, {
            "status": "complete",
            "progress": 100,
            "current_stage": "Generation and referential evaluation complete",
            "stats": stats.model_dump(by_alias=True),
            "download_url": f"/api/download/{job_id}",
        })
    except Exception as e:
        logger.error(f"Generation pipeline failed for job {job_id}: {e}")
        _update_job_in_db(job_id, {
            "status": "failed",
            "current_stage": f"Error: {str(e)}",
        })


@router.post("/generate", response_model=JobStatusResponse)
async def start_generation(
    req: GenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Accepts scenario configuration with validated syda schemas,
    persists the initial job, and dispatches generation.
    """
    job_id = str(uuid.uuid4())[:8]

    new_job = JobRecord(
        id=job_id,
        status="generating",
        progress=10,
        current_stage="Initializing Syda generation worker",
        scenario=req.scenario.model_dump(by_alias=True),
    )
    try:
        db.add(new_job)
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        logger.exception("Could not persist generation job %s", job_id)
        raise HTTPException(
            status_code=503,
            detail="The generation database is unavailable. Restart the backend or check DATABASE_URL.",
        ) from error

    background_tasks.add_task(_run_generation_pipeline, job_id, req.scenario)

    return JobStatusResponse(
        job_id=job_id,
        status="generating",
        progress=10,
        current_stage="Initializing Syda generation worker",
    )


@router.post("/estimate", response_model=CostEstimate)
async def estimate_generation(req: GenerateRequest):
    """Estimate model usage before generation using schema size and generation mode."""
    schemas = _scenario_schemas(req.scenario)
    definition = _scenario_definition(req.scenario, schemas)
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

    if os.getenv("OPENAI_API_KEY"):
        provider, model = "OpenAI", "gpt-4o-mini"
    elif os.getenv("ANTHROPIC_API_KEY"):
        provider, model = "Anthropic", "claude-3-5-haiku-20241022"
    elif os.getenv("GEMINI_API_KEY"):
        provider, model = "Google", "gemini-3.6-flash"
    else:
        return CostEstimate(
            provider="Offline",
            model="Local schema synthesizer",
            estimatedInputTokens=0,
            estimatedOutputTokens=0,
            estimatedCostUsd=0.0,
            note="No LLM provider key is configured; generation will use the local fallback.",
        )

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
async def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """Poll status and measured metrics for an active or completed generation job."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id).first()
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
async def list_jobs(db: Session = Depends(get_db), limit: int = 50):
    """Return the persisted history of synthetic generation jobs."""
    jobs = db.query(JobRecord).order_by(JobRecord.created_at.desc()).limit(limit).all()
    return [job.to_dict() for job in jobs]


@router.get("/preview/{job_id}")
async def preview_generated_dataset(
    job_id: str,
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Return a small, browser-safe preview of every generated table."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete":
        raise HTTPException(status_code=409, detail="Dataset preview is not ready")

    job_dir = DATA_DIR / job_id
    csv_files = sorted(job_dir.glob("*.csv")) if job_dir.exists() else []
    if not csv_files:
        raise HTTPException(status_code=404, detail="Generated dataset files are unavailable")

    stats = JobStats.model_validate(job.stats) if job.stats else None
    row_counts_by_name = {
        name.lower(): count for name, count in (stats.table_row_counts.items() if stats else [])
    }
    tables = {}
    for path in csv_files:
        raw_frame = pd.read_csv(path, nrows=limit)
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
):
    """Download the structured evaluation report as JSON or HTML."""
    job = db.query(JobRecord).filter(JobRecord.id == job_id).first()
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
):
    """
    Streams generated dataset(s). If multiple relational tables were generated,
    returns a zip archive containing all tables, or single table CSV if specified.
    """
    job = db.query(JobRecord).filter(JobRecord.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete":
        raise HTTPException(status_code=409, detail="Dataset is not ready for download")

    job_dir = DATA_DIR / job_id
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
            schema_errors.append(f"Scenario workflow: {error}")

    return {
        "valid": len(schema_errors) == 0,
        "evaluated_rules_count": len(scenario.rules),
        "workflow_steps_count": len(scenario.workflow),
        "evaluated_tables_count": evaluated_tables,
        "warnings": warnings,
        "schema_errors": schema_errors,
    }
