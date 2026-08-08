// Behavioral harness for "välj karaktärsbild i ↻ Kör om med nya karaktärer"
// (Hugo 2026-08-09). Run by tests/test_rerun_source_image.py.
//
// The re-run modal already SENT character_source_image_ids — nothing in the UI
// could ever set it, and the character chips rendered swapCharThumb(), i.e.
// whatever was staged on the always-visible Swap upload card. So the modal both
// showed a picture it was not going to use and offered no way to change it.
//
// What is pinned here:
//
//   * the modal reads and writes its OWN sourceOverrides — never the Swap
//     form's, which drives an unrelated card the user may have staged;
//   * the bulk "bild N för alla" dropdown resolves against the MODAL's
//     selection (rerunModal.charIds), with the same clamp-to-last rule;
//   * landing on the ★ primary means "no override", so the bulk control and
//     the per-character ↕ picker can never disagree;
//   * opening the modal a second time starts clean — a pick carried over from
//     the previous re-run would silently swap from the wrong reference;
//   * submitRerun still forwards the picks, for the SELECTED cast only.
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

// helene: 2 images (primary = #1). ching: 4 (primary = #1).
// klas: 4 with primary = #2 — the case where "bild 2" means "no override".
const makeLibrary = () => [
  { char_id: 'helene', name: 'Helene', primary_image_id: 'helene_1',
    url: '/helene_1.png',
    images: [{ image_id: 'helene_1', url: '/helene_1.png' },
             { image_id: 'helene_2', url: '/helene_2.png' }] },
  { char_id: 'ching', name: 'Ching', primary_image_id: 'ching_1',
    url: '/ching_1.png',
    images: [{ image_id: 'ching_1', url: '/ching_1.png' },
             { image_id: 'ching_2', url: '/ching_2.png' },
             { image_id: 'ching_3', url: '/ching_3.png' },
             { image_id: 'ching_4', url: '/ching_4.png' }] },
  { char_id: 'klas', name: 'Klas', primary_image_id: 'klas_2',
    url: '/klas_2.png',
    images: [{ image_id: 'klas_1', url: '/klas_1.png' },
             { image_id: 'klas_2', url: '/klas_2.png' },
             { image_id: 'klas_3', url: '/klas_3.png' },
             { image_id: 'klas_4', url: '/klas_4.png' }] },
];

function comp(selected = ['helene', 'ching', 'klas']) {
  const c = studio();
  c.library = makeLibrary();
  c.rerunModal.charIds = [...selected];
  c.notifyInfo = () => {};
  c.notifyError = (m) => { c._errors = [...(c._errors || []), m]; };
  c._startReengineerPolling = () => {};
  return c;
}

// --- the modal reads and writes its OWN map ---------------------------------

{
  const c = comp();
  // A pick staged on the ALWAYS-VISIBLE Swap upload card must not colour the
  // modal — that was the shipped bug (the chips called swapCharThumb).
  c.swapFromImages.sourceOverrides = { ching: 'ching_4' };
  check(c.rerunCharThumb(c.library[1]) === '/ching_1.png',
    `the modal must show its own pick, got ${c.rerunCharThumb(c.library[1])}`);

  c.pickRerunSource('ching', 'ching_3');
  check(c.rerunModal.sourceOverrides.ching === 'ching_3',
    'the per-character ↕ pick must land in the modal map');
  check(c.swapFromImages.sourceOverrides.ching === 'ching_4',
    'the Swap form must be untouched by a pick made in the modal');
  check(c.rerunCharThumb(c.library[1]) === '/ching_3.png',
    'the chip must show the image the re-run will actually swap from');
  check(c.rerunPickerChar === null, 'picking closes the popover');
}

{
  // Back onto the ★ primary = no override, matching the bulk control.
  const c = comp();
  c.pickRerunSource('helene', 'helene_2');
  check(c.rerunModal.sourceOverrides.helene === 'helene_2', 'setup');
  c.pickRerunSource('helene', 'helene_1');
  check(!('helene' in c.rerunModal.sourceOverrides),
    'picking the ★ primary must clear the override, not pin it');
  check(c.rerunCharThumb(c.library[0]) === '/helene_1.png',
    'and the chip falls back to the primary thumbnail');
}

// --- the bulk dropdown, scoped to the modal's own selection ------------------

{
  const c = comp(['helene', 'ching', 'klas']);
  // An unrelated selection on the Swap form must not widen or narrow this.
  c.swapFromImages.charIds = ['helene'];
  check(c.bulkSourceMax('rerun') === 4,
    `positions come from the MODAL's cast, got ${c.bulkSourceMax('rerun')}`);
  c.applyBulkSourceImage('rerun', 3);
  const ov = c.rerunModal.sourceOverrides;
  check(ov.ching === 'ching_3' && ov.klas === 'klas_3', 'exact hits are set');
  check(ov.helene === 'helene_2',
    `helene (2 images) clamps to her last, got ${ov.helene}`);
  check(/1 hade färre bilder/.test(c.bulkSourceNote.rerun),
    `the clamp must be stated, got: ${c.bulkSourceNote.rerun}`);
  check(!c.bulkSourceNote.swap && Object.keys(
    c.swapFromImages.sourceOverrides).length === 0,
    'a bulk pick in the modal must not write the Swap form');
}

{
  // Hugo's flow: bulk first, then override one character for this run.
  const c = comp();
  c.applyBulkSourceImage('rerun', 2);
  check(!('klas' in c.rerunModal.sourceOverrides),
    "klas's primary IS #2 — that must read as no override");
  c.pickRerunSource('ching', 'ching_4');
  check(c.rerunModal.sourceOverrides.ching === 'ching_4',
    'the individual pick wins for that character');
  check(c.rerunModal.sourceOverrides.helene === 'helene_2',
    'the others keep what the bulk pick gave them');
  // ★ reset drops the picks for the selection.
  c.clearBulkSourceImage('rerun');
  check(Object.keys(c.rerunModal.sourceOverrides).length === 0,
    'the ★ reset row clears the modal picks');
}

{
  // Changing the cast retires the note — it described a different set.
  const c = comp(['ching']);
  c.applyBulkSourceImage('rerun', 2);
  check(!!c.bulkSourceNote.rerun, 'setup: note present');
  c.rerunToggleChar('klas');
  check(!c.bulkSourceNote.rerun,
    'a note surviving a cast change claims characters it never touched');
  c.applyBulkSourceImage('rerun', 2);
  c.rerunUseParentCast();
  check(!c.bulkSourceNote.rerun,
    '"↺ samma som förra" replaces the cast — same reasoning');
}

// --- opening the modal starts clean -----------------------------------------

{
  const c = comp([]);
  c.rerunModal.sourceOverrides = { ching: 'ching_4' };
  c.bulkSourceNote = { ...c.bulkSourceNote, rerun: 'Bild 4 → 1 karaktär' };
  c.rerunPickerChar = 'ching';
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ character_ids: [], source_name: 'x',
                         settings: {}, scenes: [] }),
  });
  await c.openRerunModal({ re_id: 're_1' });
  check(Object.keys(c.rerunModal.sourceOverrides).length === 0,
    'a pick carried into the NEXT re-run would silently swap from the wrong '
    + 'reference image');
  check(!c.bulkSourceNote.rerun, 'the stale note is cleared too');
  check(c.rerunPickerChar === null, 'no popover is left hanging open');
}

// --- the picks reach the server, for the selected cast only -----------------

{
  const c = comp(['helene', 'klas']);
  c.rerunModal.reId = 're_parent';
  c.rerunModal.rows = [{ idx: 0, include: true, missingFile: false,
                         motion_prompt: 'p', secs: 5, direct: false,
                         twoPerson: false, keepEndFrame: true,
                         reuseDirectClip: true, videoModel: '' }];
  c.applyBulkSourceImage('rerun', 2);
  c.pickRerunSource('ching', 'ching_4');   // NOT in the cast
  let sent = null;
  globalThis.fetch = async (url, opts) => {
    sent = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ re_id: 're_child', status: 'queued' }) };
  };
  await c.submitRerun();
  check(!!sent, 'the re-run was submitted');
  const picks = (sent && sent.character_source_image_ids) || {};
  check(picks.helene === 'helene_2',
    `the picks must reach the server, got ${JSON.stringify(picks)}`);
  check(!('klas' in picks),
    "klas's ★ primary IS image 2 — no override is the correct wire form");
  check(!('ching' in picks),
    'a pick for a character NOT in the cast must not be sent');
}

process.stdout.write(JSON.stringify(
  failures.length ? { ok: false, failures } : { ok: true }) + '\n');
process.exit(0);
