// Character Swap Studio — Alpine.js front-end.

function studio() {
  return {
    health: { openai_key: false, xai_key: false },
    // Step 1: array of uploaded scene objects {scene_id, url, original_name, ...}.
    // Empty until the user uploads at least one scene. The job is created from
    // scenes[] — each scene generates `images_per_character` variants per char.
    scenes: [],
    // Highlights the Step-1 dropzone when an image file is being dragged in.
    sceneDropActive: false,
    // Legacy single-scene field — kept so any old code paths that still read
    // `this.scene` don't break. Mirrors `scenes[0]` when present.
    scene: null,
    library: [],
    selectedCharacters: [],
    imagesPerChar: 1,
    videosPerChar: 1,
    job: null,
    jobsList: [],
    projects: [],
    collapsedProjects: new Set(),
    currentProjectId: null,
    editingProjectId: null,
    draftProjectName: '',
    showProjectModal: false,
    newProjectName: '',
    newProjectCharIds: [],
    submittingProject: false,
    showUploadModal: false,
    uploadFiles: [],            // [{file, url}]
    uploadTargetCharId: null,   // null = new character
    uploadNewCharName: '',
    uploadingChars: false,
    moveMenuJobId: null,
    searchQuery: '',
    jobCost: null,           // USD for the open job
    dailyCost: null,         // USD spent in last 24h
    _dailyCostTimer: null,
    toasts: [],              // {id, kind, msg, retry?}
    _toastSeq: 0,
    disk: null,              // {output_bytes, by_job}
    showDiskModal: false,
    activeTab: 'swap',
    models: { image: [], video: [], avatar: [], audio: [] },
    imageGen: { model: 'gpt-image', prompt: '', refs: [], aspect: '9:16', generating: false },
    videoGen: { model: 'grok-imagine', prompt: '', ref: null, aspect: '9:16', duration: 10, generating: false },
    avatarGen: { model: 'heygen-avatar-5', script: '', avatarId: '', voiceId: '', voiceProvider: 'heygen', aspect: '9:16', generating: false },
    audioGen: { model: 'elevenlabs-vc', voiceId: '', sourceAudio: null, script: '', generating: false },
    // B-roll: audio → transcript → planned visual prompts → clips → final mp4
    brollGen: {
      videoModel: 'grok-imagine',
      aspectRatio: '9:16',           // 9:16 | 1:1 | 16:9
      source: null,                  // {file, url, name, isVideo}
      submitting: false,
    },
    brollHistory: [],                 // [{broll_id, status, clips, ...}]
    _brollPollTimer: null,
    editor: {
      sourceVideo: null,           // {file, url, name}
      thresholdDb: -30,
      minSilenceSecs: 0.4,
      padSecs: 0.05,
      trimming: false,
      template: 'popout-yellow',
      captioning: false,
      autoEditing: false,
      voiceId: '',
      enableTrim: true,
      enableCaptions: true,
      enableNormalizeWpm: true,       // default-on; time-stretch each clip to target_wpm
      targetWpm: 190,                 // 190 WPM is the canonical "engaging pace" baseline
      rerendering: false,
      rerenderOpen: false,                    // shows the edit-result panel
      rerenderTemplate: 'popout-yellow',      // independent of editor.template so you can A/B
      rerenderTrimStart: 0,
      rerenderTrimEnd: 0,
      rerenderOverrides: {
        font: null, size: null, primary_color: null, outline_color: null,
        words_per_card: null, margin_v: null, margin_h: null, highlight_color: null, box: null,
        all_caps: null, shadow: null, alignment: null, outline: null,
      },
      overrides: {                 // CaptionStyle field overrides; null until user touches them
        font: null, size: null, primary_color: null, outline_color: null,
        words_per_card: null, margin_v: null, margin_h: null, highlight_color: null, box: null,
        all_caps: null, shadow: null, alignment: null, outline: null,
      },
      lastResult: null,            // {output_url, kind: 'trim'|'captions', ...}
    },
    // --- Studio-specific state, top-level so HTML can bind directly ---
    propTab: 'template',
    promptDropActive: false,         // highlights prompt area on file drag-over
    duration: 0,
    trimStartSecs: 0,
    trimEndSecs: 0,
    draggingText: false,
    _dragStartY: 0,
    _dragStartMargin: 400,
    _dragPreviewH: 1,
    editorTemplates: [],
    editorHistory: [],
    // --- CapCut-style timeline editor (operates on the LATEST rendered video) ---
    timeline: {
      open: false,                 // is the timeline panel visible?
      sourceUrl: '',               // URL of the video we're slicing
      sourceFilename: '',          // basename (sent to backend so it knows which file)
      sourceDuration: 0,           // full duration of the source video (s)
      segments: [],                // [{start, end}] in source seconds, played in array order
      selectedIdx: -1,             // which segment is selected (for handle visibility)
      playhead: 0,                 // current playback position in OUTPUT time (s)
      rendering: false,
      lastTimelineResult: null,    // last timeline_render response
    },
    _tlDrag: null,                 // {kind: 'left'|'right', segIdx, startX, origStart, origEnd, scale}
    _tlOnMove: null,               // bound mousemove handler (so we can remove it)
    _tlOnUp: null,                 // bound mouseup handler
    // --- Multi-clip mode (auto-order clips against a script) ---
    multiClipMode: false,
    multiClips: [],                // [{file, url, name, size}]
    multiScript: '',
    multiAutoEditing: false,
    multiResult: null,             // last response from /multi_auto_edit
    swapPrompt: '',
    swapModel: 'gpt-image',
    swapDefaultPrompt: '',          // effective default (project's if set, else global)
    swapGlobalDefaultPrompt: '',    // always the global pipeline.GENERATION_PROMPT
    swapProjectDefaultPrompt: '',   // current project's override, if any
    showCharLib: false,
    expandedCharId: null,
    charGalleries: {},
    charLibFilter: '',
    charLibLoadingId: null,
    charLibDragOver: false,
    libPanelDragOver: false,
    imageHistory: [],
    videoHistory: [],
    avatarHistory: [],
    audioHistory: [],
    // Persistent sidebar "Recent media" expand/collapse state.
    recentMediaOpen: true,

    // Per-tab history filters: free-text prompt search + optional model
    // filter. Frontend-only — operates on the already-loaded history
    // arrays via computed getters (filteredImageHistory etc.).
    historyFilters: {
      image: { q: '', model: '' },
      video: { q: '', model: '' },
      audio: { q: '', model: '' },
      avatar: { q: '', model: '' },
      broll: { q: '', status: '' },
    },
    heygenAvatars: [],
    heygenVoices: [],
    heygenCatalogueError: '',
    elevenlabsVoices: [],
    elevenlabsCatalogueError: '',
    photoAvatarModal: {
      open: false,
      variantUrl: '',
      variantName: '',     // for the toast/title
      voiceId: '',
      script: '',
      submitting: false,
    },
    _genPollTimer: null,
    generating: false,
    movementPrompt: '',
    editingVariant: null,    // {char_id, variant_id}
    editPrompt: '',
    editingTitle: false,
    draftTitle: '',
    editingCharacterId: null,
    draftCharacterName: '',
    // Which character's "pick which gallery image to use as source" popover
    // is open in Step 2. null = closed. Only one open at a time.
    sourceImagePickerCharId: null,
    ws: null,
    wsConnected: false,
    _wsBackoff: 1000,
    _sidebarRefreshTimer: null,

    async init() {
      this._loadCollapsed();
      this.activeTab = localStorage.getItem('active_tab') || 'swap';
      this.showCharLib = localStorage.getItem('char_lib_open') === '1';
      await this.loadHealth();
      await this.loadLibrary();
      // Default: every character in the library is checked.
      this.selectedCharacters = this.library.map(c => c.char_id);
      await this.loadProjects();
      await this.loadJobsList();
      await this.loadDailyCost();
      this.loadDisk();
      await this.loadGenModels();
      // Eagerly load ElevenLabs voices + editor templates once at boot so
      // the pickers are ready without waiting for a tab-switch.
      if (this.elevenlabsAvailable()) this.loadElevenlabsVoices();
      this.loadEditorTemplates();
      await this.loadGenerations();
      await this.loadSwapDefaults();
      // Restore last-used picks per-tab so the model/voice/aspect we used
      // before is still selected next session. Falls back to existing
      // defaults if no saved value exists.
      this._restorePerTabPrefs();
      // Then wire watches that save the picks back as the user changes them.
      this.$watch('imageGen.model', v => v && localStorage.setItem('imageGen.model', v));
      this.$watch('imageGen.aspect', v => v && localStorage.setItem('imageGen.aspect', v));
      this.$watch('videoGen.model', v => v && localStorage.setItem('videoGen.model', v));
      this.$watch('videoGen.aspect', v => v && localStorage.setItem('videoGen.aspect', v));
      this.$watch('videoGen.duration', v => v && localStorage.setItem('videoGen.duration', String(v)));
      this.$watch('audioGen.model', v => v && localStorage.setItem('audioGen.model', v));
      this.$watch('audioGen.voiceId', v => v && localStorage.setItem('audioGen.voiceId', v));
      this.$watch('avatarGen.model', v => v && localStorage.setItem('avatarGen.model', v));
      this.$watch('avatarGen.voiceId', v => v && localStorage.setItem('avatarGen.voiceId', v));
      this.$watch('avatarGen.voiceProvider', v => v && localStorage.setItem('avatarGen.voiceProvider', v));
      this.$watch('avatarGen.avatarId', v => v && localStorage.setItem('avatarGen.avatarId', v));
      this.$watch('brollGen.videoModel', v => v && localStorage.setItem('brollGen.videoModel', v));
      this.$watch('brollGen.aspectRatio', v => v && localStorage.setItem('brollGen.aspectRatio', v));
      // Refresh daily cost every minute while the tab is open.
      this._dailyCostTimer = setInterval(() => this.loadDailyCost(), 60000);
      // 1-second tick so the elapsed-time labels in the status toast +
      // B-roll progress card update without an extra backend round-trip.
      this._tickTimer = setInterval(() => { this._tickNow = Date.now(); }, 1000);
      // Reload swap defaults whenever the active project changes — picks up
      // the project's `default_prompt` (or falls back to global).
      this.$watch('currentProjectId', () => this.loadSwapDefaults());
      // Poll in-flight generations every 4s
      this._genPollTimer = setInterval(() => this.pollActiveGens(), 4000);
      // URL routing: if we landed on /j/<id>, open that job.
      this._openFromUrl();
      window.addEventListener('popstate', () => this._openFromUrl());
    },

    // --- tabs ---------------------------------------------------------------

    switchTab(slug) {
      this.activeTab = slug;
      localStorage.setItem('active_tab', slug);
    },

    // --- generations: models + history --------------------------------------

    // --- right-side character library --------------------------------------

    toggleCharLib() {
      this.showCharLib = !this.showCharLib;
      localStorage.setItem('char_lib_open', this.showCharLib ? '1' : '0');
    },

    filteredLibrary() {
      const q = (this.charLibFilter || '').trim().toLowerCase();
      if (!q) return this.library;
      return this.library.filter(c => (c.name || '').toLowerCase().includes(q));
    },

    async toggleCharGallery(charId) {
      if (this.expandedCharId === charId) {
        this.expandedCharId = null;
        return;
      }
      this.expandedCharId = charId;
      if (!this.charGalleries[charId]) await this.loadCharGallery(charId);
    },

    async loadCharGallery(charId) {
      this.charLibLoadingId = charId;
      try {
        const r = await fetch('/api/characters/' + charId + '/gallery');
        if (!r.ok) return;
        const data = await r.json();
        this.charGalleries = { ...this.charGalleries, [charId]: data.appearances || [] };
      } finally {
        this.charLibLoadingId = null;
      }
    },

    _invalidateGalleryFor(charId) {
      if (this.charGalleries[charId]) {
        const next = { ...this.charGalleries };
        delete next[charId];
        this.charGalleries = next;
        if (this.expandedCharId === charId) this.loadCharGallery(charId);
      }
    },

    onCharDragStart(ev, char, img) {
      try {
        ev.dataTransfer.effectAllowed = 'copy';
        ev.dataTransfer.setData('text/x-charswap-char-id', char.char_id);
        // Also set text/plain as a fallback so devtools / drop targets that
        // only inspect text/plain see something sensible.
        ev.dataTransfer.setData('text/plain', char.name || char.char_id);
      } catch (_) {}
    },

    onLibraryDragOver(ev) {
      if (this.activeTab !== 'swap') return;
      // Only highlight when our internal payload is present (avoids
      // hijacking unrelated drags like an OS-level file drag onto the page).
      if (ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes('text/x-charswap-char-id')) {
        ev.preventDefault();
        this.charLibDragOver = true;
      }
    },

    onLibraryDragLeave() {
      this.charLibDragOver = false;
    },

    onLibraryDrop(ev) {
      this.charLibDragOver = false;
      const cid = ev.dataTransfer?.getData('text/x-charswap-char-id');
      if (!cid) return;
      if (!this.library.find(c => c.char_id === cid)) return;
      if (!this.selectedCharacters.includes(cid)) {
        this.selectedCharacters = [...this.selectedCharacters, cid];
        const ch = this.library.find(c => c.char_id === cid);
        this.notifyInfo(`Added ${ch?.name || cid} to job`);
      }
    },

    // Drop files from Finder/Desktop onto the right-side library panel
    // to upload them as new characters in one go.
    onLibPanelDragOver(ev) {
      const types = Array.from(ev.dataTransfer?.types || []);
      if (types.includes('Files')) this.libPanelDragOver = true;
    },

    onLibPanelDragLeave(ev) {
      // Only clear when leaving the panel itself, not when entering a child.
      if (ev.currentTarget && !ev.currentTarget.contains(ev.relatedTarget)) {
        this.libPanelDragOver = false;
      }
    },

    onLibPanelDrop(ev) {
      this.libPanelDragOver = false;
      const files = Array.from(ev.dataTransfer?.files || [])
        .filter(f => f.type.startsWith('image/'));
      if (!files.length) return;
      // Open the upload modal pre-filled with the dropped files so the user
      // can pick whether they belong to an existing character or a new one.
      this.openUploadModal({ files });
    },

    async loadSwapDefaults() {
      // Pass the active project_id so the backend can return that
      // project's `default_prompt` instead of the global one.
      try {
        const url = '/api/swap/defaults' + (this.currentProjectId
          ? '?project_id=' + encodeURIComponent(this.currentProjectId) : '');
        const r = await fetch(url);
        if (!r.ok) return;
        const data = await r.json();
        this.swapDefaultPrompt = data.prompt || '';
        this.swapGlobalDefaultPrompt = data.global_prompt || data.prompt || '';
        this.swapProjectDefaultPrompt = data.project_prompt || '';
        if (!this.swapPrompt) this.swapPrompt = this.swapDefaultPrompt;
      } catch (_) {}
    },

    resetSwapPrompt() {
      this.swapPrompt = this.swapDefaultPrompt;
    },

    resetSwapPromptToGlobal() {
      this.swapPrompt = this.swapGlobalDefaultPrompt;
    },

    // Save the current textarea contents as the active project's default
    // prompt. Future jobs in this project will inherit it.
    async saveProjectDefaultPrompt() {
      if (!this.currentProjectId) return;
      const newPrompt = (this.swapPrompt || '').trim();
      try {
        const r = await fetch('/api/projects/' + encodeURIComponent(this.currentProjectId), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_prompt: newPrompt || null }),
        });
        if (!r.ok) { this.notifyError('Save failed: ' + await r.text()); return; }
        const updated = await r.json();
        // Refresh in-memory project list so currentProject() reflects it.
        const i = this.projects.findIndex(p => p.project_id === updated.project_id);
        if (i >= 0) this.projects.splice(i, 1, updated);
        this.swapProjectDefaultPrompt = updated.default_prompt || '';
        this.swapDefaultPrompt = this.swapProjectDefaultPrompt || this.swapGlobalDefaultPrompt;
        this.notifyInfo(newPrompt
          ? 'Saved as project default — new jobs in this project inherit it'
          : 'Cleared project default — new jobs fall back to the global default');
      } catch (e) {
        this.notifyError('Save failed: ' + e.message);
      }
    },

    swapPromptIsDefault() {
      return (this.swapPrompt || '').trim() === (this.swapDefaultPrompt || '').trim();
    },

    async loadGenModels() {
      try {
        const r = await fetch('/api/generations/models');
        if (r.ok) this.models = await r.json();
        // Make sure the default selection actually maps to an available model.
        const firstImage = (this.models.image || []).find(m => m.available);
        if (firstImage && !this.models.image.find(m => m.slug === this.imageGen.model)?.available) {
          this.imageGen.model = firstImage.slug;
        }
        const firstVideo = (this.models.video || []).find(m => m.available);
        if (firstVideo && !this.models.video.find(m => m.slug === this.videoGen.model)?.available) {
          this.videoGen.model = firstVideo.slug;
        }
      } catch (_) {}
    },

    // Step 3 variant gallery: group a character's variants by scene so the
    // UI can render "Scene 1: [v1, v2, v3]  Scene 2: [v4, v5, v6]" instead
    // of a flat strip. Returns [{scene, variants}].
    //   - Multi-scene jobs: one group per scene in this.job.scenes (in
    //     order). Edits without scene_id (or matching the parent's
    //     scene_id) join their parent's group.
    //   - Single-scene jobs: one group containing all variants.
    //   - Loose variants without scene_id (legacy data) → trailing group
    //     so they're still visible.
    variantGroupsFor(jc) {
      const scenes = (this.job?.scenes || []);
      const images = jc?.images || [];
      if (scenes.length <= 1) {
        return [{ scene: scenes[0] || null, variants: images }];
      }
      const groups = scenes.map(s => ({
        scene: s,
        variants: images.filter(v => v.scene_id === s.scene_id),
      }));
      const orphans = images.filter(v => !v.scene_id || !scenes.find(s => s.scene_id === v.scene_id));
      if (orphans.length) groups.push({ scene: null, variants: orphans });
      return groups;
    },

    currentImageModel() {
      return (this.models.image || []).find(m => m.slug === this.imageGen.model);
    },

    currentVideoModel() {
      return (this.models.video || []).find(m => m.slug === this.videoGen.model);
    },

    async loadGenerations() {
      try {
        const [iR, vR, aR, auR] = await Promise.all([
          fetch('/api/generations?kind=image'),
          fetch('/api/generations?kind=video'),
          fetch('/api/generations?kind=avatar'),
          fetch('/api/generations?kind=audio'),
        ]);
        if (iR.ok) this.imageHistory = await iR.json();
        if (vR.ok) this.videoHistory = await vR.json();
        if (aR.ok) this.avatarHistory = await aR.json();
        if (auR.ok) this.audioHistory = await auR.json();
      } catch (_) {}
    },

    // Build a human-readable download filename from a generation object.
    // E.g. "swollen-ankles-2026-05-14.mp4" instead of "g_a1b2c3d4e5.mp4".
    friendlyName(g, extOverride) {
      const ext = extOverride
        || (g.output_url && /\.mp4($|\?)/i.test(g.output_url) ? 'mp4'
            : g.kind === 'image' ? 'png'
            : g.kind === 'audio' ? 'mp3'
            : 'mp4');
      const date = new Date((g.completed_at || g.created_at || Date.now()))
        .toISOString().slice(0, 10);
      const slug = ((g.prompt || g.model || 'gen') + '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 60);
      return `${slug || g.gen_id}-${date}.${ext}`;
    },

    _historyForKind(kind) {
      return kind === 'image' ? this.imageHistory
           : kind === 'video' ? this.videoHistory
           : kind === 'avatar' ? this.avatarHistory
           : this.audioHistory;
    },

    _filterHistory(kind, arr) {
      const f = this.historyFilters[kind];
      if (!f) return arr;
      const q = (f.q || '').trim().toLowerCase();
      const model = f.model || '';
      if (!q && !model) return arr;
      return arr.filter(g => {
        if (model && g.model !== model) return false;
        if (q) {
          const hay = ((g.prompt || '') + ' ' + (g.model || '')).toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
    },

    // Convenience getters used by the templates.
    get filteredImageHistory() { return this._filterHistory('image', this.imageHistory); },
    get filteredVideoHistory() { return this._filterHistory('video', this.videoHistory); },
    get filteredAudioHistory() { return this._filterHistory('audio', this.audioHistory); },
    get filteredAvatarHistory() { return this._filterHistory('avatar', this.avatarHistory); },
    get filteredBrollHistory() {
      const f = this.historyFilters.broll;
      const q = (f.q || '').trim().toLowerCase();
      const st = f.status || '';
      if (!q && !st) return this.brollHistory;
      return this.brollHistory.filter(b => {
        if (st && b.status !== st) return false;
        if (q) {
          const hay = ((b.transcript || '') + ' ' + (b.broll_id || '')).toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
    },

    // Models that have actually been used in this history kind, for the
    // dropdown filter so we don't show locked/unused models.
    _modelsInHistory(arr) {
      const seen = new Set();
      for (const g of arr) if (g.model) seen.add(g.model);
      return [...seen].sort();
    },

    // Internal tick so any elapsed-time UI re-renders without polling
    // the backend on every second. Bumped once per second by a
    // setInterval kicked off in init.
    _tickNow: Date.now(),

    // ISO timestamp diff to "Xm Ys" or "Xs" string.
    formatElapsed(iso) {
      if (!iso) return '';
      const start = Date.parse(iso);
      if (isNaN(start)) return '';
      const sec = Math.max(0, Math.floor((this._tickNow - start) / 1000));
      if (sec < 60) return `${sec}s`;
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return `${m}m ${s}s`;
    },

    // B-roll: aggregate progress for the current run — n_done, n_failed,
    // n_total, percent_done, and an ETA estimate based on per-clip
    // average runtime so far.
    brollProgress(b) {
      const clips = b.clips || [];
      const n = clips.length;
      if (!n) return null;
      const done = clips.filter(c => c.status === 'done').length;
      const failed = clips.filter(c => c.status === 'failed').length;
      const inFlight = clips.filter(c => ['image_running','image_done','video_running'].includes(c.status)).length;
      const percent = Math.round((done / n) * 100);
      // ETA: assume remaining clips take same wall-time as completed ones.
      let eta = '';
      if (b.created_at && done > 0 && done < n) {
        const elapsedMs = this._tickNow - Date.parse(b.created_at);
        const perClipMs = elapsedMs / done;
        const remainingClips = n - done - failed;
        const etaMs = perClipMs * remainingClips;
        if (etaMs > 0 && etaMs < 1000 * 60 * 60) {
          const etaMin = Math.floor(etaMs / 60000);
          const etaSec = Math.round((etaMs % 60000) / 1000);
          eta = etaMin > 0 ? `~${etaMin}m ${etaSec}s` : `~${etaSec}s`;
        }
      }
      return { n, done, failed, inFlight, percent, eta };
    },

    // Aggregate every in-flight job across tabs into one list for the
    // persistent status toast at the bottom-right. Each entry has:
    //   {kind, label, status, tab, navigate(): switch to its tab}
    get activeJobs() {
      const out = [];
      const transient = ['pending', 'running'];
      for (const kind of ['image', 'video', 'audio', 'avatar']) {
        const arr = this._historyForKind(kind);
        for (const g of arr) {
          if (!transient.includes(g.status)) continue;
          out.push({
            id: g.gen_id, kind, tab: kind,
            label: (g.prompt || g.model || g.gen_id).slice(0, 50),
            status: g.status, model: g.model,
            created_at: g.created_at,
          });
        }
      }
      // B-roll has a richer state machine. Anything not in the resting
      // states is "in flight" for toast purposes.
      const brollResting = ['done', 'failed', 'partial_success'];
      for (const b of this.brollHistory) {
        const stillRolling = !brollResting.includes(b.status)
          || (b.clips || []).some(c => ['pending', 'image_running', 'image_done', 'video_running'].includes(c.status));
        if (!stillRolling) continue;
        const nDone = (b.clips || []).filter(c => c.status === 'done').length;
        const nTotal = (b.clips || []).length;
        out.push({
          id: b.broll_id, kind: 'broll', tab: 'broll',
          label: (b.transcript || b.broll_id).slice(0, 50),
          status: b.status,
          progress: nTotal ? `${nDone}/${nTotal} clips` : '',
          created_at: b.created_at,
        });
      }
      // Swap-flow video animations are not currently aggregated here
      // (they live in the project sidebar and have their own progress).
      // Easy to add later if needed.
      return out.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    },

    // Cross-kind list of recent finished media for the sidebar thumbnail
    // strip. Each entry has {kind, id, tab, thumb, label, created_at}.
    // Includes Image, Video, Audio, Avatar, and B-roll final outputs.
    // Sorted newest-first, capped to 50 for performance.
    get recentMedia() {
      const out = [];
      for (const g of this.imageHistory) {
        if (g.output_url) out.push({
          kind: 'image', id: g.gen_id, tab: 'image',
          thumb: g.output_url, label: g.prompt || g.model,
          created_at: g.completed_at || g.created_at,
        });
      }
      for (const g of this.videoHistory) {
        if (g.output_url) out.push({
          kind: 'video', id: g.gen_id, tab: 'video',
          thumb: g.output_url, label: g.prompt || g.model,
          created_at: g.completed_at || g.created_at,
        });
      }
      for (const g of this.audioHistory) {
        if (g.output_url) out.push({
          kind: 'audio', id: g.gen_id, tab: 'audio',
          // Audio outputs that came from video VC are mp4; use them as thumb.
          // Pure-audio (mp3) has no thumb — fall back to null.
          thumb: /\.mp4($|\?)/i.test(g.output_url) ? g.output_url : null,
          label: g.prompt || g.model,
          created_at: g.completed_at || g.created_at,
        });
      }
      for (const g of this.avatarHistory) {
        if (g.output_url) out.push({
          kind: 'avatar', id: g.gen_id, tab: 'avatar',
          thumb: g.output_url, label: g.prompt || g.model,
          created_at: g.completed_at || g.created_at,
        });
      }
      for (const b of this.brollHistory) {
        if (b.final_video_url) out.push({
          kind: 'broll', id: b.broll_id, tab: 'broll',
          thumb: b.final_video_url,
          label: (b.transcript || b.broll_id).slice(0, 40),
          created_at: b.completed_at || b.created_at,
        });
      }
      out.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
      return out.slice(0, 50);
    },

    jumpToActiveJob(job) {
      if (job.tab) this.switchTab(job.tab);
      // Tiny delay so the tab renders before we scroll
      this.$nextTick(() => {
        // Try to scroll to the specific card. Each card uses gen_id or broll_id
        // as part of its key.
        const sel = `[x-key="${job.id}"], [x-key="br-${job.id}"]`;
        const el = document.querySelector(sel);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    },

    async pollActiveGens() {
      const active = [...this.imageHistory, ...this.videoHistory, ...this.avatarHistory, ...this.audioHistory]
        .filter(g => ['pending', 'running'].includes(g.status));
      if (active.length === 0) return;
      try {
        const results = await Promise.all(active.map(g =>
          fetch('/api/generations/' + g.gen_id).then(r => r.ok ? r.json() : null)
        ));
        for (const updated of results) {
          if (!updated) continue;
          const target = this._historyForKind(updated.kind);
          const idx = target.findIndex(g => g.gen_id === updated.gen_id);
          if (idx !== -1) target[idx] = updated;
        }
        if (results.some(r => r && ['done', 'failed'].includes(r.status))) {
          this.loadDailyCost();
        }
      } catch (_) {}
    },

    // --- avatar generation (HeyGen) -----------------------------------------

    currentAvatarModel() {
      return (this.models.avatar || []).find(m => m.slug === this.avatarGen.model);
    },

    async loadElevenlabsVoices() {
      this.elevenlabsCatalogueError = '';
      try {
        const r = await fetch('/api/elevenlabs/voices');
        if (r.ok) this.elevenlabsVoices = await r.json();
        else this.elevenlabsCatalogueError = await r.text();
      } catch (e) {
        this.elevenlabsCatalogueError = String(e);
      }
      // Restore last-used voice now that the list is loaded.
      try {
        const saved = localStorage.getItem('audioGen.voiceId');
        if (saved && this.elevenlabsVoices.some(v => v.voice_id === saved)) {
          this.audioGen.voiceId = saved;
        }
        const savedAv = localStorage.getItem('avatarGen.voiceId');
        if (savedAv && this.avatarGen.voiceProvider === 'elevenlabs'
            && this.elevenlabsVoices.some(v => v.voice_id === savedAv)) {
          this.avatarGen.voiceId = savedAv;
        }
      } catch (_) {}
    },

    elevenlabsAvailable() {
      const m = (this.models.audio || []).find(x => x.slug === 'elevenlabs-vc');
      return !!m?.available;
    },

    // --- Audio tab (ElevenLabs Voice Changer) -------------------------------

    currentAudioModel() {
      return (this.models.audio || []).find(m => m.slug === this.audioGen.model);
    },

    setAudioSource(file) {
      if (!file) return;
      // Voice Changer also accepts video — the server extracts the audio,
      // swaps the voice, then re-muxes it back into the original video.
      const isVideo = (file.type || '').startsWith('video/')
        || /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(file.name || '');
      if (this.audioGen.sourceAudio?.url) URL.revokeObjectURL(this.audioGen.sourceAudio.url);
      this.audioGen.sourceAudio = {
        file, url: URL.createObjectURL(file), name: file.name, isVideo,
      };
    },

    async submitAudioGen() {
      const m = this.currentAudioModel();
      if (!m?.available) { this.notifyError('ElevenLabs not configured'); return; }
      if (!this.audioGen.voiceId) { this.notifyError('Pick a target voice'); return; }
      if (this.audioGen.model === 'elevenlabs-vc' && !this.audioGen.sourceAudio) {
        this.notifyError('Upload a source audio file');
        return;
      }
      if (this.audioGen.model === 'elevenlabs-tts' && !this.audioGen.script.trim()) {
        this.notifyError('Write some text to speak');
        return;
      }
      this.audioGen.generating = true;
      try {
        const fd = new FormData();
        fd.append('kind', 'audio');
        fd.append('model', this.audioGen.model);
        fd.append('voice_id', this.audioGen.voiceId);
        if (this.audioGen.model === 'elevenlabs-vc') {
          fd.append('prompt', '(voice changer)');  // server requires non-empty prompt
          fd.append('files', this.audioGen.sourceAudio.file);
        } else {
          fd.append('prompt', this.audioGen.script.trim());
        }
        const r = await fetch('/api/generations', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Generate failed: ' + await r.text()); return; }
        const gen = await r.json();
        this.audioHistory = [gen, ...this.audioHistory];
        // Preserve sourceAudio + script so users can iterate (tweak script
        // for TTS, or A/B different voices for VC). Use clearAudioForm()
        // to start fresh.
      } finally {
        this.audioGen.generating = false;
      }
    },

    clearAudioForm() {
      if (this.audioGen.sourceAudio?.url) URL.revokeObjectURL(this.audioGen.sourceAudio.url);
      this.audioGen.sourceAudio = null;
      this.audioGen.script = '';
    },

    // --- B-roll generator (audio → planned cinematic clips → final mp4) ----

    setBrollSource(file) {
      if (!file) return;
      const isVideo = (file.type || '').startsWith('video/')
        || /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(file.name || '');
      if (this.brollGen.source?.url) URL.revokeObjectURL(this.brollGen.source.url);
      this.brollGen.source = {
        file, url: URL.createObjectURL(file), name: file.name, isVideo,
      };
    },

    async submitBroll() {
      if (!this.brollGen.source) { this.notifyError('Drop a narration file first'); return; }
      this.brollGen.submitting = true;
      try {
        const fd = new FormData();
        fd.append('file', this.brollGen.source.file);
        fd.append('video_model', this.brollGen.videoModel || 'grok-imagine');
        fd.append('aspect_ratio', this.brollGen.aspectRatio || '9:16');
        const r = await fetch('/api/broll/generate', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('B-roll submit failed: ' + await r.text()); return; }
        const job = await r.json();
        this.brollHistory = [job, ...this.brollHistory.filter(b => b.broll_id !== job.broll_id)];
        if (this.brollGen.source?.url) URL.revokeObjectURL(this.brollGen.source.url);
        this.brollGen.source = null;
        this.notifyInfo(`B-roll queued (${job.broll_id}) — polling for progress`);
        this._startBrollPolling();
      } finally {
        this.brollGen.submitting = false;
      }
    },

    async loadBrollHistory() {
      try {
        const r = await fetch('/api/broll');
        if (!r.ok) return;
        this.brollHistory = await r.json();
        if (this.brollHistory.some(b => this._brollIsActive(b))) {
          this._startBrollPolling();
        }
      } catch (_) {}
    },

    // A broll is "active" (i.e. we should keep polling it) if the job
    // status itself is mid-pipeline OR any of its clips are transient
    // (single-clip regeneration after awaiting_approval).
    _brollIsActive(b) {
      const restingStatuses = ['done', 'failed', 'partial_success', 'awaiting_approval'];
      if (!restingStatuses.includes(b.status)) return true;   // mid-pipeline
      return this._hasInFlightClips(b);                       // mid-regen
    },

    async refreshBroll(brollId) {
      try {
        const r = await fetch(`/api/broll/${encodeURIComponent(brollId)}`);
        if (!r.ok) return;
        const fresh = await r.json();
        const i = this.brollHistory.findIndex(b => b.broll_id === brollId);
        if (i >= 0) {
          // Preserve client-side transient flags across server refreshes so
          // the UI doesn't flicker spinner state during a poll.
          const prev = this.brollHistory[i];
          fresh._regenerating_idx = prev._regenerating_idx;
          fresh._finalizing = prev._finalizing;
          // Once the regenerated clip is back in flight, drop the marker.
          if (prev._regenerating_idx != null) {
            const c = (fresh.clips || [])[prev._regenerating_idx];
            if (c && !['done', 'failed'].includes(c.status)) {
              fresh._regenerating_idx = null;
            }
          }
          // Once finalize lands (status becomes done/partial_success), drop flag.
          if (prev._finalizing && ['done', 'partial_success', 'failed'].includes(fresh.status)) {
            fresh._finalizing = false;
          }
          this.brollHistory.splice(i, 1, fresh);
        } else {
          this.brollHistory = [fresh, ...this.brollHistory];
        }
      } catch (_) {}
    },

    async deleteBroll(brollId) {
      if (!confirm(`Delete B-roll ${brollId}? This removes the source, clips, and final video.`)) return;
      try {
        const r = await fetch(`/api/broll/${encodeURIComponent(brollId)}`, { method: 'DELETE' });
        if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
        this.brollHistory = this.brollHistory.filter(b => b.broll_id !== brollId);
      } catch (_) {}
    },

    async rejectClip(brollId, idx) {
      const b = this.brollHistory.find(x => x.broll_id === brollId);
      if (b) b._regenerating_idx = idx;
      try {
        const fd = new FormData();
        fd.append('idx', String(idx));
        const r = await fetch(`/api/broll/${encodeURIComponent(brollId)}/regenerate_clip`,
          { method: 'POST', body: fd });
        if (!r.ok) {
          this.notifyError('Regenerate failed: ' + await r.text());
          if (b) b._regenerating_idx = null;
          return;
        }
        this.notifyInfo(`Regenerating clip #${idx + 1}…`);
        // Polling will pick up the status change.
        this._startBrollPolling();
        // Clear the regenerating marker on the next refresh (when the
        // clip's status flips away from done/failed).
        setTimeout(() => { if (b) b._regenerating_idx = null; }, 8000);
      } catch (e) {
        this.notifyError('Regenerate failed: ' + e.message);
        if (b) b._regenerating_idx = null;
      }
    },

    async finalizeBroll(brollId) {
      const b = this.brollHistory.find(x => x.broll_id === brollId);
      if (b) b._finalizing = true;
      try {
        const r = await fetch(`/api/broll/${encodeURIComponent(brollId)}/finalize`,
          { method: 'POST' });
        if (!r.ok) {
          this.notifyError('Finalize failed: ' + await r.text());
          if (b) b._finalizing = false;
          return;
        }
        this.notifyInfo('Finalizing video — concatenating + muxing audio');
        this._startBrollPolling();
        setTimeout(() => { if (b) b._finalizing = false; }, 8000);
      } catch (e) {
        this.notifyError('Finalize failed: ' + e.message);
        if (b) b._finalizing = false;
      }
    },

    // Are any of this broll's clips still mid-generation? Used to gate the
    // Finalize button — can't concat while clips are in flight.
    _hasInFlightClips(b) {
      const transient = ['pending', 'image_running', 'image_done', 'video_running'];
      return (b.clips || []).some(c => transient.includes(c.status));
    },

    _startBrollPolling() {
      if (this._brollPollTimer) return;
      const tick = async () => {
        const active = this.brollHistory.filter(b => this._brollIsActive(b));
        if (active.length === 0) {
          clearInterval(this._brollPollTimer);
          this._brollPollTimer = null;
          return;
        }
        await Promise.all(active.map(b => this.refreshBroll(b.broll_id)));
      };
      this._brollPollTimer = setInterval(tick, 5000);
      tick();
    },

    // --- Video Editor (silence-trim + captions) -----------------------------

    _restorePerTabPrefs() {
      // Restore last-used picks from localStorage for every tab where it
      // matters. Only restore if the saved value still references something
      // that exists in the currently-loaded models/voices lists, so we
      // don't pick a locked or removed item.
      const get = k => { try { return localStorage.getItem(k); } catch (_) { return null; } };
      const inList = (val, list, key='slug') => val && (list || []).some(x => x[key] === val);

      const imageModels = (this.models?.image) || [];
      const videoModels = (this.models?.video) || [];
      const audioModels = (this.models?.audio) || [];

      const im = get('imageGen.model');
      if (inList(im, imageModels) && imageModels.find(m => m.slug === im)?.available) this.imageGen.model = im;
      const ia = get('imageGen.aspect'); if (ia) this.imageGen.aspect = ia;

      const vm = get('videoGen.model');
      if (inList(vm, videoModels) && videoModels.find(m => m.slug === vm)?.available) this.videoGen.model = vm;
      const va = get('videoGen.aspect'); if (va) this.videoGen.aspect = va;
      const vd = get('videoGen.duration'); if (vd) this.videoGen.duration = Number(vd) || 10;

      const am = get('audioGen.model');
      if (inList(am, audioModels) && audioModels.find(m => m.slug === am)?.available) this.audioGen.model = am;

      const avm = get('avatarGen.model');
      if (avm) this.avatarGen.model = avm;
      const avp = get('avatarGen.voiceProvider'); if (avp) this.avatarGen.voiceProvider = avp;

      const bm = get('brollGen.videoModel');
      if (inList(bm, videoModels) && videoModels.find(m => m.slug === bm)?.available) this.brollGen.videoModel = bm;
      const ba = get('brollGen.aspectRatio');
      if (ba && ['9:16','1:1','16:9'].includes(ba)) this.brollGen.aspectRatio = ba;

      // Voice IDs are restored once the voices are loaded — handled lazily
      // by `loadElevenlabsVoices` / `loadHeygenCatalogue` since those run
      // asynchronously and aren't necessarily ready at init time.
    },

    async loadEditorTemplates() {
      try {
        const r = await fetch('/api/editor/templates');
        if (r.ok) this.editorTemplates = await r.json();
        // Restore last-used template, or fall back to the first available
        // (or the existing `editor.template` default which is popout-yellow).
        const saved = localStorage.getItem('editor.template');
        if (saved && this.editorTemplates.some(t => t.slug === saved)) {
          this.editor.template = saved;
        } else if (this.editorTemplates.length && !this.editorTemplates.some(t => t.slug === this.editor.template)) {
          this.editor.template = this.editorTemplates[0].slug;
        }
      } catch (_) {}
    },

    setEditorVideo(file) {
      if (!file) return;
      if (this.editor.sourceVideo?.url) URL.revokeObjectURL(this.editor.sourceVideo.url);
      this.editor.sourceVideo = { file, url: URL.createObjectURL(file), name: file.name };
      this.editor.lastResult = null;
    },

    async editorTrimSilences() {
      if (!this.editor.sourceVideo) { this.notifyError('Upload a video first'); return; }
      this.editor.trimming = true;
      try {
        const fd = new FormData();
        fd.append('file', this.editor.sourceVideo.file);
        fd.append('threshold_db', this.editor.thresholdDb);
        fd.append('min_silence_secs', this.editor.minSilenceSecs);
        fd.append('pad_secs', this.editor.padSecs);
        const r = await fetch('/api/editor/trim_silences', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Trim failed: ' + await r.text()); return; }
        const data = await r.json();
        this.editor.lastResult = { ...data, kind: 'trim' };
        this.editorHistory = [{ ...data, kind: 'trim', ts: Date.now() }, ...this.editorHistory];
        this.notifyInfo(`Trimmed ${data.saved_secs}s (${data.n_cuts} segments kept)`);
      } finally {
        this.editor.trimming = false;
      }
    },

    _activeOverrides() {
      const o = this.editor.overrides;
      const out = {};
      for (const k of Object.keys(o)) {
        if (o[k] !== null && o[k] !== '') out[k] = o[k];
      }
      return out;
    },

    async editorAddCaptions() {
      if (!this.editor.sourceVideo) { this.notifyError('Upload a video first'); return; }
      this.editor.captioning = true;
      try {
        const fd = new FormData();
        fd.append('file', this.editor.sourceVideo.file);
        fd.append('template', this.editor.template);
        const overrides = this._activeOverrides();
        if (Object.keys(overrides).length) fd.append('overrides', JSON.stringify(overrides));
        const r = await fetch('/api/editor/captions', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Captions failed: ' + await r.text()); return; }
        const data = await r.json();
        this.editor.lastResult = { ...data, kind: 'captions' };
        this.editorHistory = [{ ...data, kind: 'captions', ts: Date.now() }, ...this.editorHistory];
        this.notifyInfo(`Captioned ${data.n_words} words with ${data.template}`);
      } finally {
        this.editor.captioning = false;
      }
    },

    _activeRerenderOverrides() {
      const o = this.editor.rerenderOverrides;
      const out = {};
      for (const k of Object.keys(o)) {
        if (o[k] !== null && o[k] !== '') out[k] = o[k];
      }
      return out;
    },

    openRerenderPanel() {
      // Seed the panel with whatever was used last time so the user starts
      // from where they were, not from scratch.
      this.editor.rerenderTemplate = this.editor.template;
      this.editor.rerenderOverrides = { ...this.editor.overrides };
      this.editor.rerenderTrimStart = 0;
      this.editor.rerenderTrimEnd = 0;
      this.editor.rerenderOpen = true;
    },

    async submitRerender() {
      const r0 = this.editor.lastResult;
      if (!r0?.edit_id) { this.notifyError('No auto-edit result to re-render'); return; }
      this.editor.rerendering = true;
      try {
        const fd = new FormData();
        fd.append('edit_id', r0.edit_id);
        fd.append('template', this.editor.rerenderTemplate);
        const overrides = this._activeRerenderOverrides();
        if (Object.keys(overrides).length) fd.append('overrides', JSON.stringify(overrides));
        if (this.editor.rerenderTrimStart > 0) fd.append('trim_start_secs', this.editor.rerenderTrimStart);
        if (this.editor.rerenderTrimEnd > 0) fd.append('trim_end_secs', this.editor.rerenderTrimEnd);
        const r = await fetch('/api/editor/rerender', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Rerender failed: ' + await r.text()); return; }
        const data = await r.json();
        // Replace the result view with the new render (keep the same edit_id
        // so further re-renders also work).
        this.editor.lastResult = {
          ...this.editor.lastResult,
          output_url: data.output_url,
          template: data.template,
          n_words: data.n_words,
          version: data.version,
        };
        this.editorHistory = [{
          edit_id: data.edit_id, kind: 'rerender', version: data.version,
          template: data.template, n_words: data.n_words,
          output_url: data.output_url, ts: Date.now(),
        }, ...this.editorHistory];
        this.notifyInfo(`Rerendered v${data.version} with ${data.template}`);
      } finally {
        this.editor.rerendering = false;
      }
    },

    // --- CapCut-style timeline: trim + split + delete + reorder ----------

    openTimeline() {
      // Open the timeline panel against the current lastResult video.
      const r = this.editor.lastResult;
      if (!r?.output_url) { this.notifyError('No rendered video to edit'); return; }
      const url = r.output_url;
      const filename = url.split('/').pop();
      // Reset segments only if we're switching files.
      const isNewFile = this.timeline.sourceFilename !== filename;
      this.timeline.sourceUrl = url;
      this.timeline.sourceFilename = filename;
      this.timeline.open = true;
      this.timeline.selectedIdx = -1;
      this.timeline.playhead = 0;
      if (isNewFile) this.timeline.segments = [];  // wait for @loadedmetadata
    },

    onTimelineMeta(event) {
      const v = event.target;
      const dur = v.duration || 0;
      this.timeline.sourceDuration = dur;
      if (!this.timeline.segments.length && dur > 0) {
        this.timeline.segments = [{ start: 0, end: dur }];
      }
    },

    onTimelineTime(event) {
      if (!this.timeline.open) return;
      this.timeline.playhead = this._sourceToOutputTime(event.target.currentTime);
    },

    closeTimeline() {
      this.timeline.open = false;
      const v = this.$refs.timelineVideo;
      if (v) { v.pause(); v.src = ''; }
    },

    timelineOutputDuration() {
      return this.timeline.segments.reduce((s, seg) => s + Math.max(0, seg.end - seg.start), 0);
    },

    // Map a source-time `t` to the corresponding position on the output
    // timeline (segments concatenated in order). Returns 0 if `t` isn't
    // within any segment.
    _sourceToOutputTime(t) {
      let acc = 0;
      for (const seg of this.timeline.segments) {
        if (t >= seg.start && t <= seg.end) return acc + (t - seg.start);
        acc += Math.max(0, seg.end - seg.start);
      }
      return acc; // past end
    },

    // Inverse: output-time → {segIdx, srcTime}. Used for split-at-playhead.
    _outputToSource(t) {
      let acc = 0;
      for (let i = 0; i < this.timeline.segments.length; i++) {
        const seg = this.timeline.segments[i];
        const len = Math.max(0, seg.end - seg.start);
        if (t <= acc + len) return { segIdx: i, srcTime: seg.start + (t - acc) };
        acc += len;
      }
      const last = this.timeline.segments.length - 1;
      return { segIdx: last, srcTime: this.timeline.segments[last]?.end || 0 };
    },

    splitAtPlayhead() {
      const t = this.timeline.playhead;
      const total = this.timelineOutputDuration();
      if (t <= 0.05 || t >= total - 0.05) {
        this.notifyError('Move the playhead inside a segment first');
        return;
      }
      const { segIdx, srcTime } = this._outputToSource(t);
      const seg = this.timeline.segments[segIdx];
      if (!seg) return;
      if (srcTime - seg.start < 0.1 || seg.end - srcTime < 0.1) {
        this.notifyError('Too close to an existing edge — move the playhead');
        return;
      }
      const left = { start: seg.start, end: srcTime };
      const right = { start: srcTime, end: seg.end };
      this.timeline.segments.splice(segIdx, 1, left, right);
      this.timeline.selectedIdx = segIdx;
    },

    deleteSegment(idx) {
      if (this.timeline.segments.length <= 1) {
        this.notifyError("Can't delete the only segment — split first");
        return;
      }
      this.timeline.segments.splice(idx, 1);
      if (this.timeline.selectedIdx === idx) this.timeline.selectedIdx = -1;
    },

    moveSegment(idx, delta) {
      const next = idx + delta;
      if (next < 0 || next >= this.timeline.segments.length) return;
      const arr = this.timeline.segments;
      [arr[idx], arr[next]] = [arr[next], arr[idx]];
      this.timeline.selectedIdx = next;
    },

    resetTimeline() {
      this.timeline.segments = [{ start: 0, end: this.timeline.sourceDuration }];
      this.timeline.selectedIdx = -1;
    },

    timelineSegmentWidthPct(seg) {
      const total = this.timelineOutputDuration();
      if (total <= 0) return 0;
      return ((seg.end - seg.start) / total) * 100;
    },

    timelinePlayheadPct() {
      const total = this.timelineOutputDuration();
      if (total <= 0) return 0;
      return Math.min(100, Math.max(0, (this.timeline.playhead / total) * 100));
    },

    // Start dragging a left/right trim handle on segment[idx].
    startHandleDrag(event, idx, side) {
      event.preventDefault();
      event.stopPropagation();
      const seg = this.timeline.segments[idx];
      if (!seg) return;
      const trackEl = this.$refs.timelineTrack;
      if (!trackEl) return;
      const trackWidthPx = trackEl.getBoundingClientRect().width;
      const total = this.timelineOutputDuration();
      const scale = trackWidthPx / Math.max(0.001, total);  // px per output-second; frozen during drag
      this._tlDrag = {
        kind: side,                 // 'left' or 'right'
        segIdx: idx,
        startX: event.clientX,
        origStart: seg.start,
        origEnd: seg.end,
        scale,
      };
      // Bind handlers so we can remove them later. Plain method refs lose
      // `this` when called by the window's event loop.
      this._tlOnMove = (ev) => {
        const d = this._tlDrag;
        if (!d) return;
        const dxPx = ev.clientX - d.startX;
        const dt = dxPx / d.scale;
        const s = this.timeline.segments[d.segIdx];
        if (!s) return;
        const srcDur = this.timeline.sourceDuration;
        if (d.kind === 'left') {
          s.start = Math.max(0, Math.min(d.origEnd - 0.1, d.origStart + dt));
        } else {
          s.end = Math.max(d.origStart + 0.1, Math.min(srcDur, d.origEnd + dt));
        }
      };
      this._tlOnUp = () => {
        this._tlDrag = null;
        window.removeEventListener('mousemove', this._tlOnMove);
        window.removeEventListener('mouseup', this._tlOnUp);
        this._tlOnMove = null;
        this._tlOnUp = null;
      };
      window.addEventListener('mousemove', this._tlOnMove);
      window.addEventListener('mouseup', this._tlOnUp);
    },

    // Click anywhere on the track to move the playhead (in output time).
    seekTimeline(event) {
      const trackEl = this.$refs.timelineTrack;
      if (!trackEl) return;
      const r = trackEl.getBoundingClientRect();
      const xPx = event.clientX - r.left;
      const ratio = Math.min(1, Math.max(0, xPx / r.width));
      const total = this.timelineOutputDuration();
      const outT = ratio * total;
      this.timeline.playhead = outT;
      // Convert back to source-time of the segment under outT so the video
      // element jumps to the right frame.
      const { srcTime } = this._outputToSource(outT);
      const v = this.$refs.timelineVideo;
      if (v) v.currentTime = srcTime;
    },

    formatTC(secs) {
      if (!isFinite(secs) || secs < 0) return '0:00';
      const m = Math.floor(secs / 60);
      const s = secs - m * 60;
      return `${m}:${s.toFixed(1).padStart(4, '0')}`;
    },

    async submitTimeline() {
      const r0 = this.editor.lastResult;
      if (!r0?.edit_id) { this.notifyError('No edit_id on current result'); return; }
      const segs = this.timeline.segments.filter(s => s.end - s.start > 0.05);
      if (!segs.length) { this.notifyError('No valid segments to render'); return; }
      this.timeline.rendering = true;
      try {
        const fd = new FormData();
        fd.append('edit_id', r0.edit_id);
        fd.append('segments_json', JSON.stringify(
          segs.map(s => ({ start: s.start, end: s.end }))
        ));
        fd.append('source_filename', this.timeline.sourceFilename);
        const r = await fetch('/api/editor/timeline_render', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Timeline render failed: ' + await r.text()); return; }
        const data = await r.json();
        this.timeline.lastTimelineResult = data;
        // Swap lastResult so the user sees the new clip in the player. Keep
        // edit_id so they can keep iterating.
        this.editor.lastResult = {
          ...this.editor.lastResult,
          output_url: data.output_url,
          version: data.version,
          n_words: this.editor.lastResult?.n_words,
        };
        this.editorHistory = [{
          edit_id: data.edit_id, kind: 'timeline', version: data.version,
          n_segments: data.n_segments, duration: data.duration,
          output_url: data.output_url, ts: Date.now(),
        }, ...this.editorHistory];
        this.notifyInfo(`Timeline v${data.version}: ${data.n_segments} segments, ${data.duration}s`);
        // Re-anchor the timeline to the new render so the user can iterate
        // again on the result of this round.
        this.openTimeline();
      } finally {
        this.timeline.rendering = false;
      }
    },

    // --- Multi-clip flow: many clips + script → auto-order by transcript ---

    addMultiClips(fileList) {
      const files = Array.from(fileList || []).filter(f => f.type.startsWith('video/'));
      for (const f of files) {
        this.multiClips.push({
          file: f, url: URL.createObjectURL(f),
          name: f.name, size: f.size,
        });
      }
    },

    removeMultiClip(idx) {
      const c = this.multiClips[idx];
      if (c?.url) URL.revokeObjectURL(c.url);
      this.multiClips.splice(idx, 1);
    },

    formatMB(b) {
      return (b / 1024 / 1024).toFixed(1) + ' MB';
    },

    async submitMultiAutoEdit() {
      if (this.multiClips.length < 1) { this.notifyError('Add at least one clip'); return; }
      if (!this.multiScript.trim()) { this.notifyError('Paste or write a script first'); return; }
      if (!this.health.openai_key) { this.notifyError('Whisper needs OPENAI_API_KEY'); return; }
      this.multiAutoEditing = true;
      try {
        const fd = new FormData();
        for (const c of this.multiClips) fd.append('files', c.file);
        fd.append('script', this.multiScript);
        fd.append('threshold_db', this.editor.thresholdDb);
        fd.append('min_silence_secs', this.editor.minSilenceSecs);
        fd.append('pad_secs', this.editor.padSecs);
        fd.append('template', this.editor.template);
        if (this.editor.voiceId) fd.append('voice_id', this.editor.voiceId);
        fd.append('enable_trim', this.editor.enableTrim ? 'true' : 'false');
        fd.append('enable_captions', this.editor.enableCaptions ? 'true' : 'false');
        fd.append('enable_wpm_normalize', this.editor.enableNormalizeWpm ? 'true' : 'false');
        fd.append('target_wpm', String(this.editor.targetWpm || 190));
        const overrides = this._activeOverrides();
        if (Object.keys(overrides).length) fd.append('overrides', JSON.stringify(overrides));
        const r = await fetch('/api/editor/multi_auto_edit', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Multi auto-edit failed: ' + await r.text()); return; }
        const data = await r.json();
        this.multiResult = data;
        // Slot the final result into the same lastResult slot so the existing
        // rerender flow works on it.
        this.editor.lastResult = {
          ...data, kind: 'auto',
          template: data.captions?.template, n_words: data.captions?.n_words,
        };
        const unmatched = (data.matching || []).filter(m => m.unmatched).length;
        this.notifyInfo(`Stitched ${data.n_clips} clips${unmatched ? ` (${unmatched} unmatched)` : ''} · ${data.captions?.n_words} words captioned`);
      } finally {
        this.multiAutoEditing = false;
      }
    },

    async editorAutoEdit() {
      if (!this.editor.sourceVideo) { this.notifyError('Upload a video first'); return; }
      if (!this.health.openai_key) { this.notifyError('Whisper needs OPENAI_API_KEY'); return; }
      this.editor.autoEditing = true;
      try {
        const fd = new FormData();
        fd.append('file', this.editor.sourceVideo.file);
        fd.append('threshold_db', this.editor.thresholdDb);
        fd.append('min_silence_secs', this.editor.minSilenceSecs);
        fd.append('pad_secs', this.editor.padSecs);
        fd.append('template', this.editor.template);
        if (this.editor.voiceId) fd.append('voice_id', this.editor.voiceId);
        fd.append('enable_trim', this.editor.enableTrim ? 'true' : 'false');
        fd.append('enable_captions', this.editor.enableCaptions ? 'true' : 'false');
        fd.append('enable_wpm_normalize', this.editor.enableNormalizeWpm ? 'true' : 'false');
        fd.append('target_wpm', String(this.editor.targetWpm || 190));
        const overrides = this._activeOverrides();
        if (Object.keys(overrides).length) fd.append('overrides', JSON.stringify(overrides));
        const r = await fetch('/api/editor/auto_edit', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Auto-edit failed: ' + await r.text()); return; }
        const data = await r.json();
        this.editor.lastResult = { ...data, kind: 'auto' };
        this.editorHistory = [{ ...data, kind: 'auto', ts: Date.now() }, ...this.editorHistory];
        const parts = [];
        if (data.trim) parts.push(`trimmed ${data.trim.saved_secs}s`);
        if (data.voice_swap) parts.push(`voice swapped`);
        if (data.captions) parts.push(`${data.captions.n_words} words captioned`);
        this.notifyInfo('Auto-edit done: ' + parts.join(' · '));
      } finally {
        this.editor.autoEditing = false;
      }
    },

    // --- Live CSS preview of caption templates ------------------------------
    // Converts ASS &HAABBGGRR color → CSS rgba. ASS alpha 00 = opaque,
    // FF = fully transparent.
    _assToCss(assColor, fallback = 'rgba(255,255,255,1)') {
      if (!assColor || typeof assColor !== 'string') return fallback;
      const hex = assColor.replace(/^&H/i, '').padStart(8, '0').toUpperCase();
      const aa = parseInt(hex.slice(0, 2), 16);
      const bb = parseInt(hex.slice(2, 4), 16);
      const gg = parseInt(hex.slice(4, 6), 16);
      const rr = parseInt(hex.slice(6, 8), 16);
      if ([aa, bb, gg, rr].some(n => isNaN(n))) return fallback;
      const a = (255 - aa) / 255;
      return `rgba(${rr},${gg},${bb},${a.toFixed(2)})`;
    },

    // 8-direction text-shadow stack to approximate ASS outline.
    _outlineCss(color, px) {
      if (!px || px <= 0) return '';
      const dirs = [
        [px, 0], [-px, 0], [0, px], [0, -px],
        [px, px], [px, -px], [-px, px], [-px, -px],
      ];
      return dirs.map(([x, y]) => `${x}px ${y}px 0 ${color}`).join(', ');
    },

    _shadowCss(px) {
      if (!px || px <= 0) return '';
      return `${px}px ${px}px ${Math.max(2, px * 2)}px rgba(0,0,0,0.55)`;
    },

    // Returns the inline-style object for a single preview word.
    previewWordStyle(t, isActive) {
      const color = isActive && t.highlight_color
        ? this._assToCss(t.highlight_color)
        : this._assToCss(t.primary_color);
      const outline = this._outlineCss(this._assToCss(t.outline_color, 'rgba(0,0,0,1)'), Math.min(t.outline || 0, 3));
      const shadow = this._shadowCss(Math.min(t.shadow || 0, 6));
      const textShadow = [outline, shadow].filter(Boolean).join(', ');
      const fontSizePx = Math.max(10, Math.min(28, Math.round((t.size || 60) * 0.18)));
      const style = {
        color,
        fontFamily: `'${t.font}', Impact, Helvetica, sans-serif`,
        fontWeight: t.bold ? '900' : '700',
        fontSize: fontSizePx + 'px',
        lineHeight: '1.05',
        textShadow: textShadow || 'none',
        textTransform: t.all_caps ? 'uppercase' : 'none',
        display: 'inline-block',
        padding: '0 2px',
        whiteSpace: 'nowrap',
      };
      if (t.box && !isActive) {
        style.background = this._assToCss(t.back_color, 'rgba(0,0,0,0.7)');
        style.padding = '2px 4px';
        style.borderRadius = '2px';
      }
      return style;
    },

    // Wrapper style: positions the preview text on the 9:16 mock canvas,
    // approximating both vertical (margin_v + alignment) and horizontal
    // (margin_h) ASS positioning. Used both for tiny template thumbnails
    // and the big Studio preview.
    previewWrapStyle(t) {
      const a = t.alignment || 2;
      const vertical = a <= 3 ? 'flex-end'
                    : a >= 5 && a <= 7 ? 'flex-start'
                    : 'center';
      const tileH = 160;
      const norm = Math.min(1, (t.margin_v || 100) / 1080);
      const offsetY = Math.round(norm * tileH * 0.85);
      // Horizontal: percentage offset from center. margin_h is in 1080-wide coords.
      const marginH = t.margin_h || 0;
      const xPercent = 50 + (marginH / 1080) * 100;

      const transforms = ['translateX(-50%)'];
      if (vertical === 'center') transforms.push('translateY(-50%)');

      return {
        position: 'absolute',
        left: `${xPercent}%`,
        maxWidth: '92%',
        transform: transforms.join(' '),
        display: 'flex',
        flexWrap: 'wrap',
        gap: '4px 6px',
        justifyContent: 'center',
        alignItems: 'center',
        textAlign: 'center',
        bottom: vertical === 'flex-end' ? offsetY + 'px' : 'auto',
        top:    vertical === 'flex-start' ? offsetY + 'px'
             : vertical === 'center' ? '50%' : 'auto',
      };
    },

    // --- Studio: current template object + merged-with-overrides ---
    currentTemplate() {
      return (this.editorTemplates || []).find(t => t.slug === this.editor.template)
             || this.editorTemplates[0] || { font: 'Anton', size: 100, primary_color: '&H00FFFFFF',
                                              outline_color: '&H00000000', outline: 4, shadow: 3,
                                              words_per_card: 3, margin_v: 400, all_caps: true };
    },

    // Template with active overrides folded in — used to drive the live preview overlay.
    activeStyle() {
      const base = this.currentTemplate();
      const o = this._activeOverrides();
      const merged = { ...base, ...o };
      return merged;
    },

    // Free 2D drag: mousedown on caption text → updates both margin_v (Y from
    // bottom) and margin_h (X offset from center). Listens on window so the
    // pointer can leave the caption bounds without dropping the drag.
    startTextDrag(ev) {
      ev.preventDefault();
      const isTouch = ev.type === 'touchstart';
      const stage = ev.currentTarget.closest('.studio-preview') || ev.currentTarget.parentElement;
      const rect = stage ? stage.getBoundingClientRect() : { width: 360, height: 640 };
      this._dragPreviewW = rect.width;
      this._dragPreviewH = rect.height;
      this._dragStartX = isTouch ? ev.touches[0].clientX : ev.clientX;
      this._dragStartY = isTouch ? ev.touches[0].clientY : ev.clientY;
      this._dragStartMarginV = this.editor.overrides.margin_v ?? this.currentTemplate().margin_v ?? 400;
      this._dragStartMarginH = this.editor.overrides.margin_h ?? this.currentTemplate().margin_h ?? 0;
      this.draggingText = true;

      const onMove = (m) => {
        if (!this.draggingText) return;
        const x = m.type === 'touchmove' ? m.touches[0].clientX : m.clientX;
        const y = m.type === 'touchmove' ? m.touches[0].clientY : m.clientY;
        const ratioY = 1920 / Math.max(1, this._dragPreviewH);
        const ratioX = 1080 / Math.max(1, this._dragPreviewW);
        // margin_v counts UP from the bottom, so subtract Y delta.
        this.editor.overrides.margin_v = Math.max(0, Math.min(1700,
          Math.round(this._dragStartMarginV - (y - this._dragStartY) * ratioY)));
        // margin_h counts right-positive from center.
        this.editor.overrides.margin_h = Math.max(-540, Math.min(540,
          Math.round(this._dragStartMarginH + (x - this._dragStartX) * ratioX)));
      };
      const onUp = () => {
        this.draggingText = false;
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('touchmove', onMove);
        window.removeEventListener('touchend', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('touchmove', onMove);
      window.addEventListener('touchend', onUp);
    },

    // Drag the visible resize handle at bottom-right of the caption — scales
    // font size based on the diagonal pointer movement.
    startTextResize(ev) {
      ev.preventDefault();
      ev.stopPropagation();  // don't also kick off the text-drag
      const isTouch = ev.type === 'touchstart';
      const startX = isTouch ? ev.touches[0].clientX : ev.clientX;
      const startY = isTouch ? ev.touches[0].clientY : ev.clientY;
      const baseSize = this.editor.overrides.size ?? this.currentTemplate().size ?? 100;

      const onMove = (m) => {
        const x = m.type === 'touchmove' ? m.touches[0].clientX : m.clientX;
        const y = m.type === 'touchmove' ? m.touches[0].clientY : m.clientY;
        // Combine horizontal and vertical drag — pulling away grows, pulling in shrinks.
        const delta = ((x - startX) + (y - startY)) / 2;
        const next = Math.max(20, Math.min(220, Math.round(baseSize + delta * 0.7)));
        this.editor.overrides.size = next;
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('touchmove', onMove);
        window.removeEventListener('touchend', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('touchmove', onMove);
      window.addEventListener('touchend', onUp);
    },

    // Mouse-wheel-resize: tweak font size live.
    onTextWheel(ev) {
      ev.preventDefault();
      const base = this.editor.overrides.size ?? this.currentTemplate().size ?? 100;
      const step = 4;
      const next = Math.max(20, Math.min(220, base + (ev.deltaY < 0 ? step : -step)));
      this.editor.overrides.size = next;
    },

    onVideoLoaded(ev) {
      this.duration = ev.target.duration || 0;
      this.trimStartSecs = 0;
      this.trimEndSecs = 0;
    },

    formatSecs(s) {
      if (s == null || isNaN(s)) return '0:00';
      const m = Math.floor(s / 60);
      const sec = Math.floor(s % 60);
      return `${m}:${sec.toString().padStart(2, '0')}`;
    },

    selectEditorTemplate(slug) {
      this.editor.template = slug;
      try { localStorage.setItem('editor.template', slug); } catch (_) {}
      // Reset custom overrides whenever the user picks a new template so the
      // custom card shows the new defaults next time it's opened.
      this.editor.overrides = {
        font: null, size: null, primary_color: null, outline_color: null,
        words_per_card: null, margin_v: null, highlight_color: null, box: null,
        all_caps: null,
      };
    },

    async loadHeygenCatalogue() {
      // Idempotent — only fetch once per session.
      this.heygenCatalogueError = '';
      try {
        const [aR, vR] = await Promise.all([
          fetch('/api/heygen/avatars'),
          fetch('/api/heygen/voices'),
        ]);
        if (aR.ok) this.heygenAvatars = await aR.json();
        else this.heygenCatalogueError = await aR.text();
        if (vR.ok) this.heygenVoices = await vR.json();
      } catch (e) {
        this.heygenCatalogueError = String(e);
      }
      // Restore last-used HeyGen avatar + voice now that catalogue loaded.
      try {
        const savedAvatar = localStorage.getItem('avatarGen.avatarId');
        if (savedAvatar && this.heygenAvatars.some(a => a.avatar_id === savedAvatar)) {
          this.avatarGen.avatarId = savedAvatar;
        }
        if (this.avatarGen.voiceProvider === 'heygen' || !this.avatarGen.voiceProvider) {
          const savedVoice = localStorage.getItem('avatarGen.voiceId');
          if (savedVoice && this.heygenVoices.some(v => v.voice_id === savedVoice)) {
            this.avatarGen.voiceId = savedVoice;
          }
        }
      } catch (_) {}
    },

    // --- Photo-avatar from a swap variant ----------------------------------

    photoAvatarAvailable() {
      const m = (this.models.avatar || []).find(x => x.slug === 'heygen-photo-avatar');
      return !!m?.available;
    },

    async openPhotoAvatarModal(variant, charName) {
      this.photoAvatarModal = {
        open: true,
        variantUrl: variant.url,
        variantImageId: variant.variant_id,
        variantName: charName || 'character',
        voiceId: '',
        script: '',
        submitting: false,
      };
      // Make sure the catalogue is loaded so the voice dropdown has options.
      if (this.heygenVoices.length === 0 && this.photoAvatarAvailable()) {
        await this.loadHeygenCatalogue();
      }
    },

    closePhotoAvatarModal() {
      if (this.photoAvatarModal.submitting) return;
      this.photoAvatarModal.open = false;
    },

    async submitPhotoAvatarModal() {
      const m = this.photoAvatarModal;
      if (!m.script.trim()) { this.notifyError('Script is empty'); return; }
      if (!m.voiceId) { this.notifyError('Pick a voice'); return; }
      m.submitting = true;
      try {
        // Fetch the variant image and re-upload it as the source. The current
        // file is on the local server's disk; an absolute /files/... URL works.
        const imgResp = await fetch(m.variantUrl);
        if (!imgResp.ok) { this.notifyError('Could not read variant image'); return; }
        const blob = await imgResp.blob();
        const ext = (blob.type.split('/')[1] || 'png').split('+')[0];
        const fd = new FormData();
        fd.append('kind', 'avatar');
        fd.append('model', 'heygen-photo-avatar');
        fd.append('prompt', m.script.trim());
        fd.append('voice_id', m.voiceId);
        fd.append('aspect_ratio', '9:16');
        fd.append('files', new File([blob], `${m.variantName}.${ext}`, { type: blob.type }));
        const r = await fetch('/api/generations', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Submit failed: ' + await r.text()); return; }
        const gen = await r.json();
        this.avatarHistory = [gen, ...this.avatarHistory];
        this.notifyInfo('Avatar video queued — see the Avatar tab');
        this.photoAvatarModal.open = false;
      } finally {
        m.submitting = false;
      }
    },

    async submitAvatarGen() {
      if (!this.avatarGen.script.trim()) return;
      if (!this.avatarGen.avatarId || !this.avatarGen.voiceId) {
        this.notifyError('Pick an avatar and a voice first');
        return;
      }
      const m = this.currentAvatarModel();
      if (!m?.available) { this.notifyError('Model not configured'); return; }
      this.avatarGen.generating = true;
      try {
        const fd = new FormData();
        fd.append('kind', 'avatar');
        fd.append('model', this.avatarGen.model);
        fd.append('prompt', this.avatarGen.script.trim());
        fd.append('avatar_id', this.avatarGen.avatarId);
        fd.append('voice_id', this.avatarGen.voiceId);
        fd.append('voice_provider', this.avatarGen.voiceProvider || 'heygen');
        if (this.avatarGen.aspect) fd.append('aspect_ratio', this.avatarGen.aspect);
        const r = await fetch('/api/generations', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Generate failed: ' + await r.text()); return; }
        const gen = await r.json();
        this.avatarHistory = [gen, ...this.avatarHistory];
        // Avatar + voice are kept selected (batch-generation friendly).
        // Script preserved so users can iterate. Use clearAvatarForm()
        // to start fresh.
      } finally {
        this.avatarGen.generating = false;
      }
    },

    clearAvatarForm() {
      this.avatarGen.script = '';
    },

    // --- generations: image -------------------------------------------------

    // --- Paste + drop image refs into prompts (Image / Video tabs) ---

    _filesFromClipboard(ev) {
      const items = ev.clipboardData?.items || [];
      const out = [];
      for (const it of items) {
        if (it.kind === 'file' && (it.type || '').startsWith('image/')) {
          const f = it.getAsFile();
          if (f) out.push(f);
        }
      }
      return out;
    },

    onPromptPaste(ev, target) {
      const files = this._filesFromClipboard(ev);
      if (files.length === 0) return;            // text paste — let it through
      ev.preventDefault();
      if (target === 'image') {
        this.addImageRefs(files);
        this.notifyInfo(`Added ${files.length} reference image${files.length > 1 ? 's' : ''} from clipboard`);
      } else if (target === 'video') {
        this.setVideoRef(files[0]);
        if (files.length > 1) this.notifyInfo('Video uses one ref — kept the first');
        else this.notifyInfo('Reference image set from clipboard');
      }
    },

    onPromptDrop(ev, target) {
      const files = Array.from(ev.dataTransfer?.files || []).filter(f => f.type.startsWith('image/'));
      if (files.length === 0) return;
      ev.preventDefault();
      if (target === 'image') this.addImageRefs(files);
      else if (target === 'video') this.setVideoRef(files[0]);
      this.promptDropActive = false;
    },

    onPromptDragOver(ev) {
      // Only highlight when actual files are being dragged (avoid plain text drags).
      const types = Array.from(ev.dataTransfer?.types || []);
      if (types.includes('Files')) {
        ev.preventDefault();
        this.promptDropActive = true;
      }
    },

    onPromptDragLeave(ev) {
      if (!ev.currentTarget.contains(ev.relatedTarget)) {
        this.promptDropActive = false;
      }
    },

    // Reuse a past generation's prompt + settings in the active form.
    reuseImageGen(g) {
      if (!g) return;
      this.imageGen.prompt = g.prompt || '';
      if (g.model) this.imageGen.model = g.model;
      if (g.aspect_ratio) this.imageGen.aspect = g.aspect_ratio;
      this.activeTab = 'image';
      this.notifyInfo('Prompt loaded — drop new refs if needed');
    },

    reuseVideoGen(g) {
      if (!g) return;
      this.videoGen.prompt = g.prompt || '';
      if (g.model) this.videoGen.model = g.model;
      if (g.aspect_ratio) this.videoGen.aspect = g.aspect_ratio;
      if (g.duration_secs) this.videoGen.duration = g.duration_secs;
      this.activeTab = 'video';
      this.notifyInfo('Prompt loaded — drop a reference image');
    },

    reuseAvatarGen(g) {
      if (!g) return;
      this.avatarGen.script = g.prompt || '';
      if (g.model) this.avatarGen.model = g.model;
      if (g.avatar_id) this.avatarGen.avatarId = g.avatar_id;
      if (g.voice_id) this.avatarGen.voiceId = g.voice_id;
      if (g.voice_provider) this.avatarGen.voiceProvider = g.voice_provider;
      this.activeTab = 'avatar';
      this.notifyInfo('Script + avatar + voice loaded');
    },

    reuseAudioGen(g) {
      if (!g) return;
      if (g.model) this.audioGen.model = g.model;
      // Voice changer's "prompt" was a placeholder, not user-meaningful text;
      // only restore script text for TTS.
      if (g.model === 'elevenlabs-tts') this.audioGen.script = g.prompt || '';
      if (g.voice_id) this.audioGen.voiceId = g.voice_id;
      this.activeTab = 'audio';
      this.notifyInfo(g.model === 'elevenlabs-vc'
        ? 'Voice loaded — drop a new source clip'
        : 'Script + voice loaded');
    },

    addImageRefs(fileList) {
      const files = Array.from(fileList || []);
      const slots = 3 - this.imageGen.refs.length;
      for (const f of files.slice(0, Math.max(0, slots))) {
        this.imageGen.refs.push({ file: f, url: URL.createObjectURL(f) });
      }
    },

    removeImageRef(i) {
      const r = this.imageGen.refs[i];
      if (r?.url) URL.revokeObjectURL(r.url);
      this.imageGen.refs.splice(i, 1);
    },

    async submitImageGen() {
      if (!this.imageGen.prompt.trim()) return;
      const m = this.currentImageModel();
      if (!m?.available) { this.notifyError('Model not configured'); return; }
      this.imageGen.generating = true;
      try {
        const fd = new FormData();
        fd.append('kind', 'image');
        fd.append('model', this.imageGen.model);
        fd.append('prompt', this.imageGen.prompt.trim());
        if (this.imageGen.aspect) fd.append('aspect_ratio', this.imageGen.aspect);
        for (const r of this.imageGen.refs) fd.append('files', r.file);
        const resp = await fetch('/api/generations', { method: 'POST', body: fd });
        if (!resp.ok) { this.notifyError('Generate failed: ' + await resp.text()); return; }
        const gen = await resp.json();
        this.imageHistory = [gen, ...this.imageHistory];
        // Form state is intentionally preserved so users can tweak prompt
        // and re-submit without re-uploading refs. Use the "Clear" button
        // or `clearImageForm()` to start fresh.
      } finally {
        this.imageGen.generating = false;
      }
    },

    clearImageForm() {
      for (const r of this.imageGen.refs) if (r.url) URL.revokeObjectURL(r.url);
      this.imageGen.refs = [];
      this.imageGen.prompt = '';
    },

    // --- generations: video -------------------------------------------------

    setVideoRef(file) {
      if (!file) return;
      if (this.videoGen.ref?.url) URL.revokeObjectURL(this.videoGen.ref.url);
      this.videoGen.ref = { file, url: URL.createObjectURL(file) };
    },

    async submitVideoGen() {
      if (!this.videoGen.prompt.trim() || !this.videoGen.ref) return;
      const m = this.currentVideoModel();
      if (!m?.available) { this.notifyError('Model not configured'); return; }
      this.videoGen.generating = true;
      try {
        const fd = new FormData();
        fd.append('kind', 'video');
        fd.append('model', this.videoGen.model);
        fd.append('prompt', this.videoGen.prompt.trim());
        if (this.videoGen.aspect) fd.append('aspect_ratio', this.videoGen.aspect);
        if (this.videoGen.duration) fd.append('duration_secs', String(this.videoGen.duration));
        fd.append('files', this.videoGen.ref.file);
        const resp = await fetch('/api/generations', { method: 'POST', body: fd });
        if (!resp.ok) { this.notifyError('Generate failed: ' + await resp.text()); return; }
        const gen = await resp.json();
        this.videoHistory = [gen, ...this.videoHistory];
        // Preserve form so user can tweak the prompt and re-animate the
        // same reference image. Use clearVideoForm() to start fresh.
      } finally {
        this.videoGen.generating = false;
      }
    },

    clearVideoForm() {
      if (this.videoGen.ref?.url) URL.revokeObjectURL(this.videoGen.ref.url);
      this.videoGen.ref = null;
      this.videoGen.prompt = '';
    },

    // --- generations: retry + delete ----------------------------------------

    async retryGen(genId) {
      const r = await fetch('/api/generations/' + genId + '/retry', { method: 'POST' });
      if (!r.ok) { this.notifyError('Retry failed: ' + await r.text()); return; }
      await this.loadGenerations();
    },

    async deleteGen(genId) {
      if (!confirm('Delete this generation and its file?')) return;
      const r = await fetch('/api/generations/' + genId, { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      this.imageHistory = this.imageHistory.filter(g => g.gen_id !== genId);
      this.videoHistory = this.videoHistory.filter(g => g.gen_id !== genId);
      this.avatarHistory = this.avatarHistory.filter(g => g.gen_id !== genId);
      this.audioHistory = this.audioHistory.filter(g => g.gen_id !== genId);
      this.loadDisk();
    },

    _openFromUrl() {
      const m = (location.pathname || '/').match(/^\/j\/([A-Za-z0-9_-]+)\/?$/);
      if (m) {
        const jid = m[1];
        if (!this.job || this.job.job_id !== jid) this.openJob(jid, { pushState: false });
      } else if (this.job) {
        // navigated back to root
        this.resetJob();
      }
    },

    async loadDailyCost() {
      try {
        const r = await fetch('/api/costs?days=1');
        if (r.ok) {
          const data = await r.json();
          this.dailyCost = data.usd;
        }
      } catch (_) {}
    },

    async loadJobCost(jobId) {
      try {
        const r = await fetch('/api/jobs/' + jobId + '/cost');
        if (r.ok) {
          const data = await r.json();
          this.jobCost = data.usd;
        }
      } catch (_) {}
    },

    formatUsd(n) {
      if (n == null) return '';
      return '$' + Number(n).toFixed(2);
    },

    formatBytes(n) {
      if (n == null || isNaN(n)) return '–';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0; let v = Number(n);
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
    },

    async loadDisk() {
      try {
        const r = await fetch('/api/disk');
        if (r.ok) this.disk = await r.json();
      } catch (_) {}
    },

    async openDiskModal() {
      await this.loadDisk();
      this.showDiskModal = true;
    },

    // --- toasts --------------------------------------------------------------

    notify(kind, msg, opts = {}) {
      const id = ++this._toastSeq;
      const ttl = opts.ttl ?? (kind === 'error' ? 8000 : 4000);
      this.toasts = [...this.toasts, { id, kind, msg, retry: opts.retry || null }];
      setTimeout(() => this.dismissToast(id), ttl);
    },

    notifyError(msg, retry = null) { this.notify('error', msg, { retry }); },
    notifyInfo(msg)               { this.notify('info', msg); },

    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    async copyToast(msg) {
      try { await navigator.clipboard.writeText(msg); this.notifyInfo('Copied'); }
      catch (_) {}
    },

    async runToastRetry(toast) {
      this.dismissToast(toast.id);
      try { await toast.retry(); }
      catch (e) { this.notifyError('Retry failed: ' + (e?.message || e)); }
    },

    // --- health & library ----------------------------------------------------

    async loadHealth() {
      try { this.health = await (await fetch('/api/health')).json(); } catch (_) {}
    },

    async loadLibrary() {
      this.library = await (await fetch('/api/characters')).json();
    },

    async loadJobsList() {
      try {
        const r = await fetch('/api/jobs?summary=1');
        if (!r.ok) return;
        const list = await r.json();
        list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        this.jobsList = list;
      } catch (_) {}
    },

    _scheduleSidebarRefresh() {
      if (this._sidebarRefreshTimer) clearTimeout(this._sidebarRefreshTimer);
      this._sidebarRefreshTimer = setTimeout(() => this.loadJobsList(), 500);
    },

    // --- projects ------------------------------------------------------------

    async loadProjects() {
      try {
        const r = await fetch('/api/projects');
        if (!r.ok) return;
        this.projects = await r.json();
      } catch (_) {}
    },

    _loadCollapsed() {
      try {
        const raw = localStorage.getItem('projects_collapsed');
        if (raw) this.collapsedProjects = new Set(JSON.parse(raw));
      } catch (_) {}
    },

    _saveCollapsed() {
      try {
        localStorage.setItem('projects_collapsed',
          JSON.stringify(Array.from(this.collapsedProjects)));
      } catch (_) {}
    },

    isCollapsed(projectId) {
      return this.collapsedProjects.has(projectId);
    },

    toggleCollapsed(projectId) {
      const next = new Set(this.collapsedProjects);
      if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
      this.collapsedProjects = next;
      this._saveCollapsed();
    },

    _matchesSearch(job) {
      const q = (this.searchQuery || '').trim().toLowerCase();
      if (!q) return true;
      if ((job.title || '').toLowerCase().includes(q)) return true;
      if ((job.job_id || '').toLowerCase().includes(q)) return true;
      return false;
    },

    groupedJobs() {
      const byProject = new Map();
      const unfiled = [];
      for (const j of this.jobsList) {
        if (!this._matchesSearch(j)) continue;
        if (j.project_id) {
          if (!byProject.has(j.project_id)) byProject.set(j.project_id, []);
          byProject.get(j.project_id).push(j);
        } else {
          unfiled.push(j);
        }
      }
      const groups = this.projects
        .map(p => ({ project: p, jobs: byProject.get(p.project_id) || [] }))
        .filter(g => !this.searchQuery || g.jobs.length > 0);
      if (unfiled.length) {
        groups.push({ project: null, jobs: unfiled });
      }
      return groups;
    },

    openProjectModal() {
      this.newProjectName = '';
      this.newProjectCharIds = [];
      this.showProjectModal = true;
    },

    closeProjectModal() {
      if (this.submittingProject) return;
      this.showProjectModal = false;
      this.newProjectName = '';
      this.newProjectCharIds = [];
    },

    toggleNewProjectChar(cid) {
      if (this.newProjectCharIds.includes(cid)) {
        this.newProjectCharIds = this.newProjectCharIds.filter(x => x !== cid);
      } else {
        this.newProjectCharIds = [...this.newProjectCharIds, cid];
      }
    },

    async submitProjectModal() {
      const name = (this.newProjectName || '').trim();
      if (!name) return;
      this.submittingProject = true;
      try {
        const r = await fetch('/api/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, character_ids: this.newProjectCharIds }),
        });
        if (!r.ok) { this.notifyError('Create failed: ' + await r.text()); return; }
        const project = await r.json();
        this.showProjectModal = false;
        this.newProjectName = '';
        this.newProjectCharIds = [];
        await this.loadProjects();
        this.notifyInfo(`Project "${project.name}" created`);
      } finally {
        this.submittingProject = false;
      }
    },

    startEditProject(p) {
      this.editingProjectId = p.project_id;
      this.draftProjectName = p.name;
    },

    cancelEditProject() {
      this.editingProjectId = null;
      this.draftProjectName = '';
    },

    async saveProjectName(projectId) {
      const name = (this.draftProjectName || '').trim();
      if (!name) { this.cancelEditProject(); return; }
      const r = await fetch('/api/projects/' + projectId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) { this.notifyError('Rename failed: ' + await r.text()); return; }
      this.cancelEditProject();
      await this.loadProjects();
    },

    async deleteProject(projectId) {
      const p = this.projects.find(x => x.project_id === projectId);
      if (!p) return;
      const n = p.n_jobs || 0;
      const msg = n > 0
        ? `Delete project "${p.name}" and ${n} job${n === 1 ? '' : 's'} inside? This cannot be undone.`
        : `Delete project "${p.name}"?`;
      if (!confirm(msg)) return;
      const r = await fetch('/api/projects/' + projectId, { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      const data = await r.json().catch(() => ({}));
      const deleted = data.deleted_jobs || [];
      if (this.job && deleted.includes(this.job.job_id)) this.resetJob();
      await this.loadProjects();
      await this.loadJobsList();
    },

    startNewJobInProject(projectId) {
      this.currentProjectId = projectId;
      this.resetJob();
      const p = this.projects.find(x => x.project_id === projectId);
      if (p && Array.isArray(p.character_ids) && p.character_ids.length) {
        // Filter to characters that still exist in the library.
        const have = new Set(this.library.map(c => c.char_id));
        this.selectedCharacters = p.character_ids.filter(cid => have.has(cid));
      }
    },

    currentProject() {
      if (!this.currentProjectId) return null;
      return this.projects.find(p => p.project_id === this.currentProjectId) || null;
    },

    projectDefaultsMatchSelection() {
      const p = this.currentProject();
      if (!p) return true;
      const a = (p.character_ids || []).slice().sort().join(',');
      const b = this.selectedCharacters.slice().sort().join(',');
      return a === b;
    },

    async saveProjectDefaults() {
      const p = this.currentProject();
      if (!p) return;
      const r = await fetch('/api/projects/' + p.project_id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ character_ids: this.selectedCharacters }),
      });
      if (!r.ok) { this.notifyError('Save defaults failed: ' + await r.text()); return; }
      await this.loadProjects();
    },

    async moveJob(jobId, projectId) {
      this.moveMenuJobId = null;
      const r = await fetch('/api/jobs/' + jobId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId }),
      });
      if (!r.ok) { this.notifyError('Move failed: ' + await r.text()); return; }
      if (this.job && this.job.job_id === jobId) {
        this.job = await r.json();
      }
      await this.loadProjects();
      await this.loadJobsList();
    },

    toggleMoveMenu(jobId) {
      this.moveMenuJobId = this.moveMenuJobId === jobId ? null : jobId;
    },

    jobBadge(j) {
      if (j.n_failed > 0 && j.n_done === 0) return 'failed';
      if (j.n_done >= j.n_characters && j.n_characters > 0) return 'done';
      if (j.movement_set) return 'animating';
      if (j.n_approved > 0) return 'approved';
      return 'awaiting';
    },

    toggleCharacter(cid) {
      if (this.selectedCharacters.includes(cid)) {
        this.selectedCharacters = this.selectedCharacters.filter(x => x !== cid);
      } else {
        this.selectedCharacters.push(cid);
      }
    },

    async uploadScene(file) {
      if (!file) return;
      const fd = new FormData(); fd.append('file', file);
      const r = await fetch('/api/scenes', { method: 'POST', body: fd });
      if (!r.ok) { this.notifyError('Scene upload failed: ' + await r.text()); return; }
      const scene = await r.json();
      // Append to the scenes list; dedupe by scene_id (content-addressed)
      // so a paste of the same image twice doesn't create duplicates.
      if (!this.scenes.find(s => s.scene_id === scene.scene_id)) {
        this.scenes.push(scene);
      }
      this.scene = this.scenes[0] || null;   // legacy mirror
    },

    async uploadScenes(files) {
      for (const f of files) {
        if (f && f.type && f.type.startsWith('image/')) {
          await this.uploadScene(f);
        }
      }
    },

    removeScene(scene_id) {
      this.scenes = this.scenes.filter(s => s.scene_id !== scene_id);
      this.scene = this.scenes[0] || null;
    },

    // --- Step 1 paste + drop handlers (clipboard image → scene upload) ---
    _scenesFromClipboard(ev) {
      const items = ev.clipboardData?.items || [];
      const out = [];
      for (const it of items) {
        if (it.kind === 'file' && (it.type || '').startsWith('image/')) {
          const f = it.getAsFile();
          if (f) out.push(f);
        }
      }
      return out;
    },

    async onScenePaste(ev) {
      const files = this._scenesFromClipboard(ev);
      if (files.length === 0) return;        // plain text — let it through
      ev.preventDefault();
      await this.uploadScenes(files);
      this.notifyInfo(`Added ${files.length} scene${files.length > 1 ? 's' : ''} from clipboard`);
    },

    async onSceneDrop(ev) {
      const files = Array.from(ev.dataTransfer?.files || []).filter(f => f.type.startsWith('image/'));
      this.sceneDropActive = false;
      if (files.length === 0) return;
      ev.preventDefault();
      await this.uploadScenes(files);
    },

    onSceneDragOver(ev) {
      const types = Array.from(ev.dataTransfer?.types || []);
      if (types.includes('Files')) {
        ev.preventDefault();
        this.sceneDropActive = true;
      }
    },

    onSceneDragLeave(ev) {
      if (!ev.currentTarget.contains(ev.relatedTarget)) {
        this.sceneDropActive = false;
      }
    },

    // Legacy direct upload (kept for tests / future programmatic use). New code
    // paths go through the upload modal so the user can pick the target
    // character (existing vs new).
    async uploadCharacters(files, opts = {}) {
      const charId = opts.targetCharId || null;
      for (const f of files) {
        const fd = new FormData();
        fd.append('file', f);
        if (charId) fd.append('character_id', charId);
        const r = await fetch('/api/characters', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Character upload failed: ' + await r.text()); continue; }
      }
      await this.loadLibrary();
      this._reflectLibrarySelection();
    },

    // Always-on default: every character in the library is checked unless the
    // user manually unchecks it. New uploads auto-include themselves.
    _reflectLibrarySelection() {
      if (this.job) return;
      const have = new Set(this.selectedCharacters);
      for (const c of this.library) have.add(c.char_id);
      this.selectedCharacters = Array.from(have);
    },

    // --- Upload modal -------------------------------------------------------

    openUploadModal(opts = {}) {
      this.uploadTargetCharId = opts.targetCharId || null;
      this.uploadNewCharName = '';
      this._clearUploadFiles();
      if (opts.files && opts.files.length) this._addUploadFiles(opts.files);
      this.showUploadModal = true;
    },

    closeUploadModal() {
      if (this.uploadingChars) return;
      this._clearUploadFiles();
      this.uploadTargetCharId = null;
      this.uploadNewCharName = '';
      this.showUploadModal = false;
    },

    _clearUploadFiles() {
      for (const f of this.uploadFiles) if (f.url) URL.revokeObjectURL(f.url);
      this.uploadFiles = [];
    },

    _addUploadFiles(fileList) {
      for (const f of Array.from(fileList || [])) {
        if (!f.type.startsWith('image/')) continue;
        this.uploadFiles.push({ file: f, url: URL.createObjectURL(f) });
      }
    },

    removeUploadFile(i) {
      const f = this.uploadFiles[i];
      if (f?.url) URL.revokeObjectURL(f.url);
      this.uploadFiles.splice(i, 1);
    },

    async submitUploadModal() {
      if (this.uploadFiles.length === 0) return;
      if (!this.uploadTargetCharId && !(this.uploadNewCharName || '').trim()) {
        this.notifyError('Pick an existing character or name a new one');
        return;
      }
      this.uploadingChars = true;
      try {
        let charId = this.uploadTargetCharId;
        const newName = (this.uploadNewCharName || '').trim();
        for (const entry of this.uploadFiles) {
          const fd = new FormData();
          fd.append('file', entry.file);
          if (charId) {
            fd.append('character_id', charId);
          } else if (newName) {
            fd.append('name', newName);
          }
          const r = await fetch('/api/characters', { method: 'POST', body: fd });
          if (!r.ok) {
            this.notifyError('Upload failed: ' + await r.text());
            continue;
          }
          const ch = await r.json();
          // After the first upload to a "new character" target, subsequent files
          // in this batch should append to the just-created character.
          if (!charId) charId = ch.char_id;
        }
        await this.loadLibrary();
        this._reflectLibrarySelection();
        this.notifyInfo(`Uploaded ${this.uploadFiles.length} image${this.uploadFiles.length === 1 ? '' : 's'}`);
        this.closeUploadModal();
      } finally {
        this.uploadingChars = false;
      }
    },

    async deleteCharacterImage(charId, imageId) {
      if (!confirm('Remove this image from the character?')) return;
      const r = await fetch(`/api/characters/${charId}/images/${imageId}`, { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      await this.loadLibrary();
      // If the character was deleted (last image), also drop from selection.
      const data = await r.json().catch(() => ({}));
      if (data.character_deleted) {
        this.selectedCharacters = this.selectedCharacters.filter(x => x !== charId);
      }
    },

    async deleteCharacter(cid) {
      if (!confirm('Remove this character from the library?')) return;
      const r = await fetch('/api/characters/' + cid, { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      this.selectedCharacters = this.selectedCharacters.filter(x => x !== cid);
      await this.loadLibrary();
    },

    // --- character rename ----------------------------------------------------

    startEditCharacter(ch) {
      this.editingCharacterId = ch.char_id;
      this.draftCharacterName = ch.name;
    },

    cancelEditCharacter() {
      this.editingCharacterId = null;
      this.draftCharacterName = '';
    },

    async saveCharacterName(charId) {
      const name = this.draftCharacterName.trim();
      if (!name) { this.cancelEditCharacter(); return; }
      const r = await fetch('/api/characters/' + charId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) { this.notifyError('Rename failed: ' + await r.text()); return; }
      this.cancelEditCharacter();
      await this.loadLibrary();
      await this.loadJobsList();
      if (this.job) await this.refreshActiveJob();
    },

    // --- job lifecycle -------------------------------------------------------

    async startJob() {
      if (this.scenes.length === 0 || this.selectedCharacters.length === 0) return;
      this.generating = true;
      try {
        const r = await fetch('/api/jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            // Send the full list — backend treats this as the canonical source.
            // For new jobs we also send scene_id=scenes[0] just in case any
            // older code path needs it; backend dedupes.
            scene_ids: this.scenes.map(s => s.scene_id),
            scene_id: this.scenes[0].scene_id,
            character_ids: this.selectedCharacters,
            images_per_character: this.imagesPerChar,
            project_id: this.currentProjectId,
            prompt: (this.swapPrompt || '').trim() || null,
            image_model: this.swapModel,
          }),
        });
        if (!r.ok) { this.notifyError('Job creation failed: ' + await r.text()); return; }
        const job = await r.json();
        this.job = job;
        this.connectWS(job.job_id);
        await this.loadJobsList();
        await this.loadProjects();
      } finally {
        this.generating = false;
      }
    },

    async openJob(jobId, opts = {}) {
      if (this.ws) { try { this.ws.close(); } catch (_) {} this.ws = null; this.wsConnected = false; }
      const r = await fetch('/api/jobs/' + jobId);
      if (!r.ok) { this.notifyError('Could not open job: ' + await r.text()); return; }
      this.job = await r.json();
      this.jobCost = null;
      this.loadJobCost(jobId);
      if (opts.pushState !== false) {
        history.pushState({ jobId }, '', '/j/' + jobId);
      }
      // Reload scenes from the job. Multi-scene jobs serialize their scenes
      // in `job.scenes`. Legacy single-scene jobs only have scene_image_url —
      // fall back to a 1-item list.
      if (Array.isArray(this.job.scenes) && this.job.scenes.length > 0) {
        this.scenes = this.job.scenes.map(s => ({
          scene_id: s.scene_id, url: s.url,
          original_name: s.scene_id,
        }));
      } else if (this.job.scene_image_url) {
        this.scenes = [{
          scene_id: this.job.scene_id,
          url: this.job.scene_image_url,
          original_name: this.job.title || this.job.job_id,
        }];
      } else {
        this.scenes = [];
      }
      this.scene = this.scenes[0] || null;
      this.selectedCharacters = Object.keys(this.job.characters);
      this.imagesPerChar = this.job.images_per_character || 1;
      this.videosPerChar = this.job.videos_per_character || 1;
      this.movementPrompt = this.job.movement_prompt || '';
      this.swapPrompt = this.job.prompt || this.swapDefaultPrompt;
      this.swapModel = this.job.image_model || 'gpt-image';
      this.editingVariant = null;
      this.editingTitle = false;
      this.connectWS(jobId);
    },

    async compactJob(jobId) {
      if (!confirm('Compact this job? Removes all rejected variants and failed/in-flight videos from disk. Keeps the approved variant and finished videos.')) return;
      const r = await fetch('/api/jobs/' + jobId + '/compact', { method: 'POST' });
      if (!r.ok) { this.notifyError('Compact failed: ' + await r.text()); return; }
      const data = await r.json();
      this.notifyInfo('Freed ' + this.formatBytes(data.bytes_freed));
      if (this.job && this.job.job_id === jobId) await this.refreshActiveJob();
      this.loadDisk();
    },

    isJobInFlight(j) {
      if (!j) return false;
      const busy = new Set(['queued', 'generating', 'animating']);
      return Object.values(j.characters || {}).some(c => busy.has(c.status));
    },

    async duplicateJob(jobId) {
      const r = await fetch('/api/jobs/' + jobId + '/duplicate', { method: 'POST' });
      if (!r.ok) { this.notifyError('Duplicate failed: ' + await r.text()); return; }
      const job = await r.json();
      this.openJob(job.job_id);
      await this.loadJobsList();
      this.notifyInfo('Duplicated → ' + (job.title || job.job_id));
    },

    async deleteJob(jobId) {
      if (!confirm('Delete this job and all its generated files?')) return;
      const r = await fetch('/api/jobs/' + jobId, { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      if (this.job && this.job.job_id === jobId) this.resetJob();
      await this.loadJobsList();
      await this.loadProjects();
    },

    async refreshActiveJob() {
      if (!this.job) return;
      try {
        const r = await fetch('/api/jobs/' + this.job.job_id);
        if (r.ok) this.job = await r.json();
      } catch (_) {}
    },

    // --- title rename --------------------------------------------------------

    startEditTitle() {
      this.editingTitle = true;
      this.draftTitle = this.job?.title || this.job?.job_id || '';
    },

    cancelEditTitle() {
      this.editingTitle = false;
      this.draftTitle = '';
    },

    swapPromptDirty() {
      if (!this.job) return false;
      const current = this.swapPrompt || '';
      const stored = this.job.prompt || this.swapDefaultPrompt || '';
      return current.trim() !== stored.trim() || this.swapModel !== (this.job.image_model || 'gpt-image');
    },

    async saveJobPromptModel(opts = {}) {
      if (!this.job) return;
      const body = {
        prompt: (this.swapPrompt || '').trim() || null,
        image_model: this.swapModel,
      };
      const r = await fetch('/api/jobs/' + this.job.job_id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { this.notifyError('Save failed: ' + await r.text()); return false; }
      this.job = await r.json();
      this.notifyInfo('Job updated');
      if (opts.thenRegenerate) {
        await this.regenerateAllChars();
      }
      return true;
    },

    async regenerateAllChars() {
      if (!this.job) return;
      const cids = Object.keys(this.job.characters || {});
      for (const cid of cids) {
        await this.approve(cid, 'regenerate');
      }
    },

    async deleteVariant(charId, variantId) {
      if (!this.job) return;
      if (!confirm('Delete this generated image?')) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/characters/${charId}/variants/${variantId}`, {
        method: 'DELETE',
      });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      this.job = await r.json();
    },

    async retryVariant(charId, variantId) {
      if (!this.job) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/characters/${charId}/variants/${variantId}/retry`, {
        method: 'POST',
      });
      if (!r.ok) { this.notifyError('Retry failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.notifyInfo('Retrying just this variant — others are untouched');
    },

    // --- Step 2: per-character source-image picker ------------------------

    // What URL should the character card show in Step 2? When a job is
    // loaded, use the actual source image set on that JobCharacter (which
    // can differ from the library's primary). Otherwise fall back to the
    // library primary.
    cardThumbUrl(ch) {
      if (this.job) {
        const jc = this.job.characters?.[ch.char_id];
        if (jc?.source_image_url) return jc.source_image_url;
      }
      return ch.url;
    },

    // Match the JobCharacter's source_image_path's filename to a specific
    // CharacterImage in the library so we can highlight "currently selected"
    // in the picker. Returns image_id or null.
    currentSourceImageId(ch) {
      if (!this.job) return ch.primary_image_id;
      const jc = this.job.characters?.[ch.char_id];
      if (!jc?.source_image_url) return ch.primary_image_id;
      // URL ends with /characters/<filename> — strip query strings + path.
      const filename = jc.source_image_url.split('?')[0].split('/').pop();
      const match = (ch.images || []).find(img => img.filename === filename);
      return match?.image_id || ch.primary_image_id;
    },

    openImagePicker(charId, event) {
      // Don't let the parent card's @click="toggleCharacter" fire too.
      if (event) event.stopPropagation();
      this.sourceImagePickerCharId =
        this.sourceImagePickerCharId === charId ? null : charId;
    },

    closeImagePicker() {
      this.sourceImagePickerCharId = null;
    },

    async setCharSourceImage(charId, imageId) {
      this.sourceImagePickerCharId = null;
      if (!this.job) {
        // Creating a new job — no JobCharacter exists yet. For now we tell
        // the user to start the job first, then swap. (Could be enhanced
        // later to stage the override client-side.)
        this.notifyError('Start the job first, then swap the reference image');
        return;
      }
      try {
        const r = await fetch(
          `/api/jobs/${this.job.job_id}/characters/${charId}/source_image`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image_id: imageId }),
          },
        );
        if (!r.ok) { this.notifyError('Swap failed: ' + await r.text()); return; }
        this.job = await r.json();
        this.notifyInfo('Source image swapped — click ↻ regenerate to rebuild variants with the new reference');
      } catch (e) {
        this.notifyError('Swap failed: ' + e.message);
      }
    },

    async saveTitle() {
      if (!this.job) return;
      const title = this.draftTitle.trim();
      if (!title) { this.cancelEditTitle(); return; }
      const r = await fetch('/api/jobs/' + this.job.job_id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      if (!r.ok) { this.notifyError('Rename failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.cancelEditTitle();
      await this.loadJobsList();
    },

    // --- approval + edit -----------------------------------------------------

    async approve(cid, action, variantId) {
      if (!this.job) return;
      const body = { char_id: cid, action };
      if (variantId) body.variant_id = variantId;
      const r = await fetch('/api/jobs/' + this.job.job_id + '/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { this.notifyError('Action failed: ' + await r.text()); return; }
      this.job = await r.json();
      this._scheduleSidebarRefresh();
    },

    async submitMovement() {
      if (!this.job || !this.movementPrompt.trim()) return;
      const r = await fetch('/api/jobs/' + this.job.job_id + '/movement', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: this.movementPrompt.trim(),
          videos_per_character: this.videosPerChar,
        }),
      });
      if (!r.ok) { this.notifyError('Movement submit failed: ' + await r.text()); return; }
      this.job = await r.json();
      this._scheduleSidebarRefresh();
    },

    async retryVideo(charId, videoId) {
      if (!this.job) return;
      const r = await fetch('/api/jobs/' + this.job.job_id + '/retry_video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ char_id: charId, video_id: videoId }),
      });
      if (!r.ok) { this.notifyError('Retry failed: ' + await r.text()); return; }
      this.job = await r.json();
    },

    async unlockMovement() {
      if (!this.job) return;
      if (!confirm('Unlock movement prompt? This clears all pending/failed videos so you can re-prompt.')) return;
      const r = await fetch('/api/jobs/' + this.job.job_id + '/unlock_movement', {
        method: 'POST',
      });
      if (!r.ok) { this.notifyError('Unlock failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.movementPrompt = '';
      this._scheduleSidebarRefresh();
    },

    canUnlockMovement() {
      if (!this.job || !this.job.movement_prompt) return false;
      return !Object.values(this.job.characters).some(
        c => (c.videos || []).some(v => v.status === 'done')
      );
    },

    openEdit(charId, variantId) {
      this.editingVariant = { char_id: charId, variant_id: variantId };
      this.editPrompt = '';
    },

    closeEdit() {
      this.editingVariant = null;
      this.editPrompt = '';
    },

    async submitEdit() {
      if (!this.job || !this.editingVariant || !this.editPrompt.trim()) return;
      const { char_id, variant_id } = this.editingVariant;
      const r = await fetch('/api/jobs/' + this.job.job_id + '/edit_variant', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          char_id,
          variant_id,
          prompt: this.editPrompt.trim(),
        }),
      });
      if (!r.ok) { this.notifyError('Edit failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.closeEdit();
    },

    // --- derived helpers -----------------------------------------------------

    hasApprovedChar() {
      if (!this.job) return false;
      return Object.values(this.job.characters)
        .some(c => c.status === 'approved' || ['animating','done','failed'].includes(c.status));
    },

    hasVideoStage() {
      return !!(this.job && this.job.movement_prompt);
    },

    approvedCount() {
      if (!this.job) return 0;
      return Object.values(this.job.characters)
        .filter(c => c.status === 'approved' || ['animating','done'].includes(c.status))
        .length;
    },

    // Rough per-video cost in USD. Override via localStorage 'video_price_usd'
    // if the real price drifts. Defaults are a back-of-envelope guess.
    _videoPriceUsd() {
      const v = parseFloat(localStorage.getItem('video_price_usd') || '');
      return Number.isFinite(v) && v > 0 ? v : 0.40;
    },

    estimatedCost() {
      const n = this.approvedCount() * this.videosPerChar;
      const total = n * this._videoPriceUsd();
      return '$' + total.toFixed(2);
    },

    statusBadgeClasses(status) {
      switch (status) {
        case 'awaiting_approval': return 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300';
        case 'approved':          return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300';
        case 'rejected':          return 'bg-neutral-200 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400';
        case 'animating':         return 'bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300';
        case 'done':              return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300';
        case 'failed':            return 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300';
        case 'generating':        return 'bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300';
        default:                  return 'bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300';
      }
    },

    // --- reset / ws ----------------------------------------------------------

    newJob() {
      this.currentProjectId = null;
      this.resetJob();
      if (location.pathname !== '/') history.pushState(null, '', '/');
    },

    resetJob() {
      if (this.ws) { try { this.ws.close(); } catch (_) {} }
      this.ws = null;
      this.wsConnected = false;
      this.job = null;
      this.scenes = [];
      this.scene = null;
      // Default: all library characters checked. startNewJobInProject() will
      // override this with the project's preset if there is one.
      this.selectedCharacters = (this.library || []).map(c => c.char_id);
      this.movementPrompt = '';
      this.editingVariant = null;
      this.editPrompt = '';
      this.editingTitle = false;
      this.draftTitle = '';
      this.jobCost = null;
      this.swapPrompt = this.swapDefaultPrompt;
      this.swapModel = 'gpt-image';
    },

    connectWS(jobId) {
      const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
        + '/ws/jobs/' + jobId;
      const ws = new WebSocket(url);
      this.ws = ws;
      ws.onopen = () => { this.wsConnected = true; this._wsBackoff = 1000; };
      ws.onmessage = (e) => this.handleEvent(JSON.parse(e.data));
      ws.onclose = () => {
        this.wsConnected = false;
        if (this.job && this.job.job_id === jobId) {
          const delay = Math.min(this._wsBackoff, 10000);
          this._wsBackoff = Math.min(this._wsBackoff * 2, 10000);
          setTimeout(() => this.connectWS(jobId), delay);
        }
      };
      ws.onerror = () => {};
    },

    async handleEvent(evt) {
      if (!evt || !evt.kind) return;
      if (evt.kind === 'snapshot') {
        this.job = evt.job;
        return;
      }
      if (!this.job || evt.job_id !== this.job.job_id) return;
      this._scheduleSidebarRefresh();
      try {
        const r = await fetch('/api/jobs/' + this.job.job_id);
        if (r.ok) this.job = await r.json();
      } catch (_) {}
      // Refresh job cost on terminal-ish events (a variant landed / video done / failed).
      if (['variant.ready', 'variant.failed', 'video.ready', 'video.failed', 'video.submitted'].includes(evt.kind)) {
        this.loadJobCost(this.job.job_id);
        this.loadDailyCost();
      }
      // A fresh variant means this character's library gallery is stale.
      if (evt.kind === 'variant.ready' && evt.char_id) {
        this._invalidateGalleryFor(evt.char_id);
      }
    },
  };
}
