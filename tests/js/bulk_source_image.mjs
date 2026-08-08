// Behavioral harness for app.js's "Använd bild N för alla" bulk reference-image
// picker + the library image reorder (Hugo 2026-08-08). Run by
// tests/test_bulk_source_image.py.
//
// The two features are one mechanism: the bulk picker resolves "bild N" by
// POSITION in the character's gallery, and the reorder controls are what let
// the user decide what position N means. The rules worth pinning:
//
//   * a character with FEWER images than N takes its LAST image (Hugo's call —
//     never skipped, never left on a stale pick) and the note SAYS so, so the
//     substitution is never silent;
//   * landing on the ★ primary means "no override", matching the per-character
//     ↕ picker, so the two controls can never disagree;
//   * only SELECTED characters are touched;
//   * a reorder PATCH always sends a full permutation, and a failed one puts
//     the old order back instead of leaving the UI and server disagreeing.
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

// wang: 1 image (primary). Helene: 2 (primary = #1). Ching: 4 (primary = #1).
// Klas: 4 with primary = #2 — the case where "bild 2" means "no override".
const makeLibrary = () => [
  { char_id: 'wang', name: 'wang', primary_image_id: 'wang_1',
    images: [{ image_id: 'wang_1' }] },
  { char_id: 'helene', name: 'Helene', primary_image_id: 'helene_1',
    images: [{ image_id: 'helene_1' }, { image_id: 'helene_2' }] },
  { char_id: 'ching', name: 'Ching', primary_image_id: 'ching_1',
    images: [{ image_id: 'ching_1' }, { image_id: 'ching_2' },
             { image_id: 'ching_3' }, { image_id: 'ching_4' }] },
  { char_id: 'klas', name: 'Klas', primary_image_id: 'klas_2',
    images: [{ image_id: 'klas_1' }, { image_id: 'klas_2' },
             { image_id: 'klas_3' }, { image_id: 'klas_4' }] },
];

// A component with the library loaded and `selected` picked on the swap form.
function comp(selected = ['wang', 'helene', 'ching', 'klas'], fetchImpl = null) {
  const c = studio();
  c.library = makeLibrary();
  c.swapFromImages.charIds = [...selected];
  c.notifyError = (m) => { c._errors = [...(c._errors || []), m]; };
  c.loadLibrary = async () => { c._reloads = (c._reloads || 0) + 1; };
  if (fetchImpl) globalThis.fetch = fetchImpl;
  return c;
}

// --- bulk pick ---------------------------------------------------------------

{
  const c = comp();
  c.applyBulkSourceImage('swap', 3);
  const ov = c.swapFromImages.sourceOverrides;
  // Enough images: the exact position.
  check(ov.ching === 'ching_3', `ching should take image 3, got ${ov.ching}`);
  check(ov.klas === 'klas_3', `klas should take image 3, got ${ov.klas}`);
  // Fewer images than asked for → their LAST image, not a skip.
  check(ov.helene === 'helene_2',
    `helene (2 images) should clamp to her last, got ${ov.helene}`);
  // wang's only image IS her primary, so "no override" is the correct
  // representation of "use image 1" — the resolved source is still that image.
  check(!('wang' in ov),
    'a clamp that lands on the primary must clear the override, not pin it');
  check(/2 hade färre bilder/.test(c.bulkSourceNote.swap),
    `the note must say how many were clamped, got: ${c.bulkSourceNote.swap}`);
  check(/Bild 3 → 2 karaktärer/.test(c.bulkSourceNote.swap),
    `the note must count the exact hits, got: ${c.bulkSourceNote.swap}`);
}

{
  // Position 1 is the common "reset everyone to their first image" case.
  const c = comp();
  c.applyBulkSourceImage('swap', 1);
  const ov = c.swapFromImages.sourceOverrides;
  check(ov.klas === 'klas_1',
    `klas's primary is #2, so image 1 must be an explicit override, got ${ov.klas}`);
  check(!('ching' in ov) && !('wang' in ov) && !('helene' in ov),
    'characters whose image 1 IS the primary need no override');
  check(!/färre bilder/.test(c.bulkSourceNote.swap),
    'nothing is clamped at position 1 — the note must not claim otherwise');
}

{
  // A previous pick must not survive a new one that resolves to the primary.
  const c = comp();
  c.applyBulkSourceImage('swap', 4);
  check(c.swapFromImages.sourceOverrides.klas === 'klas_4', 'setup: klas on #4');
  c.applyBulkSourceImage('swap', 2);
  check(!('klas' in c.swapFromImages.sourceOverrides),
    'picking the position that IS the primary must clear the stale override');
}

{
  // Only the selected characters are touched.
  const c = comp(['ching']);
  c.swapFromImages.sourceOverrides = { klas: 'klas_4' };
  c.applyBulkSourceImage('swap', 2);
  const ov = c.swapFromImages.sourceOverrides;
  check(ov.ching === 'ching_2', 'the selected character is set');
  check(ov.klas === 'klas_4',
    'an UNSELECTED character keeps its own pick — the bulk apply is scoped');
  check(c.bulkSourceMax('swap') === 4, 'max positions come from the selection');
}

{
  // The dropdown only offers positions the selection can actually reach.
  const c = comp(['wang', 'helene']);
  check(c.bulkSourceMax('swap') === 2,
    `max should be the largest gallery in the selection, got ${c.bulkSourceMax('swap')}`);
  const empty = comp([]);
  check(empty.bulkSourceMax('swap') === 0, 'no selection → no dropdown');
  empty.applyBulkSourceImage('swap', 2);
  check(!empty.bulkSourceNote.swap,
    'applying with nothing selected must be a no-op, not a note claiming work');
}

{
  // ★ reset drops the picks for the selection only.
  const c = comp(['ching']);
  c.swapFromImages.sourceOverrides = { ching: 'ching_3', klas: 'klas_4' };
  c.clearBulkSourceImage('swap');
  check(!('ching' in c.swapFromImages.sourceOverrides), 'selected pick cleared');
  check(c.swapFromImages.sourceOverrides.klas === 'klas_4',
    'unselected characters keep their picks on a reset');
}

{
  // The two forms are independent: the Reengineer picker must not write the
  // Swap form's overrides.
  const c = comp([]);
  c.reengineerGen.charIds = ['ching'];
  c.applyBulkSourceImage('reengineer', 2);
  check(c.reengineerGen.sourceOverrides.ching === 'ching_2',
    'the reengineer form gets its own override');
  check(!('ching' in c.swapFromImages.sourceOverrides),
    'the swap form must be untouched by a reengineer bulk pick');
  check(!c.bulkSourceNote.swap && !!c.bulkSourceNote.reengineer,
    'the note is per form');
}

{
  // Hugo's stated flow (2026-08-08): "välj bild 2 för alla och sedan välja
  // specifikt per karaktär för den körningen som override". The bulk pick and
  // the per-character ↕ picker write the SAME map, so the individual pick that
  // comes second simply wins — for that character only.
  const c = comp();
  c.applyBulkSourceImage('swap', 2);
  c.pickSwapSource('ching', 'ching_4');
  const ov = c.swapFromImages.sourceOverrides;
  check(ov.ching === 'ching_4', `the per-character pick must win, got ${ov.ching}`);
  check(ov.helene === 'helene_2' && !('klas' in ov),
    'the other characters keep exactly what the bulk pick gave them');
  // …and an individual pick back onto the ★ primary drops only that entry.
  c.pickSwapSource('helene', 'helene_1');
  check(!('helene' in c.swapFromImages.sourceOverrides) &&
        c.swapFromImages.sourceOverrides.ching === 'ching_4',
    'picking the primary clears that one character, not the run');
  // A LATER bulk pick is the blunt instrument and re-covers everyone.
  c.applyBulkSourceImage('swap', 1);
  check(c.swapFromImages.sourceOverrides.ching === undefined ||
        c.swapFromImages.sourceOverrides.ching === 'ching_1',
    'a second bulk pick overrides the individual one');
}

{
  // Changing the selection retires the note — it described a different set.
  const c = comp(['ching']);
  c.applyBulkSourceImage('swap', 2);
  check(!!c.bulkSourceNote.swap, 'setup: note present');
  c.toggleSwapChar('klas');
  check(!c.bulkSourceNote.swap,
    'a note that survives a selection change claims characters it never touched');
}

{
  // The dropdown resets itself so it never reads as current state.
  const c = comp(['ching']);
  const ev = { target: { value: '2' } };
  c.onBulkSourceSelect('swap', ev);
  check(c.swapFromImages.sourceOverrides.ching === 'ching_2', 'select applies');
  check(ev.target.value === '', 'the select resets to the placeholder');
  const reset = { target: { value: 'primary' } };
  c.onBulkSourceSelect('swap', reset);
  check(!('ching' in c.swapFromImages.sourceOverrides), 'the primary row resets');
  const noop = { target: { value: '' } };
  c.swapFromImages.sourceOverrides = { ching: 'ching_4' };
  c.onBulkSourceSelect('swap', noop);
  check(c.swapFromImages.sourceOverrides.ching === 'ching_4',
    'the placeholder itself must do nothing');
}

// --- reorder -----------------------------------------------------------------

const okFetch = (sink) => async (url, opts) => {
  sink.push({ url, body: JSON.parse(opts.body) });
  return { ok: true, json: async () => ({}), text: async () => '' };
};

{
  const sent = [];
  const c = comp([], okFetch(sent));
  await c.moveCharacterImage('ching', 'ching_3', -1);
  check(c.library[2].images.map(i => i.image_id).join() === 'ching_1,ching_3,ching_2,ching_4',
    'a ◀ move swaps the image one step earlier');
  check(sent.length === 1 && sent[0].url === '/api/characters/ching',
    'the move is persisted');
  check(sent[0].body.image_order.join() === 'ching_1,ching_3,ching_2,ching_4',
    'the PATCH carries the FULL new order');
  check(!('primary_image_id' in sent[0].body),
    'a reorder must not touch the ★ primary');
}

{
  const sent = [];
  const c = comp([], okFetch(sent));
  await c.moveCharacterImage('ching', 'ching_1', -1);
  check(sent.length === 0 && c.library[2].images[0].image_id === 'ching_1',
    'moving the first image backwards is a no-op, not a wrap-around');
  await c.moveCharacterImage('ching', 'ching_4', 1);
  check(sent.length === 0 && c.library[2].images[3].image_id === 'ching_4',
    'moving the last image forwards is a no-op');
}

{
  // Drag the 4th image onto the 1st: it lands at position 1, the rest shift.
  const sent = [];
  const c = comp([], okFetch(sent));
  c.startImageReorder({ dataTransfer: { setData: () => {} } }, 'ching', 'ching_4');
  check(!!c.imgReorder, 'the drag is registered');
  let prevented = false;
  c.onImageReorderOver({ preventDefault: () => { prevented = true; } }, 'ching', 'ching_1');
  check(prevented, 'a same-character reorder drag must be droppable');
  check(c.imgReorderOver === 'ching|ching_1', 'the hovered tile is highlighted');
  await c.dropImageReorder({ preventDefault: () => {} }, 'ching', 'ching_1');
  check(c.library[2].images.map(i => i.image_id).join() === 'ching_4,ching_1,ching_2,ching_3',
    'the dragged image lands at the target position');
  check(sent.length === 1 &&
        sent[0].body.image_order.join() === 'ching_4,ching_1,ching_2,ching_3',
    'the drop is persisted as a full permutation');
  check(!c.imgReorder && !c.imgReorderOver, 'drag state is cleared after the drop');
}

{
  // A drag that started on ANOTHER character must never be droppable here —
  // the same tiles also carry the drag-into-a-job gesture.
  const sent = [];
  const c = comp([], okFetch(sent));
  c.startImageReorder({ dataTransfer: { setData: () => {} } }, 'ching', 'ching_2');
  let prevented = false;
  c.onImageReorderOver({ preventDefault: () => { prevented = true; } }, 'klas', 'klas_1');
  check(!prevented, 'a cross-character reorder must not become droppable');
  await c.dropImageReorder({ preventDefault: () => {} }, 'klas', 'klas_1');
  check(sent.length === 0 && c.library[3].images[0].image_id === 'klas_1',
    'and dropping it anyway changes nothing');
  // No drag at all (e.g. an add-to-job drag ending on a tile) is also inert.
  const c2 = comp([], okFetch(sent));
  let prevented2 = false;
  c2.onImageReorderOver({ preventDefault: () => { prevented2 = true; } }, 'ching', 'ching_1');
  check(!prevented2, 'without a reorder drag in flight nothing is droppable');
}

{
  // A rejected PATCH must not leave the UI showing an order the server
  // doesn't have.
  const c = comp([], async () => ({
    ok: false, json: async () => ({}), text: async () => 'boom' }));
  await c.moveCharacterImage('ching', 'ching_3', -1);
  check(c.library[2].images.map(i => i.image_id).join() === 'ching_1,ching_2,ching_3,ching_4',
    'a failed save must put the old order back');
  check((c._errors || []).length === 1, 'the failure is reported, not swallowed');
  check((c._reloads || 0) === 1, 'and the library is resynced from the server');
}

console.log(JSON.stringify(failures.length
  ? { ok: false, failures }
  : { ok: true }));
