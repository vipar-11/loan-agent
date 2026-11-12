/* Sign-in page: user ID & password sign-in, first-time password setup, voluntary password change.
   Deliberately independent of api.js (which redirects here on 401). */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const BASE = '/api/v1';
  const CSRF = { 'X-Requested-With': 'ApexWorkbench' };  // required on state-changing calls with the session cookie
  const alertBox = $('auth-alert');
  const params = new URLSearchParams(window.location.search);
  const nextPath = document.body.getAttribute('data-next') || '';
  const minLength = Number(document.body.getAttribute('data-min-length')) || 8;
  const changeRequested = params.get('change') === '1';
  let identity = null;

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function showAlert(type, message) { alertBox.innerHTML = `<div class="alert alert-${type}">${escapeHtml(message)}</div>`; }
  function clearAlert() { alertBox.innerHTML = ''; }

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

  /** Where to go once fully signed in: the requested page if this user may open it, else the user's home page. */
  function destination(id) {
    const pages = (id && id.pages) || [];
    if (nextPath && pages.some(p => nextPath === p || nextPath.startsWith(p + '?') || nextPath.startsWith(p + '/'))) return nextPath;
    return (id && id.home_path) || '/';
  }

  function show(form) {
    $('auth-loading').classList.add('hidden');
    $('sign-in-form').classList.toggle('hidden', form !== 'sign-in');
    $('password-form').classList.toggle('hidden', form !== 'password');
    if (form !== 'sign-in') $('signup-link').classList.add('hidden');
  }

  function showSignIn() {
    show('sign-in');
    $('signup-link').classList.remove('hidden');
    $('user_id').focus();
    call('GET', '/auth/providers').then(options => {
      const redirects = (options || []).filter(o => o.type === 'redirect');
      $('sso-options').innerHTML = redirects.length
        ? `<div class="auth-divider"><span>or</span></div>` + redirects.map(o =>
            `<a class="btn btn-block" style="margin-top:8px;" href="${escapeHtml(o.url)}">${escapeHtml(o.label)}</a>`).join('')
        : '';
    }).catch(() => { /* password sign-in still works */ });
  }

  function showPasswordForm(firstTime) {
    show('password');
    $('password-title').textContent = firstTime ? 'Choose your password' : 'Change your password';
    $('password-intro').textContent = (firstTime
      ? `Welcome, ${identity.display_name}. You need to choose a new password before continuing. `
      : `Signed in as ${identity.display_name} (${identity.user_id}). `) + `Use at least ${minLength} characters.`;
    $('current-password-row').classList.toggle('hidden', firstTime);
    $('link-cancel-change').classList.toggle('hidden', firstTime);
    $('password-form').dataset.firstTime = firstTime ? '1' : '';
    (firstTime ? $('new_password') : $('current_password')).focus();
  }

  function route(id) {
    identity = id;
    if (id.password_change_required) return showPasswordForm(true);
    if (changeRequested) return showPasswordForm(false);
    window.location.replace(destination(id));
  }

  /* ---------------- Sign in ---------------- */
  $('sign-in-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    clearAlert();
    const userId = $('user_id').value.trim();
    if (!userId) { showAlert('error', 'Please enter your user ID.'); return; }
    const btn = $('btn-sign-in');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Signing in…';
    try {
      const res = await call('POST', '/auth/login', { user_id: userId, password: $('password').value });
      try { sessionStorage.removeItem('apex.sessionId'); } catch (_) { /* storage disabled */ }
      route(res.identity);
    } catch (ex) {
      showAlert('error', ex.message);
      $('password').value = '';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  });

  /* ---------------- Set / change password ---------------- */
  $('password-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    clearAlert();
    const firstTime = $('password-form').dataset.firstTime === '1';
    const newPassword = $('new_password').value;
    if (newPassword.length < minLength) { showAlert('error', `The new password must be at least ${minLength} characters long.`); return; }
    if (newPassword !== $('confirm_password').value) { showAlert('error', 'The two new passwords do not match.'); return; }
    const btn = $('btn-save-password');
    btn.disabled = true;
    try {
      const id = await call('POST', '/auth/password', {
        current_password: firstTime ? '' : $('current_password').value,
        new_password: newPassword,
      });
      showAlert('success', 'Password saved. Redirecting…');
      window.location.replace(destination(id));
    } catch (ex) {
      if (ex.status === 401) { showAlert('error', 'Your session has expired. Please sign in again.'); showSignIn(); return; }
      showAlert('error', ex.message);
    } finally {
      btn.disabled = false;
    }
  });

  $('link-cancel-change').addEventListener('click', (e) => { e.preventDefault(); window.location.replace(destination(identity)); });
  $('link-sign-out').addEventListener('click', async (e) => {
    e.preventDefault();
    try { await call('POST', '/auth/logout'); } catch (_) { /* already signed out */ }
    window.location.replace('/login');
  });

  /* ---------------- Boot: already signed in? ---------------- */
  call('GET', '/session')
    .then(route)
    .catch((ex) => {
      showSignIn();
      if (ex.status !== 401) showAlert('error', ex.message);
    });
})();
