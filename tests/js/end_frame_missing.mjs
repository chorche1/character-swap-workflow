// Behavioral harness for the "slutbilden saknas" state
// (run by tests/test_end_frame_missing.py).
//
// Hugo 2026-08-11, pointing at five blank white boxes: "varför är det
// fortfarande vitt här". They were a SKELETON whose tooltip said "genererar
// slutbild…", shown whenever a scene had a shared end pose and a character had
// no swapped frame — without ever asking whether anything was actually
// generating. On that run the swap phase produced 30 variants and zero end
// frames (verified in calls.jsonl), so the placeholder sat there claiming
// progress forever, with nothing to click.
//
// End poses are only ever produced by the swap phase and by the ↻ button, so
// outside those states a missing frame is STUCK, not pending — and must say so.
//
// Prints one JSON line: {"ok": true} or {"ok": false, "failures": [...]}.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = readFileSync(process.env.APPJS || join(root, 'web', 'app.js'), 'utf8');

globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.document = { activeElement: null, addEventListener: () => {} };
globalThis.window = globalThis;
globalThis.location = { protocol: 'http:', host: '127.0.0.1:8000' };
globalThis.WebSocket = class { constructor() {} close() {} };

const s = new Function(`${src}\nreturn studio;`)()();
const failures = [];
const check = (cond, msg) => { if (!cond) failures.push(msg); };

const sc = { scene_id: 's1', idx: 1 };
const withPose = () => { s.reSceneEndFrameUrl = () => '/pose.png'; };
const noPose = () => { s.reSceneEndFrameUrl = () => null; };

withPose();

// THE REPORTED CASE: run parked at the gate, pose set, no swapped frame.
check(s.reEndFrameMissing({ status: 'awaiting_approval' }, sc, {}) === true,
  'a missing frame at the approval gate must be reported as missing');
check(s.reEndFrameMissing({ status: 'awaiting_assembly' }, sc, {}) === true,
  'and on a finished run too');
check(s.reEndFrameMissing({ status: 'done' }, sc, {}) === true,
  'and on a done run');

// While the swap phase runs it genuinely IS generating — the skeleton is right.
check(s.reEndFrameGenerating({ status: 'swapping' }) === true,
  'the swap phase is the one state that really generates end frames');
check(s.reEndFrameMissing({ status: 'swapping' }, sc, {}) === false,
  'no "missing" button while the swap phase is running');

// Anything the character already has wins over the missing state.
check(s.reEndFrameMissing({ status: 'done' }, sc,
  { end_frame_urls: { s1: '/e.png' } }) === false,
  'a swapped frame means nothing is missing');
check(s.reEndFrameMissing({ status: 'done' }, sc,
  { end_frame_upload_urls: { s1: '/u.png' } }) === false,
  "the character's OWN uploaded end frame wins over everything");
check(s.reEndFrameMissing({ status: 'done' }, sc,
  { end_frame_errors: { s1: 'content policy' } }) === false,
  'a FAILED swap has its own red marker — two states for one tile is noise');

// No shared pose at all: there is nothing to be missing.
noPose();
check(s.reEndFrameMissing({ status: 'done' }, sc, {}) === false,
  'a scene without a shared pose must show nothing');

console.log(JSON.stringify(failures.length ? { ok: false, failures } : { ok: true }));
