// Character Swap Studio — Alpine.js front-end.

function studio() {
  return {
    health: { openai_key: false, anthropic_key: false, xai_key: false },
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
    // Map of char_id -> image_id chosen as the reference BEFORE a job exists.
    // Sent to POST /api/jobs as character_source_image_ids. Cleared when
    // the job is created or the character is deselected.
    charSourceOverrides: {},
    imagesPerChar: 1,
    // OS-level notification preferences. Persisted to localStorage in init().
    // Both default to ON; user can disable via header toggles. The chime is
    // synthesized via Web Audio API (no asset file). OS popup needs browser
    // permission — _requestNotifPermission() prompts once.
    notif: {
      os: (typeof localStorage !== 'undefined') ? (localStorage.getItem('notif.os') !== '0') : true,
      sound: (typeof localStorage !== 'undefined') ? (localStorage.getItem('notif.sound') !== '0') : true,
    },
    // Snapshot of last-seen swap job state per job_id — used to detect
    // transitions like "char moved to awaiting_approval" so we only fire
    // ONE milestone notification per gate, not on every WS refresh.
    _lastSwapJobSnapshot: {},
    // Same idea for freeform gens: stash previous status keyed by gen_id.
    _lastGenStatus: {},
    videosPerChar: 1,
    // While a generate_more_videos request is in flight for char X this
    // holds X — used to disable the "+ N more" buttons + show a spinner
    // so the user doesn't fire 10 parallel batches with rapid clicks.
    generatingMoreFor: null,
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
    // Prompt enrichment toggles — per-form so Hugo can A/B per pipeline.
    // Defaults match where enrichment helps most (video especially).
    // Persisted in init() via $watch.
    enrich: {
      swap:  (typeof localStorage !== 'undefined') ? (localStorage.getItem('enrich.swap')  !== '0') : true,
      image: (typeof localStorage !== 'undefined') ? (localStorage.getItem('enrich.image') !== '0') : true,
      video: (typeof localStorage !== 'undefined') ? (localStorage.getItem('enrich.video') !== '0') : true,
    },
    // 🎬 AI Director toggles — opt-in Claude Opus agent that writes tailored
    // per-(character, scene, variant) prompts. Slower (~15-25s) and pricier
    // (~$0.05) than ✨ enrich, but produces character-specific prompts that
    // reference visible features. Default OFF; persisted via $watch.
    director: {
      swap:  (typeof localStorage !== 'undefined') ? (localStorage.getItem('director.swap')  === '1') : false,
      image: (typeof localStorage !== 'undefined') ? (localStorage.getItem('director.image') === '1') : false,
      video: (typeof localStorage !== 'undefined') ? (localStorage.getItem('director.video') === '1') : false,
    },
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
    // Reengineer: reference video → scenes → swap → Kling clips (native
    // audio) → reassembled final per character.
    reengineerGen: {
      file: null,                    // File object
      name: '',
      charIds: [],                   // selected character ids
      imageModel: 'gpt2-id-swap',    // Hugo's preset (2026-06-11) — sticky via
                                     // localStorage; loadGenModels re-asserts
      autoMode: false,               // skip the image-approval gate
      useDirector: false,            // 🎬 tailored per-scene swap prompts (Claude)
      outfitMode: 'scene',           // scene | character | custom
      outfitText: '',                // clothing description for custom mode
      sceneSensitivity: 'high',      // normal | high | max cut detection
      background: null,              // optional File: replacement environment
      backgroundUrl: '',             // object URL for the thumbnail
      sourceOverrides: {},           // char_id → image_id (outfit/reference pick)
      submitting: false,
    },
    reengineerPickerChar: null,       // char_id whose reference-image popover is open
    reengineerHistory: [],            // [{re_id, status, scenes, job, finals, ...}]
    _reengineerPollTimer: null,
    // variant_id → Date.now() set on per-slot retry. A retry overwrites the
    // SAME file path, so the thumbnail <img> needs a cache-busting query to
    // show the regenerated image instead of the browser-cached old one.
    reengineerRetryNonce: {},
    // job_id → WebSocket. Reengineer runs ride the same /ws/jobs/{job_id}
    // stream the Swap tab uses — every variant.* event triggers a debounced
    // slim refetch so thumbnails/progress land in ~real time instead of on
    // the next 5s poll tick.
    _reengineerSockets: {},
    _reRefreshTimers: {},
    // Edit mode (opt-in iteration on a finished run / at the gate):
    // re_id → bool toggle; drafts keyed `${re_id}:${idx}` so the 5s poll
    // can't clobber a half-typed prompt; per-run add-scene form state.
    reEdit: {},
    reSceneDrafts: {},
    reAdd: {},
    editor: {
      sourceVideo: null,           // {file, url, name}
      thresholdDb: -30,       // Hugo's preset
      minSilenceSecs: 0.30,   // Hugo's preset
      padSecs: 0.03,          // Hugo's preset
      trimming: false,
      template: 'capcut-purple-pill',   // Hugo's preferred default (was popout-yellow)
      captioning: false,
      autoEditing: false,
      voiceId: '',
      enableTrim: true,
      enableCaptions: true,
      enableNormalizeWpm: false,      // Hugo's preset: WPM normalize OFF
      targetWpm: 190,                 // 190 WPM is the canonical "engaging pace" baseline
      playbackSpeed: 1.1,             // Hugo's preset: 10% global speed-up
      // Auto-fire the Resolve pipeline (Phase 4) after a successful render.
      // Persisted to localStorage so the toggle survives reloads.
      autoExportResolve: (typeof localStorage !== 'undefined'
                          && localStorage.getItem('editor.autoExportResolve') === '1'),
      pipelineState: null,            // {status, drive_link, error, ...} from /api/editor/.../pipeline_state
      _pipelinePoll: null,            // setInterval handle while polling
      rerendering: false,
      rerenderOpen: false,                    // shows the edit-result panel
      rerenderTemplate: 'capcut-purple-pill', // independent of editor.template so you can A/B
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
        // New tunables (May 2026): user-controllable font weight, opacity,
        // and separate shadow blur + distance. `highlight_color_hex` and
        // `outline_color_hex` are UI-only mirrors of their ASS-BGR twins.
        font_weight: null, opacity: null,
        shadow_distance: null, shadow_blur: null,
        highlight_color_hex: null,
        outline_color_hex: null,
      },
      lastResult: null,            // {output_url, kind: 'trim'|'captions', ...}
      // Editor "Character" dropdown (Phase B). When the user picks a
      // character from their library, its preset voice_id auto-fills
      // `editor.voiceId`. Manual voice override still works afterwards —
      // we only auto-fill on dropdown CHANGE, not on every render.
      linkedCharId: '',
      // --- CapCut-style caption editor ---
      // Visible only when the user clicks "Edit captions" on a finished
      // caption render. Mirrors Submagic's transcript edit: each card row
      // shows start/end + the words; user retunes timing or fixes
      // misheard words, then "Save & re-render" posts edits back.
      captionEditOpen: false,
      captionEditMode: 'line',       // 'line' (group by words_per_card) | 'word' (per-word fine-tune)
      // The editable transcript. Initialized from lastResult.words when the
      // panel opens; saved back to /api/editor/rerender as words_json.
      editedWords: [],
      savingCaptionEdits: false,
    },
    // Drive-export modal state. Lives at the top level (not nested under
    // editor.*) so the modal can be opened from either single-clip or
    // multi-clip render results without juggling per-mode flags.
    driveExport: {
      open: false,
      filename: '',
      uploading: false,
      lastUrl: '',
    },
    // Step 6: per-character compile settings. Shared across all characters
    // in the active job (one set of editor settings → one batch). Voice
    // override blank → each character uses its library preset voice.
    // Step 6 defaults: capcut-purple-pill + trim + captions, with WPM normalize
    // and voice swap OFF ("nothing else from the multiclip editor"). Persisted
    // as ONE versioned JSON blob so every choice — not just template/WPM —
    // survives reloads; the v2 key cleanly supersedes the old split keys + the
    // previous submagic-pro default.
    compileSettings: (() => {
      const defaults = {
        template: 'capcut-purple-pill',
        enableTrim: true,
        enableCaptions: true,
        enableWpmNormalize: false,
        enableVoiceSwap: false,
        thresholdDb: -30,       // Hugo's preset
        minSilenceSecs: 0.30,   // Hugo's preset
        padSecs: 0.03,          // Hugo's preset
        targetWpm: 190,
        voiceOverride: '',
      };
      try {
        const saved = JSON.parse(localStorage.getItem('compile.settings.v2') || '{}');
        return { ...defaults, ...(saved && typeof saved === 'object' ? saved : {}) };
      } catch (_) { return defaults; }
    })(),
    // Reengineer ⚙ Slutvideo (Editor) settings — same shape as Step 6's
    // compileSettings minus the trim-tuning knobs (the runner uses Hugo's
    // preset values for those). Defaults keep Kling's voice + pacing.
    reAsmSettings: (() => {
      const defaults = {
        template: 'capcut-bluebox',     // Hugo 2026-06-12: bluebox @ 68 is
        captionSize: 68,                // the Reengineer-final standard
        enableTrim: true,
        enableCaptions: true,
        enableWpmNormalize: false,
        enableVoiceSwap: false,
        targetWpm: 190,
        voiceOverride: '',
      };
      try {
        const saved = JSON.parse(localStorage.getItem('reassemble.settings.v1') || '{}');
        return { ...defaults, ...(saved && typeof saved === 'object' ? saved : {}) };
      } catch (_) { return defaults; }
    })(),
    compiling: false,
    pipelineRunning: false,         // true while the 🚀 Run-full-pipeline orchestrator is running
    // --- Visual scrubbing timeline state (kept at top level for Alpine
    // x-show / x-bind brevity in markup; logically belongs to the caption
    // editor). `playheadSecs` is driven by the Remotion Player's
    // frameupdate event during playback OR by the user dragging the
    // playhead. `isScrubbing` blocks the auto-follow from fighting the user.
    playheadSecs: 0,
    isScrubbing: false,
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
    // Higgsfield Drive inbox — clips auto-pulled from a user-configured
    // Drive folder via the background watcher. Shape:
    // {drive: {ready, folder_name, folder_id, poll_secs}, items: [...]}
    higgsfieldInbox: null,
    higgsfieldPolling: false,
    swapPrompt: '',
    swapModel: 'gpt-image',
    // Optional 3rd reference image for the swap flow's image model. Filename
    // is what we send back to POST /api/jobs; URL is for the inline preview.
    extraRefFilename: '',
    extraRefUrl: '',
    extraRefOriginalName: '',
    uploadingExtraRef: false,
    // Step-4 video provider for the swap flow. Defaults to grok-imagine for
    // back-compat; users can switch to Kling / Veo / Runway / Luma / Pika /
    // Hailuo / Sora / Wan / Seedance / Higgsfield via the picker.
    swapVideoModel: 'kling-v3',   // Hugo's preset default video model
    // --- Animate tab (Step A): build a video job from already-finished images.
    // seqImages holds staged client-side files (not yet uploaded) in the order
    // they'll be animated. Each {uid, file, previewUrl, name}. On createSequence()
    // they POST to /api/jobs/from_images and become a normal pre-approved job.
    seqImages: [],
    seqTitle: '',
    seqVideoModel: 'kling-v2-6',
    seqCreating: false,
    // Per-job clip duration override (seconds). null = use the env default
    // (settings.video_duration_secs). Picker in Step 4 sets it to one of
    // the selected model's `duration_options` from /api/generations/models.
    swapDurationSecs: null,
    swapDefaultPrompt: '',          // effective default (project's if set, else global)
    swapGlobalDefaultPrompt: '',    // always the global pipeline.GENERATION_PROMPT
    swapProjectDefaultPrompt: '',   // current project's override, if any
    showCharLib: false,
    mobileNav: false,          // <md: jobs sidebar as slide-over drawer
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
    // --- Chat tab ---------------------------------------------------------
    chatSessions: [],        // list of {chat_id, title, n_messages, ...}
    activeChat: null,        // full chat object incl messages + media
    chatInput: '',
    chatPending: false,
    chatPendingLabel: '',
    _chatInited: false,
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
    // Legacy single-prompt; kept as a buffer for the single-scene case to
    // simplify x-model binding. New flow uses `movementPrompts` (dict).
    movementPrompt: '',
    // Per-scene movement prompts: scene_id → string. Hugo's multi-scene
    // jobs get one textarea per scene in Step 4; this dict is what posts
    // to /api/jobs/{id}/movement.
    movementPrompts: {},
    // Per-SCENE movement prompts + durations (Step 4 rows). scene_id → prompt /
    // seconds. One prompt+duration per scene, shared by all that scene's images.
    // For multiple clips from one image, duplicate the scene (Arrange panel).
    movementByScene: {},
    durationByScene: {},
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
      // Validate the stored tab against the tabs that still exist — users
      // who last sat on a removed tab (image/video/avatar/audio/broll)
      // would otherwise land on a blank page.
      const _validTabs = ['chat', 'swap', 'animate', 'reengineer', 'editor'];
      const _storedTab = localStorage.getItem('active_tab');
      this.activeTab = _validTabs.includes(_storedTab) ? _storedTab : 'swap';
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
      this.loadReengineerHistory();
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
      // Reengineer swap-engine pick is a sticky preset (Hugo 2026-06-11:
      // gpt2-id-swap is his GPT engine of choice — make it survive reloads).
      this.$watch('reengineerGen.imageModel',
                  v => v && localStorage.setItem('reengineerGen.imageModel', v));
      // Refresh daily cost every minute while the tab is open.
      this._dailyCostTimer = setInterval(() => this.loadDailyCost(), 60000);
      // 1-second tick so the elapsed-time labels in the status toast +
      // B-roll progress card update without an extra backend round-trip.
      this._tickTimer = setInterval(() => { this._tickNow = Date.now(); }, 1000);
      // Reload swap defaults whenever the active project changes — picks up
      // the project's `default_prompt` (or falls back to global).
      this.$watch('currentProjectId', () => this.loadSwapDefaults());
      // OS-level notifications: ask permission once (browser remembers the
      // answer); persist the user's toggle picks across reloads.
      this._requestNotifPermission();
      this.$watch('notif.os',    v => localStorage.setItem('notif.os',    v ? '1' : '0'));
      this.$watch('notif.sound', v => localStorage.setItem('notif.sound', v ? '1' : '0'));
      // Persist enrichment toggles per-pipeline.
      ['swap', 'image', 'video'].forEach(k => {
        this.$watch(`enrich.${k}`, v => localStorage.setItem(`enrich.${k}`, v ? '1' : '0'));
      });
      // Persist 🎬 AI Director toggles per-pipeline.
      ['swap', 'image', 'video'].forEach(k => {
        this.$watch(`director.${k}`, v => localStorage.setItem(`director.${k}`, v ? '1' : '0'));
      });
      // Drive the Remotion preview: react to template / overrides / source-video
      // changes so the in-browser player mirrors what the server will render.
      this.$watch('editor.template', () => this._refreshRemotionPreview());
      this.$watch('editor.sourceVideo', () => this._refreshRemotionPreview());
      // overrides is a flat object; watch each field individually since Alpine's
      // string-path $watch only fires on direct property identity changes.
      ['font','size','primary_color','outline_color','words_per_card',
       'margin_v','margin_h','highlight_color','box','all_caps',
       'outline','shadow','alignment'].forEach(k => {
        this.$watch(`editor.overrides.${k}`, () => this._refreshRemotionPreview());
      });
      // Live preview: rerender Remotion when the caption editor's transcript
      // changes. Debounced (180ms) so per-keystroke retyping doesn't spam
      // re-mounts — Remotion's mount/unmount is cheap but not free.
      this.$watch('editor.captionEditOpen', () => this._refreshRemotionPreview());
      this.$watch('editor.editedWords', () => {
        if (this._editedWordsTimer) clearTimeout(this._editedWordsTimer);
        this._editedWordsTimer = setTimeout(() => this._refreshRemotionPreview(), 180);
      });
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

    // --- Chat tab -----------------------------------------------------------
    // Claude agent loop: user types → /api/chats/<id>/turn → Claude runs
    // tool_use loop until end_turn → we get back the full updated chat.
    // Inline media for each assistant turn is matched by walking forward
    // from the message's tool_use blocks → the tool_result that followed →
    // the chat.media entry it produced.

    async initChat() {
      if (this._chatInited) return;
      this._chatInited = true;
      await this.loadChatSessions();
      // Auto-select the most recent chat if any exist; otherwise show
      // the empty-state placeholder.
      if (this.chatSessions.length > 0 && !this.activeChat) {
        await this.selectChat(this.chatSessions[0].chat_id);
      }
    },

    async loadChatSessions() {
      try {
        const r = await fetch('/api/chats');
        if (!r.ok) return;
        this.chatSessions = await r.json();
      } catch { /* offline / not configured */ }
    },

    async newChat() {
      try {
        const r = await fetch('/api/chats', { method: 'POST' });
        if (!r.ok) {
          this.notifyError('Could not start chat: ' + await r.text());
          return;
        }
        const data = await r.json();
        this.chatSessions = [data, ...this.chatSessions];
        this.activeChat = data;
        this.chatInput = '';
      } catch (e) {
        this.notifyError('Chat init error: ' + e);
      }
    },

    async selectChat(chatId) {
      try {
        const r = await fetch(`/api/chats/${chatId}`);
        if (!r.ok) return;
        this.activeChat = await r.json();
        this.$nextTick(() => this._scrollChatToBottom());
      } catch { /* swallow */ }
    },

    async deleteChat(chatId) {
      if (!confirm('Delete this chat?')) return;
      try {
        await fetch(`/api/chats/${chatId}`, { method: 'DELETE' });
        this.chatSessions = this.chatSessions.filter(c => c.chat_id !== chatId);
        if (this.activeChat?.chat_id === chatId) {
          this.activeChat = null;
        }
      } catch (e) {
        this.notifyError('Delete failed: ' + e);
      }
    },

    async saveChatTitle() {
      if (!this.activeChat) return;
      try {
        await fetch(`/api/chats/${this.activeChat.chat_id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: this.activeChat.title }),
        });
        // Mirror new title into the session list.
        const row = this.chatSessions.find(c => c.chat_id === this.activeChat.chat_id);
        if (row) row.title = this.activeChat.title;
      } catch { /* swallow */ }
    },

    async sendChat() {
      if (!this.activeChat || !this.chatInput.trim() || this.chatPending) return;
      const msg = this.chatInput.trim();
      this.chatInput = '';
      // Optimistically render the user message so it shows up instantly.
      this.activeChat.messages = [...(this.activeChat.messages || []),
                                   { role: 'user', content: msg }];
      this.activeChat.n_messages = (this.activeChat.n_messages || 0) + 1;
      this.chatPending = true;
      this.chatPendingLabel = 'Claude is thinking…';
      this.$nextTick(() => this._scrollChatToBottom());

      try {
        const r = await fetch(`/api/chats/${this.activeChat.chat_id}/turn`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: msg }),
        });
        if (!r.ok) {
          this.notifyError('Chat turn failed: ' + await r.text());
          return;
        }
        this.activeChat = await r.json();
        // Update sidebar row (title may have changed; updated_at moved it to top).
        const idx = this.chatSessions.findIndex(c => c.chat_id === this.activeChat.chat_id);
        if (idx >= 0) {
          this.chatSessions[idx] = { ...this.chatSessions[idx],
                                     title: this.activeChat.title,
                                     n_messages: this.activeChat.n_messages,
                                     n_media: this.activeChat.n_media,
                                     updated_at: this.activeChat.updated_at };
          // Move to top.
          const [row] = this.chatSessions.splice(idx, 1);
          this.chatSessions.unshift(row);
        }
        this.notifyMilestone('Chat turn done',
          `${this.activeChat.n_messages} messages · ${this.activeChat.n_media} media`,
          { kind: 'done', tag: `chat-${this.activeChat.chat_id}` });
      } catch (e) {
        this.notifyError('Chat error: ' + e);
      } finally {
        this.chatPending = false;
        this.chatPendingLabel = '';
        this.$nextTick(() => this._scrollChatToBottom());
      }
    },

    // Walk forward from the assistant message at index `mi` to find the
    // matching tool_result blocks (which arrive as the NEXT message with
    // role=user, content=array). Each tool_result.tool_use_id maps back to
    // a tool_use block in `msg` — and the result's parsed content tells us
    // which chat.media URL to render.
    inlineMediaForMessage(mi) {
      const msg = this.activeChat?.messages?.[mi];
      if (!msg || msg.role !== 'assistant' || !Array.isArray(msg.content)) return [];
      const toolUses = msg.content.filter(b => b.type === 'tool_use');
      if (toolUses.length === 0) return [];
      const next = this.activeChat.messages[mi + 1];
      if (!next || next.role !== 'user' || !Array.isArray(next.content)) return [];
      const results = next.content.filter(b => b.type === 'tool_result');

      const out = [];
      for (const tr of results) {
        let parsed = null;
        try { parsed = JSON.parse(tr.content || '{}'); } catch { continue; }
        if (!parsed || !parsed.url || parsed.status !== 'done') continue;
        // Best-guess kind from the tool name. Falls back to "edit" or "image".
        const use = toolUses.find(u => u.id === tr.tool_use_id);
        const toolName = use?.name || '';
        let kind = 'image';
        if (toolName.includes('video')) kind = 'video';
        else if (toolName.includes('audio')) kind = 'audio';
        else if (toolName.includes('avatar')) kind = 'avatar';
        else if (toolName.includes('caption')) kind = 'edit';
        else if (toolName.includes('broll')) kind = 'video';
        out.push({
          kind, url: parsed.url,
          generation_id: parsed.generation_id || parsed.edit_id || parsed.job_id,
          model: parsed.model || parsed.template || '',
        });
      }
      return out;
    },

    _scrollChatToBottom() {
      const el = this.$refs?.chatLog;
      if (el) el.scrollTop = el.scrollHeight;
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
        // Image URL so the Image/Video tab prompt areas can pull it in as
        // a reference image. Prefer the specific image being dragged; fall
        // back to the character's primary image when dragging the card row.
        const url = img?.url || char.url;
        if (url) ev.dataTransfer.setData('text/x-charswap-image-url', url);
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
        // Re-assert the Reengineer engine pick AFTER models load. The <select>
        // renders before its options exist, so the browser auto-selects the
        // first option in the DOM without telling Alpine; setting state to
        // the SAME value wouldn't trigger a re-sync — bounce it through ''
        // so the change is observable and Alpine re-applies it to the DOM.
        // Preference order: the user's sticky localStorage pick → the preset
        // default gpt2-id-swap (Hugo's GPT engine of choice, 2026-06-11;
        // replaces the old nbp-swap default) → first available model.
        const _avail = slug => this.models.image?.find(m => m.slug === slug)?.available;
        const stored = localStorage.getItem('reengineerGen.imageModel');
        const reTarget = (stored && _avail(stored)) ? stored
          : _avail('gpt2-id-swap') ? 'gpt2-id-swap'
          : (firstImage ? firstImage.slug : this.reengineerGen.imageModel);
        this.reengineerGen.imageModel = '';
        this.$nextTick(() => { this.reengineerGen.imageModel = reTarget; });
        const firstVideo = (this.models.video || []).find(m => m.available);
        if (firstVideo && !this.models.video.find(m => m.slug === this.videoGen.model)?.available) {
          this.videoGen.model = firstVideo.slug;
        }
        // Initialize the Step-4 swap-flow duration picker to the selected
        // model's default. If a job is already loaded with a stored
        // duration_secs, syncDurationToModel() keeps it (still-valid).
        if (typeof this.syncDurationToModel === 'function') {
          this.syncDurationToModel();
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

    // Swap/Reengineer: aggregate image-phase progress across every variant
    // slot of a job — drives the "k/N images · m QC retries" counters in the
    // Step-3 header, the Reengineer run header, and the status toast.
    // retries counts qc_attempts beyond the first per slot (live while the
    // slot is still generating, since the runner bumps qc_attempts in place).
    jobImageProgress(job) {
      const vs = Object.values(job?.characters || {}).flatMap(c => c.images || []);
      if (!vs.length) return null;
      const ready = vs.filter(v => v.status === 'ready').length;
      const failed = vs.filter(v => v.status === 'failed').length;
      const retries = vs.reduce((a, v) => a + Math.max(0, (v.qc_attempts || 1) - 1), 0);
      return { total: vs.length, ready, failed, generating: vs.length - ready - failed, retries };
    },

    // Compact text form of jobImageProgress for headers/toast entries.
    jobImageProgressText(job) {
      const p = this.jobImageProgress(job);
      if (!p) return '';
      return `${p.ready + p.failed}/${p.total} images`
        + (p.failed ? ` (${p.failed} failed)` : '')
        + (p.retries ? ` · ${p.retries} QC ${p.retries === 1 ? 'retry' : 'retries'}` : '');
    },

    // Aggregate every in-flight job across tabs into one list for the
    // persistent status toast at the bottom-right. Each entry has:
    //   {kind, label, status, tab, navigate(): switch to its tab}
    get activeJobs() {
      // (Image/Video/Audio/Avatar/B-roll tabs were removed 2026-06-10 —
      // their histories are no longer aggregated here.)
      const out = [];
      for (const r of this.reengineerHistory) {
        if (!this._reengineerIsActive(r) && r.status !== 'awaiting_approval') continue;
        out.push({
          id: r.re_id, kind: 'reengineer', tab: 'reengineer',
          label: (r.source_name || r.re_id).slice(0, 50),
          status: r.status,
          // During the swap phase show live image progress; otherwise the
          // static scene count.
          progress: (r.status === 'swapping' && this.jobImageProgress(r.job))
            ? this.jobImageProgressText(r.job)
            : (r.n_scenes ? `${r.n_scenes} scenes` : ''),
          created_at: r.created_at,
        });
      }
      return out.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    },

    // Cross-kind list of recent finished media for the sidebar thumbnail
    // strip. Each entry has {kind, id, tab, thumb, label, created_at}.
    // Includes Image, Video, Audio, Avatar, and B-roll final outputs.
    // Sorted newest-first, capped to 50 for performance.
    get recentMedia() {
      // Only kinds whose tabs still exist are aggregated (the Image/Video/
      // Audio/Avatar/B-roll tabs were removed 2026-06-10 — a thumbnail that
      // jumps to a nonexistent tab would land on a blank page).
      const out = [];
      for (const r of this.reengineerHistory) {
        for (const [cid, f] of Object.entries(r.finals || {})) {
          if (f.final_url) out.push({
            kind: 'reengineer', id: r.re_id, tab: 'reengineer',
            thumb: f.final_url,
            label: (r.source_name || r.re_id).slice(0, 40),
            created_at: r.completed_at || r.created_at,
          });
        }
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
        // Snapshot statuses BEFORE the refetch so we can detect the
        // running→done|failed transition once and only once per gen.
        const prevStatusById = {};
        for (const g of active) prevStatusById[g.gen_id] = g.status;

        const results = await Promise.all(active.map(g =>
          fetch('/api/generations/' + g.gen_id).then(r => r.ok ? r.json() : null)
        ));
        for (const updated of results) {
          if (!updated) continue;
          const target = this._historyForKind(updated.kind);
          const idx = target.findIndex(g => g.gen_id === updated.gen_id);
          if (idx !== -1) target[idx] = updated;

          // Fire a milestone notification on the first transition from
          // pending/running → done|failed. _lastGenStatus is the durable
          // dedup key so a long poll loop doesn't re-fire on re-entry.
          const prev = prevStatusById[updated.gen_id];
          const seen = this._lastGenStatus[updated.gen_id];
          if ((prev === 'running' || prev === 'pending')
              && ['done', 'failed'].includes(updated.status)
              && seen !== updated.status) {
            this._lastGenStatus[updated.gen_id] = updated.status;
            const verb = updated.status === 'done' ? 'done' : 'failed';
            const label = (updated.prompt || updated.model || updated.gen_id || '')
                            .toString().slice(0, 80);
            this.notifyMilestone(
              `${updated.kind || 'gen'} ${verb}`,
              label || `${updated.gen_id}`,
              { kind: 'done', tag: `gen-${updated.gen_id}` },
            );
          }
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

    // --- Reengineer: video → scenes → swap → Kling clips → final ------------

    setReengineerSource(file) {
      if (!file) return;
      this.reengineerGen.file = file;
      this.reengineerGen.name = file.name;
    },

    setReengineerBackground(file) {
      if (!file) return;
      if (this.reengineerGen.backgroundUrl) URL.revokeObjectURL(this.reengineerGen.backgroundUrl);
      this.reengineerGen.background = file;
      this.reengineerGen.backgroundUrl = URL.createObjectURL(file);
    },

    // Thumbnail for a character chip: the picked reference image (outfit
    // choice) when overridden, else the primary.
    reengineerCharThumb(ch) {
      const picked = this.reengineerGen.sourceOverrides[ch.char_id];
      if (picked) {
        const img = (ch.images || []).find(i => i.image_id === picked);
        if (img) return img.url;
      }
      return ch.url;
    },

    pickReengineerSource(charId, imageId) {
      const ch = this.library.find(c => c.char_id === charId);
      if (ch && imageId === ch.primary_image_id) {
        const next = { ...this.reengineerGen.sourceOverrides };
        delete next[charId];
        this.reengineerGen.sourceOverrides = next;
      } else {
        this.reengineerGen.sourceOverrides = {
          ...this.reengineerGen.sourceOverrides, [charId]: imageId };
      }
      this.reengineerPickerChar = null;
    },

    toggleReengineerChar(cid) {
      const ids = this.reengineerGen.charIds;
      const i = ids.indexOf(cid);
      if (i >= 0) ids.splice(i, 1); else ids.push(cid);
    },

    async submitReengineer() {
      const g = this.reengineerGen;
      if (!g.file || !g.charIds.length || g.submitting) return;
      if (g.outfitMode === 'custom' && !g.outfitText.trim()) {
        this.notifyError('Describe the outfit (or pick another outfit option)');
        return;
      }
      g.submitting = true;
      try {
        const fd = new FormData();
        fd.append('file', g.file);
        fd.append('character_ids', JSON.stringify(g.charIds));
        fd.append('image_model', g.imageModel);
        fd.append('auto_mode', g.autoMode ? 'true' : 'false');
        fd.append('use_director',
                  (g.useDirector && this.health.anthropic_key) ? 'true' : 'false');
        fd.append('outfit_mode', g.outfitMode);
        fd.append('outfit_text', g.outfitText || '');
        fd.append('scene_sensitivity', g.sceneSensitivity);
        if (g.background) fd.append('background_file', g.background);
        const pickedOverrides = {};
        for (const cid of g.charIds) {
          if (g.sourceOverrides[cid]) pickedOverrides[cid] = g.sourceOverrides[cid];
        }
        if (Object.keys(pickedOverrides).length) {
          fd.append('character_source_image_ids', JSON.stringify(pickedOverrides));
        }
        const r = await fetch('/api/reengineer', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Reengineer failed: ' + await r.text()); return; }
        const state = await r.json();
        this.reengineerHistory = [state, ...this.reengineerHistory.filter(x => x.re_id !== state.re_id)];
        this.notifyInfo('Reengineering started — analyzing scenes…');
        this._startReengineerPolling();
        // Keep the form: Hugo iterates. Only the file is consumed.
        g.file = null; g.name = '';
      } finally {
        g.submitting = false;
      }
    },

    async loadReengineerHistory() {
      try {
        const r = await fetch('/api/reengineer');
        if (!r.ok) return;
        const list = await r.json();
        // The list view is light (no scenes/job) — fetch details for visible runs.
        this.reengineerHistory = list;
        for (const row of list.slice(0, 8)) this.refreshReengineer(row.re_id);
        if (list.some(x => this._reengineerIsActive(x))) this._startReengineerPolling();
      } catch (_) {}
    },

    _reengineerIsActive(r) {
      // awaiting_approval / finished runs count as active while edit-mode
      // work is in flight (per-slot retries, added-scene images, clip redos)
      // — otherwise the poll filter drops the run and nothing repaints.
      return ['queued', 'analyzing', 'swapping', 'animating', 'reanimating', 'assembling'].includes(r.status)
        || (['awaiting_approval', 'awaiting_assembly', 'done', 'partial_success', 'failed'].includes(r.status)
            && this._reengineerHasInFlight(r));
    },

    _reengineerHasInFlight(r) {
      return Object.values(r.job?.characters || {})
        .some(c => (c.images || []).some(v => v.status === 'generating')
                || (c.videos || []).some(v => ['pending', 'processing'].includes(v.status)));
    },

    // Live updates: subscribe to the existing per-job WebSocket (the runner
    // emits variant.started/ready/failed/qc_retry/fallback on it) and turn
    // any event into a debounced slim refetch. The 5s poll stays as the
    // fallback for reengineer-level transitions (swapping→awaiting_approval
    // is written to state.json by the watcher, not emitted over WS).
    _ensureReengineerWS(run) {
      const jid = run.job_id;
      if (!jid || this._reengineerSockets[jid]) return;
      const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
        + '/ws/jobs/' + jid;
      const ws = new WebSocket(url);
      this._reengineerSockets[jid] = ws;
      ws.onmessage = () => this._debouncedReengineerRefresh(run.re_id);
      ws.onclose = () => { delete this._reengineerSockets[jid]; };
      ws.onerror = () => {};
    },

    _closeReengineerWS(jobId) {
      const ws = this._reengineerSockets[jobId];
      if (ws) { try { ws.close(); } catch (_) {} delete this._reengineerSockets[jobId]; }
    },

    _debouncedReengineerRefresh(reId) {
      clearTimeout(this._reRefreshTimers[reId]);
      this._reRefreshTimers[reId] = setTimeout(() => this.refreshReengineer(reId), 400);
    },

    async refreshReengineer(reId) {
      try {
        // slim=1: variant prompts (~3.3KB × 45) are never rendered here.
        const r = await fetch('/api/reengineer/' + reId + '?slim=1');
        if (!r.ok) return;
        const fresh = await r.json();
        const i = this.reengineerHistory.findIndex(x => x.re_id === reId);
        const prev = i >= 0 ? this.reengineerHistory[i] : null;
        if (i >= 0) this.reengineerHistory.splice(i, 1, fresh);
        else this.reengineerHistory.unshift(fresh);
        // Keep the WS while edit-mode work is in flight on a finished run.
        if (['done', 'partial_success', 'failed'].includes(fresh.status)
            && !this._reengineerHasInFlight(fresh)) {
          if (fresh.job_id) this._closeReengineerWS(fresh.job_id);
        } else if (fresh.job_id) {
          this._ensureReengineerWS(fresh);
        }
        if (prev && prev.status !== fresh.status) {
          if (fresh.status === 'awaiting_approval') {
            this.notifyMilestone('Reengineer — review swapped images',
              (fresh.source_name || reId) + ' is ready for approval',
              { kind: 'approval', tag: 're-approve-' + reId });
          } else if (fresh.status === 'awaiting_assembly') {
            this.notifyMilestone('Reengineer — granska klippen',
              (fresh.source_name || reId) + ' — alla Kling-klipp är klara; granska och bygg ihop',
              { kind: 'approval', tag: 're-clips-' + reId });
          } else if (prev.status === 'reanimating'
                     && ['done', 'partial_success', 'failed'].includes(fresh.status)) {
            this.notifyMilestone('Re-animation klar',
              (fresh.source_name || reId) + ' — bygg ihop finalerna igen',
              { kind: 'approval', tag: 're-reanim-' + reId });
          } else if (['done', 'partial_success', 'failed'].includes(fresh.status)) {
            this.notifyMilestone('Reengineer ' + fresh.status,
              fresh.source_name || reId, { tag: 're-done-' + reId });
          }
        }
      } catch (_) {}
    },

    _startReengineerPolling() {
      if (this._reengineerPollTimer) return;
      this._reengineerPollTimer = setInterval(async () => {
        const active = this.reengineerHistory.filter(x => this._reengineerIsActive(x));
        if (!active.length) {
          clearInterval(this._reengineerPollTimer);
          this._reengineerPollTimer = null;
          return;
        }
        for (const x of active) await this.refreshReengineer(x.re_id);
      }, 5000);
    },

    // Approve/unapprove a swapped variant on the underlying Swap job (same
    // endpoint the Swap tab uses), then refresh the run so the ✓ updates.
    async reengineerApprove(run, charId, variantId) {
      if (!run.job_id) return;
      const r = await fetch(`/api/jobs/${run.job_id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ char_id: charId, action: 'approve', variant_id: variantId }),
      });
      if (!r.ok) { this.notifyError('Approve failed: ' + await r.text()); return; }
      // Swapping the approved image on an already-animated scene makes its
      // existing clip stale — flag it so "Animera om ändrade" picks it up.
      await this._reMarkVariantSceneDirty(run, charId, variantId);
      await this.refreshReengineer(run.re_id);
    },

    // Retry one failed slot in a reengineer run — same endpoint as the Swap
    // tab's per-variant ↻ (keeps the slot in place, regenerates only it).
    // Human-readable one-liner for a failed variant slot — shown as visible
    // text in the strip (the tooltip-only error was invisible on iPhone).
    // The full raw error stays in the title attribute.
    variantFailText(v) {
      const e = v.error || '';
      if (/moderation_blocked|safety system/i.test(e)) {
        const m = e.match(/categories[^\[]*\[([^\]]*)\]/);
        const cat = m ? m[1].replace(/['" ]/g, '') : '';
        return '⛔ OpenAI:s säkerhetssystem blockerade bilden'
          + (cat ? ` (${cat})` : '') + ' — ↻ funkar ofta, annars ✎↻ eller ⬆';
      }
      if (/interrupted \(server restart\)/i.test(e)) {
        return 'avbruten av serveromstart — ↻ för att köra om';
      }
      if (/timeout|timed out/i.test(e)) return 'timeout — ↻ för att köra om';
      if (/rate.?limit|429/i.test(e)) return 'rate-limit hos leverantören — vänta en stund och ↻';
      if (!e) return 'genereringen misslyckades — ↻ för att köra om';
      return e.slice(0, 110);
    },

    async reengineerRetryVariant(run, charId, variantId) {
      if (!run.job_id) return;
      const r = await fetch(
        `/api/jobs/${run.job_id}/characters/${charId}/variants/${variantId}/retry`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}) });
      if (!r.ok) { this.notifyError('Retry failed: ' + await r.text()); return; }
      this.notifyInfo('Regenerating this image — a fresh take is on its way…');
      // Cache-buster: the retry regenerates into the SAME file path.
      this.reengineerRetryNonce = { ...this.reengineerRetryNonce, [variantId]: Date.now() };
      // Optimistically flip the slot so the poll filter sees the run as
      // in-flight even from awaiting_approval (and the skeleton shows now).
      const slot = (run.job?.characters?.[charId]?.images || [])
        .find(v => v.variant_id === variantId);
      if (slot) slot.status = 'generating';
      // A regenerated image differs from the clip that animated the old one.
      await this._reMarkVariantSceneDirty(run, charId, variantId);
      this._startReengineerPolling();
      await this.refreshReengineer(run.re_id);
    },

    async reengineerApproveAll(run) {
      if (!run.job_id) return;
      const r = await fetch(`/api/jobs/${run.job_id}/approve_all`, { method: 'POST' });
      if (!r.ok) { this.notifyError('Approve all failed: ' + await r.text()); return; }
      await this.refreshReengineer(run.re_id);
    },

    // ⚙ Slutvideo (Editor) settings for the Reengineer final build. Sent
    // with BOTH ▶ Generate videos (persisted server-side so the automatic
    // assemble after the video phase uses them) and ▶ Bygg ihop igen.
    // Defaults mirror Swap Step 6 EXCEPT voice swap + WPM normalize, which
    // stay OFF so Kling's own lip-synced voice and pacing survive.
    _reAsmBody() {
      try {
        localStorage.setItem('reassemble.settings.v1', JSON.stringify(this.reAsmSettings));
      } catch (_) { /* private window etc. */ }
      const s = this.reAsmSettings;
      return {
        template: s.template,
        // Caption size rides as a style override (works for both caption
        // engines). Clamped so a typo can't render unreadable captions.
        overrides: { size: Math.min(200, Math.max(24, Number(s.captionSize) || 68)) },
        enable_trim: !!s.enableTrim,
        enable_captions: !!s.enableCaptions,
        enable_wpm_normalize: !!s.enableWpmNormalize,
        // Clamp to the server's ge=80/le=400 — a typed out-of-range value
        // would otherwise 422-block ▶ Generate videos on every click.
        target_wpm: Math.min(400, Math.max(80, Number(s.targetWpm) || 190)),
        enable_voice_swap: !!s.enableVoiceSwap,
        voice_override: s.voiceOverride || '',
      };
    },

    async reengineerAnimate(run) {
      const r = await fetch(`/api/reengineer/${run.re_id}/animate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this._reAsmBody()),
      });
      if (!r.ok) { this.notifyError('Animate failed: ' + await r.text()); return; }
      this.notifyInfo('Generating Kling clips with native audio…');
      run.status = 'animating';
      this._startReengineerPolling();
    },

    async reengineerAssemble(run) {
      const r = await fetch(`/api/reengineer/${run.re_id}/assemble`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this._reAsmBody()),
      });
      if (!r.ok) { this.notifyError('Assemble failed: ' + await r.text()); return; }
      run.status = 'assembling';
      this._startReengineerPolling();
    },

    async deleteReengineer(run) {
      if (!confirm('Delete this reengineer run (files + state)?')) return;
      const r = await fetch('/api/reengineer/' + run.re_id, { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete failed: ' + await r.text()); return; }
      this.reengineerHistory = this.reengineerHistory.filter(x => x.re_id !== run.re_id);
    },

    // ---------------------------------------------------------- edit mode

    reEditable(r) {
      return ['awaiting_approval', 'awaiting_assembly',
              'done', 'partial_success', 'failed'].includes(r.status);
    },

    // The approve/Generate-videos gate bar. Besides awaiting_approval it
    // must also show on a FAILED run whose video phase never produced
    // anything (e.g. animate was clicked with zero approvals) — the animate
    // endpoint accepts 'failed', but the button used to be hidden, leaving
    // the user stuck with 45 ready images and no way forward.
    reengineerGateVisible(r) {
      if (r.status === 'awaiting_approval') return true;
      if (r.status !== 'failed' || !r.job) return false;
      const chars = Object.values(r.job.characters || {});
      const noVideos = chars.every(c => !(c.videos || []).length);
      const anyReady = chars.some(c => (c.images || []).some(v => v.status === 'ready'));
      return noVideos && anyReady;
    },

    toggleReengineerEdit(run) {
      this.reEdit = { ...this.reEdit, [run.re_id]: !this.reEdit[run.re_id] };
    },

    _spliceReengineerView(view) {
      const i = this.reengineerHistory.findIndex(x => x.re_id === view.re_id);
      if (i >= 0) this.reengineerHistory.splice(i, 1, view);
    },

    // Draft-or-state read/write — drafts survive the 5s poll splice.
    reSceneVal(run, sc, field) {
      const d = this.reSceneDrafts[run.re_id + ':' + sc.idx];
      return (d && d[field] !== undefined) ? d[field] : sc[field];
    },

    // EXACT mirror of runner_reengineer._kling_duration: the whole-second
    // clip length Kling actually gets — the scene's original length OR the
    // time the dialogue needs (words / 2.2 wps + 1.0s margin, parsed from
    // the says-clause / speech field), whichever is longer, rounded UP and
    // clamped to [3, 15]. A pytest keeps the constants in sync.
    klingDuration(run, sc) {
      // A manual override (the editable "Kling s" field) wins outright;
      // AUTO = the original scene clip's length rounded UP to the
      // SECOND-next whole second, ceil + 1 (Hugo 2026-06-13: 6.4s → 8s;
      // mirror of _kling_duration — no speech extension).
      const override = Number(this.reSceneVal(run, sc, 'kling_secs')) || 0;
      if (override) return Math.max(3, Math.min(15, Math.ceil(override - 1e-9)));
      const dur = Number(this.reSceneVal(run, sc, 'duration')) || 0;
      return Math.max(3, Math.min(15, Math.ceil(dur - 1e-9) + 1));
    },

    // Seconds the dialogue needs to be spoken (words / 2.2 + 1.0 — mirror
    // of _speech_secs). HINT only: shown as ⚠ at the gate when it exceeds
    // the clip length, so the user can bump the Kling field deliberately.
    klingSpeechSecs(run, sc) {
      const prompt = String(this.reSceneVal(run, sc, 'motion_prompt') || '');
      const m = [...prompt.matchAll(/says[^"“”]{0,160}?["“]([^"”]+)["”]/gi)];
      const spoken = (m.map(x => x[1]).join(' ').trim() || String(sc.speech || '').trim());
      const words = spoken ? spoken.split(/\s+/).length : 0;
      return words ? words / 2.2 + 1.0 : 0;
    },

    // Gate coverage + cost preview (backlog #32): unapproved (char × scene)
    // slots silently vanish from the finals — surface exactly what the
    // animate click will generate and bill BEFORE the expensive step.
    reGateCoverage(r) {
      const chars = Object.values(r.job?.characters || {});
      const scenes = r.scenes || [];
      const firstSid = (scenes[0] || {}).scene_id;
      let approved = 0, total = 0, secs = 0;
      const missing = [];
      for (const sc of scenes) {
        for (const jc of chars) {
          total += 1;
          const appr = new Set([...(jc.approved_variant_ids || []),
                                ...(jc.approved_variant_id ? [jc.approved_variant_id] : [])]);
          const ok = (jc.images || []).some(v =>
            appr.has(v.variant_id) && (v.scene_id || firstSid) === sc.scene_id);
          if (ok) { approved += 1; secs += this.klingDuration(r, sc); }
          else missing.push((jc.name || jc.char_id) + ' × scen ' + (sc.idx + 1));
        }
      }
      return { approved, total, secs, missing };
    },

    // EXACT mirror of runner_reengineer._with_accent: the only thing the
    // backend adds to a Reengineer motion prompt before it reaches Kling.
    // Shown live under the gate textarea so "the prompt you see" + this
    // suffix == the literal Kling input. A pytest keeps the clause strings
    // byte-identical with the Python side — edit both together.
    klingSuffix(text) {
      let out = String(text || '');
      let suffix = '';
      if (!out.toLowerCase().includes('american')) {
        const clause = ' The person speaks fluent American English with a natural American accent.';
        out = out.replace(/\s+$/, '') + clause;
        suffix += clause;
      }
      if (!out.toLowerCase().includes('pronounc')) {
        const clause = ' Every word is pronounced clearly, correctly and distinctly.';
        out = out.replace(/\s+$/, '') + clause;
        suffix += clause;
      }
      if (!out.toLowerCase().includes('music')) {
        const clause = ' No background music — natural ambient room sound only.';
        suffix += clause;
      }
      return suffix.trim();
    },

    reSceneEdit(run, sc, field, value) {
      const key = run.re_id + ':' + sc.idx;
      this.reSceneDrafts = {
        ...this.reSceneDrafts,
        [key]: { ...(this.reSceneDrafts[key] || {}), [field]: value },
      };
    },

    async reengineerSaveScene(run, sc) {
      const key = run.re_id + ':' + sc.idx;
      const draft = this.reSceneDrafts[key];
      if (!draft) return;
      const body = {};
      if (draft.motion_prompt !== undefined
          && draft.motion_prompt !== sc.motion_prompt) body.motion_prompt = draft.motion_prompt;
      if (draft.duration !== undefined
          && Number(draft.duration) !== sc.duration) body.duration = Number(draft.duration);
      if (draft.kling_secs !== undefined
          && Number(draft.kling_secs || 0) !== (sc.kling_secs || 0)) {
        body.kling_secs = Number(draft.kling_secs) || 0;   // 0 = clear → auto
      }
      if (!Object.keys(body).length) {
        const { [key]: _, ...rest } = this.reSceneDrafts;
        this.reSceneDrafts = rest;
        return;
      }
      const r = await fetch(`/api/reengineer/${run.re_id}/scenes/${sc.idx}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { this.notifyError('Kunde inte spara scenen: ' + await r.text()); return; }
      this._spliceReengineerView(await r.json());
      const { [key]: _, ...rest } = this.reSceneDrafts;
      this.reSceneDrafts = rest;
    },

    // Upload an image created ELSEWHERE as a variant for one (char × scene)
    // slot (Hugo 2026-06-12). Server marks it READY (QC skipped) and
    // auto-approves it for the scene, replacing the previous approval.
    async reengineerUploadOwnImage(run, charId, sc, ev) {
      const f = ev.target.files?.[0];
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f);
      fd.append('scene_id', sc.scene_id);
      const r = await fetch(`/api/jobs/${run.job_id}/characters/${charId}/variants/upload`,
                            { method: 'POST', body: fd });
      ev.target.value = '';
      if (!r.ok) {
        this.notifyError('Uppladdningen misslyckades: ' + await r.text());
        return;
      }
      const data = await r.json();
      if (data.job) run = Object.assign(run, { job: data.job });
      await this._reMarkVariantSceneDirty(run, charId, data.variant_id);
      this.notify('info', 'Egen bild uppladdad och godkänd för scenen.');
    },

    // After approve-swaps / image-regens on an ALREADY-ANIMATED scene, the
    // existing clip no longer matches the chosen image — flag the scene.
    async _reMarkVariantSceneDirty(run, charId, variantId) {
      // awaiting_assembly included (review 2026-06-13): approving a
      // regenerated image at the clip-review gate must re-flag the scene,
      // or the old clip silently ships with the new image.
      if (!['done', 'partial_success', 'failed',
            'awaiting_assembly'].includes(run.status)) return;
      const v = (run.job?.characters?.[charId]?.images || [])
        .find(x => x.variant_id === variantId);
      const idx = (run.scenes || []).findIndex(sc => sc.scene_id === v?.scene_id);
      if (idx < 0) return;
      try {
        const r = await fetch(`/api/reengineer/${run.re_id}/scenes/${idx}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dirty: true }),
        });
        if (r.ok) this._spliceReengineerView(await r.json());
      } catch (_) {}
    },

    reengineerAddSceneFile(run, file) {
      if (!file) return;
      this.reAdd = {
        ...this.reAdd,
        [run.re_id]: {
          file, name: file.name,
          isVideo: /\.(mp4|mov|webm)$/i.test(file.name),
          whisper: false, prompt: '', submitting: false,
        },
      };
    },

    async reengineerSubmitAddScene(run) {
      const a = this.reAdd[run.re_id];
      if (!a || !a.file || a.submitting) return;
      a.submitting = true;
      const fd = new FormData();
      fd.append('file', a.file);
      fd.append('motion_prompt', a.prompt || '');
      fd.append('duration', '0');
      fd.append('whisper', a.whisper ? 'true' : 'false');
      fd.append('position', '-1');
      const r = await fetch(`/api/reengineer/${run.re_id}/scenes`, {
        method: 'POST', body: fd,
      });
      if (!r.ok) {
        this.notifyError('Kunde inte lägga till scen: ' + await r.text());
        a.submitting = false;
        return;
      }
      this._spliceReengineerView(await r.json());
      const { [run.re_id]: _, ...rest } = this.reAdd;
      this.reAdd = rest;
      this.notifyInfo('Scen tillagd — bilder genereras för varje karaktär. Godkänn dem, sen ▶ Animera om.');
      this._startReengineerPolling();
    },

    async reengineerDuplicateScene(run, sc) {
      const r = await fetch(`/api/reengineer/${run.re_id}/scenes/${sc.idx}/duplicate`,
                            { method: 'POST' });
      if (!r.ok) { this.notifyError('Kunde inte duplicera: ' + await r.text()); return; }
      this._spliceReengineerView(await r.json());
      this.notifyInfo('Scen duplicerad (gratis — bilderna återanvänds). Redigera kopians prompt, sen ▶ Animera om.');
    },

    async reengineerDeleteScene(run, sc) {
      if (!confirm(`Ta bort scen ${sc.idx + 1} ur finalen?`)) return;
      const r = await fetch(`/api/reengineer/${run.re_id}/scenes/${sc.idx}`,
                            { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Kunde inte ta bort: ' + await r.text()); return; }
      this._spliceReengineerView(await r.json());
    },

    async reengineerMoveScene(run, sc, dir) {
      const n = (run.scenes || []).length;
      const j = sc.idx + dir;
      if (j < 0 || j >= n) return;
      const order = [...Array(n).keys()];
      [order[sc.idx], order[j]] = [order[j], order[sc.idx]];
      const r = await fetch(`/api/reengineer/${run.re_id}/scene_order`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
      });
      if (!r.ok) { this.notifyError('Kunde inte flytta: ' + await r.text()); return; }
      this._spliceReengineerView(await r.json());
    },

    // The clip a (scene × character) pair would contribute to the final:
    // JS mirror of the backend's approved-variant resolution.
    reengineerClipFor(run, sc, cid) {
      const jc = run.job?.characters?.[cid];
      if (!jc) return null;
      const approved = new Set(jc.approved_variant_ids || []);
      const variant = (jc.images || [])
        .find(v => approved.has(v.variant_id) && v.scene_id === sc.scene_id);
      if (!variant) return { variant: null, video: null };
      const video = (jc.videos || [])
        .find(v => v.source_variant_id === variant.variant_id) || null;
      return { variant, video };
    },

    async reengineerRedoClip(run, sc, cid) {
      const r = await fetch(`/api/reengineer/${run.re_id}/scenes/${sc.idx}/redo`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ char_id: cid }),
      });
      if (!r.ok) { this.notifyError('Kunde inte göra om klippet: ' + await r.text()); return; }
      this.notifyInfo('Ny tagning på väg (~2 min)…');
      run.status = 'reanimating';
      this._startReengineerPolling();
    },

    async reengineerRedoScene(run, sc) {
      if (!confirm(`Ta om scen ${sc.idx + 1} för ALLA karaktärer? (en Kling-rendering per karaktär)`)) return;
      const r = await fetch(`/api/reengineer/${run.re_id}/scenes/${sc.idx}/redo`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!r.ok) { this.notifyError('Kunde inte ta om scenen: ' + await r.text()); return; }
      this.notifyInfo('Nya tagningar på väg för alla karaktärer…');
      run.status = 'reanimating';
      this._startReengineerPolling();
    },

    reengineerDirtyCount(run) {
      return (run.scenes || []).filter(sc => sc.dirty).length;
    },

    async reengineerAnimateDirty(run) {
      const r = await fetch(`/api/reengineer/${run.re_id}/animate_scenes`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!r.ok) { this.notifyError('Kunde inte animera om: ' + await r.text()); return; }
      const out = await r.json();
      if ((out.skipped || []).length) {
        this.notifyInfo(`${out.skipped.length} (scen × karaktär)-par hoppas över — bilden är inte godkänd än.`);
      }
      this.notifyInfo(`Animerar om ${out.idxs.length} scen(er)…`);
      run.status = 'reanimating';
      this._startReengineerPolling();
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
        const prevStatus = (i >= 0) ? this.brollHistory[i].status : null;
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
        // Milestone: clips are done & b-roll is waiting for Hugo's approval
        // before the optional finalize step. Or the whole b-roll is done.
        const APPROVAL = 'awaiting_approval';
        const TERMINAL = ['done', 'partial_success', 'failed'];
        if (prevStatus !== APPROVAL && fresh.status === APPROVAL) {
          this.notifyMilestone('B-roll ready — review clips',
            `${fresh.broll_id}: pick which clips to keep before finalize`,
            { kind: 'approval', tag: `broll-${fresh.broll_id}-approve` });
        } else if (!TERMINAL.includes(prevStatus) && TERMINAL.includes(fresh.status)) {
          const verb = fresh.status === 'done' ? 'done'
                       : fresh.status === 'partial_success' ? 'done (partial)'
                       : 'failed';
          this.notifyMilestone(`B-roll ${verb}`,
            `${fresh.broll_id}: final video ready`,
            { kind: 'done', tag: `broll-${fresh.broll_id}-done` });
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

    // Image models offered in the SWAP flow. Google/Gemini models (Nano
    // Banana, Nano Banana Pro) are intentionally EXCLUDED from Swap — they
    // remain available in the standalone Image tab.
    swapImageModels() {
      return ((this.models && this.models.image) || [])
        .filter(m => m.provider !== 'gemini');
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
        this.notifyMilestone('Trim done',
          `${data.saved_secs}s removed (${data.n_cuts} segments kept)`,
          { kind: 'done', tag: 'editor-trim' });
      } finally {
        this.editor.trimming = false;
      }
    },

    _activeOverrides() {
      const o = this.editor.overrides;
      const out = {};
      // `*_color_hex` are UI-only mirrors — the backend wants the ASS BGR
      // format in the canonical fields. Drop the hex twins before sending.
      const skip = new Set(['highlight_color_hex', 'outline_color_hex']);
      for (const k of Object.keys(o)) {
        if (skip.has(k)) continue;
        if (o[k] !== null && o[k] !== '') out[k] = o[k];
      }
      return out;
    },

    // Color conversion helpers — the Style tab's color picker speaks CSS hex
    // (#RRGGBB) but our CaptionStyle / ASS templates store colors as BGR
    // (&H00BBGGRR). These two convert between the two so the UI swatch and
    // the persisted override stay in sync.
    _assToHex(ass) {
      if (!ass || typeof ass !== 'string') return '#8B5CF6';
      const hex = ass.replace(/^&H/i, '').padStart(8, '0').toUpperCase();
      // ASS &HAABBGGRR: skip alpha, then swap BGR → RGB.
      const bb = hex.slice(2, 4), gg = hex.slice(4, 6), rr = hex.slice(6, 8);
      return `#${rr}${gg}${bb}`;
    },
    _hexToAss(hex) {
      if (!hex || typeof hex !== 'string') return '&H008B5CF6';
      const h = hex.replace(/^#/, '').toUpperCase().padStart(6, '0');
      const rr = h.slice(0, 2), gg = h.slice(2, 4), bb = h.slice(4, 6);
      return `&H00${bb}${gg}${rr}`;
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
        this.notifyMilestone('Captions done',
          `${data.n_words} words · ${data.template}`,
          { kind: 'done', tag: 'editor-captions' });
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
        // If the user edited captions, send the modified words back so the
        // server persists + re-renders against THEM. Skip when unchanged
        // to keep the rerender path identical to the no-edit case.
        if (this.editor.captionEditOpen && this.editor.editedWords.length) {
          const cleanWords = this._editedWordsForPost();
          if (cleanWords) fd.append('words_json', JSON.stringify(cleanWords));
        }
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
        this.notifyMilestone('Rerender done',
          `v${data.version} · ${data.template}`,
          { kind: 'done', tag: `editor-rerender-${data.version}` });
      } finally {
        this.editor.rerendering = false;
      }
    },

    // --- CapCut/Submagic-style caption editor ---------------------------
    //
    // The transcript comes back from Whisper as word-level `{text, start, end}`
    // entries. The user can now:
    //   - retune per-word start/end (precision drag handles in the UI)
    //   - fix misheard words inline
    //   - split / merge words via the per-word panel
    // Submit triggers /api/editor/rerender with `words_json=...` which
    // persists the edits back to `words.json` so subsequent rerenders pick
    // them up automatically.

    openCaptionEditor() {
      const words = this.editor.lastResult?.captions?.words
        || this.editor.lastResult?.words
        || [];
      if (!words.length) {
        this.notifyError('No transcript on this render to edit');
        return;
      }
      // Deep clone so live mutations don't mutate `lastResult.words`.
      this.editor.editedWords = words.map(w => ({
        text: String(w.text || ''),
        start: Number(w.start) || 0,
        end: Number(w.end) || 0,
      }));
      this.editor.captionEditOpen = true;
      this.playheadSecs = 0;
      // Make sure the rerender panel is open too — the Save button lives
      // inside that flow.
      this.editor.rerenderOpen = true;
      // Subscribe to the Remotion Player's frameupdate so the playhead
      // follows playback automatically.
      this._attachPlayheadFollower();
    },

    closeCaptionEditor() {
      this.editor.captionEditOpen = false;
      this._detachPlayheadFollower();
    },

    revertCaptionEdits() {
      const words = this.editor.lastResult?.captions?.words
        || this.editor.lastResult?.words
        || [];
      this.editor.editedWords = words.map(w => ({
        text: String(w.text || ''),
        start: Number(w.start) || 0,
        end: Number(w.end) || 0,
      }));
    },

    // Group editedWords into "cards" the way the renderer will. Mirrors the
    // backend's `_group_words` so the UI shows lines exactly as they'll
    // appear on the rendered video. `words_per_card` honors the user's
    // override; falls back to the template default.
    captionCards() {
      const perCard = Math.max(1, parseInt(
        this.editor.rerenderOverrides.words_per_card
          ?? this._activeOverrides().words_per_card
          ?? this._currentTemplateInfo()?.words_per_card
          ?? 3,
      10) || 3);
      // Mirrors video_edit.CARD_GAP_BREAK_SECS — a card never spans a real
      // pause/scene join. A pytest keeps the constant in sync.
      const GAP_BREAK_SECS = 0.8;
      const cards = [];
      let startIdx = 0;
      let slice = [];
      const flush = (endIdx) => {
        if (!slice.length) return;
        cards.push({
          startIdx,
          endIdx,
          start: slice[0].start,
          end: slice[slice.length - 1].end,
          words: slice,
        });
        slice = [];
      };
      this.editor.editedWords.forEach((w, i) => {
        if (slice.length && (slice.length >= perCard
            || w.start - slice[slice.length - 1].end > GAP_BREAK_SECS)) {
          flush(i - 1);
          startIdx = i;
        }
        slice.push(w);
      });
      flush(this.editor.editedWords.length - 1);
      return cards;
    },

    _currentTemplateInfo() {
      const slug = this.editor.rerenderTemplate || this.editor.template;
      return (this.editorTemplates || []).find(t => t.slug === slug);
    },

    // Adjust a single word's start time (clamped between previous word's
    // end and this word's own end). Used by the per-word drag handles.
    setWordStart(idx, newStart) {
      const ws = this.editor.editedWords;
      if (idx < 0 || idx >= ws.length) return;
      const min = idx === 0 ? 0 : ws[idx - 1].end;
      const max = ws[idx].end - 0.02;  // keep a tiny non-zero duration
      ws[idx].start = Math.min(max, Math.max(min, parseFloat(newStart) || 0));
    },

    setWordEnd(idx, newEnd) {
      const ws = this.editor.editedWords;
      if (idx < 0 || idx >= ws.length) return;
      const min = ws[idx].start + 0.02;
      const max = idx === ws.length - 1
        ? (ws[idx].end + 60)  // open-ended at the end (clamped to +60s)
        : ws[idx + 1].start;
      ws[idx].end = Math.min(max, Math.max(min, parseFloat(newEnd) || 0));
    },

    setWordText(idx, newText) {
      const ws = this.editor.editedWords;
      if (idx < 0 || idx >= ws.length) return;
      ws[idx].text = String(newText || '');
    },

    // Split one word into two equal-time halves at its midpoint. Useful
    // when Whisper merged two words. Inserts a placeholder right after.
    splitWord(idx) {
      const ws = this.editor.editedWords;
      if (idx < 0 || idx >= ws.length) return;
      const w = ws[idx];
      const mid = (w.start + w.end) / 2;
      const newWord = { text: '', start: mid, end: w.end };
      ws[idx] = { ...w, end: mid };
      ws.splice(idx + 1, 0, newWord);
    },

    // Merge a word into its previous neighbor (concatenate text + extend
    // the previous word's end to swallow this one).
    mergeWordLeft(idx) {
      const ws = this.editor.editedWords;
      if (idx <= 0 || idx >= ws.length) return;
      const cur = ws[idx];
      const prev = ws[idx - 1];
      prev.text = `${prev.text} ${cur.text}`.trim();
      prev.end = cur.end;
      ws.splice(idx, 1);
    },

    removeWord(idx) {
      const ws = this.editor.editedWords;
      if (idx < 0 || idx >= ws.length) return;
      ws.splice(idx, 1);
    },

    // True iff editedWords differs from the original lastResult.words —
    // gates the "Save & re-render" button and the "Unsaved" badge.
    captionEditsDirty() {
      const orig = this.editor.lastResult?.captions?.words
        || this.editor.lastResult?.words
        || [];
      const cur = this.editor.editedWords || [];
      if (orig.length !== cur.length) return true;
      for (let i = 0; i < cur.length; i++) {
        const a = orig[i], b = cur[i];
        if (!a || a.text !== b.text) return true;
        if (Math.abs((a.start || 0) - (b.start || 0)) > 0.005) return true;
        if (Math.abs((a.end || 0) - (b.end || 0)) > 0.005) return true;
      }
      return false;
    },

    // Sanitize before posting: drop blanks, sort by start. Returns null
    // when nothing meaningful changed (so submitRerender doesn't send a
    // pointless words_json field).
    _editedWordsForPost() {
      if (!this.captionEditsDirty()) return null;
      const cleaned = (this.editor.editedWords || [])
        .map(w => ({
          text: String(w.text || '').trim(),
          start: Math.max(0, Number(w.start) || 0),
          end: Math.max(0, Number(w.end) || 0),
        }))
        .filter(w => w.text && w.end > w.start);
      cleaned.sort((a, b) => a.start - b.start);
      return cleaned.length ? cleaned : null;
    },

    fmtSecs(s) {
      const v = Math.max(0, Number(s) || 0);
      const mm = Math.floor(v / 60);
      const ss = (v - mm * 60).toFixed(2);
      return `${mm}:${ss.padStart(5, '0')}`;
    },

    // --- Step 6: per-character compile -----------------------------------
    //
    // After Step 5 finishes generating per-(char, scene) videos, this lets
    // the user compile ONE final MP4 per character by concatenating their
    // scene videos in order and running through the Editor pipeline.

    // Characters that have at least one approved variant AND at least one
    // DONE video — eligible for compile.
    compilableCharacters() {
      if (!this.job) return {};
      const out = {};
      for (const [cid, jc] of Object.entries(this.job.characters || {})) {
        const hasApproved = (jc.approved_variant_ids || []).length > 0
          || !!jc.approved_variant_id;
        const hasDoneVideo = (jc.videos || []).some(
          v => v.status === 'done' && v.url,
        );
        if (hasApproved && hasDoneVideo) out[cid] = jc;
      }
      return out;
    },

    hasCompilableChars() {
      return Object.keys(this.compilableCharacters()).length > 0;
    },

    compilableCharCount() {
      return Object.keys(this.compilableCharacters()).length;
    },

    canCompile() {
      return this.hasCompilableChars() && !!this.health.openai_key;
    },

    async submitCompile(opts) {
      // opts.forResolve: boolean — forces enable_captions=false so the
      // compile produces a clean MP4 ready for caption + color work in
      // DaVinci Resolve. Per-char "Export to Resolve" links appear after
      // each compile_status flips to 'done'.
      if (!this.job || !this.canCompile()) return;
      const forResolve = !!opts?.forResolve;
      this.compiling = true;
      try {
        // Persist all settings (one versioned blob) so they survive reloads.
        try {
          localStorage.setItem('compile.settings.v2', JSON.stringify(this.compileSettings));
        } catch (_) { /* private window etc. */ }

        const body = {
          template: this.compileSettings.template,
          enable_trim: !!this.compileSettings.enableTrim,
          enable_captions: forResolve ? false : !!this.compileSettings.enableCaptions,
          enable_wpm_normalize: !!this.compileSettings.enableWpmNormalize,
          target_wpm: Number(this.compileSettings.targetWpm) || 190,
          voice_override: this.compileSettings.voiceOverride || null,
          enable_voice_swap: !!this.compileSettings.enableVoiceSwap,
          threshold_db: Number.isFinite(+this.compileSettings.thresholdDb) ? +this.compileSettings.thresholdDb : -30,
          min_silence_secs: Number.isFinite(+this.compileSettings.minSilenceSecs) ? +this.compileSettings.minSilenceSecs : 0.4,
          pad_secs: Number.isFinite(+this.compileSettings.padSecs) ? +this.compileSettings.padSecs : 0.05,
        };
        const r = await fetch('/api/jobs/' + this.job.job_id + '/compile_videos', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) { this.notifyError('Compile failed: ' + await r.text()); return; }
        this.job = await r.json();  // server flips eligible chars to compiling
      } finally {
        this.compiling = false;
      }
    },

    async runFullPipeline() {
      // Phase 4: chain compile-no-captions → spawn automate.py per char.
      // Backend orchestrator runs each char in parallel; we just kick it off
      // here and let WS events drive the per-char status badges.
      if (!this.job || !this.canCompile()) return;
      if (this.pipelineRunning) return;
      this.pipelineRunning = true;
      try {
        const r = await fetch('/api/jobs/' + this.job.job_id + '/run_full_pipeline', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_ids: null }),
        });
        if (!r.ok) {
          this.notifyError('Pipeline failed to start: ' + await r.text());
          this.pipelineRunning = false;
          return;
        }
        this.job = await r.json();
        this.notifyInfo(`Pipeline started for ${this.compilableCharCount()} character${this.compilableCharCount() === 1 ? '' : 's'}…`);
      } catch (e) {
        this.notifyError('Pipeline failed: ' + (e?.message || e));
        this.pipelineRunning = false;
      }
    },

    // Per-character retry (called from the failed-card's ↻). Just resubmits
    // with `char_ids: [cid]` so only that character re-compiles.
    async retryCompile(charId) {
      if (!this.job) return;
      const body = {
        template: this.compileSettings.template,
        enable_trim: !!this.compileSettings.enableTrim,
        enable_captions: !!this.compileSettings.enableCaptions,
        enable_wpm_normalize: !!this.compileSettings.enableWpmNormalize,
        target_wpm: Number(this.compileSettings.targetWpm) || 190,
        voice_override: this.compileSettings.voiceOverride || null,
        enable_voice_swap: !!this.compileSettings.enableVoiceSwap,
        threshold_db: Number.isFinite(+this.compileSettings.thresholdDb) ? +this.compileSettings.thresholdDb : -30,
        min_silence_secs: Number.isFinite(+this.compileSettings.minSilenceSecs) ? +this.compileSettings.minSilenceSecs : 0.4,
        pad_secs: Number.isFinite(+this.compileSettings.padSecs) ? +this.compileSettings.padSecs : 0.05,
        char_ids: [charId],
      };
      const r = await fetch('/api/jobs/' + this.job.job_id + '/compile_videos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { this.notifyError('Retry compile failed: ' + await r.text()); return; }
      this.job = await r.json();
    },

    // --- Visual scrubbing playhead + draggable timeline cards ----------
    //
    // Render the caption cards as positioned rectangles on a horizontal
    // track. A vertical playhead line moves across them, auto-following
    // the Remotion preview during playback and draggable for scrubbing.
    // Dragging a card's left/right edge retimes the first/last word in
    // that card; dragging the card body shifts ALL words in the card.

    // Total timeline length — the longer of (last word end) and (video
    // duration), since the user may want to retime past the last word.
    captionTimelineLength() {
      const words = this.editor.editedWords || [];
      const lastEnd = words.length ? Math.max(...words.map(w => w.end || 0)) : 0;
      const videoDur = this.editor.sourceVideo?.durationSecs
        || this.editor.sourceVideo?.duration
        || this.editor.lastResult?.duration
        || 0;
      return Math.max(0.5, lastEnd, videoDur);
    },

    // Map a time-in-seconds to a percentage offset across the timeline.
    secsToPercent(secs) {
      const total = this.captionTimelineLength();
      return Math.max(0, Math.min(100, (Number(secs) || 0) / total * 100));
    },

    // True iff the playhead is currently inside this card's [start, end].
    isCardActiveAtPlayhead(card) {
      const p = this.playheadSecs;
      return card.start <= p && p <= card.end;
    },

    // Open the Remotion preview's frame-update listener so the playhead
    // follows playback. Called automatically when the caption editor opens
    // and the Remotion bundle is available.
    _attachPlayheadFollower() {
      this._detachPlayheadFollower();
      if (typeof window === 'undefined' || !window.RemotionPreview?.onFrameUpdate) return;
      // Defer slightly so the Player has time to mount.
      this._followerAttachTimer = setTimeout(() => {
        if (!this.editor.captionEditOpen) return;
        try {
          this._unsubFollower = window.RemotionPreview.onFrameUpdate(
            'remotion-preview-host',
            (secs) => {
              // Ignore frame events while the user is actively dragging —
              // they're the source of truth.
              if (this.isScrubbing) return;
              this.playheadSecs = secs;
            },
          );
        } catch { /* player not ready */ }
      }, 250);
    },

    _detachPlayheadFollower() {
      if (this._followerAttachTimer) {
        clearTimeout(this._followerAttachTimer);
        this._followerAttachTimer = null;
      }
      if (typeof this._unsubFollower === 'function') {
        try { this._unsubFollower(); } catch { /* nothing */ }
        this._unsubFollower = null;
      }
    },

    // Click anywhere on the timeline track → jump playhead AND seek
    // the Remotion preview to that point. Same handler used for the
    // mousedown that starts a playhead drag.
    seekTimeline(ev) {
      const track = ev.currentTarget;
      const rect = track.getBoundingClientRect();
      const x = (ev.clientX ?? (ev.touches && ev.touches[0]?.clientX)) - rect.left;
      const ratio = Math.max(0, Math.min(1, x / Math.max(1, rect.width)));
      const secs = ratio * this.captionTimelineLength();
      this.playheadSecs = secs;
      this._seekRemotion(secs);
    },

    startPlayheadDrag(ev) {
      ev.preventDefault();
      const track = ev.currentTarget.closest('.cap-tl-track');
      if (!track) return;
      this.isScrubbing = true;
      // Pause playback so the user can scrub freely.
      try { window.RemotionPreview?.pause('remotion-preview-host'); } catch { /* nothing */ }
      const rect = track.getBoundingClientRect();
      const onMove = (m) => {
        const x = (m.clientX ?? (m.touches && m.touches[0]?.clientX)) - rect.left;
        const ratio = Math.max(0, Math.min(1, x / Math.max(1, rect.width)));
        const secs = ratio * this.captionTimelineLength();
        this.playheadSecs = secs;
        this._seekRemotion(secs);
      };
      const onUp = () => {
        this.isScrubbing = false;
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('touchmove', onMove);
        window.removeEventListener('touchend', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('touchmove', onMove, { passive: false });
      window.addEventListener('touchend', onUp);
    },

    _seekRemotion(secs) {
      try { window.RemotionPreview?.seekToSecs('remotion-preview-host', secs); }
      catch { /* nothing */ }
    },

    // Click on a card → jump playhead to its start (snap-to-card scrubbing).
    seekToCard(card) {
      this.playheadSecs = card.start;
      this._seekRemotion(card.start);
    },

    togglePlayhead() {
      if (typeof window === 'undefined' || !window.RemotionPreview) return;
      const playing = window.RemotionPreview.isPlaying('remotion-preview-host');
      if (playing) window.RemotionPreview.pause('remotion-preview-host');
      else window.RemotionPreview.play('remotion-preview-host');
    },

    // Drag the LEFT edge of a card → adjust first word's start time.
    // Clamped between previous card's end and this card's existing end.
    startCardLeftDrag(ev, card) {
      ev.preventDefault();
      ev.stopPropagation();
      const track = ev.currentTarget.closest('.cap-tl-track');
      if (!track) return;
      const rect = track.getBoundingClientRect();
      const onMove = (m) => {
        const x = (m.clientX ?? (m.touches && m.touches[0]?.clientX)) - rect.left;
        const ratio = Math.max(0, Math.min(1, x / Math.max(1, rect.width)));
        const newStart = ratio * this.captionTimelineLength();
        // Clamp: must stay after previous word's end (gap from card-1's last
        // word) and at least 0.05s before this card's last-word end.
        const cards = this.captionCards();
        const cardIdx = cards.findIndex(c => c.startIdx === card.startIdx);
        const prevEnd = cardIdx > 0 ? cards[cardIdx - 1].end : 0;
        const max = card.words[card.words.length - 1].end - 0.05;
        const clamped = Math.max(prevEnd, Math.min(max, newStart));
        this.setWordStart(card.startIdx, clamped);
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('touchmove', onMove);
        window.removeEventListener('touchend', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('touchmove', onMove, { passive: false });
      window.addEventListener('touchend', onUp);
    },

    // Drag the RIGHT edge of a card → adjust last word's end time.
    startCardRightDrag(ev, card) {
      ev.preventDefault();
      ev.stopPropagation();
      const track = ev.currentTarget.closest('.cap-tl-track');
      if (!track) return;
      const rect = track.getBoundingClientRect();
      const onMove = (m) => {
        const x = (m.clientX ?? (m.touches && m.touches[0]?.clientX)) - rect.left;
        const ratio = Math.max(0, Math.min(1, x / Math.max(1, rect.width)));
        const newEnd = ratio * this.captionTimelineLength();
        const cards = this.captionCards();
        const cardIdx = cards.findIndex(c => c.startIdx === card.startIdx);
        const nextStart = cardIdx < cards.length - 1
          ? cards[cardIdx + 1].start
          : this.captionTimelineLength();
        const min = card.words[0].start + 0.05;
        const clamped = Math.min(nextStart, Math.max(min, newEnd));
        // setWordEnd targets the LAST word in the card.
        this.setWordEnd(card.endIdx, clamped);
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('touchmove', onMove);
        window.removeEventListener('touchend', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('touchmove', onMove, { passive: false });
      window.addEventListener('touchend', onUp);
    },

    // Drag the BODY of a card → shift every word in the card by the same
    // delta. Clamped so the whole card stays within [prev_card.end, next_card.start].
    // Click-without-drag (move < 4px) falls back to seeking to the card.
    startCardBodyDrag(ev, card) {
      ev.preventDefault();
      ev.stopPropagation();
      const track = ev.currentTarget.closest('.cap-tl-track');
      if (!track) return;
      const rect = track.getBoundingClientRect();
      const startX = ev.clientX ?? (ev.touches && ev.touches[0]?.clientX);
      const originalWords = card.words.map((w, i) => ({
        start: w.start, end: w.end, idx: card.startIdx + i,
      }));
      const cards = this.captionCards();
      const cardIdx = cards.findIndex(c => c.startIdx === card.startIdx);
      const prevEnd = cardIdx > 0 ? cards[cardIdx - 1].end : 0;
      const nextStart = cardIdx < cards.length - 1
        ? cards[cardIdx + 1].start
        : this.captionTimelineLength();
      const minDelta = prevEnd - card.start;
      const maxDelta = nextStart - card.end;
      let dragged = false;

      const onMove = (m) => {
        const x = m.clientX ?? (m.touches && m.touches[0]?.clientX);
        if (!dragged && Math.abs(x - startX) < 4) return;  // tap, not drag
        dragged = true;
        const pixelsPerSec = Math.max(1, rect.width) / Math.max(0.01, this.captionTimelineLength());
        let delta = (x - startX) / pixelsPerSec;
        delta = Math.max(minDelta, Math.min(maxDelta, delta));
        for (const ow of originalWords) {
          this.editor.editedWords[ow.idx].start = Math.max(0, ow.start + delta);
          this.editor.editedWords[ow.idx].end = Math.max(0.02, ow.end + delta);
        }
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('touchmove', onMove);
        window.removeEventListener('touchend', onUp);
        // No drag happened → treat as a click → seek to this card.
        if (!dragged) this.seekToCard(card);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('touchmove', onMove, { passive: false });
      window.addEventListener('touchend', onUp);
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
        // Touch events carry coordinates on touches[0] (mobile 2026-06-12).
        startX: event.clientX ?? event.touches?.[0]?.clientX ?? 0,
        origStart: seg.start,
        origEnd: seg.end,
        scale,
      };
      // Bind handlers so we can remove them later. Plain method refs lose
      // `this` when called by the window's event loop.
      this._tlOnMove = (ev) => {
        const d = this._tlDrag;
        if (!d) return;
        const x = ev.clientX ?? ev.touches?.[0]?.clientX;
        if (x === undefined) return;
        const dxPx = x - d.startX;
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
        window.removeEventListener('touchmove', this._tlOnMove);
        window.removeEventListener('touchend', this._tlOnUp);
        this._tlOnMove = null;
        this._tlOnUp = null;
      };
      window.addEventListener('mousemove', this._tlOnMove);
      window.addEventListener('mouseup', this._tlOnUp);
      window.addEventListener('touchmove', this._tlOnMove, { passive: true });
      window.addEventListener('touchend', this._tlOnUp);
    },

    // Click anywhere on the track to move the playhead (in output time).
    seekTimeline(event) {
      const trackEl = this.$refs.timelineTrack;
      if (!trackEl) return;
      const r = trackEl.getBoundingClientRect();
      const cx = event.clientX ?? event.touches?.[0]?.clientX
        ?? event.changedTouches?.[0]?.clientX;
      if (cx === undefined) return;
      const xPx = cx - r.left;
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
        this.notifyMilestone('Timeline render done',
          `v${data.version} · ${data.n_segments} segments · ${data.duration}s`,
          { kind: 'done', tag: `editor-timeline-${data.version}` });
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

    // --- Higgsfield Drive inbox ------------------------------------------
    // Server polls a user-configured Drive folder for Supercomputer outputs
    // and stages them under `output/higgsfield-inbox/`. UI shows them as a
    // strip above the manual clips upload area; click → add to multi-clip
    // list. Same shape as addMultiClips(fileList), but we fetch the file
    // from our own /files/ mount and wrap it in a File object so all the
    // downstream multi-clip code keeps working unchanged.

    async loadHiggsfieldInbox() {
      try {
        const r = await fetch('/api/higgsfield/inbox');
        if (!r.ok) return;
        this.higgsfieldInbox = await r.json();
      } catch { /* offline */ }
    },

    async pollHiggsfieldInbox() {
      if (this.higgsfieldPolling) return;
      this.higgsfieldPolling = true;
      try {
        const r = await fetch('/api/higgsfield/inbox/poll', { method: 'POST' });
        if (!r.ok) {
          this.notifyError('Higgsfield poll failed: ' + await r.text());
          return;
        }
        const data = await r.json();
        if (data?.ok === false) {
          this.notifyError(`Higgsfield poll: ${data.reason}` +
            (data.looked_for ? ` (looked for "${data.looked_for}")` : ''));
        } else if ((data?.n_new || 0) > 0) {
          this.notifyMilestone('Higgsfield inbox',
            `${data.n_new} new clip${data.n_new === 1 ? '' : 's'} pulled from Drive`,
            { kind: 'done', tag: 'higgsfield-poll' });
        }
        await this.loadHiggsfieldInbox();
      } finally {
        this.higgsfieldPolling = false;
      }
    },

    async clearHiggsfieldInbox(driveId) {
      try {
        await fetch(`/api/higgsfield/inbox/${encodeURIComponent(driveId)}`,
                    { method: 'DELETE' });
        await this.loadHiggsfieldInbox();
      } catch (e) {
        this.notifyError('Inbox clear failed: ' + e);
      }
    },

    async bootstrapHiggsfieldDrive() {
      this.notify('info', 'A browser tab should open for Google OAuth. Complete it, then we poll Drive automatically.');
      try {
        const r = await fetch('/api/higgsfield/drive/bootstrap',
                              { method: 'POST' });
        const data = await r.json();
        if (data?.ok) {
          this.notifyMilestone('Drive connected',
            'OAuth complete — watcher will start pulling clips on next poll.',
            { kind: 'done', tag: 'higgsfield-oauth' });
          await this.loadHiggsfieldInbox();
        } else {
          this.notifyError(
            'Drive OAuth failed. Ensure `credentials.json` is at ~/character-swap-data/ and try again.',
          );
        }
      } catch (e) {
        this.notifyError('Bootstrap error: ' + e);
      }
    },

    async addOneHiggsfieldInbox(item) {
      // Fetch the staged file from /files/output/... → Blob → File so it
      // slots into the same multiClips shape as a normal upload.
      try {
        const r = await fetch(item.file_url);
        if (!r.ok) throw new Error('fetch failed');
        const blob = await r.blob();
        const file = new File([blob], item.name,
                              { type: blob.type || 'video/mp4' });
        this.addMultiClips([file]);
      } catch (e) {
        this.notifyError(`Couldn't add ${item.name}: ${e}`);
      }
    },

    async addAllHiggsfieldInbox() {
      const items = this.higgsfieldInbox?.items || [];
      for (const it of items) {
        await this.addOneHiggsfieldInbox(it);
      }
      this.notifyMilestone('Higgsfield clips added',
        `${items.length} clip${items.length === 1 ? '' : 's'} ready for multi-clip auto-edit`,
        { kind: 'done', tag: 'higgsfield-add-all' });
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
        fd.append('playback_speed', String(this.editor.playbackSpeed || 1));
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
        this.notifyMilestone('Multi-clip auto-edit done',
          `${data.n_clips} clips${unmatched ? ` (${unmatched} unmatched)` : ''} · ${data.captions?.n_words} words captioned`,
          { kind: 'done', tag: 'editor-multi-auto-edit' });
        if (this.editor.autoExportResolve && data.edit_id) {
          await this.runEditorPipeline(data.edit_id);
        }
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
        this.notifyMilestone('Auto-edit pipeline done', parts.join(' · '),
          { kind: 'done', tag: 'editor-auto-edit' });
        if (this.editor.autoExportResolve && data.edit_id) {
          await this.runEditorPipeline(data.edit_id);
        }
      } finally {
        this.editor.autoEditing = false;
      }
    },

    // --- Editor → DaVinci Resolve auto-export ------------------------------
    // Kicks off the same Phase-4 pipeline used by Step 6 of the Swap flow,
    // but driven from an editor edit_id instead of a JobCharacter. The
    // backend (runner_pipeline.run_editor_pipeline) packages the rendered
    // MP4 + SRT + automate.py into a temp dir and spawns the subprocess.
    // We then poll /api/editor/{edit_id}/pipeline_state every 2s for
    // status transitions. Status pill in the result panel reflects the
    // current `editor.pipelineState`.
    async runEditorPipeline(editId) {
      if (!editId) return;
      this._stopPipelinePolling();
      this.editor.pipelineState = { status: 'queued' };
      try {
        const r = await fetch('/api/editor/run_full_pipeline', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ edit_id: editId }),
        });
        if (!r.ok) {
          const txt = await r.text();
          this.editor.pipelineState = { status: 'failed', error: txt };
          this.notifyError('Resolve pipeline failed to start: ' + txt);
          return;
        }
        this.editor.pipelineState = await r.json();
        this._startPipelinePolling(editId);
      } catch (e) {
        this.editor.pipelineState = { status: 'failed', error: String(e) };
        this.notifyError('Resolve pipeline kickoff error: ' + e);
      }
    },

    _startPipelinePolling(editId) {
      this._stopPipelinePolling();
      const self = this;
      this.editor._pipelinePoll = setInterval(async () => {
        try {
          const r = await fetch(`/api/editor/${editId}/pipeline_state`);
          if (!r.ok) return;
          const data = await r.json();
          if (!data || !data.status) return;
          self.editor.pipelineState = data;
          if (data.status === 'done' || data.status === 'failed') {
            self._stopPipelinePolling();
            const title = data.status === 'done'
              ? 'Resolve pipeline done'
              : 'Resolve pipeline failed';
            const body = data.drive_link
              ? `Drive: ${data.drive_link}`
              : (data.error || data.status);
            self.notifyMilestone(title, body,
              { kind: data.status, tag: `editor-pipeline-${editId}` });
          }
        } catch { /* ignore transient poll errors */ }
      }, 2000);
    },

    _stopPipelinePolling() {
      if (this.editor._pipelinePoll) {
        clearInterval(this.editor._pipelinePoll);
        this.editor._pipelinePoll = null;
      }
    },

    // --- Editor → Google Drive upload --------------------------------------
    // Replacement for the old Resolve / Phase-4 path. Click "☁︎ Export to
    // Drive" → modal lets you name the file → backend uploads via the
    // drive.file scope. First time: bootstrap kicks off a browser OAuth
    // consent for write access (separate from the read scope the
    // Higgsfield inbox watcher uses).
    openDriveExport() {
      if (!this.driveExport) {
        this.driveExport = { open: false, filename: '', uploading: false, lastUrl: '' };
      }
      // Suggest a friendly default filename based on prompt + ISO date,
      // mirroring how downloads are named elsewhere in the app.
      const slug = this.friendlyName
        ? this.friendlyName({ prompt: this.editor.lastResult?.prompt, kind: 'editor' }, 'mp4')
        : (this.editor.lastResult?.edit_id || 'export') + '.mp4';
      this.driveExport.filename = slug.replace(/\.mp4$/i, '');
      this.driveExport.open = true;
      this.driveExport.lastUrl = '';
    },

    async bootstrapDriveWrite() {
      try {
        const r = await fetch('/api/editor/drive_export/bootstrap', { method: 'POST' });
        if (!r.ok) {
          this.notifyError('Drive write bootstrap failed: ' + await r.text());
          return;
        }
        await this.loadHealth?.();   // refresh health.drive_write_ready
        this.notifyMilestone('Drive write authorized', 'You can now upload from Editor.',
          { kind: 'done', tag: 'drive-write-bootstrap' });
      } catch (e) {
        this.notifyError('Bootstrap error: ' + e);
      }
    },

    async confirmDriveExport() {
      const editId = this.editor.lastResult?.edit_id;
      if (!editId) return;
      const name = (this.driveExport.filename || '').trim();
      if (!name) return;
      this.driveExport.uploading = true;
      try {
        const r = await fetch(`/api/editor/${editId}/drive_export`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: name }),
        });
        if (!r.ok) {
          const txt = await r.text();
          this.notifyError('Drive upload failed: ' + txt);
          return;
        }
        const data = await r.json();
        this.driveExport.lastUrl = data.url || '';
        this.driveExport.open = false;
        this.notifyMilestone('Uploaded to Drive', data.name || name,
          { kind: 'done', tag: `drive-export-${editId}` });
      } catch (e) {
        this.notifyError('Upload error: ' + e);
      } finally {
        this.driveExport.uploading = false;
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
      // Snapshot duration + intrinsic dimensions onto editor.sourceVideo so
      // the Remotion Player can size its composition correctly.
      if (this.editor?.sourceVideo) {
        this.editor.sourceVideo.duration = ev.target.duration || 0;
        this.editor.sourceVideo.width = ev.target.videoWidth || 1080;
        this.editor.sourceVideo.height = ev.target.videoHeight || 1920;
      }
      this._refreshRemotionPreview();
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
      this._refreshRemotionPreview();
    },

    // --- Remotion preview integration -------------------------------------
    // Remotion-rendered templates declare `engine: "remotion"` on the
    // template row served from `/api/editor/templates`. When picked, we
    // mount @remotion/player into the `#remotion-preview-host` div so the
    // user sees an exact preview of what the server will render.

    useRemotionPlayer() {
      if (!this.editor?.sourceVideo?.url) return false;
      const tpl = this.currentTemplate();
      return !!(tpl && tpl.engine === 'remotion' && tpl.composition_id);
    },

    _remotionSampleWords() {
      // Placeholder words used until Whisper has actually transcribed the
      // source video. Mirrors the "NEVER BUY HONEY" sample used by the
      // legacy CSS preview, padded out to 6 words so multi-word-per-card
      // templates have something to group.
      return [
        { text: 'Never', start: 0.0, end: 0.45 },
        { text: 'buy',   start: 0.5, end: 0.85 },
        { text: 'honey', start: 0.9, end: 1.45 },
        { text: 'from',  start: 1.5, end: 1.85 },
        { text: 'the',   start: 1.9, end: 2.05 },
        { text: 'store', start: 2.1, end: 2.7 },
      ];
    },

    _assToHexCss(ass) {
      if (!ass) return null;
      let s = String(ass).replace(/^&h?/i, '');
      if (s.length === 8) s = s.slice(2);
      if (s.length !== 6) return null;
      const bb = s.slice(0, 2), gg = s.slice(2, 4), rr = s.slice(4, 6);
      if (!/^[0-9a-fA-F]{6}$/.test(bb + gg + rr)) return null;
      return ('#' + rr + gg + bb).toUpperCase();
    },

    _remotionPlayerProps() {
      const tpl = this.currentTemplate();
      const overrides = this._activeOverrides();
      const tplProps = tpl?.remotion_props || {
        accent: '#FFD400', fontFamily: 'Inter', sizeScale: 1.0,
        positionPct: { x: 0.5, y: 0.78 }, allCaps: true, wordsPerCard: 3,
      };
      // Caption-editor preview: when the user is editing, prefer the
      // currently-edited transcript so the preview updates LIVE as they
      // retime / fix words. Falls back to the cached `lastResult.words`
      // when the editor isn't open.
      const editing = this.editor?.captionEditOpen
                       && Array.isArray(this.editor?.editedWords)
                       && this.editor.editedWords.length > 0;
      const realWords = editing
        ? this.editor.editedWords
        : this.editor?.lastResult?.words;
      const words = (Array.isArray(realWords) && realWords.length > 0)
        ? realWords
        : this._remotionSampleWords();

      const remOverrides = {};
      if (overrides.size != null) {
        remOverrides.sizeScale = Math.max(0.4, Math.min(2.5, overrides.size / 115.2));
      }
      if (overrides.highlight_color) {
        const hex = this._assToHexCss(overrides.highlight_color);
        if (hex) remOverrides.accent = hex;
      }
      if (overrides.font) remOverrides.fontFamily = overrides.font;
      if (overrides.all_caps != null) remOverrides.allCaps = !!overrides.all_caps;
      if (overrides.words_per_card != null) remOverrides.wordsPerCard = overrides.words_per_card;
      const basePos = tplProps.positionPct || { x: 0.5, y: 0.78 };
      let pos = null;
      if (overrides.margin_v != null) {
        pos = pos || { ...basePos };
        pos.y = Math.max(0.05, Math.min(0.95, 1 - overrides.margin_v / 1920));
      }
      if (overrides.margin_h != null) {
        pos = pos || { ...basePos };
        pos.x = Math.max(0.05, Math.min(0.95, 0.5 + overrides.margin_h / 1080));
      }
      if (pos) remOverrides.positionPct = pos;

      const videoDur = this.editor.sourceVideo?.durationSecs
                       || this.editor.sourceVideo?.duration
                       || 10;
      return {
        videoSrc: this.editor.sourceVideo.url,
        words,
        videoDurationSecs: videoDur,
        videoWidth: this.editor.sourceVideo?.width || 1080,
        videoHeight: this.editor.sourceVideo?.height || 1920,
        ...tplProps,
        ...remOverrides,
      };
    },

    _refreshRemotionPreview() {
      if (!this.useRemotionPlayer()) {
        if (typeof window !== 'undefined' && window.RemotionPreview && this._remotionMounted) {
          window.RemotionPreview.unmount('remotion-preview-host');
          this._remotionMounted = false;
        }
        return;
      }
      if (typeof window === 'undefined' || !window.RemotionPreview) {
        // Bundle still loading (or never built). Retry a few times then give up.
        if (!this._remotionLoadAttempts) this._remotionLoadAttempts = 0;
        if (this._remotionLoadAttempts < 20) {
          this._remotionLoadAttempts += 1;
          setTimeout(() => this._refreshRemotionPreview(), 200);
        } else if (!this._remotionLoadWarned) {
          this._remotionLoadWarned = true;
          console.warn('[remotion-preview] bundle missing — run `character-swap remotion-install`');
        }
        return;
      }
      this._remotionLoadAttempts = 0;
      const tpl = this.currentTemplate();
      window.RemotionPreview.mount(
        'remotion-preview-host',
        tpl.composition_id,
        this._remotionPlayerProps(),
      );
      this._remotionMounted = true;
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

    async onPromptDrop(ev, target) {
      // Path 1: dragged from the character library — pull the image URL
      // from the custom payload, fetch it, hand it in as a File.
      const libUrl = ev.dataTransfer?.getData('text/x-charswap-image-url');
      if (libUrl) {
        ev.preventDefault();
        this.promptDropActive = false;
        try {
          const resp = await fetch(libUrl);
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const blob = await resp.blob();
          const ext = (libUrl.split('?')[0].split('.').pop() || 'png').toLowerCase();
          const file = new File([blob], `char-ref.${ext}`,
                                { type: blob.type || `image/${ext}` });
          if (target === 'image') this.addImageRefs([file]);
          else if (target === 'video') this.setVideoRef(file);
        } catch (e) {
          this.notifyError('Could not load character image: ' + e.message);
        }
        return;
      }
      // Path 2: OS-level file drop.
      const files = Array.from(ev.dataTransfer?.files || []).filter(f => f.type.startsWith('image/'));
      if (files.length === 0) return;
      ev.preventDefault();
      if (target === 'image') this.addImageRefs(files);
      else if (target === 'video') this.setVideoRef(files[0]);
      this.promptDropActive = false;
    },

    onPromptDragOver(ev) {
      // Highlight for native file drags AND for drags from the character
      // library (which carry text/x-charswap-image-url instead of files).
      const types = Array.from(ev.dataTransfer?.types || []);
      if (types.includes('Files') || types.includes('text/x-charswap-image-url')) {
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

    async addLibraryImageAsRef(url) {
      // Click-handler for the "+ ref" button on every library image. Fetches
      // the URL into a File and routes it through the same addImageRefs /
      // setVideoRef plumbing as drag-and-drop. Tabs without a reference-image
      // slot (Avatar, Audio, B-roll, Editor, Swap) get a notify and no-op.
      if (!url) return;
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const ext = (url.split('?')[0].split('.').pop() || 'png').toLowerCase();
        const file = new File([blob], `char-ref.${ext}`,
                              { type: blob.type || `image/${ext}` });
        if (this.activeTab === 'image') {
          if (this.imageGen.refs.length >= 3) {
            this.notifyInfo('Image tab already has 3 reference images — remove one first');
            return;
          }
          this.addImageRefs([file]);
          this.notifyInfo('Added as reference image');
        } else if (this.activeTab === 'video') {
          this.setVideoRef(file);
          this.notifyInfo('Set as video reference image');
        } else {
          this.notifyInfo('Switch to the Image or Video tab first');
        }
      } catch (e) {
        this.notifyError('Could not load image: ' + e.message);
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
        if (this.enrich.image) fd.append('enrich_prompt', 'true');
        if (this.director.image && this.health.anthropic_key) fd.append('use_director', 'true');
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
        if (this.enrich.video) fd.append('enrich_prompt', 'true');
        if (this.director.video && this.health.anthropic_key) fd.append('use_director', 'true');
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

    // Bigger-deal notification: in-app toast + audio chime + OS popup (if
    // permitted). Use for approval gates and batch completions, NOT for
    // routine status pings.
    //
    // opts: { kind: 'approval' | 'done', tag: string }
    //   - kind picks the chime pitch (approval = higher/sharper)
    //   - tag de-dupes OS popups when the same milestone fires twice quickly
    notifyMilestone(title, body, opts = {}) {
      this.notify('info', body, { ttl: 6000 });
      if (this.notif.sound) this._playChime(opts.kind);
      if (this.notif.os && typeof Notification !== 'undefined'
          && Notification.permission === 'granted') {
        try {
          const n = new Notification(title, {
            body, icon: '/favicon.ico', tag: opts.tag, silent: false,
          });
          n.onclick = () => { window.focus(); n.close(); };
          setTimeout(() => { try { n.close(); } catch (_) {} }, 9000);
        } catch (_) {}
      }
    },

    _playChime(kind) {
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        this._audioCtx = this._audioCtx || new Ctx();
        const ctx = this._audioCtx;
        const tones = kind === 'approval' ? [880, 1320] : [660, 990];
        tones.forEach((freq, i) => {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = 'sine';
          osc.frequency.value = freq;
          osc.connect(gain).connect(ctx.destination);
          const t = ctx.currentTime + i * 0.12;
          gain.gain.setValueAtTime(0.0001, t);
          gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
          osc.start(t); osc.stop(t + 0.2);
        });
      } catch (_) {}
    },

    async _requestNotifPermission() {
      if (typeof Notification === 'undefined') return;
      if (Notification.permission === 'default') {
        try { await Notification.requestPermission(); } catch (_) {}
      }
    },

    // True iff the browser permission for OS notifications was denied
    // (user clicked "Block"). Used to grey out the 🔔 header toggle.
    notifBlockedByBrowser() {
      return typeof Notification !== 'undefined' && Notification.permission === 'denied';
    },

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

    // --- Extra reference image (3rd ref in Swap Step 2) -------------------

    async uploadExtraRef(file) {
      if (!file) return;
      if (!file.type?.startsWith('image/')) {
        this.notifyError('Extra reference must be an image');
        return;
      }
      this.uploadingExtraRef = true;
      try {
        const fd = new FormData();
        fd.append('file', file);
        const r = await fetch('/api/jobs/extra_ref', { method: 'POST', body: fd });
        if (!r.ok) {
          this.notifyError('Upload failed: ' + (await r.text()).slice(0, 200));
          return;
        }
        const data = await r.json();
        this.extraRefFilename = data.filename;
        this.extraRefUrl = data.url;
        this.extraRefOriginalName = data.original_name || data.filename;
      } catch (e) {
        this.notifyError('Upload error: ' + e);
      } finally {
        this.uploadingExtraRef = false;
      }
    },

    clearExtraRef() {
      this.extraRefFilename = '';
      this.extraRefUrl = '';
      this.extraRefOriginalName = '';
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
        // Drop any staged source-image override too so it doesn't haunt a
        // future re-selection of this char with a different intent.
        if (this.charSourceOverrides[cid]) {
          const next = { ...this.charSourceOverrides };
          delete next[cid];
          this.charSourceOverrides = next;
        }
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

    // --- Optional per-scene END POSE (Kling 3.0 start→end interpolation) ------
    // Before the job exists these are staged client-side (endPoses[sid] =
    // {scene_id, url}) and sent as `end_poses` on createJob; after the job
    // exists they live on job.scenes[].end_frame_url and are managed via the
    // set/clear endpoints. The dispatch helpers below pick the right path so
    // the Step-1 template stays simple, and the slot stays visible the whole
    // time (the old version hid it with x-show="!job" → looked deleted).
    endPoses: {},

    hasEndPose(sid) {
      if (this.job) {
        const sc = (this.job.scenes || []).find(s => s.scene_id === sid);
        return !!(sc && sc.end_frame_url);
      }
      return !!this.endPoses[sid];
    },

    endPoseUrl(sid) {
      if (this.job) {
        const sc = (this.job.scenes || []).find(s => s.scene_id === sid);
        return sc ? sc.end_frame_url : null;
      }
      return this.endPoses[sid] ? this.endPoses[sid].url : null;
    },

    // True when ANY scene has an end pose — gates the Step-4 Kling-3.0 warning.
    anyEndPoseSet() {
      if (this.job) return (this.job.scenes || []).some(s => s.end_frame_url);
      return Object.keys(this.endPoses || {}).length > 0;
    },

    // Add/replace an end pose for scene sid (dispatches by job existence).
    async addEndPose(sid, file) {
      if (!file) return;
      if (this.job) { await this.uploadSceneEndFrame(sid, file); return; }
      const fd = new FormData(); fd.append('file', file);
      const r = await fetch('/api/scenes', { method: 'POST', body: fd });
      if (!r.ok) { this.notifyError('End pose upload failed: ' + await r.text()); return; }
      const pose = await r.json();
      this.endPoses[sid] = { scene_id: pose.scene_id, url: pose.url };
    },

    // Remove an end pose for scene sid (dispatches by job existence).
    async removeEndPoseAny(sid) {
      if (this.job) { await this.clearSceneEndFrame(sid); return; }
      delete this.endPoses[sid];
    },

    // Post-creation set/clear via the job endpoints. The server regenerates the
    // swapped end frame when Step-3 variants already exist.
    async uploadSceneEndFrame(sceneId, file) {
      if (!this.job || !file) return;
      const fd = new FormData(); fd.append('file', file);
      const r = await fetch(`/api/jobs/${this.job.job_id}/scenes/${sceneId}/end_frame`,
                            { method: 'POST', body: fd });
      if (!r.ok) { this.notifyError('End frame upload failed: ' + await r.text()); return; }
      this.job = await r.json();
    },

    async clearSceneEndFrame(sceneId) {
      if (!this.job) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/scenes/${sceneId}/end_frame`,
                            { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Clear end frame failed: ' + await r.text()); return; }
      this.job = await r.json();
    },

    // Retry the end-frame swap for one scene using the EXISTING pose (e.g. after
    // a content-policy block). WS events update the result live.
    async retryEndFrame(sceneId) {
      if (!this.job || !sceneId) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/scenes/${sceneId}/regen_end_frame`,
                            { method: 'POST' });
      if (!r.ok) { this.notifyError('End-frame retry failed: ' + await r.text()); return; }
      this.job = await r.json();
    },


    async uploadScenes(files) {
      for (const f of files) {
        if (f && f.type && f.type.startsWith('image/')) {
          await this.uploadScene(f);
        }
      }
      // If a job is loaded and hasn't started generating variants yet,
      // attach the new scene list to that job too — otherwise the upload
      // is purely client-side and gets discarded when the user clicks the
      // job in the sidebar again.
      if (this.job && this.canEditJobScenes()) {
        await this._syncJobScenes();
      }
    },

    removeScene(scene_id) {
      this.scenes = this.scenes.filter(s => s.scene_id !== scene_id);
      this.scene = this.scenes[0] || null;
      delete this.endPoses[scene_id];   // drop its staged end pose too
      if (this.job && this.canEditJobScenes()) {
        this._syncJobScenes();   // fire-and-forget
      }
    },

    // True when scenes can still be edited on the active job.
    // Becomes false the moment the runner has populated `images` for ANY char
    // (i.e. variant generation has started). Used both to show / hide the
    // "+ Add another scene" tile and to gate the backend PATCH.
    canEditJobScenes() {
      if (!this.job) return true;
      const chars = this.job.characters || {};
      for (const cid in chars) {
        if ((chars[cid].images || []).length > 0) return false;
      }
      return true;
    },

    async _syncJobScenes() {
      try {
        const r = await fetch(`/api/jobs/${this.job.job_id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scene_ids: this.scenes.map(s => s.scene_id) }),
        });
        if (!r.ok) {
          // 409 = generation already started; surface the message verbatim.
          const txt = await r.text();
          this.notifyError(`Couldn't attach scenes: ${txt}`);
          return;
        }
        this.job = await r.json();
      } catch (e) {
        this.notifyError('Sync scenes error: ' + e);
      }
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

    // --- Animate tab (Step A) — stage finished images, then create a job ---

    // Add image File objects to the ordered staging list (from picker, drop,
    // or paste). We keep the File around for upload and an object-URL for the
    // thumbnail preview. uid is a monotonic key so Alpine's x-for stays stable
    // across reorders/removes.
    addSeqImages(fileList) {
      const files = Array.from(fileList || []).filter(f => (f.type || '').startsWith('image/'));
      if (!files.length) return;
      for (const f of files) {
        this.seqImages.push({
          uid: (this._seqUid = (this._seqUid || 0) + 1),
          file: f,
          previewUrl: URL.createObjectURL(f),
          name: f.name || 'image',
        });
      }
    },

    onSeqDrop(ev) {
      this.addSeqImages(ev.dataTransfer?.files);
    },

    onSeqPaste(ev) {
      const files = this._scenesFromClipboard(ev);   // reuse the swap clipboard parser
      if (!files.length) return;                       // plain text — let it through
      ev.preventDefault();
      this.addSeqImages(files);
      this.notifyInfo(`Added ${files.length} image${files.length > 1 ? 's' : ''} from clipboard`);
    },

    removeSeqImage(idx) {
      const [removed] = this.seqImages.splice(idx, 1);
      if (removed) { try { URL.revokeObjectURL(removed.previewUrl); } catch (_) {} }
    },

    // Move an image one slot earlier (-1) or later (+1) to reorder the sequence.
    moveSeqImage(idx, dir) {
      const j = idx + dir;
      if (j < 0 || j >= this.seqImages.length) return;
      const tmp = this.seqImages[idx];
      this.seqImages[idx] = this.seqImages[j];
      this.seqImages[j] = tmp;
    },

    clearSeq() {
      for (const img of this.seqImages) { try { URL.revokeObjectURL(img.previewUrl); } catch (_) {} }
      this.seqImages = [];
      this.seqTitle = '';
    },

    // POST the staged images (in order) to /api/jobs/from_images. The server
    // returns a fully pre-approved job; we adopt it exactly like a fresh Swap
    // job so Steps 4-6 take over, and default the Step 4 video-model picker to
    // the sequence's model.
    async createSequence() {
      if (!this.seqImages.length || this.seqCreating) return;
      this.seqCreating = true;
      try {
        const fd = new FormData();
        if (this.seqTitle.trim()) fd.append('title', this.seqTitle.trim());
        fd.append('video_model', this.seqVideoModel || 'kling-v2-6');
        for (const img of this.seqImages) fd.append('files', img.file, img.name);
        const r = await fetch('/api/jobs/from_images', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Sequence creation failed: ' + await r.text()); return; }
        const job = await r.json();
        this.job = job;
        // Point the Step 4 picker at the sequence's model so it shows Kling, not
        // the swap default (grok-imagine).
        this.swapVideoModel = job.video_model || this.swapVideoModel;
        this.clearSeq();
        this.connectWS(job.job_id);
        await this.loadJobsList();
      } catch (e) {
        this.notifyError('Sequence error: ' + e);
      } finally {
        this.seqCreating = false;
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

    // Set the preset ElevenLabs voice for a character. Empty string clears
    // the preset (server treats it as "no voice"). Called by the 🎤 dropdown
    // on each library card; auto-applies when the user later compiles a
    // Step-6 video for this character OR picks the character in the
    // Editor tab's "Character" dropdown.
    async setCharacterVoice(charId, voiceId) {
      const body = { voice_id: voiceId || '' };
      if (voiceId) body.voice_provider = 'elevenlabs';
      const r = await fetch('/api/characters/' + charId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) { this.notifyError('Voice update failed: ' + await r.text()); return; }
      await this.loadLibrary();
    },

    // Editor tab: when user picks a character in the "Character preset"
    // dropdown, auto-fill `editor.voiceId` with that character's preset.
    // Only fires on dropdown CHANGE so manual voice overrides aren't
    // silently overwritten on re-render.
    onEditorCharacterChange() {
      const cid = this.editor.linkedCharId;
      if (!cid) return;  // "— none —" picked; leave voice as-is
      const ch = (this.library || []).find(c => c.char_id === cid);
      if (ch && ch.voice_id) {
        this.editor.voiceId = ch.voice_id;
      }
    },

    // --- job lifecycle -------------------------------------------------------

    async startJob() {
      if (this.scenes.length === 0 || this.selectedCharacters.length === 0) return;
      this.generating = true;
      try {
        // Only send overrides for chars actually selected — keep payload tight.
        const overrides = {};
        for (const cid of this.selectedCharacters) {
          if (this.charSourceOverrides[cid]) {
            overrides[cid] = this.charSourceOverrides[cid];
          }
        }
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
            // Optional per-scene end poses: owner scene_id → pose scene_id.
            // The runner swaps each character into the pose to make a matching
            // end frame (Kling 3.0 start→end). Only scenes that still exist.
            end_poses: Object.fromEntries(
              Object.entries(this.endPoses)
                .filter(([sid]) => this.scenes.some(s => s.scene_id === sid))
                .map(([sid, p]) => [sid, p.scene_id])),
            // Only send `prompt` as a CUSTOM override when it differs from
            // the default — otherwise the backend treats unchanged default
            // text as a user-customised prompt and (with enrich on) runs it
            // through GPT-4o, which destroys the constraint phrasing
            // ("exact same pose / position / stuff") and produces a generic
            // image instead of a true swap.
            prompt: this.swapPromptIsDefault() ? null
                       : ((this.swapPrompt || '').trim() || null),
            image_model: this.swapModel,
            character_source_image_ids: Object.keys(overrides).length ? overrides : null,
            // Enrich only matters when the user TYPED a short custom prompt.
            // If they're using the default GENERATION_PROMPT (already very
            // detailed + constraint-heavy), enrichment hurts more than helps.
            enrich_prompt: !!this.enrich.swap && !this.swapPromptIsDefault(),
            // 🎬 AI Director — opt-in Claude Opus path. Independent of
            // enrich; works even when prompt is default since Director uses
            // vision on the reference images. Requires ANTHROPIC_API_KEY.
            use_director: !!this.director.swap && !!this.health.anthropic_key,
            // Optional 3rd reference image (background/outfit/prop hint).
            // Lands as ref #3 in the model call after scene + character.
            extra_reference_filename: this.extraRefFilename || null,
          }),
        });
        if (!r.ok) { this.notifyError('Job creation failed: ' + await r.text()); return; }
        const job = await r.json();
        this.job = job;
        // Overrides have been baked into the job snapshot — clear the staging.
        this.charSourceOverrides = {};
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
      // Per-scene prompts: prefer the server dict, fall back to broadcasting
      // the legacy singular field across every scene (so older jobs render
      // their existing prompt in each textarea).
      this.movementPrompts = {};
      const _scenes = this.job.scenes || (this.job.scene_id
        ? [{ scene_id: this.job.scene_id }] : []);
      const _fromServer = this.job.movement_prompts || {};
      const _hasDict = Object.keys(_fromServer).length > 0;
      for (const s of _scenes) {
        this.movementPrompts[s.scene_id] = _hasDict
          ? (_fromServer[s.scene_id] || '')
          : (this.job.movement_prompt || '');
      }
      this.swapPrompt = this.job.prompt || this.swapDefaultPrompt;
      this.swapModel = this.job.image_model || 'gpt-image';
      this.swapVideoModel = this.job.video_model || 'kling-v3';
      this.swapDurationSecs = this.job.duration_secs || null;
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

    // Retry one failed variant. `prompt` (optional) regenerates the slot with an
    // EDITED prompt; omit it to retry with the slot's existing prompt.
    async retryVariant(charId, variantId, prompt) {
      if (!this.job) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/characters/${charId}/variants/${variantId}/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(prompt != null ? { prompt } : {}),
      });
      if (!r.ok) { this.notifyError('Retry failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.notifyInfo(prompt != null
        ? 'Regenerating this variant with the edited prompt…'
        : 'Retrying just this variant — others are untouched');
    },

    // Regenerate fresh variants for ONE (character, scene) pair — additive,
    // leaves the character's other scenes + approvals untouched. Used to
    // recover a scene whose variants were all deleted (shows "0 variants").
    async regenScene(charId, sceneId) {
      if (!this.job) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/characters/${charId}/scenes/${sceneId}/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!r.ok) { this.notifyError('Regenerate failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.notifyInfo('Regenerating this scene — other scenes are untouched');
    },

    // Open the inline editor on a FAILED variant, pre-filled with the prompt
    // that failed so the user can tweak it and regenerate in place.
    openRetryEdit(charId, variantId, prompt) {
      this.editingVariant = { char_id: charId, variant_id: variantId, mode: 'retry' };
      this.editPrompt = prompt || '';
    },

    // Replace a variant's image with an UPLOADED file (not generated here) —
    // e.g. when the app can't produce it (content policy). Slot → ready+imported.
    async replaceVariant(charId, variantId, file) {
      if (!this.job || !file) return;
      const fd = new FormData(); fd.append('file', file);
      const r = await fetch(`/api/jobs/${this.job.job_id}/characters/${charId}/variants/${variantId}/replace`,
                            { method: 'POST', body: fd });
      if (!r.ok) { this.notifyError('Import failed: ' + await r.text()); return; }
      this.job = await r.json();
      this.notifyInfo('Imported your image into this slot');
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
      if (!this.job) {
        // Pre-job: prefer the user's staged override, fall back to primary.
        return this.charSourceOverrides[ch.char_id] || ch.primary_image_id;
      }
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
        // Pre-job: stage the choice client-side. It'll be sent as
        // `character_source_image_ids[charId]` when the user hits Generate.
        // The picker's "currently selected" highlight already reads from
        // currentSourceImageId() which checks charSourceOverrides first.
        this.charSourceOverrides = { ...this.charSourceOverrides, [charId]: imageId };
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

    // True iff variant `v` is currently approved on character `jc`. Multi-
    // scene jobs may have multiple approvals (one per scene), all of which
    // animate in parallel in Step 4. Falls back to legacy single-field for
    // jobs created before the multi-approve migration ran.
    isApproved(jc, v) {
      if (!jc || !v) return false;
      const ids = jc.approved_variant_ids || [];
      if (ids.includes(v.variant_id)) return true;
      return jc.approved_variant_id === v.variant_id;
    },

    // Distinct (char, scene) pairs that have ≥1 ready variant but no
    // approval yet. Multi-scene jobs: a char with 3 scenes pending counts
    // as 3 here, not 1. Drives the "Approve all (N)" counter.
    _pendingApprovalPairs() {
      if (!this.job) return [];
      const sceneIds = (this.job.scenes || []).map(s => s.scene_id);
      const fallbackScene = sceneIds[0] || this.job.scene_id || null;
      const effectiveScenes = sceneIds.length ? sceneIds : [fallbackScene];
      const pairs = [];
      for (const jc of Object.values(this.job.characters || {})) {
        if (['rejected', 'animating', 'done'].includes(jc.status)) continue;
        const approved = new Set(jc.approved_variant_ids || []);
        if (jc.approved_variant_id) approved.add(jc.approved_variant_id);
        // Which scenes does this char already have an approval for?
        const coveredScenes = new Set();
        for (const v of (jc.images || [])) {
          if (approved.has(v.variant_id)) {
            coveredScenes.add(v.scene_id || fallbackScene);
          }
        }
        for (const sid of effectiveScenes) {
          if (coveredScenes.has(sid)) continue;
          const hasReady = (jc.images || []).some(
            v => (v.scene_id || fallbackScene) === sid && v.status === 'ready',
          );
          if (hasReady) pairs.push({ char_id: jc.char_id, scene_id: sid });
        }
      }
      return pairs;
    },

    canApproveAll() {
      if (!this.job || this.job.movement_prompt) return false;
      return this._pendingApprovalPairs().length > 0;
    },

    pendingApprovalCount() {
      return this._pendingApprovalPairs().length;
    },

    async approveAll() {
      if (!this.job) return;
      const r = await fetch('/api/jobs/' + this.job.job_id + '/approve_all', {
        method: 'POST',
      });
      if (!r.ok) { this.notifyError('Approve all failed: ' + await r.text()); return; }
      const data = await r.json();
      this.job = data.job;
      this._scheduleSidebarRefresh();
    },

    async submitMovement() {
      if (!this.job) return;
      // Per-SCENE: one prompt + one duration per scene, shared by all that
      // scene's approved images. Build scene-keyed dicts, trim + drop empties.
      const scenes = this.scenesNeedingMovementPrompts();
      const prompts = {};
      const durationsByScene = {};
      const defaultDur = this.videoDurationSpec().default;
      for (const sc of scenes) {
        const t = (this.movementByScene[sc.scene_id] || '').trim();
        if (t) prompts[sc.scene_id] = t;
        durationsByScene[sc.scene_id] = this.durationByScene[sc.scene_id] || defaultDur;
      }
      const missing = scenes.filter(sc => !prompts[sc.scene_id]);
      if (missing.length) {
        this.notifyError(`Add a motion prompt for every scene (${missing.length} still empty)`);
        return;
      }
      const r = await fetch('/api/jobs/' + this.job.job_id + '/movement', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          movement_prompts: prompts,
          durations_by_scene: durationsByScene,
          videos_per_character: this.videosPerChar,
          video_model: this.swapVideoModel || 'kling-v3',
        }),
      });
      if (!r.ok) { this.notifyError('Movement submit failed: ' + await r.text()); return; }
      this.job = await r.json();
      this._scheduleSidebarRefresh();
    },

    // First approved image (any character) for a scene — the row thumbnail.
    firstApprovedImageForScene(sceneId) {
      if (!this.job) return null;
      const primaryId = (this.job.scenes || [])[0]?.scene_id || this.job.scene_id || null;
      for (const jc of Object.values(this.job.characters || {})) {
        const approved = new Set(jc.approved_variant_ids || []);
        if (jc.approved_variant_id) approved.add(jc.approved_variant_id);
        for (const v of (jc.images || [])) {
          if (approved.has(v.variant_id) && (v.scene_id || primaryId) === sceneId) {
            return v.url;
          }
        }
      }
      return null;
    },

    // Copy scene 1's prompt + duration onto every scene.
    applyFirstMovementToAll() {
      const scenes = this.scenesNeedingMovementPrompts();
      if (!scenes.length) return;
      const first = scenes[0].scene_id;
      const prompt = this.movementByScene[first] || '';
      const dur = this.durationByScene[first] || this.videoDurationSpec().default;
      for (const sc of scenes) {
        this.movementByScene[sc.scene_id] = prompt;
        this.durationByScene[sc.scene_id] = dur;
      }
    },

    // Which scenes (by scene_id) actually need a movement prompt? A scene
    // needs one iff at least one character has an approved variant whose
    // scene_id matches. Legacy variants with scene_id=null map to the job's
    // primary scene. Drives the Step-4 textareas + the submit-enabled flag.
    scenesNeedingMovementPrompts() {
      if (!this.job) return [];
      const scenes = this.job.scenes || [];
      const primaryId = scenes[0]?.scene_id || this.job.scene_id || null;
      const needed = new Set();
      for (const jc of Object.values(this.job.characters || {})) {
        const approved = new Set(jc.approved_variant_ids || []);
        if (jc.approved_variant_id) approved.add(jc.approved_variant_id);
        for (const v of (jc.images || [])) {
          if (!approved.has(v.variant_id)) continue;
          needed.add(v.scene_id || primaryId);
        }
      }
      return scenes.filter(s => needed.has(s.scene_id));
    },

    canSubmitMovement() {
      if (!this.job || this.job.movement_prompt) return false;
      if (!this.videoModelAvailable()) return false;
      const scenes = this.scenesNeedingMovementPrompts();
      if (scenes.length === 0) return false;
      // Every scene with approvals needs a non-empty motion prompt.
      return scenes.every(sc => (this.movementByScene[sc.scene_id] || '').trim());
    },

    // --- Arrange scenes (between Step 3 and Step 4) ---
    // Whether the arrange/duplicate/reorder panel is usable: a job is loaded,
    // it has approved variants, and movement hasn't been submitted yet.
    canArrangeScenes() {
      return !!this.job && !this.job.movement_prompt && this.hasApprovedChar()
             && (this.job.scenes || []).length > 0;
    },

    async duplicateScene(sceneId) {
      if (!this.job) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/scenes/${sceneId}/duplicate`,
                            { method: 'POST' });
      if (!r.ok) { this.notifyError('Duplicate failed: ' + await r.text()); return; }
      this.job = await r.json();
      this._scheduleSidebarRefresh();
    },

    async deleteScene(sceneId) {
      if (!this.job) return;
      // Removing a scene also deletes its variants + approvals — irreversible.
      if (!confirm('Remove this scene? Its swapped images and approvals are deleted too.')) return;
      const r = await fetch(`/api/jobs/${this.job.job_id}/scenes/${sceneId}`,
                            { method: 'DELETE' });
      if (!r.ok) { this.notifyError('Delete scene failed: ' + await r.text()); return; }
      // Drop any staged prompt/duration for the removed scene.
      delete this.movementByScene[sceneId];
      delete this.durationByScene[sceneId];
      this.job = await r.json();
      this._scheduleSidebarRefresh();
    },

    // Move a scene one slot earlier (-1) or later (+1), then PATCH the new order.
    async moveScene(sceneId, dir) {
      if (!this.job) return;
      const ids = (this.job.scenes || []).map(s => s.scene_id);
      const i = ids.indexOf(sceneId);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      const r = await fetch(`/api/jobs/${this.job.job_id}/scene_order`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scene_ids: ids }),
      });
      if (!r.ok) { this.notifyError('Reorder failed: ' + await r.text()); return; }
      this.job = await r.json();
    },

    // For the locked summary: rows of {sceneIndex, url, prompt} so the UI
    // can show what was submitted scene by scene.
    movementPromptRows() {
      if (!this.job) return [];
      const dict = this.job.movement_prompts || {};
      const scenes = this.job.scenes || [];
      const rows = [];
      scenes.forEach((s, idx) => {
        const p = dict[s.scene_id];
        if (p) rows.push({ sceneIndex: idx + 1, url: s.url, prompt: p,
                            scene_id: s.scene_id });
      });
      // Fall back to legacy single prompt if dict is empty (very old jobs).
      if (rows.length === 0 && this.job.movement_prompt) {
        rows.push({ sceneIndex: 1, url: scenes[0]?.url,
                     prompt: this.job.movement_prompt, scene_id: null });
      }
      return rows;
    },

    // Step 5 regen modal state. Opened by ↻ regen on any DONE video card.
    regenModal: {
      open: false, charId: null, videoId: null, charName: '',
      sceneId: null, prompt: '', hadOverride: false, submitting: false,
    },

    // IMAGE regen with an altered prompt (Hugo 2026-06-12): same retry
    // endpoint as ✕↻ but with the prompt edited first. Works from the
    // Reengineer strip (pass the run for view-splicing) and Step 3.
    imgRegenModal: {
      open: false, jobId: null, charId: null, variantId: null,
      charName: '', sceneId: null, prompt: '', loading: false,
      submitting: false, reRun: null,
    },

    async openImgRegenModal(jobId, charId, v, reRun = null) {
      const jc = (reRun ? reRun.job : this.job)?.characters?.[charId];
      this.imgRegenModal = {
        open: true, jobId, charId, variantId: v.variant_id,
        charName: jc?.name || charId, sceneId: v.scene_id || null,
        prompt: v.prompt || '', loading: !v.prompt, submitting: false, reRun,
      };
      // Reengineer context: prefill the ENGINE-EFFECTIVE prompt from the
      // server — slots can store stock templates that dispatch substitutes,
      // so the stored text is the wrong text to edit (review 2026-06-13).
      const idx = reRun
        ? (reRun.scenes || []).findIndex(sc => sc.scene_id === v.scene_id)
        : -1;
      if (reRun && idx >= 0) {
        this.imgRegenModal.loading = true;
        try {
          const r = await fetch(
            `/api/reengineer/${reRun.re_id}/scenes/${idx}/swap_prompt`
            + `?variant_id=${encodeURIComponent(v.variant_id)}`);
          if (r.ok) {
            const data = await r.json();
            if (this.imgRegenModal.variantId === v.variant_id) {
              this.imgRegenModal.prompt = data.prompt || '';
            }
          }
        } finally {
          this.imgRegenModal.loading = false;
        }
        return;
      }
      if (!v.prompt) {
        // Slim payloads omit variant prompts — fetch the full job.
        try {
          const r = await fetch('/api/jobs/' + jobId);
          if (r.ok) {
            const job = await r.json();
            const vv = (job.characters?.[charId]?.images || [])
              .find(x => x.variant_id === v.variant_id);
            if (this.imgRegenModal.variantId === v.variant_id) {
              this.imgRegenModal.prompt = vv?.prompt || '';
            }
          }
        } finally {
          this.imgRegenModal.loading = false;
        }
      }
    },

    async submitImgRegen() {
      const m = this.imgRegenModal;
      if (!m.jobId || !m.variantId || m.submitting) return;
      m.submitting = true;
      try {
        const r = await fetch(
          `/api/jobs/${m.jobId}/characters/${m.charId}/variants/${m.variantId}/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: (m.prompt || '').trim() || null }),
          });
        if (!r.ok) {
          this.notifyError('Regen misslyckades: ' + await r.text());
          return;
        }
        const job = await r.json();
        if (m.reRun) m.reRun.job = job;
        else if (this.job?.job_id === m.jobId) this.job = job;
        this.imgRegenModal.open = false;
        this.notifyInfo(`Regenererar ${m.charName}s bild med den ändrade prompten…`);
      } finally {
        m.submitting = false;
      }
    },

    // Scene-level image change for ALL characters (Hugo 2026-06-13): the
    // user describes the change in plain language → POST rewrite_prompt
    // (AI Director, pure preview) → editable prompt → POST regen_images.
    sceneImgModal: {
      open: false, reId: null, idx: null, sceneLabel: '', nChars: 0,
      change: '', prompt: '', currentPrompt: '', loading: false,
      directorLoading: false, submitting: false,
    },

    async openSceneImgModal(r, sc) {
      const nChars = Object.keys(r.job?.characters || {}).length;
      this.sceneImgModal = {
        open: true, reId: r.re_id, idx: sc.idx,
        sceneLabel: 'Scen ' + (sc.idx + 1) + (sc.summary ? ' — ' + sc.summary : ''),
        nChars, change: '', prompt: '', currentPrompt: '',
        loading: true, directorLoading: false, submitting: false,
      };
      // Prefill with the scene's ENGINE-EFFECTIVE swap prompt — the server
      // resolves what the images were ACTUALLY generated with (stock
      // templates are substituted at dispatch time, so the stored slot
      // prompt can be the wrong text to edit).
      try {
        const resp = await fetch(
          `/api/reengineer/${r.re_id}/scenes/${sc.idx}/swap_prompt`);
        if (resp.ok) {
          const data = await resp.json();
          if (this.sceneImgModal.idx === sc.idx && this.sceneImgModal.reId === r.re_id) {
            this.sceneImgModal.prompt = data.prompt || '';
            this.sceneImgModal.currentPrompt = data.prompt || '';
          }
        }
      } finally {
        this.sceneImgModal.loading = false;
      }
    },

    async sceneImgDirector() {
      const m = this.sceneImgModal;
      if (!(m.change || '').trim() || m.directorLoading) return;
      m.directorLoading = true;
      try {
        const r = await fetch(
          `/api/reengineer/${m.reId}/scenes/${m.idx}/rewrite_prompt`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            // current_prompt = the textarea content, so a second Director
            // pass builds on the previous rewrite/hand edits instead of
            // rebasing on the stored prompt.
            body: JSON.stringify({ change: m.change.trim(),
                                   current_prompt: (m.prompt || '').trim() || null }),
          });
        if (!r.ok) {
          this.notifyError('AI Director misslyckades: ' + await r.text());
          return;
        }
        const data = await r.json();
        m.prompt = data.prompt || '';
        m.currentPrompt = data.current_prompt || m.currentPrompt;
        this.notifyInfo('Directorn har skrivit om prompten — granska och regenerera.');
      } finally {
        m.directorLoading = false;
      }
    },

    async submitSceneImgRegen() {
      const m = this.sceneImgModal;
      if (!(m.prompt || '').trim() || m.submitting) return;
      m.submitting = true;
      try {
        const r = await fetch(
          `/api/reengineer/${m.reId}/scenes/${m.idx}/regen_images`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            // change rides along as the slots' QC intent: the judge treats
            // the requested deviation as authoritative instead of
            // "repairing" the original prop back.
            body: JSON.stringify({ prompt: m.prompt.trim(),
                                   change: (m.change || '').trim() || null }),
          });
        if (!r.ok) {
          this.notifyError('Regen misslyckades: ' + await r.text());
          return;
        }
        const view = await r.json();
        // Cache-busters: each slot regenerates into the SAME file path.
        const regen = view.regen_variants || {};
        const nonces = { ...this.reengineerRetryNonce };
        Object.values(regen).forEach(vid => { nonces[vid] = Date.now(); });
        this.reengineerRetryNonce = nonces;
        this._spliceReengineerView(view);
        this.sceneImgModal.open = false;
        this.notifyInfo(`Regenererar scenens bild för ${Object.keys(regen).length} karaktärer…`);
        this._startReengineerPolling();
        await this.refreshReengineer(m.reId);
      } finally {
        m.submitting = false;
      }
    },

    openRegenModal(charId, vv) {
      // Pre-fill the prompt with the previous override if any (so iterating
      // on the same video keeps building on what the user last tried),
      // otherwise the effective per-scene prompt the API resolved for us.
      this.regenModal = {
        open: true,
        charId,
        videoId: vv.video_id,
        charName: this.job?.characters?.[charId]?.name || charId,
        sceneId: this._sceneIdForVideo(charId, vv),
        prompt: vv.movement_prompt_override || vv.effective_movement_prompt || '',
        hadOverride: !!vv.movement_prompt_override,
        submitting: false,
      };
    },

    _sceneIdForVideo(charId, vv) {
      const jc = this.job?.characters?.[charId];
      if (!jc) return null;
      const src = (jc.images || []).find(im => im.variant_id === vv.source_variant_id);
      return src?.scene_id || null;
    },

    async submitRegen() {
      if (!this.regenModal.charId || !this.regenModal.videoId) return;
      this.regenModal.submitting = true;
      try {
        const body = {
          char_id: this.regenModal.charId,
          video_id: this.regenModal.videoId,
          prompt_override: this.regenModal.prompt,
        };
        const r = await fetch('/api/jobs/' + this.job.job_id + '/retry_video', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          this.notifyError('Regen failed: ' + await r.text());
          return;
        }
        this.job = await r.json();
        this.regenModal.open = false;
        this.notifyInfo(`Regenerating ${this.regenModal.charName}'s clip…`);
      } finally {
        this.regenModal.submitting = false;
      }
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

    // Reengineer card: show "↻ Ta om misslyckade" once clips exist and some
    // failed. Hidden at the approval gate (no clips yet — nothing to retry).
    reCanRetryFailed(r) {
      return r.status !== 'awaiting_approval' && this.failedVideosCount(r.job) > 0;
    },

    // Count of failed/error clips across all characters. Pass a job object
    // for the Reengineer card (r.job); defaults to the active Swap job.
    failedVideosCount(job) {
      const j = job || this.job;
      if (!j) return 0;
      return Object.values(j.characters || {}).reduce((n, jc) =>
        n + (jc.videos || []).filter(v => ['failed', 'error'].includes(v.status)).length, 0);
    },

    // "↻ Retry all failed" — one click re-submits every failed/error clip
    // (the recovery path for restart-stranded clips that resume marks failed).
    async retryAllFailedVideos(job) {
      const j = job || this.job;
      if (!j) return;
      const n = this.failedVideosCount(j);
      const r = await fetch('/api/jobs/' + j.job_id + '/retry_failed_videos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (!r.ok) { this.notifyError('Retry all failed: ' + await r.text()); return; }
      const updated = await r.json();
      if (this.job && this.job.job_id === j.job_id) this.job = updated;
      this.notifyInfo('Retrying ' + n + ' failed clip' + (n === 1 ? '' : 's') + '…');
    },

    // "+ N more videos" button per character in Step 5. Appends N takes
    // PER approved variant (so 2 approved scenes × n=3 = 6 new videos).
    // Existing videos are untouched. The lock-flag prevents rapid double-
    // clicks from queueing 30 parallel jobs.
    async generateMoreVideos(charId, n) {
      if (!this.job || this.generatingMoreFor) return;
      this.generatingMoreFor = charId;
      try {
        const r = await fetch('/api/jobs/' + this.job.job_id + '/generate_more_videos', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ char_id: charId, n }),
        });
        if (!r.ok) {
          this.notifyError('Generate more failed: ' + await r.text());
          return;
        }
        this.job = await r.json();
        const jc = this.job.characters?.[charId];
        const variants = (jc?.approved_variant_ids || []).length || 1;
        this.notifyMilestone('More videos queued',
          `${n} × ${variants} approved variant${variants === 1 ? '' : 's'} = ${n * variants} new`,
          { kind: 'info', tag: `gen-more-${charId}` });
      } catch (e) {
        this.notifyError('Generate more error: ' + e);
      } finally {
        this.generatingMoreFor = null;
      }
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
      this.movementPrompts = {};
      this._scheduleSidebarRefresh();
    },

    canUnlockMovement() {
      if (!this.job || !this.job.movement_prompt) return false;
      return !Object.values(this.job.characters).some(
        c => (c.videos || []).some(v => v.status === 'done')
      );
    },

    // --- Step-4 video model helpers ----------------------------------------

    // Lookup the chosen video model in the models registry, falling back to
    // a minimal stub so the UI still renders before /api/generations/models
    // has loaded.
    _videoModelEntry() {
      const slug = this.swapVideoModel || 'kling-v3';
      return (this.models.video || []).find(m => m.slug === slug)
        || { slug, label: slug, available: true };
    },

    videoModelLabel() {
      return this._videoModelEntry().label;
    },

    videoModelAvailable() {
      return !!this._videoModelEntry().available;
    },

    videoModelLockedReason() {
      const m = this._videoModelEntry();
      if (m.available) return '';
      // Match the label-style hint used elsewhere in the app for missing keys.
      return `${m.label} is locked — add the matching API key in .env to unlock.`;
    },

    // Duration spec for the currently-selected video model. Used by the
    // Step-4 duration dropdown to gate the user to values their provider
    // actually accepts. Falls back to a sensible [5] when models haven't
    // loaded yet (UI still renders).
    videoDurationSpec() {
      const m = this._videoModelEntry();
      if (m.duration_options && m.duration_options.length) {
        return { options: m.duration_options, default: m.duration_default || m.duration_options[0] };
      }
      return { options: [5], default: 5 };
    },

    // When the model picker changes, snap durationSecs to either the
    // user's previous pick (if it's still valid for the new model) or
    // the new model's default. Wired via @change on the model <select>.
    syncDurationToModel() {
      const spec = this.videoDurationSpec();
      if (this.swapDurationSecs && spec.options.includes(this.swapDurationSecs)) {
        return;  // user's pick is still valid — keep it
      }
      this.swapDurationSecs = spec.default;
    },

    openEdit(charId, variantId) {
      // 'edit' mode (ready variant) → spawns a NEW variant from a change prompt.
      this.editingVariant = { char_id: charId, variant_id: variantId, mode: 'edit' };
      this.editPrompt = '';
    },

    closeEdit() {
      this.editingVariant = null;
      this.editPrompt = '';
    },

    async submitEdit() {
      if (!this.job || !this.editingVariant || !this.editPrompt.trim()) return;
      const { char_id, variant_id, mode } = this.editingVariant;
      // 'retry' mode (failed variant) → regenerate this slot in place with the
      // edited prompt. 'edit' mode (ready variant) → spawn a new comparison
      // variant via edit_variant.
      if (mode === 'retry') {
        await this.retryVariant(char_id, variant_id, this.editPrompt.trim());
        this.closeEdit();
        return;
      }
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

    // Total number of APPROVED IMAGES across all characters (multi-scene
    // jobs may have multiple per char — one per scene). Drives the Step-4
    // total-video calculation and cost estimate. Falls back to counting the
    // legacy single field for very old jobs whose state predates the
    // multi-approve migration.
    approvedCount() {
      if (!this.job) return 0;
      let total = 0;
      for (const c of Object.values(this.job.characters || {})) {
        const ids = c.approved_variant_ids || [];
        if (ids.length) {
          total += ids.length;
        } else if (c.approved_variant_id) {
          total += 1;
        }
      }
      return total;
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
      this.endPoses = {};
      // Default: all library characters checked. startNewJobInProject() will
      // override this with the project's preset if there is one.
      this.selectedCharacters = (this.library || []).map(c => c.char_id);
      this.movementPrompt = '';
      this.movementPrompts = {};
      this.editingVariant = null;
      this.editPrompt = '';
      this.editingTitle = false;
      this.draftTitle = '';
      this.jobCost = null;
      this.swapPrompt = this.swapDefaultPrompt;
      this.swapModel = 'gpt-image';
      this.swapVideoModel = 'kling-v3';
      this.swapDurationSecs = null;
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
        this._fireSwapMilestones(this.job);
        return;
      }
      if (!this.job || evt.job_id !== this.job.job_id) return;
      this._scheduleSidebarRefresh();
      try {
        const r = await fetch('/api/jobs/' + this.job.job_id);
        if (r.ok) this.job = await r.json();
      } catch (_) {}
      // Refresh job cost on terminal-ish events (a variant landed / video done / failed
      // / compile done — compile runs Whisper + maybe ElevenLabs which both bill).
      if (['variant.ready', 'variant.failed', 'video.ready', 'video.failed', 'video.submitted',
           'char.compile_done', 'char.compile_failed'].includes(evt.kind)) {
        this.loadJobCost(this.job.job_id);
        this.loadDailyCost();
      }
      // Phase 4 pipeline: refresh job whenever a char's pipeline status
      // changes, plus notify on terminal states.
      if (evt.kind === 'char.pipeline_status') {
        try {
          const r = await fetch('/api/jobs/' + this.job.job_id);
          if (r.ok) this.job = await r.json();
        } catch (_) {}
        if (evt.status === 'done' || evt.status === 'failed') {
          const jc = this.job?.characters?.[evt.char_id];
          const allTerminal = this.job && Object.values(this.job.characters || {})
            .filter(c => c.pipeline_status)
            .every(c => ['done', 'failed'].includes(c.pipeline_status));
          if (allTerminal) this.pipelineRunning = false;
          if (jc && this.notifyMilestone) {
            const ok = evt.status === 'done';
            this.notifyMilestone(
              `${jc.name} pipeline ${ok ? 'done' : 'failed'}`,
              ok ? (evt.drive_link || 'Rendered in Resolve')
                 : (evt.error || 'see UI for details'),
              { kind: ok ? 'done' : 'error',
                tag: `pipeline-${this.job.job_id}-${evt.char_id}` },
            );
          }
        }
      }
      // Notify the user when their compile is done (matches the existing
      // per-batch milestone pattern).
      if (evt.kind === 'char.compile_done') {
        const jc = this.job?.characters?.[evt.char_id];
        if (jc && this.notifyMilestone) {
          this.notifyMilestone(
            `${jc.name} compile done`,
            'Final video ready in Step 6',
            { kind: 'done', tag: `compile-${this.job.job_id}-${evt.char_id}` },
          );
        }
      }
      // A fresh variant means this character's library gallery is stale.
      if (evt.kind === 'variant.ready' && evt.char_id) {
        this._invalidateGalleryFor(evt.char_id);
      }
      // Notification milestones — fire AFTER the job state has been refetched
      // above so we compare apples-to-apples against the snapshot.
      this._fireSwapMilestones(this.job);
    },

    // Compare the freshly-refetched swap job against the last snapshot we
    // saw and fire exactly one milestone per transition:
    //   • per char: any-status → awaiting_approval  (approval gate)
    //   • job-level: not all terminal → all terminal  (batch complete)
    _fireSwapMilestones(job) {
      if (!job || !job.job_id) return;
      const TERMINAL = new Set(['done', 'rejected', 'failed']);
      const prevSnap = this._lastSwapJobSnapshot[job.job_id] || { chars: {}, allTerminal: false };
      const nextChars = {};
      let allTerminal = true;
      const charEntries = Object.entries(job.characters || {});
      for (const [cid, jc] of charEntries) {
        nextChars[cid] = jc.status;
        if (!TERMINAL.has(jc.status)) allTerminal = false;
        const prevStatus = prevSnap.chars[cid];
        if (prevStatus !== 'awaiting_approval' && jc.status === 'awaiting_approval') {
          this.notifyMilestone(
            'Variant ready — approve',
            `${jc.name || cid}: first variant landed, pick one to keep`,
            { kind: 'approval', tag: `swap-${job.job_id}-${cid}-approve` },
          );
        }
      }
      if (charEntries.length > 0 && !prevSnap.allTerminal && allTerminal) {
        this.notifyMilestone(
          'Swap job complete',
          `${job.title || job.job_id}: all characters finished`,
          { kind: 'done', tag: `swap-${job.job_id}-done` },
        );
      }
      this._lastSwapJobSnapshot[job.job_id] = { chars: nextChars, allTerminal };
    },

  };
}
