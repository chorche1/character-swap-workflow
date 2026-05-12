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
    creatingProject: false,
    draftNewProjectName: '',
    moveMenuJobId: null,
    searchQuery: '',
    jobCost: null,           // USD for the open job
    dailyCost: null,         // USD spent in last 24h
    _dailyCostTimer: null,
    toasts: [],              // {id, kind, msg, retry?}
    _toastSeq: 0,
    disk: null,              // {output_bytes, by_job}
    showDiskModal: false,
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
      await this.loadHealth();
      await this.loadLibrary();
      await this.loadProjects();
      await this.loadJobsList();
      await this.loadDailyCost();
      this.loadDisk();
      // Refresh daily cost every minute while the tab is open.
      this._dailyCostTimer = setInterval(() => this.loadDailyCost(), 60000);
      // URL routing: if we landed on /j/<id>, open that job.
      this._openFromUrl();
      window.addEventListener('popstate', () => this._openFromUrl());
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

    startCreateProject() {
      this.creatingProject = true;
      this.draftNewProjectName = '';
    },

    cancelCreateProject() {
      this.creatingProject = false;
      this.draftNewProjectName = '';
    },

    async submitCreateProject() {
      const name = (this.draftNewProjectName || '').trim();
      if (!name) { this.cancelCreateProject(); return; }
      const r = await fetch('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) { this.notifyError('Create failed: ' + await r.text()); return; }
      this.cancelCreateProject();
      await this.loadProjects();
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

    async uploadCharacters(files) {
      for (const f of files) {
        const fd = new FormData(); fd.append('file', f);
        const r = await fetch('/api/characters', { method: 'POST', body: fd });
        if (!r.ok) { this.notifyError('Character upload failed: ' + await r.text()); continue; }
      }
      await this.loadLibrary();
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
      this.selectedCharacters = [];
      this.movementPrompt = '';
      this.editingVariant = null;
      this.editPrompt = '';
      this.editingTitle = false;
      this.draftTitle = '';
      this.jobCost = null;
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
    },
  };
}
