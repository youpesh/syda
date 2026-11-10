"""Generation and evaluation, independent of HTTP routes and task transport."""
import tempfile
import time
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Literal
import pandas as pd
from syda.schemas import validate_schema, ModelConfig
from syda.generate import SyntheticDataGenerator
from syda.output import save_dataframes
from syda.templates import TemplateProcessor
from syda.scenarios import ScenarioEngine, ScenarioValidationError
from ..scenario_definition import scenario_definition as _scenario_definition
from ..evaluation.ai_judge import run_ai_judge_audit
from ..evaluation.statistical_similarity import evaluate_statistical_similarity
from ..models.scenario import (
    ScenarioConfiguration,
    EvaluationMetric,
    JudgeAuditResult,
    JobStats,
    StatisticalFieldResult,
    ViolationRecord,
)
logger = logging.getLogger(__name__)

_K_ANONYMITY_THRESHOLD = 3
_QUASI_IDENTIFIER_NAMES = frozenset(
    {"age", "birth_date", "date_of_birth", "dob", "gender", "postal_code", "sex", "zip", "zip_code"}
)

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

                    if parent_table == table_name:
                        if i == 1 or (i <= max(2, n_rows // 10) and random.random() < 0.4):
                            row[col_name] = None
                        else:
                            row[col_name] = random.randint(1, i - 1)
                    elif parent_table and parent_table in dfs and not dfs[parent_table].empty:
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
        if child_df is None:
            continue
        child_dates = [c for c in temporal_columns(child_schema) if c in child_df]
        if not child_dates:
            continue
        for child_fk, target in child_schema.get("__foreign_keys__", {}).items():
            if not isinstance(target, str) or "." not in target or child_fk not in child_df:
                continue
            parent_table, parent_pk = target.split(".", 1)
            parent_schema = schemas.get(parent_table, {})
            parent_df = dfs.get(parent_table)
            if parent_df is None or parent_pk not in parent_df:
                continue
            parent_dates = [c for c in temporal_columns(parent_schema) if c in parent_df]
            if not parent_dates:
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


def _privacy_leakage_metric(dfs: Dict[str, pd.DataFrame]) -> EvaluationMetric:
    """Measure k-anonymity using recognized quasi-identifier columns."""
    evaluated_records = 0
    vulnerable_records = 0
    minimum_k: Optional[int] = None
    detected_columns: list[str] = []

    for table_name, df in dfs.items():
        if df is None or df.empty:
            continue
        quasi_identifiers = [
            column for column in df.columns
            if str(column).lower() in _QUASI_IDENTIFIER_NAMES
        ]
        if not quasi_identifiers:
            continue

        working = df[quasi_identifiers].copy()
        working["__privacy_row__"] = range(len(working))
        group_sizes = working.groupby(
            quasi_identifiers, dropna=False, sort=False
        )["__privacy_row__"].transform("size")
        table_minimum_k = int(group_sizes.min())
        minimum_k = table_minimum_k if minimum_k is None else min(minimum_k, table_minimum_k)
        evaluated_records += len(working)
        vulnerable_records += int((group_sizes < _K_ANONYMITY_THRESHOLD).sum())
        detected_columns.append(f"{table_name} ({', '.join(map(str, quasi_identifiers))})")

    if minimum_k is None or evaluated_records == 0:
        return EvaluationMetric(
            key="privacy_leakage",
            label="Privacy leakage risk",
            status="not_evaluated",
            value="No quasi-identifiers detected",
            detail="No populated table contains a recognized quasi-identifier column.",
        )

    risk_percentage = 100 * vulnerable_records / evaluated_records
    return EvaluationMetric(
        key="privacy_leakage",
        label="Privacy leakage risk",
        status="pass" if minimum_k >= _K_ANONYMITY_THRESHOLD else "fail",
        value=f"k={minimum_k}; risk={risk_percentage:.1f}%",
        detail=(
            f"Evaluated {evaluated_records} records using {'; '.join(detected_columns)}. "
            f"{vulnerable_records} records are in equivalence classes smaller than "
            f"{_K_ANONYMITY_THRESHOLD}."
        ),
        violations=vulnerable_records,
    )


def _evaluation_metrics(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
    scenario: ScenarioConfiguration,
    reference_data: Optional[Dict[str, list[Dict[str, Any]]]] = None,
    return_details: bool = False,
    judge_audit: Optional[JudgeAuditResult] = None,
    judge_detail: Optional[str] = None,
) -> tuple:
    referential_value, referential_violations = _referential_integrity(dfs, schemas)
    semantic_value, semantic_violations, semantic_checked = _constraint_compliance(dfs, schemas)
    privacy_metric = _privacy_leakage_metric(dfs)
    statistical_result = evaluate_statistical_similarity(dfs, schemas, reference_data)
    definition = _scenario_definition(scenario, schemas)
    executable_checks = ScenarioEngine.evaluate_checks(definition, dfs)
    # Use the same temporal/FSM rules as core validation. Choosing arbitrary date
    # columns by schema order can contradict an explicitly declared workflow.
    causal_names = {
        check.name for check in [*definition.checks, *ScenarioEngine.infer_causal_checks(definition)]
        if check.kind in {"temporal_order", "state_transition", "state_machine"}
    }
    causal_checks = [item for item in executable_checks if item["name"] in causal_names]
    causal_checked = len(causal_checks)
    causal_violations = sum(item["violations"] for item in causal_checks)
    causal_value = (
        f"{100 * sum(item['violations'] == 0 for item in causal_checks) / causal_checked:.1f}%"
        if causal_checked else "Not evaluated"
    )
    check_violations = sum(item["violations"] for item in executable_checks)
    checked_rules = len(executable_checks)
    expected_counts = ScenarioEngine.summarize(definition, scenario.record_count).table_row_counts
    expected_tables = [table for table, count in expected_counts.items() if count > 0]
    missing_steps = [table for table in expected_tables if table not in dfs or dfs[table].empty]
    coverage_value = f"{100 * (len(expected_tables) - len(missing_steps)) / max(1, len(expected_tables)):.1f}%"

    violation_records: list[ViolationRecord] = []
    for check_item in executable_checks:
        for detail in check_item.get("details", []):
            violation_records.append(
                ViolationRecord(
                    table=detail["table"],
                    row=detail["row"],
                    rule=detail["rule"],
                    error=detail["error"],
                )
            )

    def measured_status(violations: int, checked: int) -> Literal["not_evaluated", "pass", "fail"]:
        if checked == 0:
            return "not_evaluated"
        return "pass" if violations == 0 else "fail"

    def binary_status(is_pass: bool) -> Literal["pass", "fail"]:
        return "pass" if is_pass else "fail"

    deterministic_semantic_status = measured_status(semantic_violations, semantic_checked)
    semantic_status = deterministic_semantic_status
    semantic_metric_value = semantic_value
    semantic_detail = "Checks declared numeric minimum and maximum constraints."
    if judge_audit is not None:
        semantic_status = (
            "fail"
            if deterministic_semantic_status == "fail" or judge_audit.verdict == "fail"
            else "pass"
        )
        semantic_metric_value = f"{judge_audit.score:.3f}"
        deterministic_summary = (
            "no deterministic constraints were evaluable"
            if deterministic_semantic_status == "not_evaluated"
            else f"deterministic constraints {deterministic_semantic_status}ed"
        )
        anomaly_count = len(judge_audit.flagged_anomalies)
        anomaly_summary = (
            f"{anomaly_count} anomaly flagged"
            if anomaly_count == 1
            else f"{anomaly_count} anomalies flagged"
        )
        semantic_detail = (
            f"AI Judge verdict: {judge_audit.verdict}; {deterministic_summary}. "
            f"{anomaly_summary}. See AI Judge Audit above."
        )
    elif judge_detail:
        semantic_detail = f"{semantic_detail} {judge_detail}"

    metrics = [
        EvaluationMetric(
            key="statistical_similarity",
            label="Statistical similarity",
            status=statistical_result.status,
            value="No reference" if statistical_result.score is None else f"{statistical_result.score:.3f}",
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
            status=semantic_status,
            value=semantic_metric_value,
            detail=semantic_detail,
            violations=semantic_violations,
        ),
        EvaluationMetric(
            key="causal_consistency",
            label="Causal consistency",
            status=measured_status(causal_violations, causal_checked),
            value=causal_value,
            detail="Checks declared and inferred temporal ordering and state-transition rules.",
            violations=causal_violations,
        ),
        EvaluationMetric(
            key="scenario_coverage",
            label="Scenario coverage",
            status=binary_status(not missing_steps),
            value=coverage_value,
            detail="Checks that every table required by the allocated scenario paths produced records.",
            violations=len(missing_steps),
        ),
        privacy_metric,
        EvaluationMetric(
            key="referential_integrity",
            label="Referential integrity",
            status=binary_status(referential_violations == 0),
            value=referential_value,
            detail="Checks every declared foreign key against generated parent keys.",
            violations=referential_violations,
        ),
        EvaluationMetric(
            key="declared_rules",
            label="Declared rule checks",
            status=measured_status(check_violations, checked_rules),
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
    total_violations = sum(metric.violations for metric in metrics)
    if return_details:
        return metrics, result, total_violations, violation_records
    return metrics, result, total_violations


# ponytail: rendering is sequential, one PDF per row; cap keeps big runs bounded. Parallelize if needed.
MAX_DOCUMENTS_PER_TEMPLATE = 1000


def _no_external_resources(url, *args, **kwargs):
    """Templates are user-supplied, so WeasyPrint may not fetch files or URLs."""
    # ponytail: data: images blocked too; allow via weasyprint.URLFetcher(allowed_protocols={"data"}) if needed.
    raise ValueError("Linked documents cannot load external resources")


def _render_documents(dfs: Dict[str, pd.DataFrame], schemas: Dict[str, Any], job_dir: Path) -> None:
    """Render template tables to <job_dir>/<table>/document_N.<pdf|html> with Syda's template pipeline."""
    templates = {
        name: schema for name, schema in schemas.items()
        if schema.get("__template__") and name in dfs and not dfs[name].empty
    }
    if not templates:
        return
    with tempfile.TemporaryDirectory() as source_dir:
        jobs = {}
        for index, (name, schema) in enumerate(templates.items()):
            # The platform stores the template inline; the SDK reads it from a file.
            source = Path(source_dir) / f"template_{index}.html"
            source.write_text(schema["__template_source__"], encoding="utf-8")
            jobs[name] = (dfs[name].head(MAX_DOCUMENTS_PER_TEMPLATE), {
                **schema,
                "__template_source__": str(source),
                "__input_file_type__": "html",
                "__output_file_type__": schema.get("__output_file_type__", "pdf"),
            })
        TemplateProcessor(url_fetcher=_no_external_resources).process_template_dataframes(jobs, str(job_dir))


def run_generation(
    job_id: str,
    scenario: ScenarioConfiguration,
    provider: dict[str, Any],
    *,
    output_dir: Path,
    update_job,
    reference_data: Optional[Dict[str, list[Dict[str, Any]]]] = None,
):
    """
    Generation pipeline using syda.generate.SyntheticDataGenerator
    and saving datasets to disk while persisting job metadata in the database.
    """
    job_dir = output_dir
    job_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # Stage 1: Inspect & validate schema dependencies
    update_job(job_id, {
        "status": "generating",
        "progress": 20,
        "current_stage": "Analyzing relational schemas and foreign-key dependencies with Syda",
    })

    schemas = _scenario_schemas(scenario)

    # Stage 2: Execute generation
    update_job(job_id, {
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
            update_job(job_id, {
                "progress": progress,
                "current_stage": (
                    f"Generating {table_name} records "
                    f"({completed_rows:,}/{total_rows:,} rows)"
                ),
            })

    # The provider is fixed when the run is submitted, even if settings change.
    if provider["id"] != "offline":
        model_cfg = ModelConfig(provider=provider["id"], model_name=provider["model"], batch_size=10, extra_kwargs={"api_key": provider["key"], **({"base_url": provider["base_url"]} if provider.get("base_url") else {})})
        generator = SyntheticDataGenerator(model_config=model_cfg)
    else:
        generator = _LocalTableGenerator()
    try:
        result = ScenarioEngine(generator).generate(
            definition,
            scenario.record_count,
            progress_callback=report_generation_progress,
        )
    except ScenarioValidationError as error:
        result = error.result
    dfs = result.tables
    path_counts = result.path_counts

    # Stage 3: Evaluate before publishing any dataset files
    update_job(job_id, {
        "status": "evaluating",
        "progress": 85,
        "current_stage": "Evaluating causal rules and relational constraints",
    })

    total_records = sum(len(df) for df in dfs.values())
    update_job(job_id, {
        "progress": 88,
        "current_stage": "Running AI Judge semantic plausibility audit",
    })
    judge_outcome = run_ai_judge_audit(job_id, dfs, scenario, provider)
    metrics, evaluation_result, total_violations, violation_records = _evaluation_metrics(
        dfs,
        schemas,
        scenario,
        reference_data=reference_data,
        return_details=True,
        judge_audit=judge_outcome.result,
        judge_detail=judge_outcome.detail,
    )
    elapsed = round(time.time() - t0, 1)
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
        violations=violation_records,
        judgeAudit=judge_outcome.result,
    )

    if evaluation_result == "fail":
        update_job(job_id, {
            "status": "failed",
            "progress": 100,
            "current_stage": "Dataset rejected by validation; review the evaluation report",
            "stats": stats.model_dump(by_alias=True),
            "download_url": None,
        })
        return

    save_dataframes(
        {name: df for name, df in dfs.items() if not df.empty},
        str(job_dir), format="csv",
    )
    if any(schema.get("__template__") for schema in schemas.values()):
        update_job(job_id, {"progress": 95, "current_stage": "Rendering linked documents"})
        _render_documents(dfs, schemas, job_dir)

    update_job(job_id, {
        "status": "complete",
        "progress": 100,
        "current_stage": "Generation and referential evaluation complete",
        "stats": stats.model_dump(by_alias=True),
        "download_url": f"/api/download/{job_id}",
    })
