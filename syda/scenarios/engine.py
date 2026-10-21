"""Scenario orchestration built on top of Syda's existing table generator.

The engine plans workflow structure deterministically, delegates unconstrained field
synthesis to the configured Syda provider, and validates the merged result.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import (
    Any,
    Callable,
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

import ast
import re
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
    ScenarioCheck,
    ScenarioDefinition,
    ScenarioGenerationResult,
    ScenarioPath,
    ScenarioPlan,
    ScenarioPlanSummary,
    ViolationReport,
)


class TableGenerator(Protocol):
    """The existing Syda generator surface required by ``ScenarioEngine``."""

    def generate_for_schemas(
        self,
        schemas: Any,
        prompts: Optional[Dict[str, str]] = None,
        sample_sizes: Optional[Dict[str, int]] = None,
        **kwargs: Any,
    ) -> Dict[str, pd.DataFrame]: ...


class _RestrictedExpressionEvaluator(ast.NodeVisitor):
    """Safely evaluates expressions for semantic invariants and custom rules without using eval().
    Prevents arbitrary code execution and restricts syntax to safe comparisons, logic, and arithmetic.
    """

    ALLOWED_BUILTINS: Dict[str, Callable[..., Any]] = {
        "abs": abs,
        "min": min,
        "max": max,
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "is_none": lambda x: x is None or pd.isna(x),
    }

    def __init__(self, scope: Dict[str, Any], table_name: Optional[str] = None):
        self.scope = scope
        self.table_name = table_name

    def eval(self, expr: str) -> Any:
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Syntax error in expression '{expr}': {e}")
        return self.visit(tree.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        name = node.id
        if name in self.scope:
            return self.scope[name]
        if name in self.ALLOWED_BUILTINS:
            return self.ALLOWED_BUILTINS[name]
        if name in ("None", "True", "False"):
            return {"None": None, "True": True, "False": False}[name]
        raise NameError(f"Field or identifier '{name}' is not defined")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr.startswith("_"):
            raise ValueError(f"Access to private attribute '{node.attr}' is forbidden")

        if isinstance(node.value, ast.Name):
            tbl = node.value.id
            col = node.attr
            if tbl in self.scope and isinstance(self.scope[tbl], (dict, Mapping)):
                if col in self.scope[tbl]:
                    return self.scope[tbl][col]
            if tbl.lower() in self.scope and isinstance(self.scope[tbl.lower()], (dict, Mapping)):
                if col in self.scope[tbl.lower()]:
                    return self.scope[tbl.lower()][col]
            if self.table_name and tbl.lower() == self.table_name.lower():
                if col in self.scope:
                    return self.scope[col]
            for candidate in (f"{col}_{tbl}", f"{tbl}_{col}", f"{col}_{tbl.lower()}", f"{tbl.lower()}_{col}"):
                if candidate in self.scope:
                    return self.scope[candidate]

        val = self.visit(node.value)
        if isinstance(val, (dict, Mapping)):
            if node.attr in val:
                return val[node.attr]
            raise KeyError(f"Key '{node.attr}' not found")
        if hasattr(val, node.attr):
            return getattr(val, node.attr)
        raise AttributeError(f"Attribute '{node.attr}' not found")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not bool(operand)
        elif isinstance(node.op, ast.USub):
            return -operand
        elif isinstance(node.op, ast.UAdd):
            return +operand
        elif isinstance(node.op, ast.Invert):
            return ~operand
        raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        elif isinstance(node.op, ast.Sub):
            return left - right
        elif isinstance(node.op, ast.Mult):
            return left * right
        elif isinstance(node.op, ast.Div):
            return left / right
        elif isinstance(node.op, ast.FloorDiv):
            return left // right
        elif isinstance(node.op, ast.Mod):
            return left % right
        elif isinstance(node.op, ast.Pow):
            return left ** right
        raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            val = True
            for val_node in node.values:
                val = self.visit(val_node)
                if not val:
                    return val
            return val
        elif isinstance(node.op, ast.Or):
            val = False
            for val_node in node.values:
                val = self.visit(val_node)
                if val:
                    return val
            return val
        raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator_node in zip(node.ops, node.comparators):
            right = self.visit(comparator_node)
            res = False
            if isinstance(op, ast.Eq):
                res = (left == right)
            elif isinstance(op, ast.NotEq):
                res = (left != right)
            elif isinstance(op, ast.Lt):
                if left is None or right is None or pd.isna(left) or pd.isna(right):
                    res = False
                else:
                    res = (left < right)
            elif isinstance(op, ast.LtE):
                if left is None or right is None or pd.isna(left) or pd.isna(right):
                    res = False
                else:
                    res = (left <= right)
            elif isinstance(op, ast.Gt):
                if left is None or right is None or pd.isna(left) or pd.isna(right):
                    res = False
                else:
                    res = (left > right)
            elif isinstance(op, ast.GtE):
                if left is None or right is None or pd.isna(left) or pd.isna(right):
                    res = False
                else:
                    res = (left >= right)
            elif isinstance(op, ast.Is):
                res = (left is right)
            elif isinstance(op, ast.IsNot):
                res = (left is not right)
            elif isinstance(op, ast.In):
                res = (left in right) if right is not None else False
            elif isinstance(op, ast.NotIn):
                res = (left not in right) if right is not None else True
            else:
                raise ValueError(f"Unsupported comparison operator: {type(op).__name__}")
            if not res:
                return False
            left = right
        return True

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only whitelisted standalone functions may be called")
        func_name = node.func.id
        if func_name not in self.ALLOWED_BUILTINS:
            raise ValueError(f"Calling function '{func_name}' is not permitted in rules")
        func = self.ALLOWED_BUILTINS[func_name]
        args = [self.visit(arg) for arg in node.args]
        kwargs: Dict[str, Any] = {
            kw.arg: self.visit(kw.value) for kw in node.keywords if kw.arg is not None
        }
        return func(*args, **kwargs)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        val = self.visit(node.value)
        slice_val = self.visit(node.slice)
        if val is None:
            return None
        return val[slice_val]

    def visit_List(self, node: ast.List) -> List[Any]:
        return [self.visit(elt) for elt in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Tuple[Any, ...]:
        return tuple(self.visit(elt) for elt in node.elts)

    def visit_Set(self, node: ast.Set) -> Set[Any]:
        return {self.visit(elt) for elt in node.elts}

    def visit_Dict(self, node: ast.Dict) -> Dict[Any, Any]:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values) if k is not None}

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"Disallowed expression syntax in rule: {type(node).__name__}")


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
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
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
        total_enrichment_rows = sum(
            len(plan.tables[table]) for table in enrichment_schemas
        )
        completed_enrichment_rows = 0
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
                completed_enrichment_rows += len(chunk_contexts)
                if progress_callback:
                    progress_callback(
                        completed_enrichment_rows,
                        total_enrichment_rows,
                        step.table,
                    )

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
        _, violations = self.validate_causal_workflow(definition, tables)

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
            violations=violations,
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
            fields: Dict[str, Any] = {
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
            for check in definition.checks:
                if check.kind != "allowed_values" or check.table != step.table or not check.field:
                    continue
                field_name = check.field
                field_schema = fields.get(field_name)
                if field_schema is None:
                    continue
                schema_dict: Dict[str, Any]
                if isinstance(field_schema, str):
                    schema_dict = {"type": field_schema}
                elif isinstance(field_schema, Mapping):
                    schema_dict = dict(field_schema)
                else:
                    schema_dict = {"type": "text"}
                raw_constraints = schema_dict.get("constraints")
                constraints: Dict[str, Any] = dict(raw_constraints) if isinstance(raw_constraints, Mapping) else {}
                constraints["enum"] = list(check.values)
                schema_dict["constraints"] = constraints
                fields[field_name] = schema_dict
            schemas[step.table] = {**metadata, **fields}
        return schemas

    @classmethod
    def infer_causal_checks(
        cls,
        definition: ScenarioDefinition,
    ) -> List[ScenarioCheck]:
        """Automatically infer causal integrity rules, temporal sequences, and semantic invariants from schemas and workflow.

        Enables non-technical users to get comprehensive causal workflow validation without
        having to manually write rule syntax.
        """
        inferred: List[ScenarioCheck] = []
        existing_names = {c.name for c in definition.checks}
        existing_temporal_tables = {c.table for c in definition.checks if c.kind == "temporal_order" and c.table}
        existing_fsm = {(c.table, c.field) for c in definition.checks if c.kind in ("state_machine", "state_transition")}
        existing_invariants = {c.table for c in definition.checks if c.kind in ("semantic_invariant", "custom_rule") and c.table}
        schemas = definition.schemas or {}

        # 1. Automatic temporal ordering across workflow steps
        for i in range(len(definition.steps) - 1):
            s_current = definition.steps[i]
            s_next = definition.steps[i + 1]
            if s_current.timestamp_field and s_next.timestamp_field:
                rule_name = f"auto_temporal_{s_current.table}_{s_next.table}"
                if rule_name not in existing_names:
                    inferred.append(
                        ScenarioCheck(
                            name=rule_name,
                            kind="temporal_order",
                            before=f"{s_current.table}.{s_current.timestamp_field}",
                            after=f"{s_next.table}.{s_next.timestamp_field}",
                            strict=False,
                        )
                    )

        # 2. Automatic temporal lifecycle chain within single tables
        phase_keywords = [
            ("initial", ("diagnosis", "admit", "admission", "intake", "start", "order", "application", "visit", "encounter")),
            ("service", ("service", "procedure", "treatment", "prescription", "submission", "submit", "claim", "processing")),
            ("review", ("adjudication", "adjudicate", "review", "decision", "approval", "approve", "dispatch", "ship")),
            ("settlement", ("payment", "paid", "settle", "discharge", "delivery", "deliver", "completion", "close")),
        ]

        # Scope single-table checks to tables present in the scenario steps (if defined)
        step_tables = {s.table for s in definition.steps if s.table}
        target_schemas = {
            t: f for t, f in schemas.items()
            if (not step_tables or t in step_tables)
        }

        for table_name, schema_fields in target_schemas.items():
            if not isinstance(schema_fields, dict):
                continue
            date_cols: List[str] = []
            non_event_dates = {"birth_date", "date_of_birth", "dob", "created_at", "updated_at"}
            for f_name, f_def in schema_fields.items():
                if f_name.startswith("__") or f_name.lower() in non_event_dates:
                    continue
                f_type = f_def if isinstance(f_def, str) else str(f_def.get("type", "")).lower() if isinstance(f_def, dict) else ""
                if f_type in ("date", "datetime"):
                    date_cols.append(f_name)

            if len(date_cols) >= 2 and table_name not in existing_temporal_tables:
                ranked_cols: List[Tuple[int, str]] = []
                for col in date_cols:
                    col_lower = col.lower()
                    assigned_rank = None
                    for rank, (_, keywords) in enumerate(phase_keywords):
                        if any(kw in col_lower for kw in keywords):
                            assigned_rank = rank
                            break
                    if assigned_rank is not None:
                        ranked_cols.append((assigned_rank, col))

                seen_ranks = set()
                sorted_by_rank: List[str] = []
                for rank, col in sorted(ranked_cols, key=lambda x: x[0]):
                    if rank not in seen_ranks:
                        seen_ranks.add(rank)
                        sorted_by_rank.append(col)

                if len(sorted_by_rank) >= 2:
                    chain_name = f"auto_lifecycle_{table_name.lower()}"
                    if chain_name not in existing_names:
                        inferred.append(
                            ScenarioCheck(
                                name=chain_name,
                                kind="temporal_order",
                                table=table_name,
                                chain=sorted_by_rank,
                                strict=False,
                            )
                        )

        return inferred

    @classmethod
    def evaluate_checks(
        cls,
        definition: ScenarioDefinition,
        tables: Mapping[str, pd.DataFrame],
    ) -> List[Dict[str, Any]]:
        """Evaluate declared executable checks against generated table frames.

        Returns a list of check results containing check name, violation count,
        and structured violation details: [{"table": ..., "row": ..., "rule": ..., "error": ...}].
        """
        results: List[Dict[str, Any]] = []
        instance_field = definition.instance_id_field

        checks_to_run = list(definition.checks)
        existing_names = {c.name for c in checks_to_run}
        for inf in cls.infer_causal_checks(definition):
            if inf.name not in existing_names:
                checks_to_run.append(inf)

        for check in checks_to_run:
            is_auto = check.name.startswith("auto_")
            violations_list: List[Dict[str, Any]] = []

            # 1. ALLOWED VALUES
            if check.kind == "allowed_values":
                table_name = check.table or ""
                frame = tables.get(table_name)
                if frame is None or check.field not in frame:
                    if not is_auto:
                        violations_list.append({
                            "table": table_name,
                            "row": "N/A",
                            "rule": check.name,
                            "error": f"Table '{table_name}' or field '{check.field}' is missing from generated data.",
                        })
                else:
                    invalid_mask = ~frame[check.field].isin(check.values)
                    if invalid_mask.any():
                        for idx, row in frame[invalid_mask].iterrows():
                            row_id = row.get(instance_field, idx)
                            val = row.get(check.field)
                            violations_list.append({
                                "table": table_name,
                                "row": row_id,
                                "rule": check.name,
                                "error": f"Value '{val}' in column '{check.field}' is not in allowed values: {check.values}.",
                            })

            # 2. TEMPORAL ORDER (chain or pairwise before/after)
            elif check.kind == "temporal_order":
                if check.chain:
                    resolved_chain: List[Tuple[str, str]] = []
                    for ref in check.chain:
                        if "." in ref:
                            t_name, f_name = _split_reference(ref)
                        else:
                            t_name, f_name = check.table or "", ref
                        resolved_chain.append((t_name, f_name))

                    missing = [
                        f"{t}.{f}" for t, f in resolved_chain
                        if t not in tables or f not in tables[t]
                    ]
                    if missing:
                        if not is_auto:
                            violations_list.append({
                                "table": resolved_chain[0][0],
                                "row": "N/A",
                                "rule": check.name,
                                "error": f"Missing references for temporal chain: {', '.join(missing)}.",
                            })
                    else:
                        unique_tables = {t for t, _ in resolved_chain}
                        if len(unique_tables) == 1:
                            tbl = next(iter(unique_tables))
                            df = tables[tbl]
                            for idx, row in df.iterrows():
                                row_id = row.get(instance_field, idx)
                                for i in range(len(resolved_chain) - 1):
                                    (_, col_a), (_, col_b) = resolved_chain[i], resolved_chain[i + 1]
                                    val_a = pd.to_datetime(row.get(col_a), errors="coerce", utc=True)
                                    val_b = pd.to_datetime(row.get(col_b), errors="coerce", utc=True)
                                    if pd.notna(val_a) and pd.notna(val_b):
                                        is_violation = (val_a > val_b) if not check.strict else (val_a >= val_b)
                                        if is_violation:
                                            op_str = "<" if check.strict else "<="
                                            violations_list.append({
                                                "table": tbl,
                                                "row": row_id,
                                                "rule": check.name,
                                                "error": f"Temporal violation: '{col_a}' ({row.get(col_a)}) must be {op_str} '{col_b}' ({row.get(col_b)}).",
                                            })
                                            break
                            if instance_field in df:
                                for inst_id, group in df.groupby(instance_field, sort=False):
                                    if len(group) > 1:
                                        for i in range(len(resolved_chain) - 1):
                                            (_, col_a), (_, col_b) = resolved_chain[i], resolved_chain[i + 1]
                                            a_vals: List[pd.Timestamp] = []
                                            for raw_a in group[col_a]:
                                                ts_a = pd.to_datetime(raw_a, errors="coerce", utc=True)
                                                if pd.notna(ts_a) and isinstance(ts_a, pd.Timestamp):
                                                    a_vals.append(ts_a)

                                            b_vals: List[pd.Timestamp] = []
                                            for raw_b in group[col_b]:
                                                ts_b = pd.to_datetime(raw_b, errors="coerce", utc=True)
                                                if pd.notna(ts_b) and isinstance(ts_b, pd.Timestamp):
                                                    b_vals.append(ts_b)
                                            if a_vals and b_vals:
                                                earlier_max = max(a_vals)
                                                later_min = min(b_vals)
                                                is_violation = (earlier_max > later_min) if not check.strict else (earlier_max >= later_min)
                                                if is_violation:
                                                    op_str = "<" if check.strict else "<="
                                                    violations_list.append({
                                                        "table": tbl,
                                                        "row": inst_id,
                                                        "rule": check.name,
                                                        "error": f"Temporal chain violation: '{col_a}' ({earlier_max.isoformat()}) must be {op_str} '{col_b}' ({later_min.isoformat()}).",
                                                    })
                                                    break
                        else:
                            instance_timestamps: Dict[Any, Dict[str, Dict[str, pd.Timestamp]]] = {}
                            for t_name, f_name in resolved_chain:
                                df = tables.get(t_name)
                                if df is None or instance_field not in df:
                                    continue
                                for _, row in df.iterrows():
                                    inst_id = row[instance_field]
                                    ts = pd.to_datetime(row.get(f_name), errors="coerce", utc=True)
                                    if pd.notna(ts) and isinstance(ts, pd.Timestamp):
                                        ref_key = f"{t_name}.{f_name}"
                                        inst_dict = instance_timestamps.setdefault(inst_id, {})
                                        if ref_key not in inst_dict:
                                            inst_dict[ref_key] = {"min": ts, "max": ts}
                                        else:
                                            if ts < inst_dict[ref_key]["min"]:
                                                inst_dict[ref_key]["min"] = ts
                                            if ts > inst_dict[ref_key]["max"]:
                                                inst_dict[ref_key]["max"] = ts

                            for inst_id, ts_map in instance_timestamps.items():
                                for i in range(len(resolved_chain) - 1):
                                    ref_a = f"{resolved_chain[i][0]}.{resolved_chain[i][1]}"
                                    ref_b = f"{resolved_chain[i+1][0]}.{resolved_chain[i+1][1]}"
                                    if ref_a in ts_map and ref_b in ts_map:
                                        earlier_max = ts_map[ref_a]["max"]
                                        later_min = ts_map[ref_b]["min"]
                                        is_violation = (earlier_max > later_min) if not check.strict else (earlier_max >= later_min)
                                        if is_violation:
                                            op_str = "<" if check.strict else "<="
                                            violations_list.append({
                                                "table": resolved_chain[i+1][0],
                                                "row": inst_id,
                                                "rule": check.name,
                                                "error": f"Temporal chain violation: '{ref_a}' ({earlier_max.isoformat()}) must be {op_str} '{ref_b}' ({later_min.isoformat()}).",
                                            })
                                            break
                else:
                    before_table, before_field = _split_reference(check.before or "")
                    after_table, after_field = _split_reference(check.after or "")
                    before = tables.get(before_table)
                    after = tables.get(after_table)
                    if (
                        before is None or after is None
                        or instance_field not in before or instance_field not in after
                        or before_field not in before or after_field not in after
                    ):
                        if not is_auto:
                            violations_list.append({
                                "table": after_table or check.table or "",
                                "row": "N/A",
                                "rule": check.name,
                                "error": f"Temporal check references missing table or field: {check.before} -> {check.after}.",
                            })
                    else:
                        before_values = before[[instance_field, before_field]].copy()
                        after_values = after[[instance_field, after_field]].copy()
                        before_values[before_field] = pd.to_datetime(
                            before_values[before_field], errors="coerce", utc=True
                        )
                        after_values[after_field] = pd.to_datetime(
                            after_values[after_field], errors="coerce", utc=True
                        )
                        before_groups = before_values.groupby(instance_field)[before_field]
                        after_groups = after_values.groupby(instance_field)[after_field]
                        shared_instances = before_groups.indices.keys() & after_groups.indices.keys()
                        for instance_id in shared_instances:
                            earlier = before_groups.get_group(instance_id).dropna()
                            later = after_groups.get_group(instance_id).dropna()
                            if earlier.empty or later.empty:
                                continue
                            is_violation = (earlier.max() > later.min()) if not check.strict else (earlier.max() >= later.min())
                            if is_violation:
                                op_str = "<" if check.strict else "<="
                                violations_list.append({
                                    "table": after_table,
                                    "row": instance_id,
                                    "rule": check.name,
                                    "error": f"Temporal violation: '{check.before}' ({earlier.max().isoformat()}) must be {op_str} '{check.after}' ({later.min().isoformat()}).",
                                })

            # 3. ABSENCE BY VALUE
            elif check.kind == "absence_by_value":
                source_table, source_field = _split_reference(check.source or "")
                source = tables.get(source_table)
                absent = tables.get(check.absent_table or "")
                if (
                    source is None or absent is None
                    or instance_field not in source or instance_field not in absent
                    or source_field not in source
                ):
                    if not is_auto:
                        violations_list.append({
                            "table": check.absent_table or "",
                            "row": "N/A",
                            "rule": check.name,
                            "error": f"Missing tables or fields for check: {check.source} absent {check.absent_table}.",
                        })
                else:
                    matching_source = source[source[source_field] == check.value]
                    source_instances = set(matching_source[instance_field].tolist())
                    forbidden_instances = sorted(source_instances & set(absent[instance_field].tolist()))
                    for inst_id in forbidden_instances:
                        violations_list.append({
                            "table": check.absent_table,
                            "row": inst_id,
                            "rule": check.name,
                            "error": f"Forbidden record in '{check.absent_table}' for scenario instance '{inst_id}' when {check.source} == '{check.value}'.",
                        })

            # 4. STATE TRANSITION / STATE MACHINE
            elif check.kind in ("state_transition", "state_machine"):
                table_name = check.table or ""
                df = tables.get(table_name)
                transitions = check.transitions or {}
                if df is None or check.field not in df:
                    if not is_auto:
                        violations_list.append({
                            "table": table_name,
                            "row": "N/A",
                            "rule": check.name,
                            "error": f"Table '{table_name}' or field '{check.field}' is missing for state transition check.",
                        })
                else:
                    all_states = set(transitions.keys())
                    for targets in transitions.values():
                        all_states.update(targets)
                    if check.initial_states:
                        all_states.update(check.initial_states)
                    if check.terminal_states:
                        all_states.update(check.terminal_states)

                    if check.previous_field and check.previous_field in df:
                        for idx, row in df.iterrows():
                            row_id = row.get(instance_field, idx)
                            from_val = row.get(check.previous_field)
                            to_val = row.get(check.field)
                            if pd.notna(from_val) and pd.notna(to_val):
                                allowed = transitions.get(str(from_val), [])
                                if str(to_val) not in allowed:
                                    violations_list.append({
                                        "table": table_name,
                                        "row": row_id,
                                        "rule": check.name,
                                        "error": f"Illegal state transition from '{from_val}' to '{to_val}'. Allowed transitions from '{from_val}': {allowed}.",
                                    })
                    elif instance_field in df:
                        order_col = check.order_by if (check.order_by and check.order_by in df) else None
                        for inst_id, group in df.groupby(instance_field, sort=False):
                            if order_col:
                                group = group.sort_values(order_col)
                            states: List[str] = [str(s) for s in group[check.field].dropna().tolist()]
                            if not states:
                                continue

                            if check.initial_states and states[0] not in check.initial_states:
                                violations_list.append({
                                    "table": table_name,
                                    "row": inst_id,
                                    "rule": check.name,
                                    "error": f"Initial state '{states[0]}' not in allowed initial states: {check.initial_states}.",
                                })
                            if check.terminal_states and states[-1] not in check.terminal_states:
                                violations_list.append({
                                    "table": table_name,
                                    "row": inst_id,
                                    "rule": check.name,
                                    "error": f"Terminal state '{states[-1]}' not in allowed terminal states: {check.terminal_states}.",
                                })

                            for s in states:
                                if s not in all_states:
                                    violations_list.append({
                                        "table": table_name,
                                        "row": inst_id,
                                        "rule": check.name,
                                        "error": f"Unknown state '{s}' not part of declared state machine.",
                                    })

                            if len(states) > 1:
                                for i in range(len(states) - 1):
                                    curr_s = states[i]
                                    next_s = states[i + 1]
                                    allowed: List[str] = transitions.get(curr_s, [])
                                    if next_s not in allowed:
                                        violations_list.append({
                                            "table": table_name,
                                            "row": inst_id,
                                            "rule": check.name,
                                            "error": f"Illegal transition sequence from '{curr_s}' to '{next_s}'. Allowed: {allowed}.",
                                        })
                    else:
                        for idx, row in df.iterrows():
                            val = row.get(check.field)
                            if pd.notna(val) and str(val) not in all_states:
                                violations_list.append({
                                    "table": table_name,
                                    "row": idx,
                                    "rule": check.name,
                                    "error": f"State '{val}' is not in known states: {sorted(all_states)}.",
                                })

            # 5. SEMANTIC INVARIANT
            elif check.kind == "semantic_invariant":
                table_name = check.table or ""
                if table_name not in tables:
                    for t in tables:
                        if t.lower() == table_name.lower():
                            table_name = t
                            break
                df = tables.get(table_name)
                if df is None:
                    if not is_auto:
                        violations_list.append({
                            "table": table_name,
                            "row": "N/A",
                            "rule": check.name,
                            "error": f"Table '{table_name}' is missing for semantic invariant check.",
                        })
                else:
                    eval_df = df.copy()
                    expr_text = f"{check.condition or ''} {check.assertion or ''} {check.expression or ''}"
                    cleaned_expr = re.sub(r"'[^']*'|\"[^\"]*\"", "", expr_text)
                    words = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", cleaned_expr)) - {
                        "or", "and", "not", "True", "False", "None", "abs", "min", "max", "len", "str", "int", "float", "bool", "is_none"
                    }
                    if is_auto and not words.issubset(set(eval_df.columns)):
                        continue

                    joined_tables: Set[str] = set()
                    for other_name, other_df in tables.items():
                        if other_name.lower() != table_name.lower() and instance_field in eval_df and instance_field in other_df:
                            is_referenced = bool(
                                re.search(rf"\b{re.escape(other_name)}\b", cleaned_expr, re.IGNORECASE)
                                or any(
                                    re.search(rf"\b{re.escape(col)}\b", cleaned_expr)
                                    for col in other_df.columns
                                    if col not in df.columns
                                )
                            )
                            if is_referenced:
                                eval_df = eval_df.merge(
                                    other_df,
                                    on=instance_field,
                                    how="left",
                                    suffixes=("", f"_{other_name}"),
                                )
                                joined_tables.add(other_name)

                    for idx, row in eval_df.iterrows():
                        row_id = row.get(instance_field, idx)
                        row_dict = {}
                        for k, v in row.items():
                            col_str = str(k)
                            row_dict[col_str] = None if pd.isna(v) else v
                        row_dict["row"] = row_dict
                        row_dict["record"] = row_dict

                        # Provide table-level scoping for prefixes like Claim.status or Claim['status']
                        if table_name:
                            primary_scope = {
                                col: row_dict.get(col)
                                for col in df.columns
                                if col in row_dict
                            }
                            row_dict[table_name] = primary_scope
                            row_dict[table_name.lower()] = primary_scope

                        for other_name, other_df in tables.items():
                            if other_name.lower() != table_name.lower():
                                other_scope = {}
                                if other_name in joined_tables:
                                    for c in other_df.columns:
                                        suffixed = f"{c}_{other_name}"
                                        if suffixed in row_dict:
                                            other_scope[c] = row_dict[suffixed]
                                        elif c not in df.columns and c in row_dict:
                                            other_scope[c] = row_dict[c]
                                        else:
                                            other_scope[c] = None
                                else:
                                    for c in other_df.columns:
                                        other_scope[c] = None
                                row_dict[other_name] = other_scope
                                row_dict[other_name.lower()] = other_scope

                        evaluator = _RestrictedExpressionEvaluator(row_dict, table_name=table_name)

                        if check.expression:
                            try:
                                passed = bool(evaluator.eval(check.expression))
                            except Exception as e:
                                violations_list.append({
                                    "table": table_name,
                                    "row": row_id,
                                    "rule": check.name,
                                    "error": f"Failed to evaluate semantic invariant '{check.expression}': {e}",
                                })
                                continue
                            if not passed:
                                violations_list.append({
                                    "table": table_name,
                                    "row": row_id,
                                    "rule": check.name,
                                    "error": f"Semantic invariant '{check.expression}' failed for row data.",
                                })
                        elif check.condition and check.assertion:
                            try:
                                cond_met = bool(evaluator.eval(check.condition))
                            except Exception as e:
                                violations_list.append({
                                    "table": table_name,
                                    "row": row_id,
                                    "rule": check.name,
                                    "error": f"Failed to evaluate semantic condition '{check.condition}': {e}",
                                })
                                continue

                            if cond_met:
                                try:
                                    assert_met = bool(evaluator.eval(check.assertion))
                                except Exception as e:
                                    violations_list.append({
                                        "table": table_name,
                                        "row": row_id,
                                        "rule": check.name,
                                        "error": f"Failed to evaluate semantic assertion '{check.assertion}': {e}",
                                    })
                                    continue
                                if not assert_met:
                                    violations_list.append({
                                        "table": table_name,
                                        "row": row_id,
                                        "rule": check.name,
                                        "error": f"Semantic invariant violation: when '{check.condition}', expected '{check.assertion}'.",
                                    })

            # 6. CUSTOM RULE
            elif check.kind == "custom_rule":
                table_name = check.table or ""
                if table_name not in tables:
                    for t in tables:
                        if t.lower() == table_name.lower():
                            table_name = t
                            break
                df = tables.get(table_name)
                if df is None:
                    violations_list.append({
                        "table": table_name,
                        "row": "N/A",
                        "rule": check.name,
                        "error": f"Table '{table_name}' is missing for custom rule check.",
                    })
                elif check.expression:
                    eval_df = df.copy()
                    cleaned_expr = re.sub(r"'[^']*'|\"[^\"]*\"", "", check.expression)
                    joined_tables = set()
                    for other_name, other_df in tables.items():
                        if other_name.lower() != table_name.lower() and instance_field in eval_df and instance_field in other_df:
                            is_referenced = bool(
                                re.search(rf"\b{re.escape(other_name)}\b", cleaned_expr, re.IGNORECASE)
                                or any(
                                    re.search(rf"\b{re.escape(col)}\b", cleaned_expr)
                                    for col in other_df.columns
                                    if col not in df.columns
                                )
                            )
                            if is_referenced:
                                eval_df = eval_df.merge(
                                    other_df,
                                    on=instance_field,
                                    how="left",
                                    suffixes=("", f"_{other_name}"),
                                )
                                joined_tables.add(other_name)

                    for idx, row in eval_df.iterrows():
                        row_id = row.get(instance_field, idx)
                        row_dict = {}
                        for k, v in row.items():
                            col_str = str(k)
                            row_dict[col_str] = None if pd.isna(v) else v
                        row_dict["row"] = row_dict
                        row_dict["record"] = row_dict
                        if table_name:
                            primary_scope = {
                                col: row_dict.get(col)
                                for col in df.columns
                                if col in row_dict
                            }
                            row_dict[table_name] = primary_scope
                            row_dict[table_name.lower()] = primary_scope

                        for other_name, other_df in tables.items():
                            if other_name.lower() != table_name.lower():
                                other_scope = {}
                                if other_name in joined_tables:
                                    for c in other_df.columns:
                                        suffixed = f"{c}_{other_name}"
                                        if suffixed in row_dict:
                                            other_scope[c] = row_dict[suffixed]
                                        elif c not in df.columns and c in row_dict:
                                            other_scope[c] = row_dict[c]
                                        else:
                                            other_scope[c] = None
                                else:
                                    for c in other_df.columns:
                                        other_scope[c] = None
                                row_dict[other_name] = other_scope
                                row_dict[other_name.lower()] = other_scope

                        evaluator = _RestrictedExpressionEvaluator(row_dict, table_name=table_name)
                        try:
                            passed = bool(evaluator.eval(check.expression))
                        except Exception as e:
                            violations_list.append({
                                "table": table_name,
                                "row": row_id,
                                "rule": check.name,
                                "error": f"Failed to evaluate custom rule '{check.expression}': {e}",
                            })
                            continue
                        if not passed:
                            violations_list.append({
                                "table": table_name,
                                "row": row_id,
                                "rule": check.name,
                                "error": f"Custom rule '{check.expression}' failed for row data.",
                            })

            results.append({
                "name": check.name,
                "violations": len(violations_list),
                "details": violations_list,
            })

        return results

    @classmethod
    def validate_causal_workflow(
        cls,
        definition: ScenarioDefinition,
        tables: Mapping[str, pd.DataFrame],
    ) -> Tuple[List[Dict[str, Any]], List[ViolationReport]]:
        """Run post-generation causal integrity pass and return both metrics and structured violations."""
        check_results = cls.evaluate_checks(definition, tables)
        violations: List[ViolationReport] = []
        for result in check_results:
            for detail in result.get("details", []):
                violations.append(
                    ViolationReport(
                        table=detail["table"],
                        row=detail["row"],
                        rule=detail["rule"],
                        error=detail["error"],
                    )
                )
        return check_results, violations

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
        records_by_table: Dict[str, Dict[Any, List[Dict[str, Any]]]] = {}

        for context_step in definition.steps[: step_index + 1]:
            context_frame = tables[context_step.table]
            by_instance: Dict[Any, List[Dict[str, Any]]] = {}
            for instance_id, group in context_frame.groupby(
                instance_field,
                sort=False,
            ):
                by_instance[instance_id] = [
                    _known_record(record, instance_field, path_field)
                    for record in group.to_dict(orient="records")
                ]
            records_by_table[context_step.table] = by_instance

        for row_key, row in zip(row_keys, frame.to_dict(orient="records")):
            instance_id = row[instance_field]
            known_records: Dict[str, List[Dict[str, Any]]] = {}
            for context_step in definition.steps[: step_index + 1]:
                records = records_by_table[context_step.table].get(instance_id, [])
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

        actual_keys: List[str] = [str(k) for k in frame[_ROW_KEY_FIELD].tolist()]
        duplicate_keys: List[str] = sorted(
            {key for key in actual_keys if actual_keys.count(key) > 1}
        )
        if duplicate_keys:
            raise ValueError(
                f"Generator returned duplicate row keys for '{table}': "
                f"{', '.join(duplicate_keys)}."
            )
        if set(actual_keys) != set(expected_keys):
            missing: List[str] = sorted(set(expected_keys) - set(actual_keys))
            unexpected: List[str] = sorted(set(actual_keys) - set(expected_keys))
            details: List[str] = []
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
            frame.loc[mask, column] = getattr(indexed.loc[mask, column], "values", indexed.loc[mask, column])

        declared = list(_schema_fields(definition.schemas[table]))
        metadata = [definition.instance_id_field, definition.path_field]
        cols: List[str] = [*declared, *metadata]
        return pd.DataFrame(frame[cols])

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
                ts_series = pd.Series(pd.to_datetime(rows[step.timestamp_field], errors="coerce"))
                if ts_series.isna().any():
                    errors.append(
                        f"scenario '{instance_id}' has invalid timestamps in "
                        f"step '{step.name}'"
                    )
                    continue
                current_min = ts_series.min()
                current_max = ts_series.max()
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
