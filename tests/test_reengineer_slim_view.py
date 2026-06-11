"""The Reengineer detail view's slim mode (?slim=1).

The Reengineer tab polls its detail view every 5s while a run is active.
Each variant carries a ~3-3.8KB generation prompt the strip never renders —
on a 45-variant run that's ~70% of the payload, re-downloaded ~480 times per
run. slim mode drops `prompt` per variant while keeping everything the UI
actually reads (status, urls, qc_*, fallback_model, approval state).
"""
from __future__ import annotations

import pytest

from character_swap import api
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)

_LONG_PROMPT = "PRESERVE EVERYTHING. " * 200       # ~4KB, like the real one


def _job():
    chars = {}
    for c in ("cA", "cB"):
        images = [
            GeneratedImage(variant_id=f"{c}-v{i}", path=f"/out/{c}-v{i}.png",
                           prompt=_LONG_PROMPT, scene_id="s1",
                           status=VariantStatus.READY, qc_status="passed",
                           qc_attempts=2,
                           fallback_model="nbp-swap" if i == 0 else None)
            for i in range(2)
        ]
        chars[c] = JobCharacter(char_id=c, name=c, source_image_path="/c.png",
                                status=CharStatus.AWAITING_APPROVAL,
                                images=images)
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/scene.png", characters=chars)


@pytest.fixture
def fake_store(monkeypatch):
    job = _job()

    class _Fake:
        def get_job(self, jid):
            return job if jid == "j1" else None

        def get_scene(self, sid):
            return None

    monkeypatch.setattr(api, "store", lambda: _Fake())
    return job


def _state():
    return {"re_id": "re_1", "status": "swapping", "job_id": "j1",
            "scenes": [{"idx": 0, "scene_id": "s1"}]}


def test_slim_drops_variant_prompts(fake_store):
    view = api._reengineer_view(_state(), slim=True)
    for jc in view["job"]["characters"].values():
        for img in jc["images"]:
            assert "prompt" not in img


def test_full_view_keeps_prompts(fake_store):
    view = api._reengineer_view(_state())
    for jc in view["job"]["characters"].values():
        for img in jc["images"]:
            assert img["prompt"] == _LONG_PROMPT


def test_slim_keeps_everything_the_strip_renders(fake_store):
    view = api._reengineer_view(_state(), slim=True)
    imgs = [img for jc in view["job"]["characters"].values()
            for img in jc["images"]]
    assert len(imgs) == 4
    for img in imgs:
        for key in ("variant_id", "url", "status", "scene_id",
                    "qc_status", "qc_attempts", "fallback_model"):
            assert key in img
    assert any(img["fallback_model"] == "nbp-swap" for img in imgs)


def test_slim_payload_is_substantially_smaller(fake_store):
    import json
    full = len(json.dumps(api._reengineer_view(_state()), default=str))
    slim = len(json.dumps(api._reengineer_view(_state(), slim=True), default=str))
    assert slim < full * 0.5


def test_slim_without_job_is_harmless(fake_store):
    view = api._reengineer_view({"re_id": "re_2", "status": "analyzing"},
                                slim=True)
    assert "job" not in view
