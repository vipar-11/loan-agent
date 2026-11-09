/* Shared API client + formatting helpers for the Apex workbench pages. */
(function (global) {
  'use strict';

  const BASE = '/api/v1';

  /* ------------------------------------------------------------------
     Per-tab session identity.
     The server identifies the caller from the X-Apex-Session-Id header (establish_user_identity()).
     The token lives in sessionStorage, which is scoped to this browser tab: a second tab starts without
     one and is issued a different identity, so a file checked out here cannot be opened there.
     ------------------------------------------------------------------ */
  const SESSION_HEADER = 'X-Apex-Session-Id';
  const SESSION_STORAGE_KEY = 'apex.sessionId';
  let sessionPromise = null;  // single in-flight bootstrap, so parallel first calls share one identity
  let identity = null;

  function storedToken() {
    try { return sessionStorage.getItem(SESSION_STORAGE_KEY); } catch (_) { return null; }
  }
  function rememberToken(res) {
    const token = res.headers.get(SESSION_HEADER);
    if (!token || token === storedToken()) return;
    try { sessionStorage.setItem(SESSION_STORAGE_KEY, token); } catch (_) { /* storage disabled */ }
  }

  async function send(method, path, body, extra) {
    const opts = { method, headers: { 'Accept': 'application/json' }, ...(extra || {}) };
    const token = storedToken();
    if (token) opts.headers[SESSION_HEADER] = token;
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(BASE + path, opts);
    rememberToken(res);
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const err = new Error(extractDetail(data) || `${res.status} ${res.statusText}`);
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  /** Establishes (first call in this tab) or confirms this tab's identity. Memoized. */
  function ensureSession() {
    if (!sessionPromise) {
      sessionPromise = send('GET', '/session')
        .then(id => { identity = id; return id; })
        .catch(err => { sessionPromise = null; throw err; });
    }
    return sessionPromise;
  }

  /** Every API call carries this tab's identity; the very first one waits for the session to be established. */
  async function request(method, path, body, extra) {
    await ensureSession();
    return send(method, path, body, extra);
  }

  function extractDetail(data) {
    if (!data) return null;
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map(d => {
        const loc = (d.loc || []).filter(x => x !== 'body').join('.');
        return loc ? `${loc}: ${d.msg}` : d.msg;
      }).join(' · ');
    }
    return null;
  }

  const api = {
    get: (p) => request('GET', p),
    post: (p, b) => request('POST', p, b === undefined ? {} : b),

    health: () => api.get('/health'),
    system: () => api.get('/system'),
    stats: () => api.get('/stats'),
    personas: () => api.get('/personas'),
    policy: () => api.get('/policy'),
    installment: (amount) => api.get(`/calculator/installment?loan_amount=${encodeURIComponent(amount)}`),

    listApplications: (status) => api.get(`/applications${status ? `?status=${encodeURIComponent(status)}` : ''}`),
    getApplication: (id) => api.get(`/applications/${encodeURIComponent(id)}`),
    submitApplication: (payload) => api.post('/applications', payload),
    getDossier: (id) => api.get(`/applications/${encodeURIComponent(id)}/dossier`),
    getAuditTrail: (id) => api.get(`/applications/${encodeURIComponent(id)}/audit-trail`),
    heuristicClassifications: (id) => api.get(`/applications/${encodeURIComponent(id)}/classifications/heuristic`),
    recalculateRatios: (id, classifications) => api.post(`/applications/${encodeURIComponent(id)}/ratios/recalculate`, { classifications }),
    recordDecision: (id, payload) => api.post(`/applications/${encodeURIComponent(id)}/decision`, payload),
    session: () => ensureSession(),
    currentIdentity: () => identity,
    // Review checkout: one identity (browser tab) per PENDING_REVIEW file; renewCheckout() is the lease heartbeat.
    checkout: (id) => api.post(`/applications/${encodeURIComponent(id)}/checkout`),
    renewCheckout: (id) => api.post(`/applications/${encodeURIComponent(id)}/checkout/renew`),
    // keepalive lets the release complete while the tab is being closed; it is sent synchronously (no session
    // bootstrap await), which is safe because a checkout can only exist after the session was established.
    releaseCheckout: (id, keepalive = false) => keepalive
      ? send('DELETE', `/applications/${encodeURIComponent(id)}/checkout`, undefined, { keepalive: true })
      : request('DELETE', `/applications/${encodeURIComponent(id)}/checkout`),

    adminStatusSummary: () => api.get('/admin/status-summary'),
    adminScheduler: () => api.get('/admin/scheduler'),
    adminScheduleConfig: () => api.get('/admin/scheduler/config'),
    adminUpdateScheduleConfig: (payload) => request('PUT', '/admin/scheduler/config', payload),
    adminWorkflowRuns: (limit = 50) => api.get(`/admin/workflow-runs?limit=${limit}`),
    adminIntakeStatus: () => api.get('/admin/intake'),
    adminIntakeBatches: (limit = 25) => api.get(`/admin/intake/batches?limit=${limit}`),
    adminIntakeBatch: (id) => api.get(`/admin/intake/batches/${encodeURIComponent(id)}`),
    adminIntakeScan: () => api.post('/admin/intake/scan'),
  };

  const fmt = {
    currency(v) {
      if (v === null || v === undefined || v === '') return '—';
      const n = Number(v);
      if (Number.isNaN(n)) return '—';
      return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    },
    pct(v, digits = 2) {
      const n = Number(v);
      return Number.isNaN(n) ? '—' : `${n.toFixed(digits)}%`;
    },
    mult(v) {
      const n = Number(v);
      return Number.isNaN(n) ? '—' : `${n.toFixed(2)}x`;
    },
    escape(s) {
      return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    badgeForDecision(d) {
      const u = String(d || '').toUpperCase();
      if (u.includes('APPROV')) return 'badge-green';
      if (u.includes('DEN') || u.includes('REJECT')) return 'badge-red';
      if (u.includes('ESCALAT') || u.includes('PENDING')) return 'badge-amber';
      return 'badge-slate';
    },
    dtiColor(v) {
      const n = Number(v || 0);
      return n <= 38 ? 'txt-green' : (n <= 45 ? 'txt-amber' : 'txt-red');
    },
  };

  /* Tiny tab controller: any element with [data-tabs] containing .tab-btn[data-tab] + .tab-panel[data-panel]. */
  function initTabs(root) {
    root.querySelectorAll('[data-tabs]').forEach(group => {
      const btns = group.querySelectorAll('.tab-btn');
      const scope = group.getAttribute('data-tabs');
      const panels = root.querySelectorAll(`.tab-panel[data-scope="${scope}"]`);
      btns.forEach(btn => btn.addEventListener('click', () => {
        btns.forEach(b => b.classList.toggle('active', b === btn));
        panels.forEach(p => p.classList.toggle('active', p.getAttribute('data-panel') === btn.getAttribute('data-tab')));
      }));
    });
  }

  function showAlert(container, type, message) {
    container.innerHTML = `<div class="alert alert-${type}">${fmt.escape(message)}</div>`;
  }

  /* ------------------------------------------------------------------
     Profile menu (every page, top-right corner): avatar + dropdown for this browser tab's session.
     Styled like a standard post-login account icon — the slot a future sign-in / account menu will occupy.
     ------------------------------------------------------------------ */
  const AVATAR_PALETTE = ['#2563EB', '#059669', '#7C3AED', '#D97706', '#DB2777', '#0EA5E9', '#475569', '#B45309'];

  function avatarColorFor(seed) {
    let hash = 0;
    for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
    return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
  }

  /** "Session 3F9A1C02" -> "3F" today; "Some Name" (a future authenticated name) -> "SN". */
  function initialsFor(displayName) {
    const anon = /^Session ([0-9A-F]+)$/i.exec(displayName || '');
    if (anon) return anon[1].slice(0, 2).toUpperCase();
    const words = String(displayName || '').trim().split(/\s+/).filter(Boolean);
    return ((words[0] || '?')[0] + (words[1] ? words[1][0] : '')).toUpperCase();
  }

  function renderProfileMenu() {
    const avatar = document.getElementById('session-badge');
    const label = document.getElementById('session-label');
    if (!avatar || !label) return;

    ensureSession()
      .then(id => {
        label.textContent = id.display_name;
        const initials = document.getElementById('session-initials');
        if (initials) initials.textContent = initialsFor(id.display_name);
        avatar.style.background = avatarColorFor(id.user_id || id.display_name);
      })
      .catch(() => {
        label.textContent = 'Session unavailable';
        avatar.classList.add('session-error');
      });

    const dropdown = document.getElementById('profile-dropdown');
    if (!dropdown) return;
    const closeDropdown = () => { dropdown.classList.remove('open'); avatar.setAttribute('aria-expanded', 'false'); };
    avatar.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = dropdown.classList.toggle('open');
      avatar.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', (e) => { if (!dropdown.contains(e.target)) closeDropdown(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDropdown(); });
  }

  global.ApexAPI = api;
  global.ApexFmt = fmt;
  global.ApexUI = { initTabs, showAlert };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderProfileMenu);
  else renderProfileMenu();
})(window);
