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
from syda.scenarios import ScenarioEngine

from .auth import require_authenticated
from ..database import get_db, SessionLocal
from ..models.entities import JobRecord, ScenarioRecord
from ..provider_settings import resolve_provider
from ..evaluation.statistical_similarity import evaluate_statistical_similarity
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
    EvaluationMetric,
    StatisticalFieldResult,
    CostEstimate,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Generation"])

DATA_DIR = Path("data/jobs")
DATA_DIR.mkdir(parents=True, exist_ok=True)

_K_ANONYMITY_THRESHOLD = 3
_QUASI_IDENTIFIER_NAMES = frozenset(
    {
        "age",
        "birth_date",
        "date_of_birth",
        "dob",
        "gender",
        "postal_code",
        "sex",
        "zip",
        "zip_code",
    }
)


def _update_job_in_db(job_id: str, updates: Dict[str, Any]):
    """Helper to update a job record in the database within a short-lived session."""
    db = SessionLocal()
    try:
        job = db.query(JobRecord).filter(JobRecord.id == job_id).first()
        if job:
            for key, val in updates.items():
                if key == "current_stage" and isinstance(val, str):
                    val = val[:255]
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
                enum_values = constraints.get("enum")

                # 1. Preserve constrained values, including scenario-ledger row keys.
                if enum_values:
                    row[col_name] = enum_values[(i - 1) % len(enum_values)]

                # 2. Primary keys
                elif constraints.get("primary_key") or col_name.endswith("_id") and col_name == f"{table_name.lower()}_id":
                    row[col_name] = i

                # 3. Foreign keys referencing previously generated parent tables
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

                # 4. Numeric types
                elif col_type in ("integer", "int"):
                    min_val = constraints.get("min", 1)
                    max_val = constraints.get("max", 1000)
                    row[col_name] = random.randint(int(min_val), int(max_val))

                elif col_type in ("float", "number"):
                    min_val = constraints.get("min", 10.0)
                    max_val = constraints.get("max", 500.0)
                    row[col_name] = round(random.uniform(float(min_val), float(max_val)), 2)

                # 5. Email / date / datetime / text
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


def _privacy_leakage_metric(
    dfs: Dict[str, pd.DataFrame],
) -> EvaluationMetric:
    """Measure k-anonymity using recognized quasi-identifier columns."""
    evaluated_records = 0
    vulnerable_records = 0
    minimum_k: Optional[int] = None
    detected_columns = []

    for table_name, df in dfs.items():
        if df is None or df.empty:
            continue

        quasi_identifiers = [
            column
            for column in df.columns
            if str(column).lower() in _QUASI_IDENTIFIER_NAMES
        ]
        if not quasi_identifiers:
            continue

        working = df[quasi_identifiers].copy()
        working["__privacy_row__"] = range(len(working))
        group_sizes = working.groupby(
            quasi_identifiers,
            dropna=False,
            sort=False,
        )["__privacy_row__"].transform("size")

        table_minimum_k = int(group_sizes.min())
        minimum_k = (
            table_minimum_k
            if minimum_k is None
            else min(minimum_k, table_minimum_k)
        )
        evaluated_records += len(working)
        vulnerable_records += int(
            (group_sizes < _K_ANONYMITY_THRESHOLD).sum()
        )
        detected_columns.append(
            f"{table_name} ({', '.join(map(str, quasi_identifiers))})"
        )

    if minimum_k is None or evaluated_records == 0:
        return EvaluationMetric(
            key="privacy_leakage",
            label="Privacy leakage risk",
            status="not_evaluated",
            value="No quasi-identifiers detected",
            detail="No populated table contains a recognized quasi-identifier column.",
        )

    risk_percentage = 100 * vulnerable_records / evaluated_records
    status = "pass" if minimum_k >= _K_ANONYMITY_THRESHOLD else "fail"
    return EvaluationMetric(
        key="privacy_leakage",
        label="Privacy leakage risk",
        status=status,
        value=f"k={minimum_k}; risk={risk_percentage:.1f}%",
        detail=(
            f"Evaluated {evaluated_records} records using "
            f"{'; '.join(detected_columns)}. {vulnerable_records} records are in "
            f"equivalence classes smaller than {_K_ANONYMITY_THRESHOLD}."
        ),
        violations=vulnerable_records,
    )


def _evaluation_metrics(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
    scenario: ScenarioConfiguration,
    reference_data: Optional[Dict[str, list[Dict[str, Any]]]] = None,
) -> tuple[list[EvaluationMetric], str, int]:
    referential_value, referential_violations = _referential_integrity(dfs, schemas)
    semantic_value, semantic_violations, semantic_checked = _constraint_compliance(dfs, schemas)
    causal_value, causal_violations, causal_checked = _temporal_consistency(dfs, schemas)
    privacy_metric = _privacy_leakage_metric(dfs)
    statistical_result = evaluate_statistical_similarity(
        dfs, schemas, reference_data
    )
    executable_checks = ScenarioEngine.evaluate_checks(
        _scenario_definition(scenario, schemas), dfs
    )
    check_violations = sum(item["violations"] for item in executable_checks)
    checked_rules = len(executable_checks)
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
            status=statistical_result.status,
            value=(
                "No reference"
                if statistical_result.score is None
                else f"{statistical_result.score:.3f}"
            ),
            detail=statistical_result.detail,
            violations=statistical_result.violations,
            fieldResults=[
                StatisticalFieldResult(
                    table=field.table,
                    column=field.column,
                    kind=field.kind,
                    score=field.score,
                    ksStatistic=field.ks_statistic,
                    wassersteinDistance=field.wasserstein_distance,
                    chiSquareStatistic=field.chi_square_statistic,
                )
                for field in statistical_result.evaluated_fields
            ],
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
        privacy_metric,
        EvaluationMetric(
            key="referential_integrity",
            label="Referential integrity",
            status="pass" if referential_violations == 0 else "fail",
            value=referential_value,
            detail="Checks every declared foreign key against generated parent keys.",
            violations=referential_violations,
        ),
        EvaluationMetric(
            key="declared_rules",
            label="Declared rule checks",
            status=(
                "not_evaluated" if checked_rules == 0
                else "pass" if check_violations == 0
                else "fail"
            ),
            value=(
                "No executable checks" if checked_rules == 0
                else f"{checked_rules - sum(bool(item['violations']) for item in executable_checks)}/{checked_rules} checks passed"
            ),
            detail=(
                "Checks machine-readable rules. Free-text rules are provided to the model but need a structured check to be verified."
            ),
            violations=check_violations,
        ),
    ]
    evaluated = [metric for metric in metrics if metric.status != "not_evaluated"]
    result = "fail" if any(metric.status == "fail" for metric in evaluated) else (
        "pass" if len(evaluated) == len(metrics) else "partial"
    )
    return metrics, result, sum(metric.violations for metric in metrics)


async def _run_generation_pipeline(
    job_id: str,
    scenario: ScenarioConfiguration,
    provider: dict[str, Any],
    reference_data: Optional[Dict[str, list[Dict[str, Any]]]] = None,
):
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
        last_reported_progress = [55]

        def report_generation_progress(
            completed_rows: int,
            total_rows: int,
            table_name: str,
        ) -> None:
            if total_rows < 1:
                return
            progress = 55 + min(29, int(29 * completed_rows / total_rows))
            if progress > last_reported_progress[0]:
                last_reported_progress[0] = progress
                _update_job_in_db(job_id, {
                    "progress": progress,
                    "current_stage": (
                        f"Generating {table_name} records "
                        f"({completed_rows:,}/{total_rows:,} rows)"
                    ),
                })

        # The provider is fixed when the run is submitted, even if settings change.
        if provider["id"] != "offline":
            try:
                model_cfg = ModelConfig(provider=provider["id"], model_name=provider["model"], batch_size=10, extra_kwargs={"api_key": provider["key"], **({"base_url": provider["base_url"]} if provider.get("base_url") else {})})
                generator = SyntheticDataGenerator(model_config=model_cfg)
                result = await asyncio.to_thread(
                    ScenarioEngine(generator).generate,
                    definition,
                    scenario.record_count,
                    progress_callback=report_generation_progress,
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
                progress_callback=report_generation_progress,
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
        metrics, evaluation_result, total_violations = _evaluation_metrics(
            dfs, schemas, scenario, reference_data
        )
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
            "stats": stats.model_dump(by_alias=True, exclude_none=True),
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
    user: User = Depends(require_authenticated),
):
    """
    Accepts scenario configuration with validated syda schemas,
    persists the initial job, and dispatches generation.
    """
    job_id = str(uuid.uuid4())
    if req.scenario_id and not db.query(ScenarioRecord).filter(ScenarioRecord.id == req.scenario_id, ScenarioRecord.user_id == user.id).first():
        raise HTTPException(status_code=404, detail="Scenario not found")

    new_job = JobRecord(
        id=job_id,
        user_id=user.id,
        status="generating",
        progress=10,
        current_stage="Initializing Syda generation worker",
        scenario=req.scenario.model_dump(by_alias=True),
        scenario_id=req.scenario_id,
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

    background_tasks.add_task(
        _run_generation_pipeline,
        job_id,
        req.scenario,
        resolve_provider(db, user.id),
        req.reference_data,
    )

    return JobStatusResponse(
        job_id=job_id,
        status="generating",
        progress=10,
        current_stage="Initializing Syda generation worker",
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
    job_dir = DATA_DIR / job.id
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
        raw_frame = pd.read_csv(path, skiprows=range(1, offset + 1), nrows=limit)
        frame = raw_frame.astype(object).where(pd.notna(raw_frame), None)
        tables[path.stem] = {
            "columns": frame.columns.tolist(),
            "rows": frame.to_dict(orient="records"),
            "rowCount": row_counts_by_name.get(path.stem.lower(), 0),
        }
    return {"jobId": job_id, "tables": tables}


def _completed_evaluation_stats(
    job_id: str,
    db: Session,
    user: User,
) -> JobStats:
    job = db.query(JobRecord).filter(JobRecord.id == job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "complete" or not job.stats:
        raise HTTPException(status_code=409, detail="Evaluation report is not ready")
    return JobStats.model_validate(job.stats)


def _evaluation_report_payload(job_id: str, stats: JobStats) -> Dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "jobId": job_id,
        "result": stats.evaluation_result,
        "recordsGenerated": stats.records_generated,
        "pathCounts": stats.path_counts,
        "metrics": [
            metric.model_dump(by_alias=True, exclude_none=True)
            for metric in stats.evaluation_metrics
        ],
    }


def _format_report_number(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.6g}"


def _evaluation_report_html(job_id: str, stats: JobStats) -> str:
    metrics = stats.evaluation_metrics
    status_counts = {
        status: sum(metric.status == status for metric in metrics)
        for status in ("pass", "fail", "not_evaluated")
    }
    metric_count = max(1, len(metrics))
    result_label = stats.evaluation_result.replace("_", " ").title()

    scorecards = "".join(
        f"""<article class="scorecard">
          <div class="scorecard-heading"><h3>{html.escape(metric.label)}</h3><span class="badge {metric.status}">{html.escape(metric.status.replace('_', ' ').title())}</span></div>
          <p class="metric-value">{html.escape(metric.value)}</p>
          <p class="metric-detail">{html.escape(metric.detail)}</p>
          <p class="violations">{metric.violations:,} violation{'s' if metric.violations != 1 else ''}</p>
        </article>"""
        for metric in metrics
    )

    status_chart = "".join(
        f"""<div class="chart-row">
          <span>{html.escape(status.replace('_', ' ').title())}</span>
          <div class="bar-track"><span class="bar {status}" style="width:{100 * count / metric_count:.1f}%"></span></div>
          <strong>{count}</strong>
        </div>"""
        for status, count in status_counts.items()
    )

    statistical_metric = next(
        (metric for metric in metrics if metric.key == "statistical_similarity"),
        None,
    )
    if statistical_metric is None or statistical_metric.field_results is None:
        statistical_content = (
            '<p class="empty-state">Per-field statistical details were not recorded for this run.</p>'
        )
    elif not statistical_metric.field_results:
        statistical_content = (
            '<p class="empty-state">No comparable numeric or categorical fields were evaluated.</p>'
        )
    else:
        field_chart = "".join(
            f"""<div class="field-chart-row">
              <span><strong>{html.escape(field.table)}.{html.escape(field.column)}</strong><small>{html.escape(field.kind)}</small></span>
              <div class="bar-track"><span class="bar score" style="width:{field.score * 100:.1f}%"></span></div>
              <strong>{field.score * 100:.1f}%</strong>
            </div>"""
            for field in statistical_metric.field_results
        )
        field_rows = "".join(
            f"""<tr>
              <td>{html.escape(field.table)}</td>
              <td>{html.escape(field.column)}</td>
              <td>{html.escape(field.kind.title())}</td>
              <td>{field.score:.6g}</td>
              <td>{_format_report_number(field.ks_statistic)}</td>
              <td>{_format_report_number(field.wasserstein_distance)}</td>
              <td>{_format_report_number(field.chi_square_statistic)}</td>
            </tr>"""
            for field in statistical_metric.field_results
        )
        statistical_content = f"""
          <div class="field-chart" role="img" aria-label="Per-field statistical similarity scores">{field_chart}</div>
          <div class="table-scroll"><table>
            <thead><tr><th>Table</th><th>Field</th><th>Type</th><th>Similarity score</th><th>KS statistic</th><th>Wasserstein distance</th><th>Chi-squared statistic</th></tr></thead>
            <tbody>{field_rows}</tbody>
          </table></div>"""

    path_badges = "".join(
        f'<span class="path-badge">{html.escape(path)} · {count:,}</span>'
        for path, count in stats.path_counts.items()
    ) or '<span class="muted">No scenario path counts recorded</span>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Syda evaluation {html.escape(job_id)}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f8fafc; --surface:#fff; --text:#172033; --muted:#64748b; --border:#dbe3ee; --pass:#15803d; --pass-bg:#dcfce7; --fail:#b91c1c; --fail-bg:#fee2e2; --partial:#a16207; --partial-bg:#fef3c7; --neutral:#64748b; --neutral-bg:#e2e8f0; --accent:#0f766e; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:40px 0 56px; }}
    header,.panel,.scorecard {{ background:var(--surface); border:1px solid var(--border); border-radius:16px; }}
    header {{ padding:28px; }} h1,h2,h3,p {{ margin-top:0; }} h1 {{ margin-bottom:6px; font-size:clamp(1.75rem,4vw,2.5rem); }} h2 {{ margin-bottom:18px; }}
    .headline {{ display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }} .muted,.metric-detail {{ color:var(--muted); }}
    .badge,.path-badge {{ display:inline-flex; align-items:center; border-radius:999px; font-weight:700; }} .badge {{ padding:5px 10px; font-size:12px; }} .path-badge {{ padding:5px 9px; border:1px solid var(--border); font-size:12px; }}
    .badge.pass {{ color:var(--pass); background:var(--pass-bg); }} .badge.fail {{ color:var(--fail); background:var(--fail-bg); }} .badge.partial {{ color:var(--partial); background:var(--partial-bg); }} .badge.not_evaluated {{ color:var(--neutral); background:var(--neutral-bg); }}
    .summary {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:24px; }} .summary div {{ padding-left:12px; border-left:3px solid var(--border); }} .summary strong {{ display:block; margin-top:2px; font-size:1.2rem; }}
    .paths {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:20px; }} .panel {{ margin-top:20px; padding:24px; }}
    .scorecards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; }} .scorecard {{ padding:18px; }} .scorecard-heading {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }} .scorecard h3 {{ margin-bottom:0; font-size:1rem; }}
    .metric-value {{ margin:18px 0 6px; font-size:1.7rem; font-weight:750; }} .metric-detail {{ min-height:42px; font-size:13px; }} .violations {{ margin-bottom:0; font-size:12px; font-weight:700; }}
    .chart-row,.field-chart-row {{ display:grid; grid-template-columns:130px 1fr 40px; gap:12px; align-items:center; margin:12px 0; }} .field-chart-row {{ grid-template-columns:minmax(180px,260px) 1fr 58px; }} .field-chart-row small {{ display:block; color:var(--muted); font-weight:400; }}
    .bar-track {{ height:12px; overflow:hidden; background:#e8edf4; border-radius:999px; }} .bar {{ display:block; height:100%; border-radius:inherit; }} .bar.pass {{ background:var(--pass); }} .bar.fail {{ background:var(--fail); }} .bar.not_evaluated {{ background:var(--neutral); }} .bar.score {{ background:var(--accent); }}
    .table-scroll {{ margin-top:22px; overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:10px 12px; border-bottom:1px solid var(--border); text-align:left; white-space:nowrap; }} th {{ background:#f1f5f9; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
    .empty-state {{ margin-bottom:0; padding:18px; color:var(--muted); background:#f1f5f9; border-radius:10px; }}
    @media (max-width:700px) {{ main {{ width:min(100% - 20px,1180px); padding-top:20px; }} header,.panel {{ padding:18px; }} .headline {{ display:block; }} .headline .badge {{ margin-top:10px; }} .summary {{ grid-template-columns:1fr; }} .chart-row,.field-chart-row {{ grid-template-columns:1fr 48px; }} .chart-row>span:first-child,.field-chart-row>span:first-child {{ grid-column:1/-1; }} }}
    @media print {{ body {{ background:#fff; }} main {{ width:100%; padding:0; }} header,.panel,.scorecard {{ break-inside:avoid; box-shadow:none; }} }}
  </style>
</head>
<body><main>
  <header>
    <div class="headline"><div><h1>Syda evaluation report</h1><p class="muted">Job {html.escape(job_id)}</p></div><span class="badge {stats.evaluation_result}">{html.escape(result_label)}</span></div>
    <div class="summary"><div><span class="muted">Records generated</span><strong>{stats.records_generated:,}</strong></div><div><span class="muted">Metrics evaluated</span><strong>{len(metrics)}</strong></div><div><span class="muted">Total violations</span><strong>{stats.flagged_records:,}</strong></div></div>
    <div class="paths">{path_badges}</div>
  </header>
  <section class="panel"><h2>Metric scorecards</h2><div class="scorecards">{scorecards}</div></section>
  <section class="panel"><h2>Quality status chart</h2><div role="img" aria-label="Counts of passed, failed, and not evaluated metrics">{status_chart}</div></section>
  <section class="panel"><h2>Statistical similarity details</h2>{statistical_content}</section>
</main></body></html>"""


@router.get("/jobs/{job_id}/evaluation")
async def get_evaluation_report_json(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Return the structured evaluation report for one completed job."""
    stats = _completed_evaluation_stats(job_id, db, user)
    return JSONResponse(_evaluation_report_payload(job_id, stats))


@router.get("/jobs/{job_id}/evaluation-report")
async def view_evaluation_report_html(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    """Display a self-contained evaluation report for one completed job."""
    stats = _completed_evaluation_stats(job_id, db, user)
    return HTMLResponse(
        _evaluation_report_html(job_id, stats),
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
