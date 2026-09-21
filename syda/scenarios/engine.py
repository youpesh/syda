"""Scenario orchestration built on top of Syda's existing table generator.

The engine plans workflow structure deterministically, delegates unconstrained field
synthesis to the configured Syda provider, and validates the merged result.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Union,
)

import pandas as pd

from ..output import save_dataframes
from ._utils import (
    _ROW_KEY_FIELD,
    _allocate_path_counts,
    _build_assignments,
    _field_type,
    _foreign_keys,
    _identity_fields,
    _known_record,
    _planned_identity,
    _primary_key_fields,
    _scenario_enrichment_prompt,
    _scenario_prompt,
    _scenario_row_keys,
    _schema_fields,
    _slugify,
    _split_reference,
    _values_equal,
)
from .models import (
    ScenarioDefinition,
    ScenarioGenerationResult,
    ScenarioPath,
    ScenarioPlan,
    ScenarioPlanSummary,
)


class TableGenerator(Protocol):
    """The existing Syda generator surface required by ``ScenarioEngine``."""

    def generate_for_schemas(
        self,
        schemas: Dict[str, Dict[str, Any]],
        prompts: Optional[Dict[str, str]] = None,
        sample_sizes: Optional[Dict[str, int]] = None,
        **kwargs: Any,
    ) -> Dict[str, pd.DataFrame]: ...


class ScenarioEngine:
    """Plan deterministic lifecycles, then enrich them with an existing generator."""

    def __init__(self, generator: TableGenerator):
        """Initialize the scenario engine.

        Args:
            generator: Table generator used to enrich ledger-owned rows.
        """
        self.generator = generator

    def generate(
        self,
        definition: ScenarioDefinition,
        instance_count: int,
        *,
        start_at: Optional[Union[date, datetime]] = None,
        prompts: Optional[Dict[str, str]] = None,
        output_dir: Optional[str | Path] = None,
        output_format: str = "csv",
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> ScenarioGenerationResult:
        """Generate ``instance_count`` complete workflows.

        ``instance_count`` counts business lifecycles, not rows. A step with one
        record per instance therefore receives ``instance_count`` rows, while a
        step with cardinality two receives twice that number.

        Args:
            definition: Scenario workflow to generate.
            instance_count: Number of complete workflow instances to generate.
            start_at: Optional date or datetime for the first workflow instance.
            prompts: Optional enrichment prompts keyed by table name.
            output_dir: Optional directory in which to save generated tables.
            output_format: Output format passed to Syda's table writer.
            generation_kwargs: Additional arguments for the table generator.

        Returns:
            Generated tables and their scenario allocation metadata.

        Raises:
            ValueError: If generation violates the scenario plan or integrity rules.
        """
        plan = self.plan(
            definition,
            instance_count,
            start_at=start_at,
        )
        enrichment_schemas = self.enrichment_schemas(definition, plan)
        kwargs = dict(generation_kwargs or {})
        kwargs.pop("output_dir", None)
        kwargs.pop("output_format", None)
        requested_batch_size = kwargs.pop("batch_size", None)
        context_batch_size = (
            50 if requested_batch_size is None else requested_batch_size
        )
        if context_batch_size < 1:
            raise ValueError("generation batch_size must be at least 1.")

        tables = {
            table: frame.copy().reset_index(drop=True).astype(object)
            for table, frame in plan.tables.items()
        }
        for step_index, step in enumerate(definition.steps):
            schema = enrichment_schemas.get(step.table)
            if not schema:
                continue

            row_keys = _scenario_row_keys(
                step.table,
                tables[step.table],
                definition.instance_id_field,
            )
            contexts = self._build_row_contexts(
                definition,
                tables,
                step_index,
                step.table,
                row_keys,
            )
            parts: List[pd.DataFrame] = []
            for start in range(0, len(contexts), context_batch_size):
                chunk_contexts = contexts[start : start + context_batch_size]
                chunk_keys = [item["rowKey"] for item in chunk_contexts]
                request_schema = {
                    _ROW_KEY_FIELD: {
                        "type": "text",
                        "description": (
                            "Copy the rowKey from the matching context exactly."
                        ),
                        "constraints": {"enum": chunk_keys},
                    },
                    **schema,
                }
                base_prompt = (
                    prompts[step.table]
                    if prompts and step.table in prompts
                    else _scenario_prompt(definition, step.table, plan.path_counts)
                )
                request_prompt = _scenario_enrichment_prompt(
                    base_prompt,
                    chunk_contexts,
                )
                request_kwargs = dict(kwargs)
                request_kwargs["batch_size"] = len(chunk_contexts)
                enriched = self.generator.generate_for_schemas(
                    schemas={step.table: request_schema},
                    prompts={step.table: request_prompt},
                    sample_sizes={step.table: len(chunk_contexts)},
                    **request_kwargs,
                )
                if step.table not in enriched:
                    raise ValueError(
                        f"Generator did not return scenario table '{step.table}'."
                    )
                part = enriched[step.table]
                self._validate_enrichment_chunk(
                    step.table,
                    schema,
                    chunk_keys,
                    part,
                )
                parts.append(part)

            generated = pd.concat(parts, ignore_index=True)
            tables[step.table] = self._merge_enrichment_table(
                definition,
                step.table,
                tables[step.table],
                schema,
                row_keys,
                generated,
            )

        self._apply_bindings(definition, tables)
        self._validate_result(definition, plan, tables)

        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            populated_tables = {
                table: frame for table, frame in tables.items() if not frame.empty
            }
            save_dataframes(populated_tables, str(output_dir), format=output_format)

        return ScenarioGenerationResult(
            tables=tables,
            instanceCount=plan.instance_count,
            instanceIds=plan.instance_ids,
            pathCounts=plan.path_counts,
        )

    def plan(
        self,
        definition: ScenarioDefinition,
        instance_count: int,
        *,
        start_at: Optional[Union[date, datetime]] = None,
    ) -> ScenarioPlan:
        """Build the deterministic scenario ledger without invoking a generator.

        Args:
            definition: Scenario workflow to plan.
            instance_count: Number of complete workflow instances to allocate.
            start_at: Optional date or datetime for the first workflow instance.

        Returns:
            A deterministic plan containing every ledger-owned value.

        Raises:
            ValueError: If ``instance_count`` is less than one.
        """
        if instance_count < 1:
            raise ValueError("instance_count must be at least 1.")

        paths = definition.resolved_paths()
        path_counts = _allocate_path_counts(paths, instance_count)
        assignments = _build_assignments(paths, path_counts)
        slug = _slugify(definition.name)
        instance_ids = [f"{slug}-{index + 1:06d}" for index in range(instance_count)]
        steps_by_name = {step.name: step for step in definition.steps}
        row_plans: Dict[str, List[Tuple[int, ScenarioPath, int]]] = {
            step.table: [] for step in definition.steps
        }
        for instance_index, path in enumerate(assignments):
            for step_name in path.steps:
                step = steps_by_name[step_name]
                for ordinal in range(step.records_per_instance):
                    row_plans[step.table].append((instance_index, path, ordinal))

        tables: Dict[str, pd.DataFrame] = {}
        for step in definition.steps:
            fields = list(_schema_fields(definition.schemas[step.table]))
            frame = pd.DataFrame(
                pd.NA,
                index=range(len(row_plans[step.table])),
                columns=[
                    *fields,
                    definition.instance_id_field,
                    definition.path_field,
                ],
                dtype=object,
            )
            frame[definition.instance_id_field] = [
                instance_ids[item[0]] for item in row_plans[step.table]
            ]
            frame[definition.path_field] = [
                item[1].name for item in row_plans[step.table]
            ]
            tables[step.table] = frame

        self._apply_identifiers(definition, tables)
        self._apply_values(definition, tables, row_plans)
        self._apply_timestamps(
            definition,
            tables,
            row_plans,
            start_at=start_at,
            instance_count=instance_count,
        )
        self._align_foreign_keys(definition, tables, row_plans)

        return ScenarioPlan(
            tables=tables,
            instanceCount=instance_count,
            instanceIds=instance_ids,
            pathCounts=path_counts,
            pathAssignments=[path.name for path in assignments],
            row_plans=row_plans,
        )

    @staticmethod
    def summarize(
        definition: ScenarioDefinition,
        instance_count: int,
    ) -> ScenarioPlanSummary:
        """Compute plan sizes and provider-owned fields without allocating rows.

        Args:
            definition: Scenario workflow to summarize.
            instance_count: Number of complete workflow instances to estimate.

        Returns:
            Allocation, row-count, and enrichment-field totals.

        Raises:
            ValueError: If ``instance_count`` is less than one.
        """
        if instance_count < 1:
            raise ValueError("instance_count must be at least 1.")

        paths = definition.resolved_paths()
        path_counts = _allocate_path_counts(paths, instance_count)
        identity_fields = _identity_fields(definition)
        binding_targets: Dict[str, Set[str]] = {}
        for binding in definition.bindings:
            for target in binding.targets:
                table, field = _split_reference(target)
                binding_targets.setdefault(table, set()).add(field)

        table_row_counts: Dict[str, int] = {}
        enrichment_fields: Dict[str, List[str]] = {}
        for step in definition.steps:
            active_paths = [
                path
                for path in paths
                if path_counts[path.name] > 0 and step.name in path.steps
            ]
            table_row_counts[step.table] = sum(
                path_counts[path.name] * step.records_per_instance
                for path in active_paths
            )

            planned = set(identity_fields[step.table])
            planned.update(_foreign_keys(definition.schemas[step.table]))
            planned.update(step.values)
            planned.update(binding_targets.get(step.table, set()))
            if step.timestamp_field:
                planned.add(step.timestamp_field)
            for field in _schema_fields(definition.schemas[step.table]):
                if active_paths and all(
                    field in step.values or field in path.overrides.get(step.name, {})
                    for path in active_paths
                ):
                    planned.add(field)
            enrichment_fields[step.table] = [
                field
                for field in _schema_fields(definition.schemas[step.table])
                if field not in planned and table_row_counts[step.table] > 0
            ]

        return ScenarioPlanSummary(
            instanceCount=instance_count,
            pathCounts=path_counts,
            tableRowCounts=table_row_counts,
            enrichmentFields=enrichment_fields,
        )

    @staticmethod
    def _apply_identifiers(
        definition: ScenarioDefinition,
        tables: Dict[str, pd.DataFrame],
    ) -> None:
        identity_fields = _identity_fields(definition)

        scenario_slug = _slugify(definition.name)
        for step in definition.steps:
            frame = tables[step.table]
            for field in identity_fields[step.table]:
                field_definition = definition.schemas[step.table].get(field, "text")
                frame[field] = [
                    _planned_identity(
                        field_definition,
                        scenario_slug,
                        step.table,
                        field,
                        row_index,
                    )
                    for row_index in range(len(frame))
                ]

    @staticmethod
    def enrichment_schemas(
        definition: ScenarioDefinition,
        plan: ScenarioPlan,
    ) -> Dict[str, Dict[str, Any]]:
        """Return fields that still require provider-generated enrichment.

        Args:
            definition: Scenario workflow associated with the plan.
            plan: Deterministic plan whose missing values require enrichment.

        Returns:
            Reduced schemas keyed by table name.
        """
        binding_targets: Dict[str, Set[str]] = {}
        for binding in definition.bindings:
            for target in binding.targets:
                table, field = _split_reference(target)
                binding_targets.setdefault(table, set()).add(field)

        schemas: Dict[str, Dict[str, Any]] = {}
        for step in definition.steps:
            ledger = plan.tables[step.table]
            if ledger.empty:
                continue
            source_schema = definition.schemas[step.table]
            fields = {
                field: field_definition
                for field, field_definition in _schema_fields(source_schema).items()
                if field not in binding_targets.get(step.table, set())
                and ledger[field].isna().any()
            }
            if not fields:
                continue
            metadata = {
                key: value
                for key, value in source_schema.items()
                if key in {"__description__", "__table_description__"}
            }
            schemas[step.table] = {**metadata, **fields}
        return schemas

    @staticmethod
    def _build_row_contexts(
        definition: ScenarioDefinition,
        tables: Mapping[str, pd.DataFrame],
        step_index: int,
        table: str,
        row_keys: Sequence[str],
    ) -> List[Dict[str, Any]]:
        instance_field = definition.instance_id_field
        path_field = definition.path_field
        frame = tables[table]
        contexts: List[Dict[str, Any]] = []

        for row_index, row_key in enumerate(row_keys):
            row = frame.iloc[row_index]
            instance_id = row[instance_field]
            known_records: Dict[str, List[Dict[str, Any]]] = {}
            for context_step in definition.steps[: step_index + 1]:
                context_frame = tables[context_step.table]
                matching = context_frame[context_frame[instance_field] == instance_id]
                records = [
                    _known_record(record, instance_field, path_field)
                    for _, record in matching.iterrows()
                ]
                if records:
                    known_records[context_step.table] = records

            contexts.append(
                {
                    "rowKey": row_key,
                    "scenarioInstanceId": instance_id,
                    "scenarioPath": row[path_field],
                    "currentTable": table,
                    "currentRow": _known_record(row, instance_field, path_field),
                    "knownRecords": known_records,
                }
            )
        return contexts

    @staticmethod
    def _validate_enrichment_chunk(
        table: str,
        schema: Mapping[str, Any],
        expected_keys: Sequence[str],
        frame: pd.DataFrame,
    ) -> None:
        required_fields = {_ROW_KEY_FIELD, *_schema_fields(schema)}
        missing_fields = required_fields - set(frame.columns)
        if missing_fields:
            raise ValueError(
                f"Generator omitted fields from '{table}': "
                f"{', '.join(sorted(missing_fields))}."
            )
        if len(frame) != len(expected_keys):
            raise ValueError(
                f"Generator returned {len(frame)} rows for '{table}', "
                f"expected {len(expected_keys)}."
            )

        actual_keys = frame[_ROW_KEY_FIELD].astype(str).tolist()
        duplicate_keys = sorted(
            {key for key in actual_keys if actual_keys.count(key) > 1}
        )
        if duplicate_keys:
            raise ValueError(
                f"Generator returned duplicate row keys for '{table}': "
                f"{', '.join(duplicate_keys)}."
            )
        if set(actual_keys) != set(expected_keys):
            missing = sorted(set(expected_keys) - set(actual_keys))
            unexpected = sorted(set(actual_keys) - set(expected_keys))
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected {', '.join(unexpected)}")
            raise ValueError(
                f"Generator returned invalid row keys for '{table}': "
                f"{'; '.join(details)}."
            )

    @staticmethod
    def _merge_enrichment_table(
        definition: ScenarioDefinition,
        table: str,
        ledger: pd.DataFrame,
        schema: Mapping[str, Any],
        row_keys: Sequence[str],
        generated: pd.DataFrame,
    ) -> pd.DataFrame:
        indexed = generated.copy().set_index(_ROW_KEY_FIELD, drop=True)
        indexed.index = indexed.index.astype(str)
        indexed = indexed.loc[list(row_keys)].reset_index(drop=True).astype(object)
        frame = ledger.copy().reset_index(drop=True).astype(object)

        for column in _schema_fields(schema):
            mask = frame[column].isna()
            frame.loc[mask, column] = indexed.loc[mask, column].to_numpy()

        declared = list(_schema_fields(definition.schemas[table]))
        metadata = [definition.instance_id_field, definition.path_field]
        return frame[[*declared, *metadata]]

    @staticmethod
    def _apply_values(
        definition: ScenarioDefinition,
        tables: Dict[str, pd.DataFrame],
        row_plans: Mapping[str, Sequence[Tuple[int, ScenarioPath, int]]],
    ) -> None:
        for step in definition.steps:
            frame = tables[step.table]
            for row_index, (_, path, _) in enumerate(row_plans[step.table]):
                values = dict(step.values)
                values.update(path.overrides.get(step.name, {}))
                for field, value in values.items():
                    if field not in definition.schemas[step.table]:
                        raise ValueError(
                            f"Scenario value targets unknown field "
                            f"'{step.table}.{field}'."
                        )
                    frame.at[row_index, field] = value

    @staticmethod
    def _apply_timestamps(
        definition: ScenarioDefinition,
        tables: Dict[str, pd.DataFrame],
        row_plans: Mapping[str, Sequence[Tuple[int, ScenarioPath, int]]],
        *,
        start_at: Optional[Union[date, datetime]],
        instance_count: int,
    ) -> None:
        default_offsets = [
            step.time_offset_days if step.time_offset_days is not None else step_index
            for step_index, step in enumerate(definition.steps)
            if step.timestamp_field
        ]
        recent_span_days = min(max(instance_count - 1, 0), 364)
        max_workflow_offset = max(default_offsets, default=0)
        base = start_at or (
            date.today() - timedelta(days=recent_span_days + max_workflow_offset)
        )
        if isinstance(base, date) and not isinstance(base, datetime):
            base_datetime = datetime.combine(base, time.min)
        else:
            base_datetime = base

        for step_index, step in enumerate(definition.steps):
            if not step.timestamp_field:
                continue
            field_type = _field_type(
                definition.schemas[step.table][step.timestamp_field]
            )
            offset_days = (
                step.time_offset_days
                if step.time_offset_days is not None
                else step_index
            )
            values: List[str] = []
            for instance_index, _, ordinal in row_plans[step.table]:
                instance_offset_days = (
                    instance_index if start_at is not None else instance_index % 365
                )
                value = base_datetime + timedelta(
                    days=instance_offset_days + offset_days,
                    minutes=ordinal,
                )
                values.append(
                    value.date().isoformat()
                    if field_type == "date"
                    else value.isoformat()
                )
            tables[step.table][step.timestamp_field] = values

    @staticmethod
    def _align_foreign_keys(
        definition: ScenarioDefinition,
        tables: Dict[str, pd.DataFrame],
        row_plans: Mapping[str, Sequence[Tuple[int, ScenarioPath, int]]],
    ) -> None:
        instance_field = definition.instance_id_field
        for step in definition.steps:
            child = tables[step.table]
            for child_field, (parent_table, parent_field) in _foreign_keys(
                definition.schemas[step.table]
            ).items():
                parent = tables.get(parent_table)
                if parent is None:
                    raise ValueError(
                        f"Scenario table '{step.table}' references missing parent "
                        f"table '{parent_table}'."
                    )
                if child_field not in child or parent_field not in parent:
                    raise ValueError(
                        f"Foreign key {step.table}.{child_field} -> "
                        f"{parent_table}.{parent_field} is missing from generated data."
                    )
                parent_rows = {
                    instance_id: group[parent_field].tolist()
                    for instance_id, group in parent.groupby(instance_field, sort=False)
                }
                occurrence: Dict[str, int] = {}
                for row_index, instance_id in enumerate(child[instance_field]):
                    candidates = parent_rows.get(instance_id, [])
                    if not candidates:
                        raise ValueError(
                            f"Scenario instance '{instance_id}' has a '{step.table}' "
                            f"record without a '{parent_table}' parent."
                        )
                    position = occurrence.get(instance_id, 0)
                    child.at[row_index, child_field] = candidates[
                        position % len(candidates)
                    ]
                    occurrence[instance_id] = position + 1

    @staticmethod
    def _apply_bindings(
        definition: ScenarioDefinition,
        tables: Dict[str, pd.DataFrame],
    ) -> None:
        instance_field = definition.instance_id_field
        for binding in definition.bindings:
            source_table, source_field = _split_reference(binding.source)
            source = tables[source_table]
            values = source.set_index(instance_field)[source_field].to_dict()
            for target in binding.targets:
                target_table, target_field = _split_reference(target)
                frame = tables[target_table]
                if not frame.empty:
                    missing_instances = sorted(set(frame[instance_field]) - set(values))
                    if missing_instances:
                        raise ValueError(
                            f"Binding '{binding.name}' has no source value for "
                            f"scenario instances: {', '.join(missing_instances)}."
                        )
                    frame[target_field] = pd.Series(
                        [values[instance_id] for instance_id in frame[instance_field]],
                        index=frame.index,
                        dtype=object,
                    )
                    if frame[target_field].isna().any():
                        raise ValueError(
                            f"Binding '{binding.name}' produced missing values for "
                            f"target '{target}'."
                        )

    @staticmethod
    def _validate_result(
        definition: ScenarioDefinition,
        plan: ScenarioPlan,
        tables: Mapping[str, pd.DataFrame],
    ) -> None:
        """Reject output that violates ledger-owned structural invariants."""
        errors: List[str] = []
        instance_field = definition.instance_id_field
        path_field = definition.path_field

        for step in definition.steps:
            table = step.table
            expected = plan.tables[table].reset_index(drop=True)
            actual = tables.get(table)
            if actual is None:
                errors.append(f"missing table '{table}'")
                continue
            actual = actual.reset_index(drop=True)
            if len(actual) != len(expected):
                errors.append(
                    f"table '{table}' has {len(actual)} rows; expected {len(expected)}"
                )
                continue

            for column in expected.columns:
                if column not in actual:
                    errors.append(f"table '{table}' is missing column '{column}'")
                    continue
                planned = expected[column]
                for row_index in planned[planned.notna()].index:
                    if not _values_equal(
                        actual.at[row_index, column], planned.at[row_index]
                    ):
                        errors.append(
                            f"table '{table}' row {row_index + 1} changed "
                            f"ledger field '{column}'"
                        )
                        break

            for primary_key in _primary_key_fields(definition.schemas[table]):
                if actual[primary_key].isna().any():
                    errors.append(
                        f"table '{table}' has missing primary key '{primary_key}'"
                    )
                if actual[primary_key].duplicated().any():
                    errors.append(
                        f"table '{table}' has duplicate primary key '{primary_key}'"
                    )

            if not actual.empty:
                valid_paths = {
                    path.name
                    for path in definition.resolved_paths()
                    if step.name in path.steps
                }
                unexpected_paths = set(actual[path_field]) - valid_paths
                if unexpected_paths:
                    errors.append(
                        f"table '{table}' contains invalid paths: "
                        f"{', '.join(sorted(unexpected_paths))}"
                    )

        for step in definition.steps:
            child = tables[step.table]
            for child_field, (parent_table, parent_field) in _foreign_keys(
                definition.schemas[step.table]
            ).items():
                parent = tables[parent_table]
                if parent[parent_field].isna().any():
                    errors.append(
                        f"referenced parent field {parent_table}.{parent_field} "
                        "contains missing values"
                    )
                if parent[parent_field].duplicated().any():
                    errors.append(
                        f"referenced parent field {parent_table}.{parent_field} "
                        "contains duplicate values"
                    )
                parent_instances: Dict[Any, Set[str]] = {}
                for _, parent_row in parent.iterrows():
                    parent_instances.setdefault(parent_row[parent_field], set()).add(
                        parent_row[instance_field]
                    )
                for _, child_row in child.iterrows():
                    matching_instances = parent_instances.get(
                        child_row[child_field], set()
                    )
                    if child_row[instance_field] not in matching_instances:
                        errors.append(
                            f"foreign key {step.table}.{child_field} does not resolve "
                            "within its scenario instance"
                        )
                        break

        for binding in definition.bindings:
            source_table, source_field = _split_reference(binding.source)
            source_values = (
                tables[source_table].set_index(instance_field)[source_field].to_dict()
            )
            for target in binding.targets:
                target_table, target_field = _split_reference(target)
                for _, target_row in tables[target_table].iterrows():
                    expected_value = source_values.get(
                        target_row[instance_field], pd.NA
                    )
                    if not _values_equal(target_row[target_field], expected_value):
                        errors.append(
                            f"binding '{binding.name}' does not match target '{target}'"
                        )
                        break

        for instance_id in plan.instance_ids:
            previous_max: Optional[pd.Timestamp] = None
            previous_step: Optional[str] = None
            for step in definition.steps:
                if not step.timestamp_field:
                    continue
                rows = tables[step.table]
                rows = rows[rows[instance_field] == instance_id]
                if rows.empty:
                    continue
                timestamps = pd.to_datetime(rows[step.timestamp_field], errors="coerce")
                if timestamps.isna().any():
                    errors.append(
                        f"scenario '{instance_id}' has invalid timestamps in "
                        f"step '{step.name}'"
                    )
                    continue
                current_min = timestamps.min()
                current_max = timestamps.max()
                if previous_max is not None and current_min <= previous_max:
                    errors.append(
                        f"scenario '{instance_id}' step '{step.name}' does not occur "
                        f"after '{previous_step}'"
                    )
                previous_max = current_max
                previous_step = step.name

        if errors:
            details = "; ".join(errors[:10])
            if len(errors) > 10:
                details += f"; and {len(errors) - 10} more"
            raise ValueError(f"Scenario integrity validation failed: {details}.")
