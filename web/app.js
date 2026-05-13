// Character Swap Studio — Alpine.js front-end.

function studio() {
  return {
    health: { openai_key: false, xai_key: false },
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
    editor: {
      sourceVideo: null,           // {file, url, name}
      thresholdDb: -30,
      minSilenceSecs: 0.4,
      padSecs: 0.05,
      trimming: false,
      template: 'tiktok',
      captioning: false,
      overrides: {                 // CaptionStyle field overrides; null until user touches them
        font: null, size: null, primary_color: null, outline_color: null,
        words_per_card: null, margin_v: null, highlight_color: null, box: null,
        all_caps: null,
      },
      lastResult: null,            // {output_url, kind: 'trim'|'captions', ...}
    },
    editorTemplates: [],
    editorHistory: [],
    swapPrompt: '',
    swapModel: 'gpt-image',
    swapDefaultPrompt: '',
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
    isDark: document.documentElement.classList.contains('dark'),
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
      await this.loadGenerations();
      await this.loadSwapDefaults();
      // Refresh daily cost every minute while the tab is open.
      this._dailyCostTimer = setInterval(() => this.loadDailyCost(), 60000);
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
      try {
        const r = await fetch('/api/swap/defaults');
        if (!r.ok) return;
        const data = await r.json();
        this.swapDefaultPrompt = data.prompt || '';
        if (!this.swapPrompt) this.swapPrompt = this.swapDefaultPrompt;
      } catch (_) {}
    },

    resetSwapPrompt() {
      this.swapPrompt = this.swapDefaultPrompt;
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

    _historyForKind(kind) {
      return kind === 'image' ? this.imageHistory
           : kind === 'video' ? this.videoHistory
           : kind === 'avatar' ? this.avatarHistory
           : this.audioHistory;
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
      if (this.audioGen.sourceAudio?.url) URL.revokeObjectURL(this.audioGen.sourceAudio.url);
      this.audioGen.sourceAudio = { file, url: URL.createObjectURL(file), name: file.name };
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
        if (this.audioGen.sourceAudio?.url) URL.revokeObjectURL(this.audioGen.sourceAudio.url);
        this.audioGen.sourceAudio = null;
        this.audioGen.script = '';
      } finally {
        this.audioGen.generating = false;
      }
    },

    // --- Video Editor (silence-trim + captions) -----------------------------

    async loadEditorTemplates() {
      try {
        const r = await fetch('/api/editor/templates');
        if (r.ok) this.editorTemplates = await r.json();
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

    selectEditorTemplate(slug) {
      this.editor.template = slug;
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
        this.avatarGen.script = '';
      } finally {
        this.avatarGen.generating = false;
      }
    },

    // --- generations: image -------------------------------------------------

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
        // Free object URLs and reset form
        for (const r of this.imageGen.refs) if (r.url) URL.revokeObjectURL(r.url);
        this.imageGen.refs = [];
        this.imageGen.prompt = '';
      } finally {
        this.imageGen.generating = false;
      }
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
        if (this.videoGen.ref?.url) URL.revokeObjectURL(this.videoGen.ref.url);
        this.videoGen.ref = null;
        this.videoGen.prompt = '';
      } finally {
        this.videoGen.generating = false;
      }
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

    // --- theme ---------------------------------------------------------------

    toggleTheme() {
      const root = document.documentElement;
      if (root.classList.contains('dark')) {
        root.classList.remove('dark');
        localStorage.setItem('theme', 'light');
        this.isDark = false;
      } else {
        root.classList.add('dark');
        localStorage.setItem('theme', 'dark');
        this.isDark = true;
      }
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
      this.scene = await r.json();
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
      if (!this.scene || this.selectedCharacters.length === 0) return;
      this.generating = true;
      try {
        const r = await fetch('/api/jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            scene_id: this.scene.scene_id,
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
      // Reload scene preview from the job's scene
      if (this.job.scene_image_url) {
        this.scene = {
          scene_id: this.job.scene_id,
          url: this.job.scene_image_url,
          original_name: this.job.title || this.job.job_id,
        };
      }
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
