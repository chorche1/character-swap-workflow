// Behavioral harness for app.js `refreshReengineer` (run by
// tests/test_reengineer_focus_gate.py).
//
// Locks Hugo's 2026-08-02 bug: the focus guard that protects a scene field
// from being churned mid-edit ALSO deferred the run's `status`, so the card
// froze mid-phase — re_b3170d2118 sat at awaiting_approval server-side with
// 27/27 images ready while the UI still read "swapping" and rendered no
// ✓ Approve all / ▶ Generate videos gate (it is x-show'd on r.status).
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

const failures = [];
const check = (cond, msg) => { if (!cond) failures.push(msg); };

// A run object shaped like the slim payload, at the phase the bug hit.
const makeRun = (status) => ({
  re_id: 're_x', job_id: 'j_x', status,
  source_name: '3 bilder',
  job: { characters: { ch_a: { images: [{ status: 'ready' }], videos: [] } } },
});

function harness(localStatus, serverStatus, focusHeld) {
  const c = studio();
  const fresh = makeRun(serverStatus);
  c.reengineerHistory = [makeRun(localStatus)];
  c._reDirtyPending = {};
  c._rePendingSaveViews = {};
  c._reRefreshTimers = {};
  c._isTypingProtectedField = () => focusHeld;
  c._ensureReengineerWS = () => {};
  c._closeReengineerWS = () => {};
  c.milestones = [];
  c.notifyMilestone = (title) => c.milestones.push(title);
  globalThis.fetch = async () => ({ ok: true, json: async () => JSON.parse(JSON.stringify(fresh)) });
  return c;
}

const stop = (c) => { for (const t of Object.values(c._reRefreshTimers)) clearTimeout(t); };

// 1. THE BUG: field focused, server advanced swapping → awaiting_approval.
//    The status must land anyway, or the approval gate never appears.
{
  const c = harness('swapping', 'awaiting_approval', true);
  const before = c.reengineerHistory[0];
  await c.refreshReengineer('re_x');
  check(c.reengineerHistory[0].status === 'awaiting_approval',
    `focused: status stayed "${c.reengineerHistory[0].status}" instead of awaiting_approval`);
  // In-place patch, NOT a splice — replacing the object is what re-seeds the
  // x-model bindings and eats the keystroke the guard exists to protect.
  check(c.reengineerHistory[0] === before,
    'focused: run object was replaced — the keystroke guard is defeated');
  check(c.milestones.length === 1,
    `focused: expected 1 milestone, got ${c.milestones.length}`);
  // A retry stays queued so the rest of the view lands once focus leaves.
  check(c._reRefreshTimers['re_x'] != null,
    'focused: no deferred retry queued for the full view');
  stop(c);
}

// 2. Nothing else leaks through while focused — the body stays deferred.
{
  const c = harness('swapping', 'swapping', true);
  const before = c.reengineerHistory[0];
  await c.refreshReengineer('re_x');
  check(c.reengineerHistory[0] === before,
    'focused: unchanged status must not splice the run');
  check(c.milestones.length === 0, 'focused: silent transition fired a milestone');
  stop(c);
}

// 3. No focus → the full fresh view is spliced in, as before.
{
  const c = harness('swapping', 'awaiting_approval', false);
  const before = c.reengineerHistory[0];
  await c.refreshReengineer('re_x');
  check(c.reengineerHistory[0] !== before, 'unfocused: run was not spliced');
  check(c.reengineerHistory[0].status === 'awaiting_approval', 'unfocused: status not applied');
  check(c.milestones.length === 1,
    `unfocused: expected 1 milestone, got ${c.milestones.length}`);
  stop(c);
}

// 4. The transition announces itself exactly once: the in-place patch fires
//    the chime, and the later full splice must stay silent.
{
  const c = harness('swapping', 'awaiting_approval', true);
  await c.refreshReengineer('re_x');
  c._isTypingProtectedField = () => false;
  await c.refreshReengineer('re_x');
  check(c.milestones.length === 1,
    `patch-then-splice: milestone fired ${c.milestones.length}× (must be once)`);
  check(c.reengineerHistory[0].status === 'awaiting_approval',
    'patch-then-splice: status regressed');
  stop(c);
}

console.log(JSON.stringify(failures.length ? { ok: false, failures } : { ok: true }));
