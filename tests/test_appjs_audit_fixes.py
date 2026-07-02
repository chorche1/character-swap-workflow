"""Source-grep locks for the 2026-07-01 reliability-audit app.js fixes.

Each test pins one CONFIRMED frontend bug fix so a refactor can't silently
reintroduce it. Anchors are the load-bearing lines of each fix, not styling.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_ws_reconnect_targets_current_job_only():
    # Audit: a reconnect timer captured the OLD jobId and re-attached to it
    # after a job switch; the unguarded snapshot then flipped this.job back.
    body = _JS.split("connectWS(jobId) {")[1]
    # connectWS refuses to attach to a job that's no longer open.
    assert "if (!this.job || this.job.job_id !== jobId) return;" in body[:600]
    # The pending reconnect timer is tracked + cancelled, never orphaned.
    assert "_wsReconnectTimer" in body[:900]
    assert "this._wsReconnectTimer = setTimeout(() => this.connectWS(jobId), delay);" in body


def test_snapshot_event_guarded_by_job_id():
    snap = _JS.split("if (evt.kind === 'snapshot') {")[1]
    assert "evt.job.job_id !== this.job.job_id" in snap[:300]


def test_swap_milestones_seed_baseline_on_first_snapshot():
    # Audit: prevSnap defaulted to {allTerminal:false} → opening a long-finished
    # job fired a false 'Swap job complete' chime + OS popup every session.
    body = _JS.split("_fireSwapMilestones(job) {")[1]
    assert "this._lastSwapJobSnapshot[job.job_id] || null" in body[:1000]
    # Both milestone fires require an EXISTING baseline (first sight seeds).
    assert "if (prevSnap && prevStatus !== 'awaiting_approval'" in body
    assert "if (prevSnap && charEntries.length > 0 && !prevSnap.allTerminal" in body
    # The fabricated non-terminal default must never come back.
    assert "|| { chars: {}, allTerminal: false }" not in body[:1000]


def test_load_library_never_throws_and_keeps_array():
    # Audit: an unguarded await this.loadLibrary() aborted the whole init()
    # (blank app, no sidebar/polling) on any /api/characters hiccup.
    body = _JS.split("async loadLibrary() {")[1].split("},")[0]
    assert "try {" in body
    assert "catch" in body
    assert "Array.isArray(data)" in body


def test_rerender_refreshes_words_baseline_after_save():
    # Audit: submitRerender never updated lastResult.words after persisting
    # words_json → the next open/save silently REVERTED the saved edits.
    body = _JS.split("async submitRerender() {")[1]
    assert "sentWords = cleanWords;" in body
    assert "this.editor.lastResult.words = sentWords;" in body


def test_img_regen_modal_has_retry_followups():
    # Audit: ✎↻ on a finished Reengineer run never repainted / cache-busted —
    # a paid regen looked like a no-op. Must mirror reengineerRetryVariant.
    body = _JS.split("async submitImgRegen() {")[1]
    assert "this.reengineerRetryNonce = { ...this.reengineerRetryNonce, [m.variantId]: Date.now() };" in body
    assert "slot.status = 'generating';" in body
    assert "this._startReengineerPolling();" in body
    assert "await this.refreshReengineer(live.re_id);" in body
    # Resolve the LIVE run — a background refresh can detach the modal's reRun.
    assert ".find(x => x.re_id === m.reRun.re_id)" in body


def test_job_model_switch_resnaps_per_scene_durations():
    # Audit: switching the job-wide video model left stale per-scene durations
    # that the dropdown couldn't display and the server silently dropped.
    body = _JS.split("syncDurationToModel() {")[1].split("},")[0]
    assert "this.onSceneVideoModelChange(sc)" in body


def test_seek_timeline_key_is_not_duplicated():
    # Audit: two `seekTimeline` keys in the studio() object literal — the
    # later (CapCut) one silently shadowed the caption editor's click-to-seek.
    assert len(re.findall(r"^\s*seekTimeline\(", _JS, flags=re.M)) == 1
    assert len(re.findall(r"^\s*seekCaptionTimeline\(", _JS, flags=re.M)) == 1
    # The caption track binds the renamed handler; the CapCut track keeps the old.
    assert '@mousedown.self="seekCaptionTimeline($event)"' in _HTML
    assert '@click="seekTimeline($event)"' in _HTML


def test_close_timeline_releases_src_via_binding():
    # Audit: closeTimeline wiped v.src directly; openTimeline re-assigns the
    # SAME url string (same-value set Alpine skips) → black dead player.
    body = _JS.split("closeTimeline() {")[1].split("},")[0]
    assert "v.src = ''" not in body
    assert "this.timeline.sourceUrl = '';" in body


def test_editor_submits_have_catch():
    # Audit: try/finally with NO catch — a thrown fetch (connection cut) failed
    # completely silently. Every editor submit routes through _submitError.
    for fn, label in [
        ("async editorTrimSilences() {", "'Trim'"),
        ("async editorAddCaptions() {", "'Captions'"),
        ("async submitRerender() {", "'Rerender'"),
        ("async submitTimeline() {", "'Timeline render'"),
        ("async submitMultiAutoEdit() {", "'Multi auto-edit'"),
        ("async editorAutoEdit() {", "'Auto-edit'"),
    ]:
        body = _JS.split(fn)[1].split("\n    },")[0]
        assert f"this._submitError({label}, e);" in body, fn


def test_caption_editor_reachable_for_editor_tab_renders():
    # Audit: no Editor-tab endpoint returns the transcript words — the ✎ button
    # never rendered and openCaptionEditor had nothing to edit. The words are
    # fetched on demand from the adopt endpoint (words.json exists on disk).
    body = _JS.split("async openCaptionEditor() {")[1].split("\n    },")[0]
    assert "'/api/editor/edit/' + this.editor.lastResult.edit_id" in body
    # Button gate also accepts a summary-only captions result (n_words).
    assert "editor.lastResult?.captions?.n_words) && editor.lastResult?.edit_id" in _HTML


def test_recent_media_keys_are_unique():
    # Audit: N character finals of one run (and a reel + its 🔁 mirror) shared
    # one x-for key, corrupting Alpine's keyed diff in the sidebar strip.
    body = _JS.split("get recentMedia() {")[1].split("\n    },")[0]
    assert "key: 'reengineer-' + r.re_id + '-' + cid," in body
    assert "key: 'editor-' + g.gen_id," in body
    assert "key: 'editor-' + g.gen_id + '-rp'," in body
    assert ':key="m.key"' in _HTML
