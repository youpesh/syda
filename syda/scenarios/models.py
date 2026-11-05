"""Pydantic models for scenario-driven generation."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._utils import (
    _ROW_KEY_FIELD,
    _foreign_keys,
    _identity_fields,
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


class ViolationReport(BaseModel):
    """Structured report of a causal or semantic rule violation."""

    table: str = Field(description="Table name where the violation occurred")
    row: Any = Field(description="Row index, primary key, or scenario instance ID")
    rule: str = Field(description="Name of the violated rule or check")
    error: str = Field(description="Detailed error message explaining the violation")

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScenarioCheck(BaseModel):
    """Machine-checkable rule that Syda can enforce or evaluate after generation.

    Kinds:
        allowed_values: Constrains one field to an allowed set of values.
        temporal_order: Requires earlier timestamp to precede later timestamp,
                        or an ordered chain of timestamps.
        absence_by_value: Forbids a related record when a source field has a specific value.
        state_transition: Validates legal state transitions across rows or fields.
        semantic_invariant: Conditional business constraint (e.g. status == 'paid' => amount > 0).
        custom_rule: Custom expression evaluated against records.
    """

    name: str = Field(min_length=1)
    kind: Literal[
        "allowed_values",
        "temporal_order",
        "absence_by_value",
        "state_transition",
        "state_machine",
        "semantic_invariant",
        "custom_rule",
    ]
    table: Optional[str] = None
    field: Optional[str] = None
    values: List[Any] = Field(default_factory=list)
    before: Optional[str] = None
    after: Optional[str] = None
    chain: Optional[List[str]] = None
    strict: bool = False
    source: Optional[str] = None
    value: Any = None
    absent_table: Optional[str] = None
    transitions: Optional[Dict[str, List[str]]] = None
    initial_states: Optional[List[str]] = None
    terminal_states: Optional[List[str]] = None
    previous_field: Optional[str] = None
    order_by: Optional[str] = None
    condition: Optional[str] = None
    assertion: Optional[str] = None
    expression: Optional[str] = None

    model_config = _SCENARIO_MODEL_CONFIG

    @field_validator("transitions", mode="before")
    @classmethod
    def normalize_transitions(cls, val: Any) -> Any:
        if isinstance(val, dict):
            normalized: Dict[str, List[str]] = {}
            for k, v in val.items():
                if isinstance(v, (list, tuple, set)):
                    normalized[str(k)] = [str(x) for x in v]
                elif v is not None:
                    normalized[str(k)] = [str(v)]
                else:
                    normalized[str(k)] = []
            return normalized
        return val

    @model_validator(mode="after")
    def validate_check_shape(self) -> "ScenarioCheck":
        if self.kind == "allowed_values":
            if not self.table or not self.field or not self.values:
                raise ValueError(
                    "allowed_values checks require table, field, and at least one value."
                )
        elif self.kind == "temporal_order":
            if self.chain is not None:
                if len(self.chain) < 2:
                    raise ValueError(
                        "temporal_order checks with chain require at least two field references."
                    )
            elif not self.before or not self.after or "." not in self.before or "." not in self.after:
                raise ValueError(
                    "temporal_order checks require before and after as Table.field references, or a chain."
                )
        elif self.kind == "absence_by_value":
            if not self.source or "." not in self.source or not self.absent_table:
                raise ValueError(
                    "absence_by_value checks require source as Table.field and absent_table."
                )
        elif self.kind in ("state_transition", "state_machine"):
            if not self.table or not self.field:
                raise ValueError(
                    f"{self.kind} checks require table and field."
                )
            if not self.transitions:
                raise ValueError(
                    f"{self.kind} checks require a transitions mapping of state -> allowed next states."
                )
        elif self.kind == "semantic_invariant":
            if not self.table:
                raise ValueError("semantic_invariant checks require table.")
            if not self.expression and not (self.condition and self.assertion):
                raise ValueError(
                    "semantic_invariant checks require expression, or both condition and assertion."
                )
        elif self.kind == "custom_rule":
            if not self.table or not self.expression:
                raise ValueError("custom_rule checks require table and expression.")
        return self

    def validate_references(self, schemas: Dict[str, Dict[str, Any]]) -> None:
        """Validate table/field references and the types required by this check."""
        if self.kind == "allowed_values":
            if self.table not in schemas or self.field not in _schema_fields(schemas[self.table]):
                raise ValueError(f"unknown field '{self.table}.{self.field}'")
        elif self.kind == "temporal_order":
            refs = self.chain if self.chain is not None else [self.before, self.after]
            for reference in refs:
                if not reference:
                    continue
                if "." in reference:
                    table, field = _split_reference(reference)
                else:
                    table, field = self.table or "", reference
                if table not in schemas or field not in _schema_fields(schemas[table]):
                    raise ValueError(f"unknown field '{reference}'")
                definition = schemas[table][field]
                field_type = (
                    definition if isinstance(definition, str)
                    else definition.get("type") if isinstance(definition, dict)
                    else None
                )
                if field_type not in {"date", "datetime"}:
                    raise ValueError(
                        f"'{reference}' is {field_type or 'untyped'}; expected date or datetime"
                    )
        elif self.kind == "absence_by_value":
            table, field = _split_reference(self.source or "")
            if table not in schemas or field not in _schema_fields(schemas[table]):
                raise ValueError(f"unknown field '{self.source}'")
            if self.absent_table not in schemas:
                raise ValueError(f"unknown table '{self.absent_table}'")
        elif self.kind in ("state_transition", "state_machine"):
            real_tbl = self.table if (self.table and self.table in schemas) else next((t for t in schemas if t.lower() == (self.table or "").lower()), None)
            if real_tbl is None:
                raise ValueError(f"unknown table '{self.table}'")
            tbl_fields = _schema_fields(schemas[real_tbl])

            def _find_field(name: Optional[str]) -> Optional[str]:
                if not name:
                    return None
                if name in tbl_fields:
                    return name
                return next((f for f in tbl_fields if f.lower() == name.lower()), None)

            if _find_field(self.field) is None:
                raise ValueError(f"unknown field '{self.table}.{self.field}'")
            if self.previous_field and _find_field(self.previous_field) is None:
                raise ValueError(f"unknown field '{self.table}.{self.previous_field}'")
            if self.order_by and _find_field(self.order_by) is None:
                raise ValueError(f"unknown field '{self.table}.{self.order_by}'")
        elif self.kind in ("semantic_invariant", "custom_rule"):
            real_tbl = self.table if (self.table and self.table in schemas) else next((t for t in schemas if t.lower() == (self.table or "").lower()), None)
            if real_tbl is None:
                raise ValueError(f"unknown table '{self.table}'")


class ScenarioDefinition(BaseModel):
    """Declarative definition of a multi-table business workflow.

    Attributes:
        name: Human-readable scenario name.
        description: Optional scenario context supplied during enrichment.
        secondary_metric: Optional domain target supplied as generation context.
        rules: Human-readable business rules supplied to the enrichment model.
        checks: Typed rules enforced or evaluated by Syda.
        schemas: Syda schemas keyed by table name.
        steps: Workflow steps in dependency order.
        paths: Weighted branches through the workflow.
        bindings: Values copied between tables within each scenario instance.
        instance_id_field: Output column containing the scenario instance ID.
        path_field: Output column containing the assigned path name.
    """

    name: str = Field(min_length=1)
    description: str = ""
    secondary_metric: Dict[str, str] = Field(default_factory=dict, alias="secondaryMetric")
    rules: List[str] = Field(default_factory=list)
    checks: List[ScenarioCheck] = Field(default_factory=list)
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

        for check in self.checks:
            try:
                check.validate_references(self.schemas)
            except ValueError as error:
                raise ValueError(f"Check '{check.name}': {error}.") from error
        paths = self.paths or [
            ScenarioPath(name="default", steps=step_names, weight=1.0)
        ]
        known_steps = set(step_names)
        step_position = {name: index for index, name in enumerate(step_names)}
        step_by_table = {step.table: step.name for step in self.steps}
        identity_fields = _identity_fields(self)
        ledger_owned_fields: Dict[str, Set[str]] = {}
        for step in self.steps:
            owned = set(identity_fields[step.table])
            owned.update(_foreign_keys(self.schemas[step.table]))
            if step.timestamp_field:
                owned.add(step.timestamp_field)
            ledger_owned_fields[step.table] = owned

            conflicting_values = set(step.values) & owned
            if conflicting_values:
                raise ValueError(
                    f"Scenario step '{step.name}' sets ledger-owned fields: "
                    f"{', '.join(sorted(conflicting_values))}."
                )

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
                conflicting_values = set(values) & ledger_owned_fields[step.table]
                if conflicting_values:
                    raise ValueError(
                        f"Scenario path '{path.name}' overrides ledger-owned fields "
                        f"on '{override_step}': "
                        f"{', '.join(sorted(conflicting_values))}."
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
                target_table, target_field = _split_reference(target)
                target_step = step_by_table.get(target_table)
                if target_step is None:
                    raise ValueError(
                        f"Binding '{binding.name}' target table '{target_table}' "
                        "is not a scenario step."
                    )
                if target_field in ledger_owned_fields[target_table]:
                    raise ValueError(
                        f"Binding '{binding.name}' targets ledger-owned field "
                        f"'{target}'."
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
        """Return declared paths or automatically infer branching paths from absence checks.

        Returns:
            Paths used for scenario allocation.
        """
        if self.paths:
            return self.paths

        all_steps = [step.name for step in self.steps]
        absence_checks = [
            c for c in self.checks
            if c.kind == "absence_by_value" and c.absent_table and c.source
        ]

        if not absence_checks:
            return [
                ScenarioPath(
                    name="default",
                    steps=all_steps,
                    weight=1.0,
                )
            ]

        # Inferred paths from absence rules
        # 1. Determine rate / weight from secondary metric (e.g. "Rejection rate": "20%")
        absent_weight = 0.20
        metric_val = self.secondary_metric.get("value", "") if isinstance(self.secondary_metric, dict) else ""
        if isinstance(metric_val, str) and "%" in metric_val:
            try:
                num = float(metric_val.replace("%", "").strip())
                if 0 < num < 100:
                    absent_weight = round(num / 100.0, 3)
            except Exception:
                pass

        # 2. Collect all tables that must be absent and their cascading child dependents
        absent_tables: Set[str] = {c.absent_table for c in absence_checks if c.absent_table}
        changed = True
        while changed:
            changed = False
            for tbl, sch in (self.schemas or {}).items():
                if tbl in absent_tables:
                    continue
                fks = _foreign_keys(sch)
                if any(parent in absent_tables for parent, _ in fks.values()):
                    absent_tables.add(tbl)
                    changed = True

        table_to_step: Dict[str, str] = {}
        for step in self.steps:
            table_to_step[step.table] = step.name
            table_to_step[step.table.lower()] = step.name
            table_to_step[step.name] = step.name
            table_to_step[step.name.lower()] = step.name

        absent_step_names = {
            table_to_step.get(tbl, table_to_step.get(tbl.lower(), tbl))
            for tbl in absent_tables
        }
        absent_steps = [s for s in all_steps if s not in absent_step_names]
        if not absent_steps:
            absent_steps = all_steps[:1]

        # 3. Setup state overrides for the source status field
        absent_overrides: Dict[str, Dict[str, Any]] = {}
        normal_overrides: Dict[str, Dict[str, Any]] = {}
        for c in absence_checks:
            if c.source and "." in c.source and c.value is not None:
                src_tbl, src_col = c.source.split(".", 1)
                src_step = table_to_step.get(src_tbl, table_to_step.get(src_tbl.lower(), src_tbl))
                absent_overrides.setdefault(src_step, {})[src_col] = c.value
                normal_val = None
                for other_check in self.checks:
                    tbl_match = (
                        other_check.table == src_tbl
                        or other_check.table == src_step
                        or (other_check.table and other_check.table.lower() == src_tbl.lower())
                    )
                    if tbl_match and other_check.field == src_col:
                        if other_check.kind == "allowed_values" and other_check.values:
                            candidates = [
                                v for v in (other_check.values or [])
                                if not (type(v) is type(c.value) and v == c.value)
                            ]
                            if candidates:
                                normal_val = candidates[0]
                                break
                        elif other_check.kind in ("state_machine", "state_transition") and other_check.transitions:
                            fsm_states = set(other_check.transitions.keys())
                            for targets in other_check.transitions.values():
                                fsm_states.update(targets)
                            if other_check.initial_states:
                                fsm_states.update(other_check.initial_states)
                            candidates = [
                                s for s in sorted(fsm_states)
                                if s != str(c.value)
                            ]
                            if candidates:
                                if other_check.initial_states:
                                    init_cands = [s for s in other_check.initial_states if s != str(c.value)]
                                    normal_val = init_cands[0] if init_cands else candidates[0]
                                else:
                                    normal_val = candidates[0]
                                break

                if normal_val is None and src_tbl in (self.schemas or {}):
                    col_def = self.schemas[src_tbl].get(src_col)
                    if isinstance(col_def, dict) and "constraints" in col_def:
                        enum_vals = col_def["constraints"].get("enum")
                        if isinstance(enum_vals, list):
                            candidates = [
                                v for v in enum_vals
                                if not (type(v) is type(c.value) and v == c.value)
                            ]
                            if candidates:
                                normal_val = candidates[0]

                if normal_val is None:
                    if isinstance(c.value, bool):
                        normal_val = not c.value
                    elif isinstance(c.value, (int, float)):
                        normal_val = 0 if c.value != 0 else 1
                    else:
                        normal_val = "approved" if c.value in ("denied", "rejected", "cancelled") else "completed"
                normal_overrides.setdefault(src_step, {})[src_col] = normal_val

        trigger_val = absence_checks[0].value
        if isinstance(trigger_val, bool):
            path_name = str(trigger_val).lower()
        elif isinstance(trigger_val, (int, float)):
            path_name = str(trigger_val)
        elif isinstance(trigger_val, str) and trigger_val.strip():
            candidate = trigger_val.strip()
            path_name = candidate if candidate != "approved" else "rejected"
        elif trigger_val is not None:
            path_name = str(trigger_val)
        else:
            path_name = "rejected"

        return [
            ScenarioPath(
                name="approved",
                steps=all_steps,
                weight=round(1.0 - absent_weight, 3),
                overrides=normal_overrides,
            ),
            ScenarioPath(
                name=path_name,
                steps=absent_steps,
                weight=absent_weight,
                overrides=absent_overrides,
            ),
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
    violations: List[ViolationReport] = Field(default_factory=list)

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
