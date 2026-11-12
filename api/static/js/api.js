/* Shared API client + formatting helpers for the Apex workbench pages. */
(function (global) {
  'use strict';

  const BASE = '/api/v1';

  /* ------------------------------------------------------------------
     Sign-in: the browser holds an HttpOnly session cookie set by POST /api/v1/auth/login (see login.js); fetch()
     sends it automatically. State-changing calls also carry CSRF_HEADER, which the server requires for cookie
     sessions. Once the session ends (401) — or while a password change is pending — the page returns to /login.

     Per-tab session: review checkouts are exclusive per browser tab. The server issues a tab token
     (X-Apex-Session-Id) on this tab's first call (GET /api/v1/session); it lives in sessionStorage, which is scoped
     to this tab, so a second tab is a different checkout holder even for the same signed-in user.
     ------------------------------------------------------------------ */
  const SESSION_HEADER = 'X-Apex-Session-Id';
  const SESSION_STORAGE_KEY = 'apex.sessionId';
  const CSRF_HEADER = 'X-Requested-With';
  const CSRF_VALUE = 'ApexWorkbench';
  const PASSWORD_CHANGE_HEADER = 'X-Apex-Password-Change-Required';
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

  function forgetToken() {
    try { sessionStorage.removeItem(SESSION_STORAGE_KEY); } catch (_) { /* storage disabled */ }
  }

  let leaving = false;
  /** Back to the sign-in page, returning here afterwards. */
  function toSignIn() {
    if (leaving) return;
    leaving = true;
    const here = window.location.pathname + window.location.search;
    window.location.assign(`/login?next=${encodeURIComponent(here)}`);
  }

  async function send(method, path, body, extra) {
    const opts = {
      method,
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', [CSRF_HEADER]: CSRF_VALUE },
      ...(extra || {}),
    };
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
    if (res.status === 401 || (res.status === 403 && res.headers.get(PASSWORD_CHANGE_HEADER))) toSignIn();
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
    myApplications: () => api.get('/applications/mine'),
    /** Queue listing with metadata filters / ordering, e.g. { status: 'pending_review', sort: 'oldest', q: 'some' }. */
    queryApplications: (params) => {
      const qs = new URLSearchParams();
      Object.entries(params || {}).forEach(([k, v]) => {
        if (v !== undefined && v !== null && String(v).trim() !== '') qs.append(k, String(v).trim());
      });
      const query = qs.toString();
      return api.get(`/applications${query ? `?${query}` : ''}`);
    },
    getDossier: (id) => api.get(`/applications/${encodeURIComponent(id)}/dossier`),
    getAuditTrail: (id) => api.get(`/applications/${encodeURIComponent(id)}/audit-trail`),
    heuristicClassifications: (id) => api.get(`/applications/${encodeURIComponent(id)}/classifications/heuristic`),
    recalculateRatios: (id, classifications) => api.post(`/applications/${encodeURIComponent(id)}/ratios/recalculate`, { classifications }),
    recordDecision: (id, payload) => api.post(`/applications/${encodeURIComponent(id)}/decision`, payload),
    session: () => ensureSession(),
    currentIdentity: () => identity,
    // Review checkout: one identity (browser tab) per PENDING_REVIEW file; renewCheckout() is the lease heartbeat.
    checkout: (id) => api.post(`/applications/${encodeURIComponent(id)}/checkout`),
    // Get Work: the server picks (and checks out to this tab) the next highest-priority file; application is null if none.
    nextForReview: () => api.post('/applications/next-for-review'),
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

  const signOutHooks = [];
  /** Registers work that must happen while still signed in, before signing out (e.g. releasing a checkout). */
  function onSignOut(fn) { signOutHooks.push(fn); }

  /** Ends the sign-in session (cookie) and returns to the sign-in page. */
  async function signOut() {
    await Promise.allSettled(signOutHooks.map(fn => Promise.resolve().then(fn)));
    leaving = true;
    try { await send('POST', '/auth/logout', {}); } catch (_) { /* already signed out */ }
    forgetToken();
    window.location.assign('/login');
  }

  /* ------------------------------------------------------------------
     Profile menu (every page, top-right corner): avatar + dropdown for the signed-in user
     (name, user ID and role are rendered by the server; see base.html).
     ------------------------------------------------------------------ */
  const AVATAR_PALETTE = ['#2563EB', '#059669', '#7C3AED', '#D97706', '#DB2777', '#0EA5E9', '#475569', '#B45309'];

  function avatarColorFor(seed) {
    let hash = 0;
    for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
    return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
  }

  /** "Some Name" -> "SN"; "Administrator" -> "A". */
  function initialsFor(displayName) {
    const words = String(displayName || '').trim().split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w[0] || ''));
    return ((words[0] || '?')[0] + (words.length > 1 ? words[words.length - 1][0] : '')).toUpperCase();
  }

  function renderProfileMenu() {
    const avatar = document.getElementById('session-badge');
    const label = document.getElementById('session-label');
    if (!avatar || !label) return;

    const initials = document.getElementById('session-initials');
    if (initials) initials.textContent = initialsFor(label.textContent);
    avatar.style.background = avatarColorFor(avatar.getAttribute('data-user-id') || label.textContent);
    ensureSession().catch(() => avatar.classList.add('session-error'));  // establishes this tab's session

    const signOutBtn = document.getElementById('btn-sign-out');
    if (signOutBtn) signOutBtn.addEventListener('click', signOut);

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
  global.ApexUI = { initTabs, showAlert, signOut, onSignOut };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderProfileMenu);
  else renderProfileMenu();
})(window);
