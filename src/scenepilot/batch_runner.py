"""Submit supported files or folders to the ScenePilot API and collect all outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

SUPPORTED_AUDIO = {".mp3", ".wav", ".m4a"}
SUPPORTED_TEXT = {".txt"}
SUPPORTED_JSON = {".json"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:64] or "input"


def _stable_key(relative_path: str, content: bytes, aspect_ratios: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(relative_path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(content)
    digest.update(json.dumps(aspect_ratios, separators=(",", ":")).encode("utf-8"))
    return f"scenepilot-{digest.hexdigest()[:48]}"


def _iter_inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(f"Input path does not exist: {path}")
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in SUPPORTED_AUDIO | SUPPORTED_TEXT | SUPPORTED_JSON
    )


def _load_json_input(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON input is malformed or is not UTF-8.") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON input must contain a job request object.")
    if "case_id" in payload and "request" in payload:
        request = payload["request"]
        if not isinstance(request, dict):
            raise ValueError("Test-pack case request must be a JSON object.")
        return request
    return payload


def _submit(
    client: httpx.Client,
    *,
    api_base_url: str,
    path: Path,
    relative_path: str,
    content: bytes,
    aspect_ratios: list[str],
) -> dict[str, Any]:
    headers = {"Idempotency-Key": _stable_key(relative_path, content, aspect_ratios)}
    suffix = path.suffix.lower()
    options: dict[str, Any] = {
        "aspect_ratios": aspect_ratios,
        "captions": {"burn_in": True, "srt": True},
    }
    if suffix in SUPPORTED_AUDIO:
        response = client.post(
            f"{api_base_url}/api/v1/broll/jobs",
            headers=headers,
            data={
                "aspect_ratios": json.dumps(aspect_ratios),
                "captions": json.dumps(options["captions"]),
            },
            files={"file": (path.name, content, _audio_media_type(suffix))},
        )
    elif suffix in SUPPORTED_TEXT:
        text = content.decode("utf-8-sig")
        response = client.post(
            f"{api_base_url}/api/v1/broll/jobs",
            headers=headers,
            json={**options, "script": text},
        )
    elif suffix in SUPPORTED_JSON:
        payload = _load_json_input(content)
        payload.setdefault("aspect_ratios", aspect_ratios)
        payload.setdefault("captions", options["captions"])
        response = client.post(
            f"{api_base_url}/api/v1/broll/jobs",
            headers=headers,
            json=payload,
        )
    else:
        raise ValueError(f"Unsupported input extension: {suffix or '(none)'}")
    response.raise_for_status()
    return response.json()


def _audio_media_type(suffix: str) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
    }[suffix]


def _poll(
    client: httpx.Client,
    *,
    api_base_url: str,
    job_id: str,
    poll_interval: float,
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"{api_base_url}/api/v1/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job.get("status") in {"succeeded", "failed"}:
            return job, False
        if time.monotonic() >= deadline:
            return job, True
        time.sleep(poll_interval)


def _collect_artifacts(
    client: httpx.Client,
    *,
    api_base_url: str,
    job: dict[str, Any],
    destination: Path,
) -> list[dict[str, Any]]:
    saved = []
    if job.get("status") != "succeeded":
        return saved
    for label, relative_url in job.get("result_links", {}).items():
        if not isinstance(relative_url, str) or not relative_url.startswith("/api/v1/jobs/"):
            continue
        response = client.get(f"{api_base_url}{relative_url}")
        response.raise_for_status()
        name = Path(relative_url).name
        if name in {"", ".", ".."} or "/" in name or "\\" in name:
            raise ValueError("API returned an invalid artifact filename.")
        target = destination / name
        target.write_bytes(response.content)
        saved.append(
            {
                "label": label,
                "filename": name,
                "size_bytes": len(response.content),
                "download_url": relative_url,
            }
        )
    return saved


def run_batch(
    *,
    input_path: Path,
    output_directory: Path,
    api_base_url: str,
    aspect_ratios: list[str],
    poll_interval: float,
    timeout: float,
) -> int:
    inputs = _iter_inputs(input_path)
    if not inputs:
        raise ValueError(f"No supported files found under {input_path}")
    root = input_path if input_path.is_dir() else input_path.parent
    output_directory.mkdir(parents=True, exist_ok=True)
    log_path = output_directory / "batch.jsonl"
    failures = 0

    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for index, path in enumerate(inputs, start=1):
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
            log: dict[str, Any] = {
                "timestamp": _timestamp(),
                "input": relative,
                "input_sha256": hashlib.sha256(content).hexdigest(),
                "status": "failed",
            }
            try:
                accepted = _submit(
                    client,
                    api_base_url=api_base_url.rstrip("/"),
                    path=path,
                    relative_path=relative,
                    content=content,
                    aspect_ratios=aspect_ratios,
                )
                job_id = accepted["job_id"]
                log["job_id"] = job_id
                job, timed_out = _poll(
                    client,
                    api_base_url=api_base_url.rstrip("/"),
                    job_id=job_id,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
                destination = output_directory / f"{index:02d}-{_safe_name(path.stem)}-{job_id[:8]}"
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "job.json").write_text(
                    json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                artifacts = _collect_artifacts(
                    client,
                    api_base_url=api_base_url.rstrip("/"),
                    job=job,
                    destination=destination,
                )
                log.update(
                    status="timed_out" if timed_out else job.get("status", "unknown"),
                    progress=job.get("progress"),
                    warnings=job.get("warnings", []),
                    error=job.get("error"),
                    artifacts=artifacts,
                    output_directory=destination.name,
                )
                if timed_out or job.get("status") != "succeeded":
                    failures += 1
            except (OSError, UnicodeDecodeError, ValueError, httpx.HTTPError, KeyError) as exc:
                log["error"] = f"{type(exc).__name__}: {exc}"
                failures += 1
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log, ensure_ascii=False) + "\n")
            print(json.dumps(log, ensure_ascii=False))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Submit supported files/folders to ScenePilot and retain every result."
    )
    parser.add_argument("--input", required=True, type=Path, help="Input file or recursive folder.")
    parser.add_argument("--output", required=True, type=Path, help="Directory for job data and artifacts.")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000", help="Base API URL.")
    parser.add_argument("--aspect-ratios", default="16:9", help="Comma-separated output ratios.")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=900.0, help="Per-job wait timeout in seconds.")
    args = parser.parse_args(argv)
    ratios = [ratio.strip() for ratio in args.aspect_ratios.split(",") if ratio.strip()]
    if not ratios or any(ratio not in {"16:9", "9:16", "1:1"} for ratio in ratios):
        parser.error("--aspect-ratios may contain only 16:9, 9:16, and 1:1")
    if len(set(ratios)) != len(ratios):
        parser.error("--aspect-ratios must not contain duplicates")
    if args.poll_interval <= 0 or args.timeout <= 0:
        parser.error("--poll-interval and --timeout must be positive")
    try:
        return run_batch(
            input_path=args.input,
            output_directory=args.output,
            api_base_url=args.api_base_url,
            aspect_ratios=ratios,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    except (OSError, ValueError) as exc:
        print(f"Batch runner error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
