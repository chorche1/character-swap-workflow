// Behavioral harness for app.js `clipRetryNote` / `clipTakesNote`
// (run by tests/test_clip_retry_visible.py).
//
// Hugo 2026-08-10, looking at a run in the app: "hur vet jag att något
// retryar, står det fortfarande så här rött?". It didn't say anything at all.
// A refused clip is put straight back into PROCESSING by the retry loop, so on
// screen it is byte-identical to an ordinary slow render — the only evidence a
// retry was happening lived in the server log. These two helpers turn the
// persisted `refusal_takes` counter into the amber in-flight chip and the grey
// after-the-fact note.
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

// THE REPORTED CASE: a clip that has been refused twice and is rendering its
// third take must SAY so, in flight, while Hugo is looking at it.
const retrying = { status: 'processing', refusal_takes: 2 };
check(s.clipRetryNote(retrying) !== '', 'a retrying clip must show a note');
check(/nekad/.test(s.clipRetryNote(retrying)),
  'the note must say the take was refused');
check(/2/.test(s.clipRetryNote(retrying)),
  'the note must carry the refusal count');
check(/3/.test(s.clipRetryNote(retrying)),
  'the note must name the take now running (refusals + 1)');

// PENDING counts too — a clip re-submitted but not yet picked up by the
// provider sits there, and that is exactly the window Hugo was staring at.
check(s.clipRetryNote({ status: 'pending', refusal_takes: 1 }) !== '',
  'a re-submitted clip waiting to start must show the note');

// A clip nobody refused must stay silent: the chip has to mean something.
check(s.clipRetryNote({ status: 'processing', refusal_takes: 0 }) === '',
  'an ordinary rendering clip must show no retry note');
check(s.clipRetryNote({ status: 'processing' }) === '',
  'a payload predating the counter must not paint a chip');
check(s.clipRetryNote(null) === '', 'a missing clip must not throw');
check(s.clipRetryNote(undefined) === '', 'an absent clip must not throw');

// A FAILED clip is deliberately silent here: the red explainer panel already
// says the content check refused it, and two red messages about one clip is
// noise. This is the "står det fortfarande så här rött" half of the question —
// yes, and only there.
check(s.clipRetryNote({ status: 'failed', refusal_takes: 5 }) === '',
  'a failed clip must not also show the retry chip');
check(s.clipRetryNote({ status: 'error', refusal_takes: 5 }) === '',
  'an errored clip must not also show the retry chip');

// DONE: no longer retrying, so the amber chip must go — but the count explains
// why that one clip took five minutes, so it survives as a grey note.
check(s.clipRetryNote({ status: 'done', refusal_takes: 3 }) === '',
  'a finished clip must not claim to be retrying');
const done = s.clipTakesNote({ status: 'done', refusal_takes: 3 });
check(done !== '', 'a clip that needed retries must say so once done');
check(/4/.test(done), 'the done note must name the take that succeeded');
check(s.clipTakesNote({ status: 'done', refusal_takes: 0 }) === '',
  'a clip that sailed through must show nothing');
check(s.clipTakesNote({ status: 'processing', refusal_takes: 2 }) === '',
  'the done note must not appear while the clip is still running');
check(s.clipTakesNote(null) === '', 'a missing clip must not throw');

console.log(JSON.stringify(failures.length ? { ok: false, failures } : { ok: true }));
