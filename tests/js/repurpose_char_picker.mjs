// Behavioral harness for the Repurpose modal's CHARACTER PICKER
// (run by tests/test_repurpose_char_picker.py).
//
// Hugo 2026-08-10: "i den här menyn vill jag kunna välja vilka karaktärer som
// ska få repurpose för körningen". He chose a PER-CLICK filter — the modal
// opens with everyone ticked every time — so the things worth locking on the
// client are that the seed really is everyone, that `char_ids` reaches the POST
// body only for a real SUBSET (omitting it is exactly what "the whole cast"
// means server-side), and that an Editor reel never sends the field at all
// (its endpoint's body model has no such key).
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

// Minimum wiring the modal opener touches for a Reengineer run.
s.editorTemplates = [];
s.elevenlabsVoices = [];
s.reengineerHistory = [];

const run = {
  re_id: 're_1',
  finals: { c1: { status: 'done' }, c2: { status: 'done' },
            c3: { status: 'done' } },
  repurposed: { c1: { status: 'done' } },
  char_names: { c1: 'Thor', c2: 'Connor', c3: 'Susanne' },
};

// --- the seed -------------------------------------------------------------
s.openRepurposeModal('reengineer', run);
const m = s.repurposeModal;
check(m.open === true && m.kind === 'reengineer' && m.id === 're_1',
  'opening for a Reengineer run must keep kind/id intact');
check(m.chars.length === 3, 'the picker must list every character in the run');
check(m.chars.map(c => c.name).join(',') === 'Thor,Connor,Susanne',
  'names must resolve from char_names (the LIGHT history row) — gating on the '
  + 'hydrated job.characters is what has repeatedly hidden controls on older runs');
check(m.charIds.length === 3,
  'THE SEED: every character must open TICKED (Hugo chose a per-click filter, '
  + 'so nothing may arrive pre-unticked from an earlier repurpose)');
check(m.chars.find(c => c.id === 'c1').hasRepurpose === true
   && m.chars.find(c => c.id === 'c2').hasRepurpose === false,
  'a character that already has a mirrored copy must be marked (it gets overwritten)');

// --- the POST body --------------------------------------------------------
check(s._repurposeBody().char_ids === undefined,
  'ALL ticked must send NO char_ids — omitting it is exactly what "the whole '
  + 'cast" means server-side, so an unfiltered click keeps hitting the old path');

s.repurposeToggleChar('c2');
check(s.repurposeModal.charIds.join(',') === 'c1,c3',
  'unticking must remove exactly that character');
const body = s._repurposeBody();
check(Array.isArray(body.char_ids) && body.char_ids.join(',') === 'c1,c3',
  'THE FEATURE: a real subset must reach the POST body as char_ids');

s.repurposeToggleChar('c2');
check(s._repurposeBody().char_ids === undefined,
  're-ticking must return to the unfiltered body');

// --- select-all / none ----------------------------------------------------
s.repurposeSelectAllChars(false);
check(s.repurposeModal.charIds.length === 0, '"Ingen" must untick everything');
s.repurposeSelectAllChars(true);
check(s.repurposeModal.charIds.length === 3, '"Alla" must re-tick everything');

// --- the other settings still ride along ----------------------------------
s.repurposeSelectAllChars(false);
s.repurposeToggleChar('c1');
const partial = s._repurposeBody();
check(partial.auto_telegram_send === true,
  'the Telegram ✓ must still default to true alongside a partial pick '
  + '(a missing value has always meant SEND)');
check(typeof partial.template === 'string' && 'playback_speed' in partial,
  'the edit settings must still be in the body when a subset is picked');

// --- Editor reels have no cast --------------------------------------------
s.editorJobs = [];
s.openRepurposeModal('editor', {
  gen_id: 'g1', editor: { settings: {}, repurpose_settings: {} } });
check(s.repurposeModal.chars.length === 0,
  'an Editor reel must offer no character picker');
check(s._repurposeBody().char_ids === undefined,
  'an Editor repurpose must never send char_ids — EditorRepurposeBody has no '
  + 'such field');

// --- a Swap job's picker --------------------------------------------------
s.job = {
  job_id: 'j1',
  characters: {
    a: { name: 'A', approved_variant_ids: ['v1'],
         videos: [{ status: 'done', url: '/x.mp4' }],
         repurpose_status: 'done' },
    b: { name: 'B', approved_variant_ids: ['v2'],
         videos: [{ status: 'done', url: '/y.mp4' }] },
    // No done video → not compilable → must not be offered.
    c: { name: 'C', approved_variant_ids: ['v3'], videos: [] },
  },
};
s.openRepurposeModal('swap', null);
check(s.repurposeModal.chars.map(c => c.id).join(',') === 'a,b',
  'the Swap picker must offer exactly the compilable characters');
check(s.repurposeModal.charIds.length === 2,
  'the Swap picker must also open with everyone ticked');
s.repurposeToggleChar('a');
check(s._repurposeBody().char_ids.join(',') === 'b',
  'a Swap subset must reach the POST body too');

console.log(JSON.stringify(
  failures.length ? { ok: false, failures } : { ok: true }));
process.exit(failures.length ? 1 : 0);
