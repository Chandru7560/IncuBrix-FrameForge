from __future__ import annotations

from pathlib import Path

import pytest

from scenepilot.batch_runner import (
    _iter_inputs,
    _load_json_input,
    _stable_key,
    _submit,
)


def test_batch_input_discovery_is_recursive_and_extension_scoped(tmp_path):
    (tmp_path / "script.txt").write_text("A narration script.", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "scenes.json").write_text('{"scenes":[{"text":"Scene"}]}', encoding="utf-8")
    (nested / "voice.wav").write_bytes(b"audio")
    (nested / "notes.md").write_text("not a supported input", encoding="utf-8")
    assert [path.name for path in _iter_inputs(tmp_path)] == [
        "scenes.json",
        "voice.wav",
        "script.txt",
    ]


def test_batch_json_payload_must_be_an_object():
    with pytest.raises(ValueError, match="object"):
        _load_json_input(b"[1, 2]")
    with pytest.raises(ValueError, match="malformed"):
        _load_json_input(b'{"script":')


def test_batch_json_unwraps_labeled_testpack_case():
    payload = _load_json_input(
        b'{"case_id":"case-1","request":{"script":"A valid fixture."},"coverage":["scene"]}'
    )
    assert payload == {"script": "A valid fixture."}


def test_all_testpack_json_fixtures_contain_batchable_requests():
    cases = Path(__file__).parents[2] / "testpack" / "cases"
    fixtures = sorted(cases.glob("*.json"))
    assert len(fixtures) == 10
    for fixture in fixtures:
        payload = _load_json_input(fixture.read_bytes())
        assert "script" in payload or "scenes" in payload, fixture.name


def test_batch_idempotency_key_is_repeatable_and_input_sensitive():
    first = _stable_key("voice.wav", b"fixture", ["16:9"])
    assert first == _stable_key("voice.wav", b"fixture", ["16:9"])
    assert first != _stable_key("voice.wav", b"changed", ["16:9"])
    assert first != _stable_key("voice.wav", b"fixture", ["9:16"])


def test_batch_json_submission_adds_defaults_and_stable_key():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"job_id": "job-1", "status": "queued"}

    class Client:
        def post(self, url, *, headers, json):
            self.request = (url, headers, json)
            return Response()

    source = Path("scene.json")
    client = Client()
    result = _submit(
        client,
        api_base_url="http://localhost:8000",
        path=source,
        relative_path="scene.json",
        content=b'{"scenes":[{"text":"Measured scene."}]}',
        aspect_ratios=["16:9"],
    )
    assert result["job_id"] == "job-1"
    assert client.request[0].endswith("/api/v1/broll/jobs")
    assert client.request[1]["Idempotency-Key"].startswith("scenepilot-")
    assert client.request[2]["aspect_ratios"] == ["16:9"]
    assert client.request[2]["captions"]["burn_in"] is True
