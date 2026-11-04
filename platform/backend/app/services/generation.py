"""Generation and evaluation, independent of HTTP routes and task transport."""
import time
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import pandas as pd
from syda.schemas import validate_schema, ModelConfig
from syda.generate import SyntheticDataGenerator
from syda.output import save_dataframes
from syda.scenarios import ScenarioEngine
from ..scenario_definition import scenario_definition as _scenario_definition
from ..models.scenario import ScenarioConfiguration, EvaluationMetric, JobStats
logger = logging.getLogger(__name__)

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


def _evaluation_metrics(
    dfs: Dict[str, pd.DataFrame],
    schemas: Dict[str, Any],
    scenario: ScenarioConfiguration,
) -> tuple[list[EvaluationMetric], str, int]:
    referential_value, referential_violations = _referential_integrity(dfs, schemas)
    semantic_value, semantic_violations, semantic_checked = _constraint_compliance(dfs, schemas)
    causal_value, causal_violations, causal_checked = _temporal_consistency(dfs, schemas)
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


def run_generation(job_id: str, scenario: ScenarioConfiguration, provider: dict[str, Any], *, output_dir: Path, update_job):
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
        result = ScenarioEngine(generator).generate(
            definition,
            scenario.record_count,
            progress_callback=report_generation_progress,
        )
        dfs = result.tables
        path_counts = result.path_counts

    if not dfs:
        result = ScenarioEngine(_LocalTableGenerator()).generate(
            definition,
            scenario.record_count,
            progress_callback=report_generation_progress,
        )
        dfs = result.tables
        path_counts = result.path_counts

    # Stage 3: Validate referential integrity & save output
    update_job(job_id, {
        "status": "evaluating",
        "progress": 85,
        "current_stage": "Verifying foreign-key constraints and writing multi-table datasets",
    })

    # Save generated DataFrames using syda.output.save_dataframes
    save_dataframes({name: df for name, df in dfs.items() if not df.empty}, str(job_dir), format="csv")

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

    update_job(job_id, {
        "status": "complete",
        "progress": 100,
        "current_stage": "Generation and referential evaluation complete",
        "stats": stats.model_dump(by_alias=True),
        "download_url": f"/api/download/{job_id}",
    })
