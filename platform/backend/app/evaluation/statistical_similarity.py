"""Statistical distribution comparisons for generated and reference datasets."""

from dataclasses import dataclass
from math import sqrt
from typing import Any, Dict, Iterable, Literal, Mapping, Sequence

import pandas as pd
from scipy.stats import chisquare, ks_2samp, wasserstein_distance


SIMILARITY_THRESHOLD = 0.80
_MIN_SAMPLE_SIZE = 2
_UNSEEN_CATEGORY_SMOOTHING = 0.5
_BOOKKEEPING_COLUMNS = frozenset(
    {
        "scenario_instance_id",
        "scenario_path",
    }
)
_NUMERIC_TYPES = frozenset({"decimal", "float", "int", "integer", "number", "numeric"})
_CATEGORICAL_TYPES = frozenset(
    {"bool", "boolean", "category", "enum", "str", "string", "text"}
)
_TEMPORAL_TYPES = frozenset({"date", "datetime", "time", "timestamp"})


@dataclass(frozen=True)
class FieldSimilarity:
    """One comparable field's normalized score and diagnostic measurements."""

    table: str
    column: str
    kind: Literal["numeric", "categorical"]
    score: float
    ks_statistic: float | None = None
    wasserstein_distance: float | None = None
    chi_square_statistic: float | None = None


@dataclass(frozen=True)
class StatisticalSimilarityResult:
    """Aggregate result suitable for the platform's EvaluationMetric model."""

    status: Literal["pass", "fail", "not_evaluated"]
    score: float | None
    violations: int
    evaluated_fields: tuple[FieldSimilarity, ...]
    skipped_fields: int
    detail: str


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _field_type(field: Any) -> str | None:
    if isinstance(field, str):
        return field.lower()
    if isinstance(field, dict) and isinstance(field.get("type"), str):
        return field["type"].lower()
    return None


def _is_excluded_field(
    column: str,
    field: Any,
    foreign_keys: Mapping[str, Any],
) -> bool:
    normalized = column.lower()
    constraints = field.get("constraints", {}) if isinstance(field, dict) else {}
    field_type = _field_type(field)
    return bool(
        column.startswith("__")
        or normalized in _BOOKKEEPING_COLUMNS
        or normalized.endswith("_id")
        or constraints.get("primary_key")
        or column in foreign_keys
        or field_type == "foreign_key"
        or field_type in _TEMPORAL_TYPES
    )


def _field_kind(
    field: Any,
    generated: pd.Series,
    reference: pd.Series,
) -> Literal["numeric", "categorical"] | None:
    field_type = _field_type(field)
    if field_type in _NUMERIC_TYPES:
        return "numeric"
    if field_type in _CATEGORICAL_TYPES:
        return "categorical"
    if pd.api.types.is_bool_dtype(generated) or pd.api.types.is_bool_dtype(reference):
        return "categorical"
    if pd.api.types.is_numeric_dtype(generated) and pd.api.types.is_numeric_dtype(reference):
        return "numeric"
    if pd.api.types.is_string_dtype(generated) or pd.api.types.is_string_dtype(reference):
        return "categorical"
    return None


def _numeric_similarity(
    table: str,
    column: str,
    generated: pd.Series,
    reference: pd.Series,
) -> FieldSimilarity | None:
    generated_values = pd.to_numeric(generated, errors="coerce").dropna().astype(float)
    reference_values = pd.to_numeric(reference, errors="coerce").dropna().astype(float)
    if len(generated_values) < _MIN_SAMPLE_SIZE or len(reference_values) < _MIN_SAMPLE_SIZE:
        return None

    ks_statistic = float(ks_2samp(generated_values, reference_values).statistic)
    distance = float(wasserstein_distance(generated_values, reference_values))
    reference_range = float(reference_values.max() - reference_values.min())
    reference_scale = reference_range or max(abs(float(reference_values.mean())), 1.0)
    wasserstein_similarity = 1.0 / (1.0 + (distance / reference_scale))
    score = _clamp(((1.0 - ks_statistic) + wasserstein_similarity) / 2.0)
    return FieldSimilarity(
        table=table,
        column=column,
        kind="numeric",
        score=score,
        ks_statistic=ks_statistic,
        wasserstein_distance=distance,
    )


def _categorical_similarity(
    table: str,
    column: str,
    generated: pd.Series,
    reference: pd.Series,
) -> FieldSimilarity | None:
    generated_values = generated.dropna().map(str)
    reference_values = reference.dropna().map(str)
    if len(generated_values) < _MIN_SAMPLE_SIZE or len(reference_values) < _MIN_SAMPLE_SIZE:
        return None

    categories = sorted(set(generated_values) | set(reference_values))
    if not categories:
        return None

    generated_counts = generated_values.value_counts().reindex(categories, fill_value=0).astype(float)
    reference_counts = reference_values.value_counts().reindex(categories, fill_value=0).astype(float)
    smoothed_reference = reference_counts.copy()
    smoothed_reference.loc[smoothed_reference == 0] = _UNSEEN_CATEGORY_SMOOTHING
    expected_counts = smoothed_reference / smoothed_reference.sum() * generated_counts.sum()
    chi_square_statistic = float(chisquare(generated_counts, expected_counts).statistic)
    effect_size = sqrt(chi_square_statistic / float(generated_counts.sum()))
    score = _clamp(1.0 - effect_size)
    return FieldSimilarity(
        table=table,
        column=column,
        kind="categorical",
        score=score,
        chi_square_statistic=chi_square_statistic,
    )


def _reference_frames(
    reference_data: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> Dict[str, pd.DataFrame]:
    if not reference_data:
        return {}
    return {
        table: pd.DataFrame([dict(row) for row in rows])
        for table, rows in reference_data.items()
        if rows
    }


def _lowest_scores(fields: Iterable[FieldSimilarity]) -> str:
    lowest = sorted(fields, key=lambda item: item.score)[:3]
    return ", ".join(
        f"{field.table}.{field.column}={field.score * 100:.1f}%" for field in lowest
    )


def evaluate_statistical_similarity(
    generated_data: Mapping[str, pd.DataFrame],
    schemas: Mapping[str, Mapping[str, Any]],
    reference_data: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> StatisticalSimilarityResult:
    """Compare generated fields with an optional caller-supplied reference sample."""
    references = _reference_frames(reference_data)
    if not references:
        return StatisticalSimilarityResult(
            status="not_evaluated",
            score=None,
            violations=0,
            evaluated_fields=(),
            skipped_fields=0,
            detail="Provide a reference sample to compare numeric and categorical distributions.",
        )

    evaluated: list[FieldSimilarity] = []
    skipped = 0
    for table, schema in schemas.items():
        generated_frame = generated_data.get(table)
        reference_frame = references.get(table)
        if generated_frame is None or reference_frame is None:
            continue
        foreign_keys = schema.get("__foreign_keys__", {})
        for column, field in schema.items():
            if _is_excluded_field(column, field, foreign_keys):
                continue
            if column not in generated_frame.columns or column not in reference_frame.columns:
                skipped += 1
                continue
            kind = _field_kind(field, generated_frame[column], reference_frame[column])
            if kind == "numeric":
                result = _numeric_similarity(
                    table, column, generated_frame[column], reference_frame[column]
                )
            elif kind == "categorical":
                result = _categorical_similarity(
                    table, column, generated_frame[column], reference_frame[column]
                )
            else:
                result = None
            if result is None:
                skipped += 1
            else:
                evaluated.append(result)

    if not evaluated:
        return StatisticalSimilarityResult(
            status="not_evaluated",
            score=None,
            violations=0,
            evaluated_fields=(),
            skipped_fields=skipped,
            detail=(
                "The reference sample did not contain any comparable non-identifier "
                "numeric or categorical fields."
            ),
        )

    score = _clamp(sum(field.score for field in evaluated) / len(evaluated))
    violations = sum(field.score < threshold for field in evaluated)
    status: Literal["pass", "fail"] = (
        "pass" if score >= threshold and violations == 0 else "fail"
    )
    return StatisticalSimilarityResult(
        status=status,
        score=score,
        violations=violations,
        evaluated_fields=tuple(evaluated),
        skipped_fields=skipped,
        detail=(
            f"Evaluated {len(evaluated)} fields with KS and normalized Wasserstein "
            f"distance for numeric data and chi-squared goodness-of-fit for categorical "
            f"data; skipped {skipped}. Pass threshold: {threshold * 100:.0f}%. "
            f"Lowest scores: {_lowest_scores(evaluated)}."
        ),
    )

