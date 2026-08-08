"""app.js `klingSpeechSecs` must agree with Python's extractor, behaviorally.

`test_kling_duration_js_mirror_in_sync` pins the regex TEXT in both places.
That is not enough: it cannot see how the JS COMBINES the patterns. When the
Python extractor became a span union on 2026-08-08 (a Director prompt states
its spoken line twice, and first-pattern-wins localized only one copy — four
🇪🇸/🇩🇪 characters opened their reel in English), the static test stayed green
against a JS union that was wrong in two directions:

  * it de-duplicated on TEXT CONTAINMENT instead of overlapping GROUP-1 spans,
    so a line whose two copies differ by a parenthetical stage direction was
    counted twice (measured: 21.91 s claimed against a true 8.27 s), while a
    genuinely distinct line that happened to be a prefix of another vanished;
  * it keyed with the ASCII `\\w`, which folds every accented letter in a
    Spanish or German line to a space.

This module runs the REAL function over fixtures whose expected seconds are
computed by PYTHON from Python's own `extract_dialogue`, so the two sides
cannot be edited apart. The estimate is display-only — it feeds the ⚠ badge at
the gate, never `klingDuration` — so the cost of drift is a false warning, not
a mis-billed clip.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from character_swap import runner_reengineer
from character_swap.video_edit import extract_dialogue

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = Path(__file__).parent / "js" / "kling_speech_secs_mirror.mjs"

LINE = "Put baking soda on a raw beet and see what happens"

FIXTURES: dict[str, str] = {
    # The reported bug: same line in the ACTION and the AUDIO block.
    "twin_action_and_audio":
        f'ACTION — The man addresses the camera: "{LINE}" He pours.\n\n'
        f'AUDIO — Voice: male; Language: English; Dialogue: "{LINE}"',
    # The same twin, but one copy carries a stage direction. Python strips the
    # parenthetical BEFORE keying, so the two copies must still collapse.
    "twin_with_stage_direction":
        f'ACTION — The man addresses the camera: "{LINE} (while he holds up '
        f'the beet)" He pours.\n\nAUDIO — Dialogue: "{LINE}"',
    # Two genuinely different lines, one a prefix of the other — a containment
    # test drops the short one; a span union must keep both.
    "two_distinct_lines_one_a_prefix":
        f'AUDIO — Dialogue: "{LINE} today and tomorrow"\n\n'
        f'VOICE-OVER: "{LINE}"',
    # The 2026-08-08 verb widening, with no other dialogue in the prompt: a JS
    # still carrying the old verb list reads this as a SILENT scene.
    "action_block_verb_only":
        'ACTION AND CAMERA MOTION — The man enthusiastically addresses the '
        'camera: "one two three four five" He pours.',
    # OVERLAPPING spans carrying DIFFERENT text: the says-clause body spans the
    # inner quote pair and captures the whole CTA, while the speech-verb
    # pattern's body stops dead at that inner quote and captures a fragment of
    # it. De-duplicating by text cannot collapse these — only comparing the
    # GROUP-1 spans and letting the more specific pattern win does.
    "overlapping_spans_different_text":
        'The man, speaking clearly, says to the camera: "Comment "Skin" below '
        'and I will send it"',
    # Accented text: the ASCII key folds every one of these letters.
    "spanish_twin":
        'ACTION — El hombre addresses the camera: "Pon bicarbonato de sodio '
        'en una remolacha cruda y mira qué pasa."\n\nAUDIO — Dialogue: "Pon '
        'bicarbonato de sodio en una remolacha cruda y mira qué pasa."',
    # Guards: props are not speech, and a plain says-clause is unaffected.
    "quoted_prop_is_not_speech":
        'A bottle labeled "Heinz White Vinegar" sits on the counter.',
    "plain_says_clause":
        'He says to the camera with an american accent: "one two three four".',
    "silent_scene": "SHOT — Slow push-in on the beet. No dialogue.",
}


def _expected_secs(prompt: str) -> float:
    words = len(extract_dialogue(prompt).split())
    if not words:
        return 0.0
    return (words / runner_reengineer._SPEECH_WORDS_PER_SEC
            + runner_reengineer._SPEECH_MARGIN_SECS)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_js_speech_estimate_matches_python(tmp_path):
    cases = [{"name": name, "prompt": prompt, "speech": "",
              "expected": _expected_secs(prompt)}
             for name, prompt in FIXTURES.items()]
    payload = tmp_path / "cases.json"
    payload.write_text(json.dumps(cases), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(_HARNESS), str(payload)],
        capture_output=True, text=True, timeout=60, cwd=_ROOT,
    )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], ("app.js klingSpeechSecs drifted from Python:\n  - "
                          + "\n  - ".join(result.get("failures", [])))


def test_the_twin_fixture_really_states_the_line_twice():
    """Guards the guard: if the fixture stopped exercising the union, the
    harness above would pass for the wrong reason."""
    from character_swap.video_edit import dialogue_matches
    assert len(dialogue_matches(FIXTURES["twin_action_and_audio"])) == 2
    assert extract_dialogue(FIXTURES["twin_action_and_audio"]) == LINE
