"""Shared translation and validation of platform scenario drafts."""

import re
from typing import Any, Dict

from pydantic import ValidationError
from syda.scenarios import ScenarioDefinition, ScenarioPath, ScenarioStep

from .models.scenario import ScenarioConfiguration


def scenario_definition(
    scenario: ScenarioConfiguration,
    schemas: Dict[str, Any],
) -> ScenarioDefinition:
    """Translate the platform configuration into the reusable core model."""
    for table, schema in schemas.items():
        if not schema.get("__template__"):
            continue
        # The table name becomes the document folder in the job output.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", table):
            raise ValueError(f"Document table '{table}' may only use letters, digits, '_' and '-'.")
        if not isinstance(schema.get("__template_source__"), str):
            raise ValueError(f"Document table '{table}' needs inline HTML in __template_source__.")
        if schema.get("__output_file_type__", "pdf") not in ("pdf", "html"):
            raise ValueError(f"Document table '{table}' must output 'pdf' or 'html'.")
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
        secondaryMetric=scenario.secondary_metric.model_dump(),
        rules=scenario.rules,
        checks=scenario.checks,
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


def scenario_validation_message(error: Exception) -> str:
    """Return actionable validation messages without dumping the scenario payload."""
    if isinstance(error, ValidationError):
        return " ".join(item["msg"].removeprefix("Value error, ") for item in error.errors())
    return str(error)
