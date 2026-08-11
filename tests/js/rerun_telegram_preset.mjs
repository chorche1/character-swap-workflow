// The rerun modal must open with "bygg ihop + skicka till Telegram" ON
// (run by tests/test_rerun_telegram_preset.py).
//
// Hugo 2026-08-11: "gör så att bygg ihop och skicka till telegram är på som
// preset". The modal inherited the flag from the PARENT run with
// `!!s.auto_telegram_send`, so a parent that had it off — or an older run with
// no such field at all — opened the box unticked, and a rerun quietly built
// finals nobody delivered.
//
// The safe direction is ON: a final that never reaches Telegram is a silent
// loss, noticed only when someone goes looking for it. Unticking is one click.
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

// Every form that can START a run defaults to sending.
check(s.swapFromImages.autoTelegramSend === true,
  'the Swap-from-images form must default to sending');
check(s.reengineerGen.autoTelegramSend === true,
  'the Reengineer upload form must default to sending');
check(s.rerunModal.autoTelegramSend === true,
  'the rerun modal state must default to sending');
check(s.versionModal.autoTelegramSend === true,
  'the versions modal must default to sending');

// …and the rerun PREFILL, which is what actually runs when the modal opens,
// must not be able to turn it off. This is the line that was broken: it read
// the parent run's value through `!!`, so both an explicit false and a missing
// field produced an unticked box.
const prefill = src.split('async openRerunModal', 2)[1] || src;
const assignment = prefill.split('m.autoTelegramSend =', 2)[1];
check(assignment !== undefined, 'the prefill must set autoTelegramSend');
if (assignment !== undefined) {
  const stmt = assignment.split(';', 1)[0].trim();
  check(stmt === 'true',
    `the prefill must force it on, got: ${stmt}`);
  check(!/!!\s*s\.auto_telegram_send/.test(stmt),
    'the prefill must not coerce the parent value with !!');
}

console.log(JSON.stringify(failures.length ? { ok: false, failures } : { ok: true }));
