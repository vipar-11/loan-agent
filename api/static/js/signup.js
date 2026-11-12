/* Self-service sign-up (/signup): creates a Loan Applicant account and signs in immediately.
   Deliberately independent of api.js (which redirects to /login on 401), same pattern as login.js. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const BASE = '/api/v1';
  const CSRF = { 'X-Requested-With': 'ApexWorkbench' };
  const alertBox = $('auth-alert');
  const minLength = Number(document.body.getAttribute('data-min-length')) || 8;

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function showAlert(type, message) { alertBox.innerHTML = `<div class="alert alert-${type}">${escapeHtml(message)}</div>`; }

  async function call(method, path, body) {
    const opts = { method, headers: { 'Accept': 'application/json', ...CSRF }, credentials: 'same-origin' };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const res = await fetch(BASE + path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const detail = data && (typeof data.detail === 'string' ? data.detail
        : Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(' · ') : null);
      const err = new Error(detail || `${res.status} ${res.statusText}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function show(form) { $('auth-loading').classList.add('hidden'); $('signup-form').classList.toggle('hidden', !form); }

  $('signup-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    alertBox.innerHTML = '';
    const userId = $('user_id').value.trim();
    const password = $('password').value;
    if (!userId) { showAlert('error', 'Please choose a user ID.'); return; }
    if (password.length < minLength) { showAlert('error', `Your password must be at least ${minLength} characters long.`); return; }
    if (password !== $('confirm_password').value) { showAlert('error', 'The two passwords do not match.'); return; }

    const btn = $('btn-sign-up');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Creating account…';
    try {
      await call('POST', '/auth/signup', { user_id: userId, password, display_name: $('display_name').value.trim() });
      try { sessionStorage.removeItem('apex.sessionId'); } catch (_) { /* storage disabled */ }
      window.location.replace('/portal');
    } catch (ex) {
      showAlert('error', ex.message);
      btn.disabled = false;
      btn.textContent = 'Create account';
    }
  });

  /* Already signed in (fully)? Skip straight to the workbench instead of letting them create a second account. */
  call('GET', '/session')
    .then(id => { if (!id.password_change_required) window.location.replace(id.home_path || '/'); else show(true); })
    .catch(() => show(true));
})();
