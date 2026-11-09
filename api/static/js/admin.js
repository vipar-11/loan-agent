/* Administrator console: status monitor, approval-workflow job monitor, job frequency configuration,
   partner batch intake. */
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

  /* ---------------- Partner batch intake ---------------- */
  const BATCH_BADGE = {
    ACCEPTED: 'badge-green', PARTIALLY_ACCEPTED: 'badge-amber', FLAGGED: 'badge-red', REJECTED: 'badge-red',
    DUPLICATE_FILE: 'badge-slate', FAILED: 'badge-red', INTERRUPTED: 'badge-amber', RECEIVED: 'badge-blue',
  };
  let selectedBatch = null;

  async function loadIntake() {
    const [st, batches] = await Promise.all([ApexAPI.adminIntakeStatus(), ApexAPI.adminIntakeBatches(25)]);
    $('intake-refreshed-at').textContent = `Last refreshed ${new Date().toLocaleTimeString()} — use Refresh to update.`;
    const live = st.monitoring;
    $('intake-health').innerHTML = `
      <div class="fin-card compact" style="display:flex; justify-content:space-between; align-items:center;">
        <div>
          <span class="card-title">${live ? '✅ Monitoring partner inboxes' : (st.enabled ? '⚠️ Monitor not running in this service' : '⏸ Batch intake disabled')}</span>
          <div class="card-sub" style="margin-top:2px;">Inbox root: <span class="mono">${E(st.inbox_dir)}</span> · File format v${E(st.spec_version)} (${st.columns.length} columns)</div>
        </div>
        <span class="badge-pill ${live ? 'badge-green' : 'badge-amber'}">${live ? 'MONITORING' : 'NOT MONITORING'}</span>
      </div>
      <div class="metric-grid" style="grid-template-columns: repeat(4, 1fr);">
        <div class="fin-metric"><div class="fin-metric-label">Scan Interval</div><div class="fin-metric-value">${st.poll_seconds}s</div><div class="fin-metric-delta txt-muted">files settle ${st.settle_seconds}s before pickup</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Last Scan</div><div class="fin-metric-value" style="font-size:0.95rem;">${E(st.last_scan_at || 'never')}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Files Waiting</div><div class="fin-metric-value">${st.partners.reduce((n, p) => n + p.pending_files, 0)}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Limits per File</div><div class="fin-metric-value" style="font-size:0.95rem;">${st.max_rows.toLocaleString()} rows · ${(st.max_file_bytes / 1048576).toFixed(1)} MB</div></div>
      </div>`;

    $('intake-partners').innerHTML = st.partners.length ? `
      <table class="data-table">
        <thead><tr><th>Partner ID</th><th>Partner</th><th>Inbox (FTP upload folder)</th><th>Outbox (results)</th><th>Waiting</th></tr></thead>
        <tbody>${st.partners.map(p => `<tr>
          <td class="mono">${E(p.partner_id)}</td><td>${E(p.name)}</td>
          <td class="mono" style="font-size:0.7rem;">${E(p.inbox_dir)}</td><td class="mono" style="font-size:0.7rem;">${E(p.outbox_dir)}</td>
          <td class="mono">${p.pending_files}</td></tr>`).join('')}</tbody>
      </table>` : '<div class="alert alert-warning">No approved partners are configured (batch_intake.ini, [approved_partners]).</div>';

    $('intake-batches').innerHTML = batches.length ? `
      <table class="data-table">
        <thead><tr><th>Batch #</th><th>Partner</th><th>File</th><th>Received</th><th>Processed</th><th>Status</th><th>Rows</th><th>Accepted</th><th>Flagged</th><th></th></tr></thead>
        <tbody>${batches.map(b => `<tr class="${b.id === selectedBatch ? 'selected' : ''}">
          <td class="mono">${b.id}</td><td class="mono">${E(b.partner_id)}</td><td class="mono" style="font-size:0.72rem;">${E(b.file_name)}</td>
          <td>${E(b.received_at)}</td><td>${E(b.processed_at || '—')}</td>
          <td><span class="badge-pill badge-sm ${BATCH_BADGE[b.status] || 'badge-slate'}" ${b.error_message ? `title="${E(b.error_message)}"` : ''}>${E(b.status)}</span></td>
          <td class="mono">${b.total_rows}</td><td class="mono txt-green">${b.accepted_rows}</td><td class="mono ${b.flagged_rows ? 'txt-red' : ''}">${b.flagged_rows}</td>
          <td><button class="btn btn-sm" data-batch="${b.id}">View</button></td></tr>`).join('')}</tbody>
      </table>` :
      '<div class="empty-state"><div class="icon">📥</div><div class="title">No batch files received yet</div><div class="sub">Files delivered by approved partners to their inbox folders will appear here once picked up.</div></div>';
    $('intake-batches').querySelectorAll('[data-batch]').forEach(btn =>
      btn.addEventListener('click', () => showBatch(Number(btn.getAttribute('data-batch')))));
    if (selectedBatch) await showBatch(selectedBatch);
  }

  async function showBatch(id) {
    selectedBatch = id;
    const el = $('intake-batch-detail');
    try {
      const b = await ApexAPI.adminIntakeBatch(id);
      const flagged = b.rows.filter(r => r.status === 'FLAGGED');
      el.innerHTML = `
        <div class="fin-card compact">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="card-title">Batch #${b.id} — ${E(b.file_name)}</span>
            <span class="badge-pill ${BATCH_BADGE[b.status] || 'badge-slate'}">${E(b.status)}</span>
          </div>
          <div class="card-sub" style="margin-top:2px;">Partner ${E(b.partner_id)} · received ${E(b.received_at)} · ${b.total_rows} row(s): ${b.accepted_rows} accepted, ${b.flagged_rows} flagged${b.results_path ? ` · results: <span class="mono">${E(b.results_path)}</span>` : ''}</div>
          ${b.error_message ? `<div class="alert alert-error" style="margin:8px 0 0 0;">${E(b.error_message)}</div>` : ''}
        </div>
        ${b.rows.length ? `<div class="table-scroll"><table class="data-table">
          <thead><tr><th>Row</th><th>Partner Reference</th><th>Status</th><th>Application</th><th>Problems (not submitted)</th></tr></thead>
          <tbody>${(flagged.length ? [...flagged, ...b.rows.filter(r => r.status !== 'FLAGGED')] : b.rows).map(r => `<tr>
            <td class="mono">${r.row_number}</td><td class="mono">${E(r.partner_reference || '—')}</td>
            <td><span class="badge-pill badge-sm ${r.status === 'ACCEPTED' ? 'badge-green' : 'badge-red'}">${E(r.status)}</span></td>
            <td class="mono">${E(r.application_id || '—')}</td>
            <td style="font-size:0.75rem;">${r.errors.length ? `<ul style="margin:0; padding-left:16px;">${r.errors.map(e => `<li>${E(e)}</li>`).join('')}</ul>` : ''}</td>
          </tr>`).join('')}</tbody></table></div>` : ''}`;
    } catch (ex) {
      el.innerHTML = `<div class="alert alert-error">${E(ex.message)}</div>`;
    }
  }

  $('btn-intake-scan').addEventListener('click', async () => {
    const btn = $('btn-intake-scan');
    btn.disabled = true;
    try {
      const res = await ApexAPI.adminIntakeScan();
      const n = res.batches.length;
      const accepted = res.batches.reduce((a, b) => a + b.accepted_rows, 0);
      const flagged = res.batches.reduce((a, b) => a + b.flagged_rows, 0);
      ApexUI.showAlert(alertBox, n ? 'success' : 'warning', n
        ? `📥 Scan complete: ${n} file(s) processed — ${accepted} application(s) submitted as NEW, ${flagged} row(s) flagged.`
        : '📥 Scan complete: no files ready in the partner inboxes (files are picked up once unchanged for the settling period).');
      if (n) selectedBatch = res.batches[res.batches.length - 1].id;
      await loadIntake();
      await loadStatus();
    } catch (ex) {
      ApexUI.showAlert(alertBox, 'error', ex.message);
    } finally { btn.disabled = false; }
  });

  /* ---------------- Loading strategy: on demand only (no polling) ----------------
     Data is fetched on first load, when the user switches tabs, and when a Refresh button is clicked.
     This keeps database load proportional to actual administrator activity. */
  const LOADERS = { status: loadStatus, scheduler: loadScheduler, config: loadConfig, intake: loadIntake };

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
