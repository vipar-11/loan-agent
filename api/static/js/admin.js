/* Administrator console: status monitor, approval-workflow job monitor, job frequency configuration. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const E = ApexFmt.escape;
  const alertBox = $('admin-alert');
  const RUNS_TO_SHOW = 10;

  ApexUI.initTabs(document);

  const HEALTH = {
    HEALTHY:   { badge: 'badge-green', icon: '✅', title: 'Job is running on schedule' },
    RUNNING:   { badge: 'badge-blue',  icon: '⏳', title: 'A job run is in progress' },
    OVERDUE:   { badge: 'badge-red',   icon: '🚨', title: 'Job is overdue' },
    NEVER_RUN: { badge: 'badge-amber', icon: '⚠️', title: 'Job has never run' },
  };
  const MAIN_COLORS = { 'new': 'txt-blue', 'pending processing': '', 'pending review': 'txt-amber', 'approved': 'txt-green', 'rejected': 'txt-red' };

  function fmtDuration(seconds) {
    if (seconds == null) return '—';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60), s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  /* ---------------- Status monitor ---------------- */
  async function loadStatus() {
    const groups = await ApexAPI.adminStatusSummary();
    const total = groups.reduce((n, g) => n + g.count, 0);
    document.querySelectorAll('[data-stat=total]').forEach(el => el.textContent = total);
    groups.forEach(g => document.querySelectorAll(`[data-main="${g.status}"]`).forEach(el => el.textContent = g.count));

    $('status-cards').innerHTML = groups.map(g => `
      <div class="fin-metric">
        <div class="fin-metric-label">${E(g.label)}</div>
        <div class="fin-metric-value ${MAIN_COLORS[g.status] || ''}">${g.count}</div>
      </div>`).join('');

    $('status-table').innerHTML = `
      <table class="data-table">
        <thead><tr><th>Main Status</th><th>Sub-Status</th><th style="text-align:right;">Count</th></tr></thead>
        <tbody>${groups.map(g => {
          const subs = Object.entries(g.sub_statuses);
          if (!subs.length) return `<tr><td><strong>${E(g.label)}</strong></td><td class="txt-muted">—</td><td class="mono" style="text-align:right;">0</td></tr>`;
          return subs.map(([sub, n], i) => `<tr>
            <td>${i === 0 ? `<strong>${E(g.label)}</strong>` : ''}</td>
            <td><span class="badge-pill badge-sm ${ApexFmt.badgeForDecision(sub)}">${E(sub)}</span></td>
            <td class="mono" style="text-align:right;">${n}</td></tr>`).join('');
        }).join('')}
        <tr><td><strong>Total</strong></td><td></td><td class="mono" style="text-align:right;"><strong>${total}</strong></td></tr>
        </tbody></table>`;
  }

  /* ---------------- Scheduler monitor ---------------- */
  async function loadScheduler() {
    const [st, runs] = await Promise.all([ApexAPI.adminScheduler(), ApexAPI.adminWorkflowRuns(RUNS_TO_SHOW)]);
    $('scheduler-refreshed-at').textContent = `Last refreshed ${new Date().toLocaleTimeString()} — data is loaded on demand; use Refresh or switch tabs to update.`;
    const h = HEALTH[st.health] || HEALTH.NEVER_RUN;
    const badge = $('scheduler-badge');
    badge.className = `badge-pill ${h.badge}`;
    badge.textContent = `${h.icon} JOB ${st.health.replace('_', ' ')}`;
    $('side-last-run').textContent = st.last_run ? fmtDuration(st.seconds_since_last_run) + ' ago' : 'never';

    const last = st.last_run;
    $('scheduler-health').innerHTML = `
      <div class="fin-card compact" style="display:flex; justify-content:space-between; align-items:center;">
        <div>
          <span class="card-title">${h.icon} ${E(h.title)}</span>
          <div class="card-sub" style="margin-top:2px;">${E(st.message)}</div>
        </div>
        <span class="badge-pill ${h.badge}">${E(st.health.replace('_', ' '))}</span>
      </div>
      <div class="metric-grid" style="grid-template-columns: repeat(4, 1fr);">
        <div class="fin-metric"><div class="fin-metric-label">Expected Interval</div><div class="fin-metric-value">${st.config.interval_minutes} min</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Last Run Requested</div><div class="fin-metric-value" style="font-size:0.95rem;">${last ? E(last.requested_at) : '—'}</div><div class="fin-metric-delta txt-muted">${last ? fmtDuration(st.seconds_since_last_run) + ' ago · ' + E(last.trigger_source) : ''}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Next Run Due</div><div class="fin-metric-value" style="font-size:0.95rem;">${st.next_due_at ? E(st.next_due_at) : '—'}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Automatic Runs</div><div class="fin-metric-value ${st.config.scheduler_enabled ? 'txt-green' : 'txt-amber'}" style="font-size:0.95rem;">${st.config.scheduler_enabled ? 'Enabled (this service)' : 'Disabled (external)'}</div></div>
      </div>`;

    $('runs-table').innerHTML = runs.length ? `
      <div><table class="data-table">
        <thead><tr><th>Run #</th><th>Triggered By</th><th>Status</th><th>Requested</th><th>Started</th><th>Finished</th><th>Found</th><th>Processed</th><th>Approved</th><th>Rejected</th><th>Escalated</th><th>Errors</th></tr></thead>
        <tbody>${runs.map(r => `<tr>
          <td class="mono">${r.id}</td><td>${E(r.trigger_source)}</td>
          <td><span class="badge-pill badge-sm ${r.status === 'COMPLETED' ? 'badge-green' : (r.status === 'FAILED' ? 'badge-red' : 'badge-blue')}">${E(r.status)}</span></td>
          <td>${E(r.requested_at)}</td><td>${E(r.started_at || '—')}</td><td>${E(r.finished_at || '—')}</td>
          <td class="mono">${r.found}</td><td class="mono">${r.selected}</td><td class="mono">${r.approved}</td><td class="mono">${r.rejected}</td><td class="mono">${r.escalated}</td>
          <td class="mono ${r.errors ? 'txt-red' : ''}">${r.errors}${r.error_message ? ` <span title="${E(r.error_message)}">⚠️</span>` : ''}</td>
        </tr>`).join('')}</tbody>
      </table></div>` :
      '<div class="empty-state"><div class="icon">⏱</div><div class="title">No runs recorded yet</div><div class="sub">Runs triggered by the built-in schedule, the <code>initiate_workflow</code> script, or the API will appear here.</div></div>';
  }

  /* ---------------- Configuration ---------------- */
  function renderConfig(cfg) {
    $('interval_minutes').value = cfg.interval_minutes;
    $('scheduler_enabled').checked = cfg.scheduler_enabled;
    $('config-meta').textContent = cfg.updated_at ? `Last changed ${cfg.updated_at} by ${cfg.updated_by || '—'}.` : 'Using default configuration (2 minutes, automatic runs enabled).';
  }
  async function loadConfig() { renderConfig(await ApexAPI.adminScheduleConfig()); }

  $('config-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const interval = parseInt($('interval_minutes').value, 10);
    if (!(interval >= 1 && interval <= 1440)) { ApexUI.showAlert(alertBox, 'error', 'Interval must be between 1 and 1440 minutes.'); return; }
    const btn = $('btn-save-config');
    btn.disabled = true;
    try {
      const cfg = await ApexAPI.adminUpdateScheduleConfig({
        interval_minutes: interval,
        scheduler_enabled: $('scheduler_enabled').checked,
        updated_by: $('updated_by').value.trim() || 'Administrator',
      });
      renderConfig(cfg);
      ApexUI.showAlert(alertBox, 'success', `✅ Job frequency saved: every ${cfg.interval_minutes} minute(s), automatic runs ${cfg.scheduler_enabled ? 'enabled' : 'disabled'}.`);
      await loadScheduler();
    } catch (ex) {
      ApexUI.showAlert(alertBox, 'error', ex.message);
    } finally { btn.disabled = false; }
  });

  /* ---------------- Loading strategy: on demand only (no polling) ----------------
     Data is fetched on first load, when the user switches tabs, and when a Refresh button is clicked.
     This keeps database load proportional to actual administrator activity. */
  const LOADERS = { status: loadStatus, scheduler: loadScheduler, config: loadConfig };

  async function refreshTab(tab) {
    const loader = LOADERS[tab];
    if (!loader) return;
    try { await loader(); }
    catch (ex) { ApexUI.showAlert(alertBox, 'error', ex.message); }
  }

  document.querySelectorAll('.tabs[data-tabs=admin] .tab-btn').forEach(btn =>
    btn.addEventListener('click', () => refreshTab(btn.getAttribute('data-tab'))));

  document.querySelectorAll('[data-refresh]').forEach(btn => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try { await refreshTab(btn.getAttribute('data-refresh')); } finally { btn.disabled = false; }
  }));

  // Initial load: the active tab (status) plus the header/sidebar health badge, which comes from the scheduler data.
  refreshTab('status');
  refreshTab('scheduler');
  refreshTab('config');
})();
