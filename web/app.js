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
      await this.loadHealth();
      await this.loadLibrary();
      await this.loadJobsList();
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
      if (!r.ok) { alert('Scene upload failed: ' + await r.text()); return; }
      this.scene = await r.json();
    },

    async uploadCharacters(files) {
      for (const f of files) {
        const fd = new FormData(); fd.append('file', f);
        const r = await fetch('/api/characters', { method: 'POST', body: fd });
        if (!r.ok) { alert('Character upload failed: ' + await r.text()); continue; }
      }
      await this.loadLibrary();
    },

    async deleteCharacter(cid) {
      if (!confirm('Remove this character from the library?')) return;
      const r = await fetch('/api/characters/' + cid, { method: 'DELETE' });
      if (!r.ok) { alert('Delete failed: ' + await r.text()); return; }
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
      if (!r.ok) { alert('Rename failed: ' + await r.text()); return; }
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
          }),
        });
        if (!r.ok) { alert('Job creation failed: ' + await r.text()); return; }
        const job = await r.json();
        this.job = job;
        this.connectWS(job.job_id);
        await this.loadJobsList();
      } finally {
        this.generating = false;
      }
    },

    async openJob(jobId) {
      if (this.ws) { try { this.ws.close(); } catch (_) {} this.ws = null; this.wsConnected = false; }
      const r = await fetch('/api/jobs/' + jobId);
      if (!r.ok) { alert('Could not open job: ' + await r.text()); return; }
      this.job = await r.json();
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
      this.editingVariant = null;
      this.editingTitle = false;
      this.connectWS(jobId);
    },

    async deleteJob(jobId) {
      if (!confirm('Delete this job and all its generated files?')) return;
      const r = await fetch('/api/jobs/' + jobId, { method: 'DELETE' });
      if (!r.ok) { alert('Delete failed: ' + await r.text()); return; }
      if (this.job && this.job.job_id === jobId) this.resetJob();
      await this.loadJobsList();
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

    async saveTitle() {
      if (!this.job) return;
      const title = this.draftTitle.trim();
      if (!title) { this.cancelEditTitle(); return; }
      const r = await fetch('/api/jobs/' + this.job.job_id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      if (!r.ok) { alert('Rename failed: ' + await r.text()); return; }
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
      if (!r.ok) { alert('Action failed: ' + await r.text()); return; }
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
      if (!r.ok) { alert('Movement submit failed: ' + await r.text()); return; }
      this.job = await r.json();
      this._scheduleSidebarRefresh();
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
      if (!r.ok) { alert('Edit failed: ' + await r.text()); return; }
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

    newJob() { this.resetJob(); },

    resetJob() {
      if (this.ws) { try { this.ws.close(); } catch (_) {} }
      this.ws = null;
      this.wsConnected = false;
      this.job = null;
      this.scene = null;
      this.selectedCharacters = [];
      this.movementPrompt = '';
      this.editingVariant = null;
      this.editPrompt = '';
      this.editingTitle = false;
      this.draftTitle = '';
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
    },
  };
}
