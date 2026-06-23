"""Mobile / iPhone support (Hugo 2026-06-12): same single codebase, made
fully usable at 375px touch. Pins the load-bearing pieces so a refactor
can't silently regress phone use.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_pwa_head_tags_and_assets():
    assert "viewport-fit=cover" in _HTML
    assert 'rel="manifest"' in _HTML
    assert 'rel="apple-touch-icon"' in _HTML
    assert 'name="theme-color"' in _HTML
    static = _ROOT / "web" / "static"
    assert (static / "manifest.webmanifest").exists()
    assert (static / "apple-touch-icon.png").exists()
    assert (static / "icon-512.png").exists()


def test_ios_input_zoom_guard_and_touch_hover_css():
    # <16px inputs make iOS Safari zoom on focus.
    assert "font-size: 16px !important" in _HTML
    # Hover-revealed controls must be visible on touch devices.
    assert '[class*="hover:opacity-100"] { opacity: 1 !important; }' in _HTML
    # Drag affordances get thumb-sized minimums.
    assert 'cursor-ew-resize"] { min-width: 14px' in _HTML


def test_sidebar_and_library_are_mobile_drawers():
    # Jobs sidebar: slide-over below md (was `hidden md:flex` — unreachable).
    assert "hidden md:flex" not in _HTML
    assert "mobileNav ? 'translate-x-0' : '-translate-x-full" in _HTML
    assert 'title="Jobb & projekt"' in _HTML          # hamburger FAB
    assert "mobileNav: false" in _JS
    # Character library: right slide-over with backdrop on mobile.
    assert "fixed md:static inset-y-0 right-0" in _HTML


def test_timeline_drag_supports_touch():
    # startHandleDrag + seekTimeline were mouse-only (clientX undefined on
    # touch) — the CapCut timeline was un-draggable on iPhone.
    seg = _JS.split("startHandleDrag(event, idx, side)")[1]
    assert "event.touches?.[0]?.clientX" in seg[:900]
    assert "window.addEventListener('touchmove', this._tlOnMove" in seg[:3000]
    seek = _JS.split("seekTimeline(event)")[1]
    assert "event.touches?.[0]?.clientX" in seek[:600]
    assert "@touchstart.self.passive=\"seekTimeline($event)\"" in _HTML
    assert _HTML.count("startHandleDrag($event, i,") == 4   # mouse+touch × 2


def test_motion_and_length_fields_use_x_model(_=None):
    # Hugo 2026-06-19: the motion-prompt textareas (Swap Step 4 + Reengineer
    # gate) must use x-model, NOT a one-way :value bind + @input. On mobile
    # Safari that controlled-input pattern fights the iOS keyboard's autocorrect/
    # predictive text — the first edit is wiped and you have to retype it.
    # x-model is the two-way bind that doesn't re-apply value to a focused input.
    assert 'x-model="movementByScene[scene.scene_id]"' in _HTML          # Swap motion
    assert 'x-model="reSceneDraft(r, sc).motion_prompt"' in _HTML        # Reengineer motion
    # The fragile controlled-input pattern must not come back on these fields.
    assert ":value=\"reSceneVal(r, sc, 'motion_prompt')\"" not in _HTML
    assert ":value=\"movementByScene[scene.scene_id] || ''\"" not in _HTML
    # The lazily-seeded draft helper x-model binds against.
    assert "reSceneDraft(run, sc) {" in _JS


def test_clip_length_is_a_dropdown_not_a_typed_number(_=None):
    # Hugo 2026-06-21: the two Kling clip-length fields (Reengineer scene gate +
    # Swap "from images" rows) became <select> menus — pick 3–15 s instead of
    # typing the number. The old free-text number inputs must be gone.
    assert "<input type=\"number\" min=\"3\" max=\"15\" step=\"1\" x-model.number=\"row.length\"" not in _HTML
    assert 'x-model="reSceneDraft(r, sc).kling_secs"' not in _HTML
    # Reengineer Kling-length is now a <select>; each <option> self-selects via
    # :selected so the right value shows regardless of x-for render order (an
    # x-model / :value / x-effect on the select alone shows the first option
    # because it runs before the nested option x-for populates). @change writes
    # the override.
    assert ':selected="n === klingDuration(r, sc)"' in _HTML
    assert "reSceneEdit(r, sc, 'kling_secs', $event.target.value)" in _HTML
    # Swap "from images" length: same :selected idiom, writes row.length on change.
    assert ':selected="n === row.length"' in _HTML
    assert "row.length = parseInt($event.target.value)" in _HTML
    # Both render their options from the shared 3–15 s option list.
    assert "n in klingLengthOptions" in _HTML
    assert "klingLengthOptions: [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]" in _JS


def test_non_kling_length_select_uses_selected_not_value(_=None):
    # Hugo 2026-06-22: a Reengineer scene overridden to a NON-Kling model (Veo
    # 3.1 Fast) ignored the length the user picked — set 8s, every clip rendered
    # 4s. Root cause: the per-scene "Längd (s)" <select> (the non-Kling clip
    # length) was the ONE length menu still using a select-level :value bind
    # (`:value="reSceneDuration(r, sc)"`). That binding evaluates before the
    # nested option x-for populates, so the user's pick silently reverts and is
    # never persisted — _scene_duration then animates the stale stored length.
    # Fix = the same per-option :selected idiom the Kling menu uses.
    assert ':selected="n === reSceneDuration(r, sc)"' in _HTML
    # The fragile select-level bind must not come back on this field.
    assert ':value="reSceneDuration(r, sc)"' not in _HTML
    # Still writes the override on change (shared handler with the Kling menu).
    assert "reSceneEdit(r, sc, 'kling_secs', $event.target.value)" in _HTML
    # Same defect lived on the Swap/Animate tab's per-scene Duration menu — it
    # must use the per-option :selected idiom too, never a select-level :value.
    assert (':selected="n === (durationByScene[scene.scene_id] '
            '|| sceneVideoDurationSpec(scene).default)"') in _HTML
    assert ':value="durationByScene[scene.scene_id] || sceneVideoDurationSpec(scene).default"' not in _HTML


def _owner_has_keep_focus(anchor, opener="<select"):
    # True iff the element opened by `opener` that immediately precedes `anchor`
    # carries data-keep-focus. Pins the attribute to its control (robust to
    # attribute reorder / comment edits — unlike a fixed-width char window).
    i = _HTML.index(anchor)
    return "data-keep-focus" in _HTML[_HTML.rindex(opener, 0, i):i]


def test_background_refresh_defers_while_typing(_=None):
    # Hugo's recurring "I have to enter it twice for it to stick" bug (motion
    # prompt AND video length): the 5s poll / WS refresh replaced the whole
    # job/run object mid-interaction, churning Alpine's x-for scope and
    # re-rendering the focused control — iOS drops the in-flight char on a
    # <textarea> and reverts the open native picker on a <select>. Fix = pause
    # the refresh while a protected field (textarea/input/SELECT inside
    # [data-keep-focus]) is focused, then flush on blur.
    # 2 motion textareas + 3 duration selects + 2 model selects are tagged.
    assert _HTML.count("data-keep-focus") >= 7
    # The guard helper + both churn paths must check it; <select> counts too
    # (the duration/model menus revert on re-render — the "videolängd twice" half).
    assert "_isTypingProtectedField() {" in _JS
    assert "el.closest('[data-keep-focus]')" in _JS
    assert "tag !== 'SELECT'" in _JS
    # Each duration + model control carries data-keep-focus, pinned to the control.
    assert _owner_has_keep_focus("durationByScene[scene.scene_id] = parseInt")       # Animate Step-4 duration
    assert _owner_has_keep_focus("swapVideoModelsByScene[scene.scene_id] = $event")  # Animate Step-4 model
    assert _owner_has_keep_focus('title="Videomodell', "<label")                     # Reengineer model
    assert "data-keep-focus" in _HTML.split(
        "x-if=\"reSceneEffModel(r, sc) === 'kling-v3'\"")[1][:300]                    # Reengineer Kling-längd
    assert "data-keep-focus" in _HTML.split(
        "x-if=\"reSceneEffModel(r, sc) !== 'kling-v3'\"")[1][:300]                    # Reengineer non-Kling Längd
    # Reengineer poll/WS refresh defers and retries.
    re_refresh = _JS.split("async refreshReengineer(reId) {")[1][:700]
    assert "_isTypingProtectedField()" in re_refresh
    # Swap WS handler defers the job replacement.
    handle = _JS.split("async handleEvent(evt) {")[1][:700]
    assert "_isTypingProtectedField()" in handle
    assert "this._pendingJobRefresh = true" in handle
    # A blur flush catches the deferred refresh back up.
    assert "_flushDeferredRefresh() {" in _JS
    assert "addEventListener('focusout'" in _JS
    # The redundant save-in-flight defer counter was removed (the draft already
    # covers the mid-save revert; the counter only added a hung-save freeze risk).
    assert "_sceneSaveInFlight" not in _JS
    assert "_shouldDeferRefresh" not in _JS


def test_upload_submits_surface_network_errors(_=None):
    # Hugo 2026-06-19: a reengineer video upload from the phone "loaded then
    # stopped, no result at all". fetch() THROWS (not just !r.ok) when an upload
    # is cut off mid-flight (mobile/Tailscale) — and the submit handlers had a
    # try/finally with NO catch, so the failure was swallowed: spinner stops,
    # no run, no error. Every upload submit must route a catch through the
    # shared _submitError helper so the failure is LOUD.
    assert "_submitError(label, e) {" in _JS
    # reengineer (reported) + from_images + broll + audio + avatar×2 + image +
    # video = 8 upload submits, each with a catch.
    assert _JS.count("this._submitError(") >= 8
    # The exact reported path must be covered.
    assert "this._submitError('Reengineer', e)" in _JS
