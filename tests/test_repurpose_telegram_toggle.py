"""🔁 Repurpose: sending the copy to Telegram is a CHOICE (Hugo 2026-08-09).

Every repurpose auto-sent, on all three surfaces (Swap Step-6 per character,
the Reengineer/Swap run card, and a saved Editor reel), with no way to say no.
The Repurpose modal now carries one "➤ Skicka automatiskt till Telegram" ✓ that
all three read.

Hugo's calls: the box is TICKED by default (today's behavior is preserved — an
unticked box is the new, opt-in thing), it applies to all three surfaces, and it
is remembered PER RUN in `repurpose_settings` so re-opening the modal for the
same run/job/reel shows the last choice.

What this locks, per surface:
  1. Reengineer `repurpose()` — no `send_reengineer_repurposed` call when off,
     and the build itself is unaffected either way.
  2. Swap `_compile_one_character(slot=_REPURPOSE_SLOT)` — no
     `send_character_final`, no receipt written, final still `done`.
  3. Editor `repurpose_editor_job` — no `send_editor_final`, and NO false
     "Telegram misslyckades" push (the skip is not a failure).
  4. The default is TRUE everywhere, including for state written before the
     flag existed — a missing value must never read as "don't send".
  5. `auto_telegram_send` is a REPURPOSE setting: the ⚙ assemble panel must not
     write it into `assemble_settings`, where the same-named RUN-level flag
     governs the ORIGINAL finals' delivery.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from character_swap import api, auto_finalize, runner_compile, runner_reengineer
from character_swap.api import (
    EditorRepurposeBody,
    ReAssembleSettingsBody,
    RepurposeVideosBody,
    _store_assemble_settings,
    _store_repurpose_settings,
)
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    GenKind,
    Job,
    JobCharacter,
    MediaGeneration,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
from character_swap.runner_compile import _COMPILE_SLOT, _REPURPOSE_SLOT, EditorResult

_ROOT = Path(__file__).resolve().parents[1]


# --- 1. Reengineer run card ----------------------------------------------------------


def _re_state(**extra) -> dict:
    state = {"re_id": "re_t", "job_id": "j1", "status": "done", "scenes": []}
    state.update(extra)
    return state


def _wire_reengineer(monkeypatch, state):
    """Stub out the BUILD so only the delivery decision is under test."""
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: dict(state))

    # _do_repurpose returns the characters it built; the caller delivers
    # exactly those, and an empty list means "nothing to send".
    async def fake_build(re_id, st, **kw):
        return ["c1"]
    monkeypatch.setattr(runner_reengineer, "_do_repurpose", fake_build)
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)

    sent: list[str] = []

    async def fake_send(re_id, **kw):
        sent.append(re_id)
    monkeypatch.setattr(auto_finalize, "send_reengineer_repurposed", fake_send)
    return sent


def test_reengineer_repurpose_sends_by_default(monkeypatch):
    """No stored setting at all — the pre-toggle behavior must survive."""
    sent = _wire_reengineer(monkeypatch, _re_state())
    asyncio.run(runner_reengineer.repurpose("re_t"))
    assert sent == ["re_t"]


def test_reengineer_repurpose_sends_when_ticked(monkeypatch):
    sent = _wire_reengineer(
        monkeypatch,
        _re_state(repurpose_settings={"auto_telegram_send": True}))
    asyncio.run(runner_reengineer.repurpose("re_t"))
    assert sent == ["re_t"]


def test_reengineer_repurpose_skips_send_when_unticked(monkeypatch):
    """THE FEATURE: build the mirrored finals, deliver nothing."""
    sent = _wire_reengineer(
        monkeypatch,
        _re_state(repurpose_settings={"auto_telegram_send": False}))
    asyncio.run(runner_reengineer.repurpose("re_t"))
    assert sent == []


def test_reengineer_repurpose_still_builds_when_send_is_off(monkeypatch):
    """The ✓ governs DELIVERY only — the copies must still be built."""
    state = _re_state(repurpose_settings={"auto_telegram_send": False})
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: dict(state))
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)
    built: list[str] = []

    async def fake_build(re_id, st, **kw):
        built.append(re_id)
        return ["c1"]
    monkeypatch.setattr(runner_reengineer, "_do_repurpose", fake_build)

    async def boom(re_id, **kw):
        raise AssertionError("delivery must not run when the box is unticked")
    monkeypatch.setattr(auto_finalize, "send_reengineer_repurposed", boom)

    asyncio.run(runner_reengineer.repurpose("re_t"))
    assert built == ["re_t"]
    assert "re_t" not in runner_reengineer._REPURPOSING   # guard released


def test_reengineer_failed_build_never_sends(monkeypatch):
    """Unchanged contract: a failed build delivers nothing regardless of the ✓."""
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: _re_state(
                            repurpose_settings={"auto_telegram_send": True}))
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)

    async def fake_build(re_id, st, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(runner_reengineer, "_do_repurpose", fake_build)

    async def boom(re_id, **kw):
        raise AssertionError("a failed build must not deliver")
    monkeypatch.setattr(auto_finalize, "send_reengineer_repurposed", boom)

    asyncio.run(runner_reengineer.repurpose("re_t"))


@pytest.mark.parametrize("stored, expected", [
    (None, True),                              # no repurpose_settings at all
    ({}, True),                                # settings, but not this key
    ({"auto_telegram_send": None}, True),      # explicit None = "keep default"
    ({"auto_telegram_send": True}, True),
    ({"auto_telegram_send": False}, False),
])
def test_repurpose_auto_send_resolution(stored, expected):
    state = _re_state() if stored is None else _re_state(repurpose_settings=stored)
    assert runner_reengineer._repurpose_auto_send(state) is expected


def test_repurpose_auto_send_ignores_the_run_level_flag():
    """The RUN-level `auto_telegram_send` governs the ORIGINAL finals after an
    assemble. A repurpose is an explicit per-click request with its own ✓ —
    reading the run-level flag would silently refuse to deliver a repurpose on
    every run created with auto-delivery off."""
    state = _re_state(auto_telegram_send=False)
    assert runner_reengineer._repurpose_auto_send(state) is True


# --- 2. Swap Step-6 (per character) --------------------------------------------------


def _mkjob(tmp_path):
    clip = tmp_path / "s1.mp4"; clip.write_text("clip")
    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[GeneratedImage(variant_id="var1", path="/tmp/var1.png",
                               prompt="x", scene_id="sc1",
                               status=VariantStatus.READY)],
        approved_variant_ids=["var1"],
        videos=[VideoVariant(video_id="vd1", grok_job_id="g1",
                             status=VideoStatus.DONE,
                             source_variant_id="var1",
                             final_video_path=str(clip))])
    job = Job(job_id="j1", title="t", scene_id="sc1",
              scene_image_path="/tmp/sc1.png", scene_ids=["sc1"],
              scene_image_paths=["/tmp/sc1.png"], characters={"c1": jc})
    return job, jc


def _run_repurpose_char(monkeypatch, tmp_path, *, auto_send, slot=_REPURPOSE_SLOT):
    job, jc = _mkjob(tmp_path)
    char = SimpleNamespace(char_id="c1", name="A", telegram_chat_id="@kanal",
                           voice_id=None, voice_provider=None, language=None)
    fake_store = SimpleNamespace(get_job=lambda jid: job,
                                update_job=lambda j: None,
                                get_character=lambda cid: char)
    monkeypatch.setattr(runner_compile, "store", lambda: fake_store)
    monkeypatch.setattr(type(settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    async def fake_emit(job_id, kind, **kw):
        return None
    monkeypatch.setattr(runner_compile, "_emit", fake_emit)

    final = tmp_path / "result.mp4"; final.write_text("final")

    async def fake_pipeline(paths, **kw):
        return EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)

    sent: list[dict] = []
    pushes: list[tuple] = []

    from character_swap import telegram_delivery

    async def fake_send(path, *, chat_id, char_name, base, variant, run_id,
                        label=None):
        sent.append({"path": str(path), "chat_id": chat_id, "variant": variant})
        return {"ok": True, "message_id": 1}
    monkeypatch.setattr(telegram_delivery, "send_character_final", fake_send)
    monkeypatch.setattr(runner_compile.push, "notify",
                        lambda *a, **k: pushes.append((a, k)))

    asyncio.run(runner_compile._compile_one_character(
        "j1", "c1", template="capcut-bluebox", overrides=None,
        enable_trim=False, enable_captions=False, enable_wpm_normalize=False,
        target_wpm=190, threshold_db=-24.0, min_silence_secs=0.4, pad_secs=0.1,
        voice_override=None, enable_voice_swap=False, slot=slot,
        auto_telegram_send=auto_send))
    return jc, sent, pushes


def test_swap_repurpose_sends_when_ticked(monkeypatch, tmp_path):
    jc, sent, _pushes = _run_repurpose_char(monkeypatch, tmp_path, auto_send=True)
    assert jc.repurpose_status == "done"
    assert [s["variant"] for s in sent] == ["repurpose"]
    assert jc.telegram_sends["repurpose"] == {"ok": True, "message_id": 1}


def test_swap_repurpose_skips_send_when_unticked(monkeypatch, tmp_path):
    """The final is built and playable; only delivery is withheld — and NO
    failure push, since not sending was the point."""
    jc, sent, pushes = _run_repurpose_char(monkeypatch, tmp_path, auto_send=False)
    assert jc.repurpose_status == "done"
    assert jc.repurposed_video_path.endswith("/compiled/c1__repurpose.mp4")
    assert sent == []
    assert "repurpose" not in jc.telegram_sends
    assert not [p for p in pushes if "Telegram" in str(p)]


def test_swap_repurpose_defaults_to_sending(monkeypatch, tmp_path):
    """`_compile_one_character` called WITHOUT the kwarg (any older caller)
    must still deliver — the flag defaults to the pre-toggle behavior."""
    job, jc = _mkjob(tmp_path)
    char = SimpleNamespace(char_id="c1", name="A", telegram_chat_id="@kanal",
                           voice_id=None, voice_provider=None, language=None)
    monkeypatch.setattr(runner_compile, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job,
                                                update_job=lambda j: None,
                                                get_character=lambda cid: char))
    monkeypatch.setattr(type(settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    async def fake_emit(job_id, kind, **kw):
        return None
    monkeypatch.setattr(runner_compile, "_emit", fake_emit)
    final = tmp_path / "r.mp4"; final.write_text("f")

    async def fake_pipeline(paths, **kw):
        return EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)
    from character_swap import telegram_delivery
    sent: list[str] = []

    async def fake_send(path, **kw):
        sent.append(kw["variant"])
        return {"ok": True}
    monkeypatch.setattr(telegram_delivery, "send_character_final", fake_send)

    asyncio.run(runner_compile._compile_one_character(
        "j1", "c1", template="capcut-bluebox", overrides=None,
        enable_trim=False, enable_captions=False, enable_wpm_normalize=False,
        target_wpm=190, threshold_db=-24.0, min_silence_secs=0.4, pad_secs=0.1,
        voice_override=None, enable_voice_swap=False, slot=_REPURPOSE_SLOT))
    assert sent == ["repurpose"]


def test_compile_slot_never_sends_regardless_of_the_flag(monkeypatch, tmp_path):
    """The ORIGINAL compile's delivery is the auto-finalize chain's job — the
    repurpose ✓ must not turn the compile slot into a sender."""
    jc, sent, _pushes = _run_repurpose_char(monkeypatch, tmp_path,
                                            auto_send=True, slot=_COMPILE_SLOT)
    assert jc.compile_status == "done"
    assert sent == []


def test_compile_job_videos_forwards_the_flag(monkeypatch, tmp_path):
    """The fan-out must pass the choice down — a dropped kwarg there would make
    the whole toggle a silent no-op on the Swap tab."""
    job, _jc = _mkjob(tmp_path)
    monkeypatch.setattr(runner_compile, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job,
                                                update_job=lambda j: None,
                                                get_character=lambda cid: None))
    seen: list[bool] = []

    async def fake_one(job_id, cid, **kw):
        seen.append(kw["auto_telegram_send"])
    monkeypatch.setattr(runner_compile, "_compile_one_character", fake_one)
    monkeypatch.setattr(runner_compile.push, "notify", lambda *a, **k: None)

    asyncio.run(runner_compile.repurpose_job_videos(
        "j1", template="capcut-bluebox", overrides=None,
        auto_telegram_send=False))
    assert seen == [False]


# --- 3. Saved Editor reel ------------------------------------------------------------


def _run_editor_repurpose(monkeypatch, tmp_path, *, auto_send):
    clip = tmp_path / "clip.mp4"; clip.write_text("c")
    gen = MediaGeneration(gen_id="g_1", kind=GenKind.EDITOR,
                          model="editor-multiclip", prompt="reel",
                          editor_meta={"clip_paths": [str(clip)]})
    saved: dict = {}

    class _S:
        def get_generation(self, gid):
            return gen

        def update_generation(self, g):
            saved["meta"] = dict(g.editor_meta or {})
    monkeypatch.setattr(runner_compile, "store", lambda: _S())
    monkeypatch.setattr(type(settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)
    final = tmp_path / "mirrored.mp4"; final.write_text("m")

    async def fake_pipeline(paths, **kw):
        return EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)

    from character_swap import telegram_delivery
    sent: list[str] = []
    pushes: list[tuple] = []

    async def fake_send(path, *, base, variant, edit_id):
        sent.append(variant)
        return {"ok": True, "message_id": 7}
    monkeypatch.setattr(telegram_delivery, "send_editor_final", fake_send)
    monkeypatch.setattr(runner_compile.push, "notify",
                        lambda *a, **k: pushes.append((a, k)))

    asyncio.run(runner_compile.repurpose_editor_job(
        "g_1", auto_telegram_send=auto_send))
    return gen, sent, pushes


def test_editor_repurpose_sends_when_ticked(monkeypatch, tmp_path):
    gen, sent, _pushes = _run_editor_repurpose(monkeypatch, tmp_path,
                                               auto_send=True)
    assert sent == ["repurpose"]
    assert gen.editor_meta["repurpose"]["status"] == "done"
    assert gen.editor_meta["telegram"]["repurpose"]["message_id"] == 7


def test_editor_repurpose_skips_send_when_unticked(monkeypatch, tmp_path):
    """Built, marked done, no receipt — and crucially no "Telegram
    misslyckades" push: skipping on purpose is not a failure."""
    gen, sent, pushes = _run_editor_repurpose(monkeypatch, tmp_path,
                                              auto_send=False)
    assert sent == []
    assert gen.editor_meta["repurpose"]["status"] == "done"
    assert gen.editor_meta["repurpose"]["video_path"].endswith("mirrored.mp4")
    assert "telegram" not in gen.editor_meta
    assert not [p for p in pushes if "misslyckades" in str(p)]
    assert [p for p in pushes if "Repurpose klar" in str(p)]


def test_editor_repurpose_defaults_to_sending(monkeypatch, tmp_path):
    clip = tmp_path / "clip.mp4"; clip.write_text("c")
    gen = MediaGeneration(gen_id="g_1", kind=GenKind.EDITOR,
                          model="editor-multiclip", prompt="reel",
                          editor_meta={"clip_paths": [str(clip)]})
    monkeypatch.setattr(runner_compile, "store",
                        lambda: SimpleNamespace(
                            get_generation=lambda gid: gen,
                            update_generation=lambda g: None))
    monkeypatch.setattr(type(settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)
    final = tmp_path / "m.mp4"; final.write_text("m")

    async def fake_pipeline(paths, **kw):
        return EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)
    from character_swap import telegram_delivery
    sent: list[str] = []

    async def fake_send(path, **kw):
        sent.append(kw["variant"])
        return {"ok": True}
    monkeypatch.setattr(telegram_delivery, "send_editor_final", fake_send)
    monkeypatch.setattr(runner_compile.push, "notify", lambda *a, **k: None)

    asyncio.run(runner_compile.repurpose_editor_job("g_1"))
    assert sent == ["repurpose"]


# --- 4. Request bodies + persistence -------------------------------------------------


def test_body_defaults_preserve_todays_behavior():
    assert RepurposeVideosBody().auto_telegram_send is True
    assert EditorRepurposeBody().auto_telegram_send is True
    # The Reengineer body is all-optional ("keep what's stored"), and an unset
    # stored value resolves to True in _repurpose_auto_send.
    assert ReAssembleSettingsBody().auto_telegram_send is None


def test_repurpose_settings_persist_the_choice():
    state: dict = {}
    assert _store_repurpose_settings(
        state, ReAssembleSettingsBody(auto_telegram_send=False)) is True
    assert state["repurpose_settings"]["auto_telegram_send"] is False
    assert runner_reengineer._repurpose_auto_send(state) is False
    # Re-ticking it flips back (a False must not be sticky forever).
    assert _store_repurpose_settings(
        state, ReAssembleSettingsBody(auto_telegram_send=True)) is True
    assert runner_reengineer._repurpose_auto_send(state) is True


def test_assemble_settings_never_store_the_repurpose_flag():
    """`state["auto_telegram_send"]` (run level) governs the ORIGINAL finals'
    delivery and is chosen at upload. A same-named key inside
    `assemble_settings` would read like it governs that — it doesn't, and the
    ⚙ assemble panel has no such control."""
    state: dict = {}
    changed = _store_assemble_settings(
        state, ReAssembleSettingsBody(auto_telegram_send=False,
                                      enable_captions=True))
    assert changed is True
    assert "auto_telegram_send" not in state["assemble_settings"]
    assert state["assemble_settings"]["enable_captions"] is True
    # And a body carrying ONLY the repurpose flag changes nothing there.
    state2: dict = {}
    assert _store_assemble_settings(
        state2, ReAssembleSettingsBody(auto_telegram_send=False)) is False
    assert "assemble_settings" not in state2


def test_stored_flag_is_ignored_by_the_editor_settings_reader():
    """`_repurpose_settings` feeds run_editor_pipeline — the delivery flag must
    not leak into the build config (it is not an ASSEMBLE_DEFAULTS key)."""
    cfg = runner_reengineer._repurpose_settings(
        _re_state(repurpose_settings={"auto_telegram_send": False,
                                      "enable_captions": False}))
    assert "auto_telegram_send" not in cfg
    assert cfg["enable_captions"] is False


# --- 5. The UI control ---------------------------------------------------------------


_HARNESS = Path(__file__).parent / "js" / "repurpose_telegram_toggle.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_modal_toggle_behavior():
    proc = subprocess.run(["node", str(_HARNESS)], capture_output=True,
                          text=True, timeout=60, cwd=_ROOT)
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "repurpose Telegram toggle regressions:\n  - " \
        + "\n  - ".join(result.get("failures", []))


def test_modal_has_the_checkbox():
    html = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index('x-show="repurposeModal.open"')
    modal = html[start:html.index("Repurpose (spegelvänd)", start)]
    assert 'x-model="repurposeSettings.autoTelegramSend"' in modal, (
        "the Repurpose modal must carry the auto-send checkbox")


def test_endpoints_forward_the_flag():
    """Node-free backstop for the three wiring points — a dropped kwarg makes
    the toggle a silent no-op, which is the worst failure mode here."""
    src = (_ROOT / "src" / "character_swap" / "api.py").read_text(encoding="utf-8")
    assert src.count("auto_telegram_send=body.auto_telegram_send") == 2, (
        "both the Swap repurpose_videos and the Editor repurpose endpoint must "
        "forward the flag to their runner")
    assert "auto_telegram_send" in api.RepurposeVideosBody.model_fields
    assert "auto_telegram_send" in api.EditorRepurposeBody.model_fields
    assert "auto_telegram_send" in api.ReAssembleSettingsBody.model_fields
