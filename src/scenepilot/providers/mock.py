"""Deterministic provider used for key-free job lifecycle testing."""

from __future__ import annotations


class MockJobProvider:
    """Completes intake jobs without claiming to render video or supply footage."""

    warning = (
        "Mock provider mode: job intake and lifecycle were exercised, but video rendering "
        "is not implemented and no media artifacts were generated."
    )

    def process(self, input_type: str, request_data: dict) -> dict[str, object]:
        return {
            "input_type": input_type,
            "accepted": True,
            "warnings": [self.warning],
            "result_links": {},
        }
