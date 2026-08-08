// Behavioral harness for the Repurpose modal's "skicka automatiskt till
// Telegram" ✓ (run by tests/test_repurpose_telegram_toggle.py).
//
// Hugo 2026-08-09: every repurpose used to auto-send, with no way to say no.
// The toggle keeps TRUE as the default, so the two things worth locking on the
// client are (a) the field actually reaches the POST body and (b) a MISSING
// value means auto-send, never a silent "no" — the same missing-key-vs-false
// trap that made the Repurpose button itself a dead no-op in 2026-06-27.
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

// --- the POST body --------------------------------------------------------
s.repurposeSettings = { ...s._repurposeDefault };
check(s._repurposeBody().auto_telegram_send === true,
  'default settings must POST auto_telegram_send: true (pre-toggle behavior)');

s.repurposeSettings = { ...s._repurposeDefault, autoTelegramSend: false };
check(s._repurposeBody().auto_telegram_send === false,
  'unticking the box must POST auto_telegram_send: false');

// A seed with the key entirely absent (an older cached localStorage preset, or
// any future path that forgets to include it) must mean AUTO-SEND — coercing an
// undefined with !! would silently stop delivering.
s.repurposeSettings = { template: 'capcut-bluebox' };
check(s._repurposeBody().auto_telegram_send === true,
  'a settings object WITHOUT the key must still POST true, not false');

// Explicit falsy-but-present values still mean off.
s.repurposeSettings = { autoTelegramSend: null };
check(s._repurposeBody().auto_telegram_send === false,
  'an explicit null must POST false (present-but-falsy is a real choice)');

// --- rehydration from the stored per-run settings -------------------------
check(s._mapStoredToReAsm({ auto_telegram_send: false }).autoTelegramSend === false,
  'a stored false must rehydrate the box as unticked');
check(s._mapStoredToReAsm({ auto_telegram_send: true }).autoTelegramSend === true,
  'a stored true must rehydrate the box as ticked');
check(!('autoTelegramSend' in s._mapStoredToReAsm({})),
  'settings with no stored value must not shadow the preset default');

// --- the default the modal opens with ------------------------------------
check(s._repurposeDefault.autoTelegramSend === true,
  'the repurpose preset must default to auto-send (Hugo: keep todays behavior)');

// Opening for a run whose LAST repurpose was built without auto-send must come
// back unticked — "kommas ihåg per körning".
s.openRepurposeModal('reengineer',
  { re_id: 're_x', repurpose_settings: { auto_telegram_send: false } });
check(s.repurposeSettings.autoTelegramSend === false,
  'reopening a run that last built without auto-send must open unticked');
check(s._repurposeBody().auto_telegram_send === false,
  'and that rehydrated choice must survive into the POST body');

// A run that has never been repurposed opens with the default.
s.openRepurposeModal('reengineer', { re_id: 're_y' });
check(s.repurposeSettings.autoTelegramSend === true,
  'a never-repurposed run must open with auto-send ticked');

// Same for a saved Editor reel (its snapshot lives on editor.repurpose_settings).
s.openRepurposeModal('editor',
  { gen_id: 'g_1', editor: { repurpose_settings: { auto_telegram_send: false } } });
check(s.repurposeSettings.autoTelegramSend === false,
  'an Editor reel built without auto-send must reopen unticked');

console.log(JSON.stringify(
  failures.length ? { ok: false, failures } : { ok: true }));
