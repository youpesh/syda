"""Pydantic models for scenario-driven generation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._utils import (
    _ROW_KEY_FIELD,
    _foreign_keys,
    _schema_fields,
    _split_reference,
    _validate_field_reference,
)

_SCENARIO_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    populate_by_name=True,
    serialize_by_alias=True,
)


class ScenarioStep(BaseModel):
    """One table-producing step in a scenario workflow.

    Attributes:
        name: Unique step name used by paths and overrides.
        table: Schema and output-table name produced by the step.
        records_per_instance: Number of rows produced for each scenario instance.
        timestamp_field: Optional field owned by the ledger for workflow ordering.
        time_offset_days: Optional day offset from the scenario start date.
        values: Fixed field values applied to every row produced by the step.
    """

    name: str = Field(min_length=1)
    table: str = Field(min_length=1)
    records_per_instance: int = Field(default=1, alias="recordsPerInstance", ge=1)
    timestamp_field: Optional[str] = Field(default=None, alias="timestampField")
    time_offset_days: Optional[int] = Field(default=None, alias="timeOffsetDays")
    values: Dict[str, Any] = Field(default_factory=dict)

    model_config = _SCENARIO_MODEL_CONFIG


class ScenarioPath(BaseModel):
    """A weighted workflow branch, such as approved or denied claims.

    Attributes:
        name: Unique path name written to generated rows.
        steps: Ordered step names included in the path.
        weight: Relative allocation weight for this path.
        overrides: Per-step field values that override step defaults.
    """

    name: str = Field(min_length=1)
    steps: List[str] = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)
    overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    model_config = _SCENARIO_MODEL_CONFIG


class ScenarioBinding(BaseModel):
    """Copy one generated value to other fields in the same scenario instance.

    Attributes:
        name: Unique binding name used in validation errors.
        source: Source field reference in ``Table.field`` form.
        targets: Target field references in ``Table.field`` form.
    """

    name: str = Field(min_length=1)
    source: str
    targets: List[str] = Field(min_length=1)

    model_config = _SCENARIO_MODEL_CONFIG


class ScenarioDefinition(BaseModel):
    """Declarative definition of a multi-table business workflow.

    Attributes:
        name: Human-readable scenario name.
        description: Optional scenario context supplied during enrichment.
        schemas: Syda schemas keyed by table name.
        steps: Workflow steps in dependency order.
        paths: Weighted branches through the workflow.
        bindings: Values copied between tables within each scenario instance.
        instance_id_field: Output column containing the scenario instance ID.
        path_field: Output column containing the assigned path name.
    """

    name: str = Field(min_length=1)
    description: str = ""
    schemas: Dict[str, Dict[str, Any]]
    steps: List[ScenarioStep]
    paths: List[ScenarioPath] = Field(default_factory=list)
    bindings: List[ScenarioBinding] = Field(default_factory=list)
    instance_id_field: str = Field(
        default="scenario_instance_id", alias="instanceIdField"
    )
    path_field: str = Field(default="scenario_path", alias="pathField")

    model_config = _SCENARIO_MODEL_CONFIG

    @model_validator(mode="after")
    def validate_workflow(self) -> "ScenarioDefinition":
        """Validate workflow references, ordering, ownership, and bindings.

        Returns:
            The validated scenario definition.

        Raises:
            ValueError: If the workflow contains an invalid or ambiguous rule.
        """
        if not self.steps:
            raise ValueError("A scenario requires at least one workflow step.")

        step_names = [step.name for step in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("Scenario step names must be unique.")

        table_names = [step.table for step in self.steps]
        if len(table_names) != len(set(table_names)):
            raise ValueError("Each scenario step must produce a distinct table.")

        if self.instance_id_field == self.path_field:
            raise ValueError("Scenario instance and path fields must be distinct.")

        reserved_fields = {
            self.instance_id_field,
            self.path_field,
            _ROW_KEY_FIELD,
        }
        for table, schema in self.schemas.items():
            collisions = reserved_fields & set(_schema_fields(schema))
            if collisions:
                raise ValueError(
                    f"Schema '{table}' uses reserved scenario fields: "
                    f"{', '.join(sorted(collisions))}."
                )

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
                        f"Foreign key field '{step.table}.{child_field}' "
                        "does not exist."
                    )
                if (
                    parent_table not in self.schemas
                    or parent_field not in _schema_fields(self.schemas[parent_table])
                ):
                    raise ValueError(
                        f"Foreign key target '{parent_table}.{parent_field}' "
                        "does not exist."
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
            if len(path.steps) != len(set(path.steps)):
                raise ValueError(
                    f"Scenario path '{path.name}' contains duplicate steps."
                )
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

        timestamp_offsets = [
            (
                step.name,
                (
                    step.time_offset_days
                    if step.time_offset_days is not None
                    else step_index
                ),
            )
            for step_index, step in enumerate(self.steps)
            if step.timestamp_field
        ]
        for previous, current in zip(timestamp_offsets, timestamp_offsets[1:]):
            if current[1] <= previous[1]:
                raise ValueError(
                    "Scenario timestamp offsets must increase with workflow order: "
                    f"'{current[0]}' must occur after '{previous[0]}'."
                )

        binding_names = [binding.name for binding in self.bindings]
        if len(binding_names) != len(set(binding_names)):
            raise ValueError("Scenario binding names must be unique.")

        bound_targets: Set[str] = set()
        for binding in self.bindings:
            _validate_field_reference(binding.source, self.schemas)
            source_table, _ = _split_reference(binding.source)
            source_step = step_by_table.get(source_table)
            if source_step is None:
                raise ValueError(
                    f"Binding '{binding.name}' source table '{source_table}' "
                    "is not a scenario step."
                )
            source_definition = self.steps[step_position[source_step]]
            if source_definition.records_per_instance != 1:
                raise ValueError(
                    f"Binding '{binding.name}' has an ambiguous multi-row source "
                    f"'{binding.source}'."
                )
            for target in binding.targets:
                _validate_field_reference(target, self.schemas)
                if target in bound_targets:
                    raise ValueError(
                        f"Scenario field '{target}' is targeted by multiple bindings."
                    )
                bound_targets.add(target)
                target_table, _ = _split_reference(target)
                target_step = step_by_table.get(target_table)
                if target_step is None:
                    raise ValueError(
                        f"Binding '{binding.name}' target table '{target_table}' "
                        "is not a scenario step."
                    )
                if step_position[source_step] > step_position[target_step]:
                    raise ValueError(
                        f"Binding '{binding.name}' source '{source_step}' must not "
                        f"come after target '{target_step}'."
                    )
                for path in paths:
                    if target_step in path.steps and source_step not in path.steps:
                        raise ValueError(
                            f"Scenario path '{path.name}' includes binding target "
                            f"'{target_step}' without source step '{source_step}'."
                        )

        return self

    def resolved_paths(self) -> List[ScenarioPath]:
        """Return declared paths or a default path containing every step.

        Returns:
            Paths used for scenario allocation.
        """
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
    """Generated tables and the scenario-instance distribution behind them.

    Attributes:
        tables: Generated data frames keyed by table name.
        instance_count: Number of complete business lifecycles generated.
        instance_ids: Stable identifiers assigned to scenario instances.
        path_counts: Exact number of instances allocated to each path.
    """

    tables: Dict[str, Any]
    instance_count: int = Field(alias="instanceCount")
    instance_ids: List[str] = Field(alias="instanceIds")
    path_counts: Dict[str, int] = Field(alias="pathCounts")

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScenarioPlan(BaseModel):
    """Deterministic ledger that generation must enrich without contradicting.

    Attributes:
        tables: Planned data frames containing ledger-owned values.
        instance_count: Number of planned scenario instances.
        instance_ids: Stable identifiers assigned to scenario instances.
        path_counts: Exact number of instances allocated to each path.
        path_assignments: Path name assigned to each scenario instance.
        row_plans: Internal row-allocation metadata used during generation.
    """

    tables: Dict[str, Any]
    instance_count: int = Field(alias="instanceCount")
    instance_ids: List[str] = Field(alias="instanceIds")
    path_counts: Dict[str, int] = Field(alias="pathCounts")
    path_assignments: List[str] = Field(alias="pathAssignments")
    row_plans: Dict[str, Any] = Field(exclude=True, repr=False)

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScenarioPlanSummary(BaseModel):
    """Allocation and enrichment counts without materializing ledger rows.

    Attributes:
        instance_count: Number of scenario instances summarized.
        path_counts: Exact number of instances allocated to each path.
        table_row_counts: Planned row count for each output table.
        enrichment_fields: Provider-owned fields for each output table.
    """

    instance_count: int = Field(alias="instanceCount")
    path_counts: Dict[str, int] = Field(alias="pathCounts")
    table_row_counts: Dict[str, int] = Field(alias="tableRowCounts")
    enrichment_fields: Dict[str, List[str]] = Field(alias="enrichmentFields")

    model_config = _SCENARIO_MODEL_CONFIG
