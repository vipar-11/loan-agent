/* My Applications (/status): the signed-in applicant's own submissions, polled live, with the decision SLA. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const E = ApexFmt.escape;
  const alertBox = $('status-alert');
  const listEl = $('status-list');
  const POLL_MS = 10000;

  const STATUS_BADGE = { Received: 'badge-slate', 'Under Review': 'badge-blue', Approved: 'badge-green', Denied: 'badge-red' };
  const SLA_BADGE = {
    ON_TRACK: { cls: 'badge-blue', icon: '🟦', label: 'On track' },
    DUE_SOON: { cls: 'badge-amber', icon: '⏳', label: 'Due soon' },
    OVERDUE: { cls: 'badge-red', icon: '⚠️', label: 'Past our target' },
    DECIDED: { cls: 'badge-green', icon: '✅', label: 'Decided' },
  };

  function emptyState() {
    return `<div class="empty-state"><div class="icon">📄</div><div class="title">You have not submitted an application yet</div>
      <div class="sub">Start one from the <a href="/portal">Applicant Portal</a> — decisions are typically made within a few business days.</div></div>`;
  }

  function card(a) {
    const sla = a.sla;
    const slaBadge = SLA_BADGE[sla.state] || SLA_BADGE.ON_TRACK;
    const statusBadge = STATUS_BADGE[a.status_label] || 'badge-slate';
    return `
      <div class="fin-card" style="margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
          <div>
            <span class="card-title">${E(a.applicant_name)}</span>
            <span class="mono" style="font-size:0.72rem; color:var(--blue-dark); font-weight:600; margin-left:6px;">[${E(a.application_id)}]</span>
            <div class="card-sub" style="margin-top:2px;">${ApexFmt.currency(a.loan_amount_requested)} · ${E(a.loan_purpose)} · Submitted ${E(a.submitted_at || '—')}</div>
          </div>
          <div style="text-align:right;">
            <span class="badge-pill ${statusBadge}">${E(a.status_label)}</span><br/>
            <span class="badge-pill badge-sm ${slaBadge.cls}" style="margin-top:4px; display:inline-block;">${slaBadge.icon} ${E(slaBadge.label)}</span>
          </div>
        </div>
        <div class="alert ${sla.state === 'OVERDUE' ? 'alert-warning' : 'alert-info'}" style="margin-top:10px; font-size:0.78rem;">
          ${E(sla.message)}
        </div>
        ${a.adverse_action_reasons && a.adverse_action_reasons.length ? `
          <div class="fin-metric-label txt-red" style="margin-top:10px;">Reasons for this decision (ECOA adverse action notice):</div>
          <ul style="margin:4px 0 0 18px; font-size:0.8rem; color:var(--red-dark);">
            ${a.adverse_action_reasons.map(r => `<li>${E(r)}</li>`).join('')}
          </ul>` : ''}
      </div>`;
  }

  async function load() {
    try {
      const apps = await ApexAPI.myApplications();
      listEl.innerHTML = apps.length ? apps.map(card).join('') : emptyState();
    } catch (ex) {
      ApexUI.showAlert(alertBox, 'error', ex.message);
    }
  }

  load();
  setInterval(load, POLL_MS);
})();
