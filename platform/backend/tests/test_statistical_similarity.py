import pandas as pd

from app.evaluation.statistical_similarity import evaluate_statistical_similarity


def _schema() -> dict:
    return {
        "Patient": {
            "patient_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "age": {"type": "integer"},
            "gender": {"type": "text"},
            "diagnosis_date": {"type": "date"},
        }
    }


def test_matching_numeric_and_categorical_fields_pass():
    generated = {
        "Patient": pd.DataFrame(
            {
                "patient_id": [1, 2, 3, 4],
                "age": [20, 30, 40, 50],
                "gender": ["F", "F", "M", "M"],
                "diagnosis_date": ["2026-01-01"] * 4,
            }
        )
    }
    reference = {
        "Patient": [
            {"patient_id": 10, "age": 20, "gender": "F", "diagnosis_date": "2025-01-01"},
            {"patient_id": 11, "age": 30, "gender": "F", "diagnosis_date": "2025-01-02"},
            {"patient_id": 12, "age": 40, "gender": "M", "diagnosis_date": "2025-01-03"},
            {"patient_id": 13, "age": 50, "gender": "M", "diagnosis_date": "2025-01-04"},
        ]
    }

    result = evaluate_statistical_similarity(generated, _schema(), reference)

    assert result.status == "pass"
    assert result.score == 1.0
    assert result.violations == 0
    assert {(field.column, field.kind) for field in result.evaluated_fields} == {
        ("age", "numeric"),
        ("gender", "categorical"),
    }
    assert "KS and normalized Wasserstein" in result.detail
    assert "chi-squared goodness-of-fit" in result.detail


def test_shifted_numeric_and_unseen_categorical_values_fail():
    generated = {
        "Patient": pd.DataFrame(
            {
                "age": [100, 110, 120, 130],
                "gender": ["X", "X", "X", "X"],
            }
        )
    }
    reference = {
        "Patient": [
            {"age": 20, "gender": "F"},
            {"age": 30, "gender": "F"},
            {"age": 40, "gender": "M"},
            {"age": 50, "gender": "M"},
        ]
    }

    result = evaluate_statistical_similarity(generated, _schema(), reference)

    assert result.status == "fail"
    assert result.score is not None and 0.0 <= result.score < 0.80
    assert result.violations == 2
    numeric = next(field for field in result.evaluated_fields if field.column == "age")
    categorical = next(field for field in result.evaluated_fields if field.column == "gender")
    assert numeric.ks_statistic == 1.0
    assert numeric.wasserstein_distance is not None
    assert categorical.chi_square_statistic is not None


def test_missing_reference_returns_not_evaluated_without_mutating_input():
    frame = pd.DataFrame({"age": [20, 30], "gender": ["F", "M"]})
    original = frame.copy(deep=True)

    result = evaluate_statistical_similarity({"Patient": frame}, _schema(), {})

    assert result.status == "not_evaluated"
    assert result.score is None
    assert result.violations == 0
    assert result.detail.startswith("Provide a reference sample")
    pd.testing.assert_frame_equal(frame, original)


def test_reference_without_comparable_fields_is_not_evaluated():
    result = evaluate_statistical_similarity(
        {"Patient": pd.DataFrame({"age": [20, 30]})},
        _schema(),
        {"Patient": [{"unrelated": 1}, {"unrelated": 2}]},
    )

    assert result.status == "not_evaluated"
    assert result.score is None
    assert result.skipped_fields == 2
    assert "did not contain any comparable" in result.detail


def test_nulls_and_matching_constant_values_are_handled():
    result = evaluate_statistical_similarity(
        {
            "Patient": pd.DataFrame(
                {"age": [10, 10, None], "gender": ["F", "F", None]}
            )
        },
        _schema(),
        {
            "Patient": [
                {"age": 10, "gender": "F"},
                {"age": 10, "gender": "F"},
                {"age": None, "gender": None},
            ]
        },
    )

    assert result.status == "pass"
    assert result.score == 1.0
    assert result.violations == 0


def test_matching_uneven_categorical_distribution_scores_one():
    result = evaluate_statistical_similarity(
        {
            "Patient": pd.DataFrame(
                {"gender": ["F", "F", "F", "M"]}
            )
        },
        _schema(),
        {
            "Patient": [
                {"gender": "F"},
                {"gender": "F"},
                {"gender": "F"},
                {"gender": "M"},
            ]
        },
    )

    assert result.status == "pass"
    assert result.score == 1.0
    assert result.evaluated_fields[0].chi_square_statistic == 0.0

