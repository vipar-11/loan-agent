/* Shared API client + formatting helpers for the Apex workbench pages. */
(function (global) {
  'use strict';

  const BASE = '/api/v1';

  async function request(method, path, body) {
    const opts = { method, headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(BASE + path, opts);
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

    adminStatusSummary: () => api.get('/admin/status-summary'),
    adminScheduler: () => api.get('/admin/scheduler'),
    adminScheduleConfig: () => api.get('/admin/scheduler/config'),
    adminUpdateScheduleConfig: (payload) => request('PUT', '/admin/scheduler/config', payload),
    adminWorkflowRuns: (limit = 50) => api.get(`/admin/workflow-runs?limit=${limit}`),
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

  global.ApexAPI = api;
  global.ApexFmt = fmt;
  global.ApexUI = { initTabs, showAlert };
})(window);
