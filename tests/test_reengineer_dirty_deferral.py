"""Dirty-flag deferral during in-flight video phases (audit 2026-07-01).

Approve-swaps and per-slot image regens (✕↻ / in-place retry, SAME
variant_id) are ALLOWED while a reengineer run is animating / reanimating /
assembling — the job-level approve/retry endpoints skip the movement lock for
`from_reengineer` jobs and the approval strip stays clickable. But the
scenes-PATCH that records the `dirty` flag 409s in exactly those statuses
(`runner_reengineer._EDITABLE_RUN_STATES`), and the frontend markers
(`_reMarkVariantSceneDirty` / `_reMarkSceneDirtyIdx`) used to return early
for them too — so an image regenerated mid-phase silently shipped its OLD
clip in the final: no '● ändrad' badge, no stale_scenes note, a silent
image/clip mismatch (the clip still matches by source_variant_id).

The fix queues the mark client-side (`_reDirtyPending`) and flushes the
PATCH the moment `refreshReengineer` sees the run in a markable status again
(polling is guaranteed live through the in-flight phases). These are
source-grep locks in the style of test_mobile_ui.py — the marking helpers
are thin fetch wrappers with no importable logic to unit-test.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    """Source segment between two unique anchors (asserts both exist)."""
    assert start in _JS, f"anchor missing: {start!r}"
    seg = _JS.split(start, 1)[1]
    assert end in seg, f"anchor missing after {start!r}: {end!r}"
    return seg.split(end, 1)[0]


def test_defer_queue_and_status_lists_present():
    # The pending queue field exists…
    assert "_reDirtyPending: {}" in _JS
    # …the in-flight phases DEFER (not drop) the mark…
    defer = _JS.split("_reDirtyDeferStatuses:", 1)[1][:100]
    for s in ("'animating'", "'reanimating'", "'assembling'"):
        assert s in defer
    # …and the markable set is the post-clip subset of _EDITABLE_RUN_STATES.
    markable = _JS.split("_reDirtyMarkableStatuses:", 1)[1][:130]
    for s in ("'done'", "'partial_success'", "'failed'", "'awaiting_assembly'"):
        assert s in markable
    # At-gate behavior preserved: awaiting_approval (no clips exist yet) is
    # neither marked nor deferred.
    assert "'awaiting_approval'" not in markable
    assert "'awaiting_approval'" not in defer


def test_mark_scene_dirty_idx_defers_during_video_phase():
    body = _between("async _reMarkSceneDirtyIdx(run, idx) {",
                    "async reengineerUploadEndFrame")
    assert "_reDirtyDeferStatuses.includes(run.status)" in body
    assert "_reDirtyPending" in body                    # queued, not dropped
    assert "_reDirtyMarkableStatuses.includes(run.status)" in body
    assert "dirty: true" in body                        # the PATCH still fires


def test_variant_marker_delegates_to_single_patch_site():
    body = _between("async _reMarkVariantSceneDirty(run, charId, variantId) {",
                    "reengineerAddSceneFile")
    assert "this._reMarkSceneDirtyIdx(run, idx)" in body
    # One shared PATCH site — no duplicated fetch that could drift out of
    # sync with the deferral logic.
    assert "fetch(" not in body


def test_refresh_flushes_deferred_marks():
    body = _between("async refreshReengineer(reId) {",
                    "_startReengineerPolling() {")
    assert "_reDirtyPending[reId]" in body
    assert "_reDirtyMarkableStatuses.includes(fresh.status)" in body
    assert "_reMarkSceneDirtyIdx(fresh," in body
