"""Build an evidence-based agreement report from stored job ratings and model scores."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        rank = (position + 1 + end) / 2
        for index in order[position:end]:
            ranks[index] = rank
        position = end
    return ranks


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_variance = sum((value - left_mean) ** 2 for value in left)
    right_variance = sum((value - right_mean) ** 2 for value in right)
    if left_variance == 0 or right_variance == 0:
        return None
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    return covariance / math.sqrt(left_variance * right_variance)


def build_evaluation_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    comparable = []
    excluded = 0
    for row in rows:
        human = row.get("human_score")
        model = row.get("model_score")
        if (
            not isinstance(human, int)
            or isinstance(human, bool)
            or not 1 <= human <= 5
            or not isinstance(model, (int, float))
            or isinstance(model, bool)
            or not 0 <= float(model) <= 1
            or row.get("evaluator_model") is None
        ):
            excluded += 1
            continue
        comparable.append(
            {
                **row,
                "human_score_normalized": round((human - 1) / 4, 4),
                "model_score_normalized": round(float(model), 4),
            }
        )
    human_scores = [row["human_score_normalized"] for row in comparable]
    model_scores = [row["model_score_normalized"] for row in comparable]
    mae = (
        sum(abs(human - model) for human, model in zip(human_scores, model_scores, strict=True))
        / len(comparable)
        if comparable
        else None
    )
    model_ranks = _average_ranks(model_scores) if comparable else []
    human_ranks = _average_ranks(human_scores) if comparable else []
    spearman = _correlation(model_ranks, human_ranks) if comparable else None
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "computed" if comparable else "insufficient_data",
        "normalization": {
            "human_1_to_5": "(rating - 1) / 4",
            "model_0_to_1": "used as provided by evaluator; provider 0-to-100 scores are divided by 100 at ingestion",
        },
        "metrics": {
            "comparable_rating_count": len(comparable),
            "excluded_record_count": excluded,
            "mean_absolute_error_normalized": round(mae, 4) if mae is not None else None,
            "spearman_rank_correlation": round(spearman, 4) if spearman is not None else None,
        },
        "evaluator_versions": dict(
            Counter(
                f"{row.get('evaluator_provider')}:{row.get('evaluator_model')}:{row.get('evaluator_version')}"
                for row in comparable
            )
        ),
        "comparisons": comparable,
        "interpretation": (
            "Agreement describes only the supplied human ratings and stored model evaluations; "
            "it does not establish general relevance accuracy."
        ),
    }


def _fetch_job_comparisons(api_base_url: str, job_id: str) -> list[dict[str, Any]]:
    base = api_base_url.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(30, connect=10)) as client:
        job_response = client.get(f"{base}/api/v1/jobs/{job_id}")
        job_response.raise_for_status()
        ratings_response = client.get(f"{base}/api/v1/jobs/{job_id}/ratings")
        ratings_response.raise_for_status()
    job = job_response.json()
    ratings = ratings_response.json().get("ratings", [])
    scenes = job.get("scene_plan", {}).get("scenes", [])
    by_scene: dict[str, list[dict[str, Any]]] = {}
    for rating in ratings:
        by_scene.setdefault(rating["scene_id"], []).append(rating)
    rows = []
    for scene in scenes:
        scene_id = scene.get("scene_id")
        for selection in scene.get("visual_retrieval", {}).get("by_aspect_ratio", {}).values():
            evaluation = selection.get("independent_relevance_evaluation", {})
            if evaluation.get("status") != "evaluated":
                continue
            for rating in by_scene.get(scene_id, []):
                rows.append(
                    {
                        "job_id": job_id,
                        "scene_id": scene_id,
                        "aspect_ratio": selection.get("aspect_ratio"),
                        "human_score": rating.get("score"),
                        "human_rationale": rating.get("rationale"),
                        "model_score": evaluation.get("score"),
                        "model_rationale": evaluation.get("rationale"),
                        "evaluator_provider": evaluation.get("provider"),
                        "evaluator_model": evaluation.get("model"),
                        "evaluator_version": evaluation.get("version"),
                        "compared_against": evaluation.get("compared_against"),
                    }
                )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare stored independent visual evaluations with human scene ratings."
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        rows = _fetch_job_comparisons(args.api_base_url, args.job_id)
        report = build_evaluation_report(rows)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report["metrics"], ensure_ascii=False))
        return 0
    except (OSError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        print(f"Evaluation report failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
