from __future__ import annotations

import io
import math
import struct
import threading
import time
import wave
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from scenepilot.main import create_app
from scenepilot.models import Job
from scenepilot.services.narrative_pipeline import NarrativePipelineError


def create_wav(*, silence: bool = True) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        samples = (
            0
            if silence
            else [
                int(3000 * math.sin(2 * math.pi * 440 * index / 8000))
                for index in range(8000)
            ]
        )
        audio.writeframes(
            b"\0\0" * 8000
            if silence
            else b"".join(struct.pack("<h", sample) for sample in samples)
        )
    return output.getvalue()


def create_wav_of_size(sample_count: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * sample_count)
    return output.getvalue()


def wait_for_terminal(client, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/jobs/{job_id}")
        assert result.status_code == 200
        body = result.json()
        if body["status"] in {"succeeded", "failed"}:
            return body
        time.sleep(0.01)
    raise AssertionError("Job did not reach a terminal state before the test deadline.")


def test_script_job_returns_scene_plan_and_marks_mock_audio(client):
    response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "script-job-0001"},
        json={"script": "A short test script.", "aspect_ratios": ["16:9", "1:1"]},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert response.headers["x-correlation-id"]
    job = wait_for_terminal(client, job_id)
    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    assert job["result_links"] == {}
    assert job["scene_plan"]["timing_authority"] == "measured_generated_audio"
    assert job["scene_plan"]["coverage"]["coverage_ratio"] == 1.0
    assert job["transcript"]["original_text"] == "A short test script."
    assert "not speech" in " ".join(job["warnings"])
    assert "SCENE_PLANNING_ONLY" in " ".join(job["warnings"])
    assert job["narration_duration_seconds"] > 0


def test_worker_failure_is_persisted_as_failed_job(client, monkeypatch):
    def fail_processing(self, input_type, request_data):
        raise NarrativePipelineError(
            "TEST_PROVIDER_FAILURE",
            "The injected provider failed.",
            warnings=["Fallback reason recorded."],
        )

    monkeypatch.setattr(
        "scenepilot.services.narrative_pipeline.NarrativeScenePipeline.process",
        fail_processing,
    )
    response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "failed-job-00001"},
        json={"script": "This job should fail."},
    )
    job = wait_for_terminal(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["progress"] == 100
    assert job["error"]["code"] == "TEST_PROVIDER_FAILURE"
    assert job["warnings"] == ["Fallback reason recorded."]


@pytest.mark.parametrize(
    ("artifact_name", "expected_code"),
    [
        ("missing-manifest.json", "INVALID_RENDER_ARTIFACT"),
        ("manifest.json", "RENDERED_ARTIFACT_MISSING"),
    ],
)
def test_worker_artifact_validation_failures_are_terminal_jobs(
    client, monkeypatch, artifact_name, expected_code
):
    def report_invalid_artifact(self, input_type, request_data):
        return {"warnings": [], "artifact_names": [artifact_name]}

    monkeypatch.setattr(
        "scenepilot.services.narrative_pipeline.NarrativeScenePipeline.process",
        report_invalid_artifact,
    )
    response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": f"artifact-failure-{artifact_name}"},
        json={"script": "The renderer output must be verified."},
    )

    job = wait_for_terminal(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["progress"] == 100
    assert job["error"]["code"] == expected_code


def test_scene_list_and_audio_url_are_accepted(client):
    scene_response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "scene-job-0001"},
        json={"scenes": [{"text": "A bicycle in a city", "visual_hint": "wide shot"}]},
    )
    url_response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "url-job-00001"},
        json={"audio_url": "https://example.com/voice.wav"},
    )
    assert scene_response.status_code == 202
    assert url_response.status_code == 202
    assert scene_response.json()["job_id"] != url_response.json()["job_id"]


def test_silent_audio_upload_fails_explicitly_without_fabricating_transcript(client):
    response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "upload-job-001"},
        files={"file": ("voice.wav", create_wav(), "audio/wav")},
    )
    assert response.status_code == 202, response.text
    job = wait_for_terminal(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "SILENT_AUDIO"
    assert job["transcript"] is None


def test_job_artifact_download_is_limited_to_persisted_safe_links(client, settings):
    job_id = str(uuid4())
    output = settings.output_directory / job_id
    output.mkdir(parents=True)
    (output / "video_16_9.mp4").write_bytes(b"validated-mp4-fixture")
    with client.app.state.session_factory.begin() as session:
        session.add(
            Job(
                id=job_id,
                status="succeeded",
                progress=100,
                input_type="script",
                request_data={"script": "A fixture job"},
                correlation_id="artifact-test",
                result_links={
                    "video_16:9": f"/api/v1/jobs/{job_id}/artifacts/video_16_9.mp4"
                },
                warnings=[],
            )
        )
    downloaded = client.get(
        f"/api/v1/jobs/{job_id}/artifacts/video_16_9.mp4"
    )
    missing = client.get(
        f"/api/v1/jobs/{job_id}/artifacts/manifest.json"
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "video/mp4"
    assert downloaded.content == b"validated-mp4-fixture"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_frameforge_studio_is_served_with_local_assets(client):
    page = client.get("/")
    stylesheet = client.get("/studio-assets/studio.css")
    script = client.get("/studio-assets/studio.js")
    assert page.status_code == stylesheet.status_code == script.status_code == 200
    assert "FrameForge Studio" in page.text
    assert "scene intelligence board" in page.text.lower()
    assert "Fetching is not enabled" not in page.text
    assert "downloaded with size, redirect, format, and duration checks." in page.text
    assert "Local mode plans scenes only" in page.text
    assert "/studio-assets/studio.js?v=20261019-3" in page.text
    assert "API online" in script.text
    assert "pollingJobId" in script.text
    assert "if (state.pollingJobId === jobId) return;" in script.text
    assert "if (state.polling) return;" not in script.text
    assert "https://fonts.googleapis.com" not in stylesheet.text


def test_scene_ratings_are_persistent_and_idempotent(client):
    accepted = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "rating-job-0001"},
        json={"script": "Cloud computing carries meaning beyond weather."},
    )
    job = wait_for_terminal(client, accepted.json()["job_id"])
    assert job["status"] == "succeeded"
    scene_id = job["scene_plan"]["scenes"][0]["scene_id"]
    path = f"/api/v1/jobs/{job['job_id']}/ratings"
    payload = {
        "scene_id": scene_id,
        "score": 4,
        "rationale": "The selected visual communicates the described concept.",
    }
    first = client.post(
        path,
        headers={"Idempotency-Key": "rating-submit-0001"},
        json=payload,
    )
    replay = client.post(
        path,
        headers={"Idempotency-Key": "rating-submit-0001"},
        json=payload,
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["rating_id"] == first.json()["rating_id"]
    assert replay.json()["idempotent_replay"] is True
    ratings = client.get(path)
    assert ratings.status_code == 200
    assert len(ratings.json()["ratings"]) == 1
    assert ratings.json()["ratings"][0]["score"] == 4


def test_scene_rating_rejects_idempotency_reuse_for_changed_score(client):
    accepted = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "rating-job-0002"},
        json={"script": "An independent scene to rate."},
    )
    job = wait_for_terminal(client, accepted.json()["job_id"])
    scene_id = job["scene_plan"]["scenes"][0]["scene_id"]
    path = f"/api/v1/jobs/{job['job_id']}/ratings"
    first = client.post(
        path,
        headers={"Idempotency-Key": "rating-reuse-0001"},
        json={"scene_id": scene_id, "score": 2},
    )
    changed = client.post(
        path,
        headers={"Idempotency-Key": "rating-reuse-0001"},
        json={"scene_id": scene_id, "score": 5},
    )
    assert first.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_audio_upload_uses_configured_recognizer_and_persists_scene_plan(settings, monkeypatch):
    from scenepilot.providers.narration import TranscriptSegment, TranscriptionResult
    from scenepilot.services.narrative_pipeline import NarrativeScenePipeline

    class Recognizer:
        def transcribe(self, audio_path, probe):
            return TranscriptionResult(
                original_text="Cloud computing serves useful data.",
                segments=(
                    TranscriptSegment("Cloud computing", 0.0, 0.5, confidence=0.97),
                    TranscriptSegment("serves useful data.", 0.5, 1.0, confidence=0.95),
                ),
                provider="integration-test",
                model="test-stt-v1",
                warnings=("Transcript warning retained.",),
            )

    monkeypatch.setattr(
        "scenepilot.api.routes.jobs.validate_public_https_url",
        lambda url, allow_hosts: url,
    )
    provider = NarrativeScenePipeline(settings, speech_adapter=Recognizer())
    app = create_app(settings, job_provider=provider)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/broll/jobs",
            headers={"Idempotency-Key": "recognized-upload-1"},
            files={"file": ("voice.wav", create_wav(silence=False), "audio/wav")},
        )
        assert response.status_code == 202
        job = wait_for_terminal(test_client, response.json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["transcript"]["original_text"] == "Cloud computing serves useful data."
    assert job["scene_plan"]["scenes"][0]["intent_graph"]["central_concept"] == "cloud computing"
    assert "Transcript warning retained." in job["transcript"]["warnings"]
    assert job["narration_duration_seconds"] == 1.0


def test_invalid_and_oversized_audio_uploads_are_rejected(client):
    invalid = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "invalid-upload-01"},
        files={"file": ("voice.exe", b"not audio", "application/octet-stream")},
    )
    oversized = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "oversized-upload-1"},
        files={
            "file": (
                "voice.wav",
                create_wav_of_size(600_000),
                "audio/wav",
            )
        },
    )
    assert invalid.status_code == 415
    assert invalid.json()["error"]["code"] == "UNSUPPORTED_AUDIO_FORMAT"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


def test_same_key_and_same_request_return_same_job(client):
    headers = {"Idempotency-Key": "retry-job-00001"}
    payload = {"script": "Same request."}
    first = client.post("/api/v1/broll/jobs", headers=headers, json=payload)
    second = client.post("/api/v1/broll/jobs", headers=headers, json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]


def test_same_key_different_payload_returns_structured_conflict(client):
    headers = {"Idempotency-Key": "conflict-job-01"}
    assert client.post(
        "/api/v1/broll/jobs", headers=headers, json={"script": "First"}
    ).status_code == 202
    response = client.post(
        "/api/v1/broll/jobs", headers=headers, json={"script": "Different"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_idempotency_key_is_required_for_every_post(client):
    response = client.post("/api/v1/broll/jobs", json={"script": "Missing key"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_concurrent_identical_posts_create_one_job(client):
    barrier = threading.Barrier(8)

    def submit(_):
        barrier.wait(timeout=2)
        return client.post(
            "/api/v1/broll/jobs",
            headers={"Idempotency-Key": "parallel-job-0001"},
            json={"script": "Concurrent request."},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(submit, range(8)))
    assert all(response.status_code == 202 for response in responses)
    job_ids = {response.json()["job_id"] for response in responses}
    assert len(job_ids) == 1


def test_job_records_survive_api_restart(settings, monkeypatch):
    monkeypatch.setattr(
        "scenepilot.api.routes.jobs.validate_public_https_url",
        lambda url, allow_hosts: url,
    )
    persistent_settings = replace(settings, worker_enabled=False)
    first_app = create_app(persistent_settings)
    with TestClient(first_app) as first_client:
        response = first_client.post(
            "/api/v1/broll/jobs",
            headers={"Idempotency-Key": "persistent-job-01"},
            json={"script": "Persist this job."},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

    second_app = create_app(persistent_settings)
    with TestClient(second_app) as second_client:
        persisted = second_client.get(f"/api/v1/jobs/{job_id}")
        retry = second_client.post(
            "/api/v1/broll/jobs",
            headers={"Idempotency-Key": "persistent-job-01"},
            json={"script": "Persist this job."},
        )
    assert persisted.status_code == 200
    assert persisted.json()["status"] == "queued"
    assert retry.status_code == 202
    assert retry.json()["job_id"] == job_id


def test_distinct_concurrent_jobs_are_independent(client):
    def submit(index):
        return client.post(
            "/api/v1/broll/jobs",
            headers={"Idempotency-Key": f"distinct-job-{index:04d}"},
            json={"script": f"Independent request {index}."},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(submit, range(8)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["job_id"] for response in responses}) == 8


def test_validation_errors_and_unknown_jobs_use_standard_envelope(client):
    invalid = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "invalid-job-00001"},
        json={"script": "  "},
    )
    missing = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
    for response, expected_status in ((invalid, 422), (missing, 404)):
        assert response.status_code == expected_status
        error = response.json()["error"]
        assert set(error) == {"code", "message", "retryable", "details"}


def test_health_and_readiness_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_oversized_content_length_is_rejected(client):
    response = client.post(
        "/api/v1/broll/jobs",
        headers={
            "Idempotency-Key": "oversized-job-01",
            "Content-Length": str(3 * 1024 * 1024),
            "Content-Type": "application/json",
        },
        content=b"",
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_malformed_json_is_a_structured_error(client):
    response = client.post(
        "/api/v1/broll/jobs",
        headers={
            "Idempotency-Key": "malformed-json-1",
            "Content-Type": "application/json",
        },
        content=b'{"script":',
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


def test_malformed_scene_list_returns_structured_validation_error(client):
    response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "malformed-scenes-0001"},
        json={"scenes": [{"visual_hint": "A visual without narration"}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_audio_input_storage_failure_is_not_reported_as_accepted_job(client, monkeypatch):
    def fail_write(self, data):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(Path, "write_bytes", fail_write)
    response = client.post(
        "/api/v1/broll/jobs",
        headers={"Idempotency-Key": "storage-failure-0001"},
        files={"file": ("voice.wav", create_wav(), "audio/wav")},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INPUT_STORAGE_FAILED"


def test_openapi_contract_matches_checked_in_file(client):
    import yaml

    with open("openapi.yaml", encoding="utf-8") as contract:
        checked_in_schema = yaml.safe_load(contract)
    assert checked_in_schema == client.get("/openapi.json").json()

    business_paths = [
        path for path in checked_in_schema["paths"] if path not in {"/health", "/ready"}
    ]
    assert business_paths
    assert all(path.startswith("/api/v1/") for path in business_paths)
