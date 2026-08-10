// Behavioral harness for dropping a video onto "eget klipp"
// (run by tests/test_clip_drop.py).
//
// Hugo 2026-08-11: the file picker was the only way in, and dragging a video
// onto the label — the obvious gesture — did nothing at all.
//
// The two things that could break quietly:
//   * preventDefault() on EVERY dragover would make the label look droppable
//     for the library's own internal drags (image reorder, drag-into-job),
//     which carry their own dataTransfer types and no files, and would swallow
//     those drops. Same rule onImageReorderOver already follows.
//   * a video whose mime type the browser did not recognise (.mkv, and
//     anything dragged out of certain apps arrives with an EMPTY type) must
//     not be rejected — a false refusal is worse than letting the server judge.
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

const notices = [];
s.notifyError = (m) => notices.push(['error', m]);
s.notifyInfo = (m) => notices.push(['info', m]);

const target = () => {
  const cls = new Set();
  return {
    classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c),
                 has: (c) => cls.has(c) },
  };
};
const dragEv = (types, files) => {
  const t = target();
  let prevented = false;
  return { ev: { dataTransfer: { types, files }, currentTarget: t,
                 preventDefault: () => { prevented = true; } },
           t, wasPrevented: () => prevented };
};

// --- dragover: only file drags may claim the label ---------------------------
{
  const { ev, t, wasPrevented } = dragEv(['Files']);
  s.onClipDragOver(ev);
  check(wasPrevented(), 'a file drag must be accepted (preventDefault)');
  check(t.classList.has('clipdrop'), 'a file drag must highlight the target');
}
{
  // The library's own image-reorder drag. It carries no files.
  const { ev, t, wasPrevented } = dragEv(['text/x-charswap-img-order']);
  s.onClipDragOver(ev);
  check(!wasPrevented(), 'an internal drag must NOT be captured');
  check(!t.classList.has('clipdrop'),
    'an internal drag must not make the label look droppable');
}
{
  const { ev, wasPrevented } = dragEv([]);
  s.onClipDragOver(ev);
  check(!wasPrevented(), 'a drag with no types at all must be ignored');
}

// --- the drop itself ---------------------------------------------------------
const drop = (files) => {
  const t = target();
  t.classList.add('clipdrop');
  return { file: s.clipFileFromDrop({ dataTransfer: { files }, currentTarget: t }),
           t };
};

{
  const f = { name: 'take.mp4', type: 'video/mp4' };
  const { file, t } = drop([f]);
  check(file === f, 'a plain mp4 must come through');
  check(!t.classList.has('clipdrop'), 'the highlight must clear on drop');
}
{
  // THE FALSE-REFUSAL CASE: browsers hand over an empty type for .mkv and for
  // files dragged out of some apps. Rejecting those would block real videos.
  const f = { name: 'scen 3.mkv', type: '' };
  check(drop([f]).file === f, 'a video with no mime type must not be refused');
  check(drop([{ name: 'clip.MOV', type: '' }]).file !== null,
    'extension matching must be case-insensitive');
}
{
  notices.length = 0;
  const { file } = drop([{ name: 'bild.png', type: 'image/png' }]);
  check(file === null, 'a non-video must be refused');
  check(notices.some(([k, m]) => k === 'error' && /bild\.png/.test(m)),
    'the refusal must name the file, not fail silently');
}
{
  notices.length = 0;
  const a = { name: 'a.mp4', type: 'video/mp4' };
  const { file } = drop([a, { name: 'b.mp4', type: 'video/mp4' }]);
  check(file === a, 'the first of several files is used');
  check(notices.some(([k, m]) => k === 'info' && /a\.mp4/.test(m)),
    'using one of several dropped files must be said out loud');
}
{
  check(drop([]).file === null, 'an empty drop must not throw');
  check(s.clipFileFromDrop({ currentTarget: target() }) === null,
    'a drop with no dataTransfer must not throw');
}

// --- the picker path still works, and now shares the upload code -------------
check(typeof s.importSwapClipFile === 'function'
   && typeof s.importReClipFile === 'function',
  'both import paths must expose a file-taking entry point');

console.log(JSON.stringify(failures.length ? { ok: false, failures } : { ok: true }));
