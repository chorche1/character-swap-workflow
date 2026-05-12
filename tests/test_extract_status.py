"""Unit tests for pipeline._extract_status — parses Grok response shapes."""
from __future__ import annotations

import pytest

from character_swap.pipeline import _extract_status


@pytest.mark.parametrize(
    "payload, expected_status, expected_url",
    [
        # 1. Canonical shape (verified in CLAUDE.md): {"status": "done", "video": {"url": ...}}
        (
            {"status": "done", "video": {"url": "https://x.ai/v.mp4", "duration": 10},
             "progress": 100},
            "done",
            "https://x.ai/v.mp4",
        ),
        # 2. In-flight: status with no video block.
        (
            {"status": "processing", "progress": 30},
            "processing",
            None,
        ),
        # 3. Alternative field naming: "state" instead of "status",
        #    flat "video_url".
        (
            {"state": "DONE", "video_url": "https://example.com/x.mp4"},
            "done",
            "https://example.com/x.mp4",
        ),
        # 4. Wrapped under "data" key with nested video object.
        (
            {"status": "done", "data": {"video": {"url": "https://cdn/y.mp4"}}},
            "done",
            "https://cdn/y.mp4",
        ),
        # 5. Wrapped under "data" with flat url.
        (
            {"status": "done", "data": {"url": "https://cdn/z.mp4"}},
            "done",
            "https://cdn/z.mp4",
        ),
        # 6. Outputs list shape.
        (
            {"status": "done", "outputs": [{"url": "https://cdn/o.mp4"}]},
            "done",
            "https://cdn/o.mp4",
        ),
        # 7. Empty payload — caller should treat as still-unknown.
        (
            {},
            "unknown",
            None,
        ),
        # 8. Top-level url fallback (no video block).
        (
            {"status": "done", "url": "https://cdn/top.mp4"},
            "done",
            "https://cdn/top.mp4",
        ),
        # 9. Failed terminal state.
        (
            {"status": "failed", "error": "boom"},
            "failed",
            None,
        ),
        # 10. Mixed-case status normalized to lowercase.
        (
            {"status": "Cancelled"},
            "cancelled",
            None,
        ),
    ],
)
def test_extract_status_shapes(payload, expected_status, expected_url):
    status, url = _extract_status(payload)
    assert status == expected_status
    assert url == expected_url


def test_outputs_with_video_url_key():
    """outputs[0] can use 'video_url' instead of 'url'."""
    payload = {"status": "done", "outputs": [{"video_url": "https://x/v.mp4"}]}
    status, url = _extract_status(payload)
    assert status == "done"
    assert url == "https://x/v.mp4"


def test_video_object_takes_precedence_over_top_url():
    """If both video.url and top-level url exist, video.url wins."""
    payload = {
        "status": "done",
        "video": {"url": "https://from-video/v.mp4"},
        "url": "https://from-top/v.mp4",
    }
    _, url = _extract_status(payload)
    assert url == "https://from-video/v.mp4"
