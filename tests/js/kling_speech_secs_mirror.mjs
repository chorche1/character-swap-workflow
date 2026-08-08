// Behavioral parity harness for app.js `klingSpeechSecs`
// (run by tests/test_kling_speech_secs_mirror.py).
//
// The ⚠ "the line needs more seconds than the clip has" badge at the
// Reengineer gate is computed in JS from a hand-copied mirror of
// video_edit.dialogue_matches + extract_dialogue. Three separate copies of
// those regexes exist (Python, JS, and a sync test), and the static sync test
// can only prove the REGEX TEXT matches — it cannot see that the JS combines
// the patterns differently. It didn't: a union without a GROUP-1 span check
// disagreed with Python on 52 of 988 real prompts, and a text-containment
// stand-in for that check on 12 (double-counting any line whose two copies
// differ by a parenthetical stage direction — precisely the Director shape
// this all exists for).
//
// This harness runs the REAL function and compares against seconds computed by
// Python from Python's own extractor, so the two can never be edited apart.
//
// Reads fixtures as JSON on argv[2]: [{name, prompt, speech, expected}].
// Prints one JSON line: {"ok": true} or {"ok": false, "failures": [...]}.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
// APPJS lets the suite point the harness at another revision of app.js to
// confirm these checks actually FAIL against the pre-fix source.
const src = readFileSync(process.env.APPJS || join(root, 'web', 'app.js'), 'utf8');

globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.document = { activeElement: null, addEventListener: () => {} };
globalThis.window = globalThis;
globalThis.location = { protocol: 'http:', host: '127.0.0.1:8000' };
globalThis.WebSocket = class { constructor() {} close() {} };

const studio = new Function(`${src}\nreturn studio;`)();

const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const failures = [];

for (const c of cases) {
  const app = studio();
  // klingSpeechSecs reads the scene's motion_prompt through reSceneVal, which
  // consults the pending-edit draft first; with no draft it falls through to
  // the entry itself.
  app._reDirtyPending = {};
  app._rePendingSaveViews = {};
  const scene = { idx: 0, motion_prompt: c.prompt, speech: c.speech || '' };
  let got;
  try {
    got = app.klingSpeechSecs({ re_id: 're_x' }, scene);
  } catch (e) {
    failures.push(`${c.name}: threw ${e && e.message}`);
    continue;
  }
  if (Math.abs(got - c.expected) > 1e-6) {
    failures.push(`${c.name}: JS ${got.toFixed(2)}s != Python ${c.expected.toFixed(2)}s`);
  }
}

console.log(JSON.stringify({ ok: failures.length === 0, failures }));
