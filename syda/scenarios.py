"""Scenario-driven generation built on top of Syda's table generator.

The scenario layer turns independent table outputs into complete business-workflow
instances. It deliberately delegates field synthesis to ``SyntheticDataGenerator``
and owns only scenario concerns: path selection, per-instance row cardinality,
cross-table identity, foreign-key alignment, shared values, and workflow time order.
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
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .output import save_dataframes


class ScenarioStep(BaseModel):
    """One table-producing step in a scenario workflow."""

    name: str
    table: str
    records_per_instance: int = Field(default=1, alias="recordsPerInstance", ge=1)
    timestamp_field: Optional[str] = Field(default=None, alias="timestampField")
    time_offset_days: Optional[int] = Field(default=None, alias="timeOffsetDays")
    values: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class ScenarioPath(BaseModel):
    """A weighted workflow branch, such as approved or denied claims."""

    name: str
    steps: List[str]
    weight: float = Field(default=1.0, gt=0)
    overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class ScenarioBinding(BaseModel):
    """Copy one generated scenario value to other fields in the same instance."""

    name: str
    source: str
    targets: List[str]


class ScenarioDefinition(BaseModel):
    """Declarative definition of a multi-table business workflow."""

    name: str
    description: str = ""
    schemas: Dict[str, Dict[str, Any]]
    steps: List[ScenarioStep]
    paths: List[ScenarioPath] = Field(default_factory=list)
    bindings: List[ScenarioBinding] = Field(default_factory=list)
    instance_id_field: str = Field(
        default="scenario_instance_id", alias="instanceIdField"
    )
    path_field: str = Field(default="scenario_path", alias="pathField")

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    @model_validator(mode="after")
    def validate_workflow(self) -> "ScenarioDefinition":
        if not self.steps:
            raise ValueError("A scenario requires at least one workflow step.")

        step_names = [step.name for step in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("Scenario step names must be unique.")

        table_names = [step.table for step in self.steps]
        if len(table_names) != len(set(table_names)):
            raise ValueError("Each scenario step must produce a distinct table.")

        for step in self.steps:
            if step.table not in self.schemas:
                raise ValueError(
                    f"Scenario step '{step.name}' references unknown table "
                    f"'{step.table}'."
                )
            if (
                step.timestamp_field
                and step.timestamp_field not in self.schemas[step.table]
            ):
                raise ValueError(
                    f"Timestamp field '{step.timestamp_field}' is not present in "
                    f"table '{step.table}'."
                )
            unknown_values = set(step.values) - set(
                _schema_fields(self.schemas[step.table])
            )
            if unknown_values:
                raise ValueError(
                    f"Scenario step '{step.name}' sets unknown fields: "
                    f"{', '.join(sorted(unknown_values))}."
                )
            for child_field, (parent_table, parent_field) in _foreign_keys(
                self.schemas[step.table]
            ).items():
                if child_field not in _schema_fields(self.schemas[step.table]):
                    raise ValueError(
                        f"Foreign key field '{step.table}.{child_field}' does not exist."
                    )
                if (
                    parent_table not in self.schemas
                    or parent_field not in _schema_fields(self.schemas[parent_table])
                ):
                    raise ValueError(
                        f"Foreign key target '{parent_table}.{parent_field}' does not exist."
                    )

        paths = self.paths or [
            ScenarioPath(name="default", steps=step_names, weight=1.0)
        ]
        known_steps = set(step_names)
        step_position = {name: index for index, name in enumerate(step_names)}
        step_by_table = {step.table: step.name for step in self.steps}

        path_names = [path.name for path in paths]
        if len(path_names) != len(set(path_names)):
            raise ValueError("Scenario path names must be unique.")

        for path in paths:
            unknown = set(path.steps) - known_steps
            if unknown:
                raise ValueError(
                    f"Scenario path '{path.name}' references unknown steps: "
                    f"{', '.join(sorted(unknown))}."
                )
            positions = [step_position[name] for name in path.steps]
            if positions != sorted(positions):
                raise ValueError(
                    f"Scenario path '{path.name}' does not follow workflow order."
                )

            included = set(path.steps)
            for step_name in path.steps:
                step = self.steps[step_position[step_name]]
                for _, (parent_table, _) in _foreign_keys(
                    self.schemas[step.table]
                ).items():
                    parent_step = step_by_table.get(parent_table)
                    if parent_step and parent_step not in included:
                        raise ValueError(
                            f"Scenario path '{path.name}' includes '{step_name}' "
                            f"without required parent step '{parent_step}'."
                        )
                    if (
                        parent_step
                        and step_position[parent_step] > step_position[step_name]
                    ):
                        raise ValueError(
                            f"Scenario step '{step_name}' depends on later step "
                            f"'{parent_step}'."
                        )

            for override_step, values in path.overrides.items():
                if override_step not in included:
                    raise ValueError(
                        f"Scenario path '{path.name}' overrides excluded step "
                        f"'{override_step}'."
                    )
                step = self.steps[step_position[override_step]]
                unknown_values = set(values) - set(
                    _schema_fields(self.schemas[step.table])
                )
                if unknown_values:
                    raise ValueError(
                        f"Scenario path '{path.name}' overrides unknown fields on "
                        f"'{override_step}': {', '.join(sorted(unknown_values))}."
                    )

        for binding in self.bindings:
            _validate_field_reference(binding.source, self.schemas)
            for target in binding.targets:
                _validate_field_reference(target, self.schemas)

        return self

    def resolved_paths(self) -> List[ScenarioPath]:
        if self.paths:
            return self.paths
        return [
            ScenarioPath(
                name="default",
                steps=[step.name for step in self.steps],
                weight=1.0,
            )
        ]


class ScenarioGenerationResult(BaseModel):
    """Generated tables and the scenario-instance distribution behind them."""

    tables: Dict[str, Any]
    instance_count: int = Field(alias="instanceCount")
    instance_ids: List[str] = Field(alias="instanceIds")
    path_counts: Dict[str, int] = Field(alias="pathCounts")

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScenarioPlan(BaseModel):
    """Deterministic ledger that generation must enrich without contradicting."""

    tables: Dict[str, Any]
    instance_count: int = Field(alias="instanceCount")
    instance_ids: List[str] = Field(alias="instanceIds")
    path_counts: Dict[str, int] = Field(alias="pathCounts")
    path_assignments: List[str] = Field(alias="pathAssignments")
    row_plans: Dict[str, Any] = Field(exclude=True, repr=False)

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScenarioPlanSummary(BaseModel):
    """Allocation and enrichment counts without materializing ledger rows."""

    instance_count: int = Field(alias="instanceCount")
    path_counts: Dict[str, int] = Field(alias="pathCounts")
    table_row_counts: Dict[str, int] = Field(alias="tableRowCounts")
    enrichment_fields: Dict[str, List[str]] = Field(alias="enrichmentFields")

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


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
        """
        plan = self.plan(
            definition,
            instance_count,
            start_at=start_at,
        )
        enrichment_schemas = self.enrichment_schemas(definition, plan)
        sample_sizes = {table: len(plan.tables[table]) for table in enrichment_schemas}
        scenario_prompts = {
            table: (
                prompts.get(table)
                if prompts and table in prompts
                else _scenario_prompt(definition, table, plan.path_counts)
            )
            for table in enrichment_schemas
        }
        kwargs = dict(generation_kwargs or {})
        kwargs.pop("output_dir", None)
        kwargs.pop("output_format", None)
        generated: Dict[str, pd.DataFrame] = {
            table: pd.DataFrame(index=range(len(frame)))
            for table, frame in plan.tables.items()
        }
        if enrichment_schemas:
            enriched = self.generator.generate_for_schemas(
                schemas=enrichment_schemas,
                prompts=scenario_prompts,
                sample_sizes=sample_sizes,
                **kwargs,
            )
            missing = set(enrichment_schemas) - set(enriched)
            if missing:
                raise ValueError(
                    "Generator did not return scenario tables: "
                    f"{', '.join(sorted(missing))}."
                )
            for table, schema in enrichment_schemas.items():
                missing_fields = set(_schema_fields(schema)) - set(
                    enriched[table].columns
                )
                if missing_fields:
                    raise ValueError(
                        f"Generator omitted fields from '{table}': "
                        f"{', '.join(sorted(missing_fields))}."
                    )
            generated.update(enriched)
        tables = self._enrich_plan(definition, plan, generated)
        self._apply_bindings(definition, tables)

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
        """Build the deterministic scenario ledger without invoking a generator."""
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
        """Compute plan sizes and provider-owned fields without allocating rows."""
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
        """Return only fields that still require provider-generated enrichment."""
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
    def _enrich_plan(
        definition: ScenarioDefinition,
        plan: ScenarioPlan,
        generated: Mapping[str, pd.DataFrame],
    ) -> Dict[str, pd.DataFrame]:
        tables: Dict[str, pd.DataFrame] = {}
        for step in definition.steps:
            ledger = plan.tables[step.table]
            if ledger.empty:
                tables[step.table] = ledger.copy()
                continue
            if step.table not in generated:
                raise ValueError(
                    f"Generator did not return scenario table '{step.table}'."
                )
            frame = generated[step.table].copy().reset_index(drop=True).astype(object)
            if len(frame) != len(ledger):
                raise ValueError(
                    f"Generator returned {len(frame)} rows for '{step.table}', "
                    f"expected {len(ledger)}."
                )

            for column in ledger.columns:
                if column not in frame:
                    frame[column] = pd.NA
                planned = ledger[column]
                mask = planned.notna()
                frame.loc[mask, column] = planned.loc[mask].to_numpy()

            declared = list(_schema_fields(definition.schemas[step.table]))
            metadata = [definition.instance_id_field, definition.path_field]
            tables[step.table] = frame[[*declared, *metadata]]
        return tables

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
            values = (
                source.groupby(instance_field, sort=False)[source_field]
                .first()
                .to_dict()
            )
            for target in binding.targets:
                target_table, target_field = _split_reference(target)
                frame = tables[target_table]
                if not frame.empty:
                    frame[target_field] = (
                        frame[instance_field].map(values).astype(object)
                    )


def _allocate_path_counts(
    paths: Sequence[ScenarioPath], instance_count: int
) -> Dict[str, int]:
    total_weight = sum(path.weight for path in paths)
    raw = {path.name: instance_count * path.weight / total_weight for path in paths}
    counts = {path.name: int(raw[path.name]) for path in paths}
    remaining = instance_count - sum(counts.values())

    ranked = sorted(
        enumerate(paths),
        key=lambda item: (raw[item[1].name] - int(raw[item[1].name]), -item[0]),
        reverse=True,
    )
    for index in range(remaining):
        counts[ranked[index][1].name] += 1

    if instance_count >= len(paths):
        for path in paths:
            if counts[path.name] > 0:
                continue
            donor = max(paths, key=lambda candidate: counts[candidate.name])
            counts[donor.name] -= 1
            counts[path.name] = 1
    return counts


def _build_assignments(
    paths: Sequence[ScenarioPath], path_counts: Mapping[str, int]
) -> List[ScenarioPath]:
    assignments: List[ScenarioPath] = []
    remaining = dict(path_counts)
    while any(count > 0 for count in remaining.values()):
        for path in paths:
            if remaining[path.name] > 0:
                assignments.append(path)
                remaining[path.name] -= 1
    return assignments


def _foreign_keys(schema: Mapping[str, Any]) -> Dict[str, Tuple[str, str]]:
    result: Dict[str, Tuple[str, str]] = {}
    declared = schema.get("__foreign_keys__", {})
    if isinstance(declared, Mapping):
        for field, target in declared.items():
            parsed = _parse_foreign_key_target(target)
            if parsed:
                result[field] = parsed

    for field, definition in _schema_fields(schema).items():
        if not isinstance(definition, Mapping):
            continue
        references = definition.get("references")
        parsed = _parse_foreign_key_target(references)
        if parsed:
            result.setdefault(field, parsed)
    return result


def _parse_foreign_key_target(target: Any) -> Optional[Tuple[str, str]]:
    if isinstance(target, str) and "." in target:
        table, field = target.split(".", 1)
        return table, field
    if isinstance(target, (list, tuple)) and len(target) == 2:
        return str(target[0]), str(target[1])
    if isinstance(target, Mapping):
        if "schema" in target and "field" in target:
            return str(target["schema"]), str(target["field"])
        nested = target.get("references")
        if nested is not None:
            return _parse_foreign_key_target(nested)
    return None


def _schema_fields(schema: Mapping[str, Any]) -> Dict[str, Any]:
    return {name: value for name, value in schema.items() if not name.startswith("__")}


def _primary_key_fields(schema: Mapping[str, Any]) -> List[str]:
    fields = []
    for name, definition in _schema_fields(schema).items():
        if not isinstance(definition, Mapping):
            continue
        constraints = definition.get("constraints", {})
        if definition.get("primary_key") or (
            isinstance(constraints, Mapping) and constraints.get("primary_key")
        ):
            fields.append(name)
    return fields


def _identity_fields(definition: ScenarioDefinition) -> Dict[str, Set[str]]:
    fields = {
        step.table: set(_primary_key_fields(definition.schemas[step.table]))
        for step in definition.steps
    }
    for step in definition.steps:
        for _, (parent_table, parent_field) in _foreign_keys(
            definition.schemas[step.table]
        ).items():
            if parent_table in fields:
                fields[parent_table].add(parent_field)
    return fields


def _field_type(definition: Any) -> str:
    if isinstance(definition, str):
        return definition.lower()
    if isinstance(definition, Mapping):
        return str(definition.get("type", "text")).lower()
    return "text"


def _planned_identity(
    definition: Any,
    scenario_slug: str,
    table: str,
    field: str,
    row_index: int,
) -> Any:
    field_type = _field_type(definition)
    sequence = row_index + 1
    if field_type in {"integer", "int", "number", "float"}:
        return sequence
    seed = f"{scenario_slug}:{table}:{field}:{sequence}"
    if field_type == "uuid":
        return str(uuid5(NAMESPACE_URL, seed))
    return f"{scenario_slug}-{_slugify(table)}-{_slugify(field)}-{sequence:06d}"


def _split_reference(reference: str) -> Tuple[str, str]:
    if "." not in reference:
        raise ValueError(
            f"Scenario field reference '{reference}' must use 'Table.field'."
        )
    table, field = reference.split(".", 1)
    return table, field


def _validate_field_reference(
    reference: str, schemas: Mapping[str, Mapping[str, Any]]
) -> None:
    table, field = _split_reference(reference)
    if table not in schemas or field not in _schema_fields(schemas[table]):
        raise ValueError(f"Unknown scenario field reference '{reference}'.")


def _slugify(value: str) -> str:
    slug = "".join(
        character.lower() if character.isalnum() else "-" for character in value
    )
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "scenario"


def _scenario_prompt(
    definition: ScenarioDefinition,
    table: str,
    path_counts: Mapping[str, int],
) -> str:
    summary = definition.description or definition.name
    distribution = ", ".join(f"{path}={count}" for path, count in path_counts.items())
    return (
        f"Generate realistic synthetic data for the '{table}' step of the "
        f"'{definition.name}' business scenario. Scenario: {summary}. "
        f"The deterministic scenario ledger uses these paths: {distribution}. "
        "Focus on realistic descriptive values; structural identifiers, foreign "
        "keys, statuses, and workflow timestamps are enforced by the ledger."
    )
