from scenepilot.evaluation_report import build_evaluation_report


def test_report_normalizes_human_ratings_and_compares_actual_model_scores():
    report = build_evaluation_report(
        [
            {
                "human_score": 5,
                "model_score": 1.0,
                "evaluator_provider": "vlm",
                "evaluator_model": "model-a",
                "evaluator_version": "v1",
            },
            {
                "human_score": 1,
                "model_score": 0.0,
                "evaluator_provider": "vlm",
                "evaluator_model": "model-a",
                "evaluator_version": "v1",
            },
            {
                "human_score": 2,
                "model_score": 0.5,
                "evaluator_provider": "vlm",
                "evaluator_model": "model-a",
                "evaluator_version": "v1",
            },
        ]
    )
    assert report["status"] == "computed"
    assert report["metrics"]["comparable_rating_count"] == 3
    assert report["metrics"]["mean_absolute_error_normalized"] == 0.0833
    assert report["metrics"]["spearman_rank_correlation"] == 1.0
    assert report["comparisons"][0]["human_score_normalized"] == 1.0


def test_report_does_not_claim_agreement_without_actual_comparable_ratings():
    report = build_evaluation_report(
        [{"human_score": 5, "model_score": None, "evaluator_model": None}]
    )
    assert report["status"] == "insufficient_data"
    assert report["metrics"]["comparable_rating_count"] == 0
    assert report["metrics"]["mean_absolute_error_normalized"] is None
    assert report["metrics"]["spearman_rank_correlation"] is None


def test_report_spearman_is_unavailable_for_tied_constant_scores():
    report = build_evaluation_report(
        [
            {
                "human_score": 3,
                "model_score": 0.5,
                "evaluator_model": "model-a",
            },
            {
                "human_score": 3,
                "model_score": 0.5,
                "evaluator_model": "model-a",
            },
        ]
    )
    assert report["metrics"]["spearman_rank_correlation"] is None
