// Behavioral harness for app.js `reCanRerun` (run by
// tests/test_rerun_button_visibility.py).
//
// Locks Hugo's 2026-08-08 report: on a finished run showing "9 scenes" and a
// full row of final videos, the ↻ Nya karaktärer button was simply absent.
// Cause: the predicate gated on `run.scenes`, but the light history row
// (GET /api/reengineer) omits `scenes` entirely and loadReengineerHistory
// hydrates only the newest 8 runs plus the ones parked at a gate — so every
// older FINISHED run, exactly the ones worth re-running, had an empty array
// and the button was hidden. Same bug class the file already documents twice
// for the approval gate and the multi-person chooser.
//
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
const s = studio();

const failures = [];
const check = (cond, msg) => { if (!cond) failures.push(msg); };

// THE REPORTED CASE: a finished run straight out of the light list — no
// `scenes` key at all, only the n_scenes the card's own label reads.
check(s.reCanRerun({ re_id: 're_x', status: 'done', n_scenes: 9 }) === true,
  'unhydrated finished run (n_scenes only) must offer the re-run button');

// A hydrated run still works.
check(s.reCanRerun({ re_id: 're_x', status: 'done', scenes: [{}, {}] }) === true,
  'hydrated run must offer the button');

// A failed run is a prime re-run candidate — its plan is what you retry.
check(s.reCanRerun({ re_id: 're_x', status: 'failed', n_scenes: 5 }) === true,
  'failed run must offer the button');

// Rows too old to carry n_scenes fall through to showing it: the modal loads
// the plan from the server, so nothing depends on the client-side copy.
check(s.reCanRerun({ re_id: 're_x', status: 'done' }) === true,
  'row without n_scenes must still offer the button');

// A run we positively know is empty should not.
check(s.reCanRerun({ re_id: 're_x', status: 'done', n_scenes: 0 }) === false,
  'run with n_scenes 0 must not offer the button');
check(s.reCanRerun({ re_id: 're_x', status: 'done', scenes: [], n_scenes: 0 }) === false,
  'run with an empty plan must not offer the button');
check(s.reCanRerun(null) === false, 'null run must not offer the button');

console.log(JSON.stringify(
  failures.length ? { ok: false, failures } : { ok: true }));
