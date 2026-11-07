/* Underwriter Desk: queues, archive inspector, and the adjudication modal (HitL resumption). */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const E = ApexFmt.escape;
  const alertBox = $('desk-alert');
  const POLL_MS = 4000;

  let apps = [];
  let policyHtml = null;
  let modal = { id: null, dossier: null, classifications: {} };

  ApexUI.initTabs(document);

  /* ==================================================================
     Data loading & stats
     ================================================================== */
  async function loadAll() {
    try {
      apps = await ApexAPI.listApplications('all');
      renderStats();
      renderPendingQueue();
      renderArchive();
    } catch (ex) {
      ApexUI.showAlert(alertBox, 'error', ex.message);
    }
  }

  function byStage(stage) { return apps.filter(a => a.stage === stage); }

  function renderStats() {
    const s = {
      pending_review: byStage('PENDING_REVIEW').length,
      adjudicated: byStage('ADJUDICATED').length,
    };
    document.querySelectorAll('[data-stat]').forEach(el => {
      const key = el.getAttribute('data-stat');
      if (key in s) el.textContent = s[key];
    });
  }

  function emptyState(icon, title, sub) {
    return `<div class="empty-state"><div class="icon">${icon}</div><div class="title">${title}</div><div class="sub">${sub}</div></div>`;
  }

  /* ==================================================================
     TAB: Pending review queue
     ================================================================== */
  function renderPendingQueue() {
    const items = byStage('PENDING_REVIEW');
    const el = $('pending-queue');
    if (!items.length) {
      el.innerHTML = emptyState('📭', 'Underwriting Queue Is Empty (0 Pending Reviews)', 'Applications with <strong>ESCALATE</strong> decisions will automatically appear here for credit officer review.');
      return;
    }
    el.innerHTML = items.map(a => `
      <div class="queue-row">
        <div class="queue-card">
          <div>
            <span class="queue-name">${E(a.applicant_name)}</span><span class="queue-id">[${E(a.application_id)}]</span>
            <span class="badge-pill badge-amber badge-sm" style="margin-left:8px;">ESCALATED BY AI</span>
            <div class="queue-meta">SSN: ${E(a.ssn_masked)} | Stated: ${ApexFmt.currency(a.stated_annual_income)}/yr | Purpose: ${E(a.loan_purpose)}${a.post_loan_dti != null ? ` | DTI: ${Number(a.post_loan_dti).toFixed(1)}%` : ''} | Submitted: ${E(a.submitted_at)}</div>
          </div>
          <span class="queue-amt">${ApexFmt.currency(a.loan_amount_requested)}</span>
        </div>
        <button class="btn btn-block" data-review="${E(a.application_id)}">Review & Adjudicate ↗</button>
      </div>`).join('');
    el.querySelectorAll('[data-review]').forEach(btn => btn.addEventListener('click', () => openModal(btn.getAttribute('data-review'))));
  }

  /* ==================================================================
     TAB: Archive + inspector
     ================================================================== */
  function renderArchive() {
    const items = byStage('ADJUDICATED');
    const tableEl = $('archive-table');
    const inspector = $('archive-inspector');
    if (!items.length) {
      tableEl.innerHTML = emptyState('📁', 'Archive Empty (0 Adjudicated Records)', 'Applications settled via automated rules or human adjudication will be logged here for permanent compliance auditing.');
      inspector.classList.add('hidden');
      return;
    }
    tableEl.innerHTML = `
      <div class="table-scroll tall"><table class="data-table">
        <thead><tr><th>Ref ID</th><th>Borrower</th><th>Determination</th><th>Adjudicator Type</th><th>Loan Sized</th><th>Purpose</th><th>Adjudicating Officer</th><th>Timestamp</th></tr></thead>
        <tbody>${items.map(a => {
          const adj = a.adjudication || {};
          const dec = adj.decision || a.status;
          return `<tr>
            <td class="mono">${E(a.application_id)}</td><td>${E(a.applicant_name)}</td>
            <td><span class="badge-pill badge-sm ${ApexFmt.badgeForDecision(dec)}">${E(dec)}</span></td>
            <td>${a.adjudicator_type === 'HUMAN_OFFICER' ? '👤 Human Officer' : '🤖 AI Agent'}</td>
            <td class="mono">${ApexFmt.currency(a.loan_amount_requested)}</td><td>${E(a.loan_purpose)}</td>
            <td>${E(adj.officer || 'AI Agent')}</td><td>${E(adj.adjudicated_at || a.submitted_at)}</td>
          </tr>`;
        }).join('')}</tbody>
      </table></div>`;

    const select = $('archive-select');
    const previous = select.value;
    select.innerHTML = items.map(a => `<option value="${E(a.application_id)}">${E(a.application_id)} — ${E(a.applicant_name)} [${E(a.status)}]</option>`).join('');
    if (items.some(a => a.application_id === previous)) select.value = previous;
    inspector.classList.remove('hidden');
    renderArchiveDetail(select.value);
  }
  $('archive-select').addEventListener('change', (e) => renderArchiveDetail(e.target.value));

  async function renderArchiveDetail(id) {
    const el = $('archive-detail');
    if (!id) { el.innerHTML = ''; return; }
    try {
      const a = await ApexAPI.getApplication(id);
      const adj = a.adjudication || {};
      const st = a.graph_state || {};
      const dec = adj.decision || a.status;
      const citations = adj.policy_citations || st.policy_citations || [];
      const adverse = adj.adverse_action_reasons || st.adverse_action_reasons || [];
      const logs = st.execution_log || [];
      el.innerHTML = `
        <div class="fin-card compact" style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <span class="card-title">Determination: ${E(dec)}</span>
            <div class="card-sub" style="margin-top:2px;">File: ${E(a.application_id)} | Borrower: ${E(a.applicant_name)} | Officer: ${E(adj.officer || 'Underwriter')} | Timestamp: ${E(adj.adjudicated_at || '—')}</div>
          </div>
          <span class="badge-pill ${ApexFmt.badgeForDecision(dec)}">${E(dec)}</span>
        </div>
        <div class="metric-grid">
          <div class="fin-metric"><div class="fin-metric-label">Loan Sized</div><div class="fin-metric-value">${ApexFmt.currency(a.loan_amount_requested)}</div></div>
          <div class="fin-metric"><div class="fin-metric-label">Stated Income</div><div class="fin-metric-value">${ApexFmt.currency(a.stated_annual_income)}</div></div>
          <div class="fin-metric"><div class="fin-metric-label">Loan Purpose</div><div class="fin-metric-value" style="font-size:1rem;">${E(a.loan_purpose)}</div></div>
        </div>
        <div class="fin-card compact">
          <div class="fin-metric-label">Recorded Underwriting Reason & Memorandum:</div>
          <div style="font-size:0.82rem; line-height:1.5; white-space:pre-wrap;">${E(adj.reason || 'No memorandum recorded.')}</div>
        </div>
        ${citations.length ? `<div class="fin-metric-label" style="margin-top:10px;">Policy Citations:</div><div class="pill-wrap">${citations.map(c => `<span class="badge-pill badge-blue">${E(c)}</span>`).join('')}</div>` : ''}
        ${adverse.length ? `<div class="fin-metric-label txt-red" style="margin-top:10px;">ECOA Adverse Action Reasons:</div><ul style="margin:4px 0 10px 18px; font-size:0.8rem; color:var(--red-dark);">${adverse.map(r => `<li>${E(r)}</li>`).join('')}</ul>` : ''}
        ${logs.length ? `<details><summary>🔍 Workflow Execution Trace (${logs.length} steps)</summary><div class="log-box">${logs.map(l => `<div>${E(l)}</div>`).join('')}</div></details>` : ''}
        <details id="audit-trail-details"><summary>🧾 Application Database Audit Trail</summary><div id="audit-trail-box" class="log-box">Loading…</div></details>`;
      el.querySelector('#audit-trail-details').addEventListener('toggle', async (ev) => {
        if (!ev.target.open) return;
        const box = $('audit-trail-box');
        try {
          const trail = await ApexAPI.getAuditTrail(id);
          box.innerHTML = trail.map(t => `<div>${E(t.timestamp)} · <strong>${E(t.event_type)}</strong> · ${E(t.actor)} · ${E(t.from_status || '∅')} → ${E(t.to_status || '∅')}${t.details ? ` · ${E(typeof t.details === 'string' ? t.details : JSON.stringify(t.details))}` : ''}</div>`).join('') || '<div>No events.</div>';
        } catch (ex) { box.textContent = ex.message; }
      }, { once: true });
    } catch (ex) {
      el.innerHTML = `<div class="alert alert-error">${E(ex.message)}</div>`;
    }
  }

  /* ==================================================================
     ADJUDICATION MODAL
     ================================================================== */
  const modalEl = $('review-modal');
  const modalBody = $('modal-body');
  $('btn-modal-close').addEventListener('click', closeModal);
  modalEl.addEventListener('click', (e) => { if (e.target === modalEl) closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !modalEl.classList.contains('hidden')) closeModal(); });

  function closeModal() {
    modalEl.classList.add('hidden');
    modal = { id: null, dossier: null, classifications: {} };
    modalBody.innerHTML = '<div class="txt-muted"><span class="spinner"></span> Loading dossier...</div>';
  }

  async function openModal(id) {
    modalEl.classList.remove('hidden');
    try {
      const [dossier, policy] = await Promise.all([ApexAPI.getDossier(id), policyHtml ? Promise.resolve(null) : ApexAPI.policy()]);
      if (policy) policyHtml = policy.html;
      modal = { id, dossier, classifications: Object.fromEntries(dossier.bank.transactions.map(t => [t.tx_id, t.classification])) };
      renderModal();
    } catch (ex) {
      modalBody.innerHTML = `<div class="alert alert-error">${E(ex.message)}</div>`;
    }
  }

  function renderModal() {
    const d = modal.dossier;
    const a = d.application;
    const ai = d.ai_assessment;
    const memoSnip = ai.underwriter_memo.length > 180 ? ai.underwriter_memo.slice(0, 180) + '…' : ai.underwriter_memo;
    modalBody.innerHTML = `
    <div class="modal-grid">
      <div>
        <div class="fin-card compact">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="card-title">${E(a.applicant_name)}</span>
            <span class="mono" style="font-size:0.72rem; color:var(--blue-dark); font-weight:600;">[${E(a.application_id)}]</span>
          </div>
          <div class="card-sub" style="margin-top:2px;">SSN: ${E(a.ssn_masked)} | Stated: ${ApexFmt.currency(a.stated_annual_income)}/yr (${ApexFmt.currency(d.stated_monthly_income)}/mo) | Purpose: ${E(a.loan_purpose)}</div>
        </div>

        <div class="tabs" data-tabs="dossier">
          <button class="tab-btn active" data-tab="ai">🤖 AI Assessment</button>
          <button class="tab-btn" data-tab="bureau">🏛️ Credit Tradelines</button>
          <button class="tab-btn" data-tab="bank">💳 Bank Cash Flow</button>
          <button class="tab-btn" data-tab="ratios">📊 Financial Ratios</button>
          <button class="tab-btn" data-tab="policy">📜 Policy Guidelines</button>
        </div>

        <div class="tab-panel active" data-scope="dossier" data-panel="ai">${renderAiTab(ai)}</div>
        <div class="tab-panel" data-scope="dossier" data-panel="bureau">${renderBureauTab(d.credit)}</div>
        <div class="tab-panel" data-scope="dossier" data-panel="bank" id="bank-tab">${renderBankTab(d.bank)}</div>
        <div class="tab-panel" data-scope="dossier" data-panel="ratios" id="ratios-tab">${renderRatiosTab(d.ratios)}</div>
        <div class="tab-panel" data-scope="dossier" data-panel="policy"><div class="policy-doc">${policyHtml || ''}</div></div>
      </div>

      <div>
        <div class="sizing-box">
          <div class="fin-metric-label">Loan Underwriting Sizing</div>
          <div class="row"><span class="amt">${ApexFmt.currency(d.sizing.loan_amount_requested)}</span><span class="pmt">${ApexFmt.currency(d.sizing.monthly_payment)}/mo</span></div>
          <div class="card-sub" style="margin-top:2px;">Fixed Term: ${d.sizing.term_months} Months @ ${(d.sizing.annual_rate * 100).toFixed(1)}% APR</div>
        </div>
        <div class="agent-callout">
          <div class="hdr"><span>🤖 Agent Recommendation:</span><span class="badge-pill badge-amber">${E(ai.underwriting_decision)}</span></div>
          <div class="body">${E(memoSnip)}</div>
        </div>
        <div class="section-label">Human Credit Officer Determination</div>
        <form id="decision-form">
          <label class="field-label" for="officer">Adjudicating Officer *</label>
          <input type="text" id="officer" value="Human Underwriter" />
          <label class="field-label" for="notes" style="margin-top:10px;">Credit Officer Justification (Mandatory) *</label>
          <textarea id="notes" placeholder="e.g. Verified W-2 and pay stubs; tax withholding explains income variance. Approved 41.8% DTI within near-prime policy guidelines." title="Mandatory: Record justification memorandum before finalizing credit determination."></textarea>
          <div id="decision-error" style="margin-top:8px;"></div>
          <div class="metric-grid-2" style="margin-top:10px;">
            <button type="button" class="btn btn-primary btn-block" data-decision="APPROVE">✅ Approve</button>
            <button type="button" class="btn btn-block" data-decision="DENY">❌ Reject</button>
          </div>
        </form>
        <button class="btn btn-block" style="margin-top:10px;" id="btn-cancel-modal">Cancel / Close</button>
      </div>
    </div>`;

    ApexUI.initTabs(modalBody);
    $('btn-cancel-modal').addEventListener('click', closeModal);
    bindBankTab();
    bindRatiosTab();
    modalBody.querySelectorAll('[data-decision]').forEach(btn => btn.addEventListener('click', () => submitDecision(btn.getAttribute('data-decision'))));
  }

  function renderAiTab(ai) {
    const badge = ai.underwriting_decision === 'ESCALATE' ? 'badge-amber' : (ai.underwriting_decision === 'APPROVE' ? 'badge-green' : 'badge-red');
    return `
      <div class="form-row" style="grid-template-columns: 1.5fr 1fr; margin-bottom:8px;">
        <div class="fin-metric"><div class="fin-metric-label">AI Agent Recommendation</div><div style="margin-top:4px;"><span class="badge-pill ${badge}" style="font-size:0.85rem;">${E(ai.underwriting_decision)}</span></div></div>
        <div class="fin-metric"><div class="fin-metric-label">Agent Confidence Score</div><div class="fin-metric-value txt-blue">${E(ai.confidence_pct)}</div></div>
      </div>
      ${ai.error_message ? `<div class="alert alert-warning">⚠️ ${E(ai.error_message)}</div>` : ''}
      <div class="section-label">Underwriter Recommendation Memorandum</div>
      <div class="memo-box">${E(ai.underwriter_memo)}</div>
      ${ai.policy_citations.length ? `<div class="fin-metric-label">Triggered Policy Citations:</div><div class="pill-wrap">${ai.policy_citations.map(c => `<span class="badge-pill badge-blue">${E(c)}</span>`).join('')}</div>` : ''}
      <div class="section-label">Bank Statement Analyst Agent Summary</div>
      <div class="summary-box">${E(ai.bank_analyst_summary)}</div>
      ${ai.execution_log.length ? `<details><summary>🔍 Complete Workflow Execution Trace (${ai.execution_log.length} steps)</summary><div class="log-box">${ai.execution_log.map(l => `<div>${E(l)}</div>`).join('')}</div></details>` : ''}`;
  }

  function renderBureauTab(c) {
    return `
      <div class="metric-grid">
        <div class="fin-metric"><div class="fin-metric-label">FICO Score</div><div class="fin-metric-value">${c.fico_score}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Bureau Debt</div><div class="fin-metric-value">${ApexFmt.currency(c.bureau_monthly_debt)}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Public Records</div><div class="fin-metric-value">${c.public_records.length}</div><div class="fin-metric-delta ${c.public_records.length ? 'txt-red' : 'txt-green'}">${c.public_records.length ? 'Alert' : 'Clean'}</div></div>
      </div>
      ${c.public_records.map(pr => `<div class="alert alert-warning">Public Record: ${E(pr.type)} (${E(pr.status)})</div>`).join('')}
      ${c.tradelines.length ? `<div class="table-scroll"><table class="data-table">
        <thead><tr><th>Creditor Name</th><th>Account Type</th><th>Monthly Obligation</th><th>12M Delinquencies</th><th>Standing</th></tr></thead>
        <tbody>${c.tradelines.map(t => `<tr><td>${E(t.creditor)}</td><td>${E(t.account_type)}</td><td class="mono">${ApexFmt.currency(t.monthly_payment)}</td><td>${E(t.delinquencies)}</td><td>${t.standing === 'Current' ? '<span class="badge-pill badge-green badge-sm">Current</span>' : '<span class="badge-pill badge-red badge-sm">Delinquent</span>'}</td></tr>`).join('')}</tbody>
      </table></div>` : '<div class="card-sub">No active tradelines on file.</div>'}`;
  }

  function renderBankTab(b) {
    const total = b.transactions.length;
    return `
      <div class="metric-grid">
        <div class="fin-metric"><div class="fin-metric-label">Avg EOM Balance</div><div class="fin-metric-value">${ApexFmt.currency(b.avg_eom_balance)}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Deposits (90D)</div><div class="fin-metric-value">${ApexFmt.currency(b.total_inflows)}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Debits (90D)</div><div class="fin-metric-value">${ApexFmt.currency(b.total_outflows)}</div></div>
      </div>
      <div class="toolbar">
        <div style="font-size:0.75rem; font-weight:600;" class="${b.classified_by_ai ? 'txt-green' : 'txt-amber'}">
          ${b.classified_by_ai ? `✅ All ${total} transactions auto-classified by AI Bank Statement Analyst.` : `⚠️ AI classification unavailable — ${total} transactions classified heuristically.`}
        </div>
        <button class="btn btn-sm" id="btn-reset-heuristic" title="Re-runs heuristic classification">✨ Reset to Heuristic</button>
      </div>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Date</th><th>Description</th><th>Amount</th><th>Flow</th><th>Classification</th></tr></thead>
        <tbody>${b.transactions.map(t => {
          const credit = t.flow === 'CREDIT';
          const amt = (credit ? '+' : '-') + ApexFmt.currency(Math.abs(t.amount));
          const tip = t.ai_reasoning ? `${t.description} — AI Note: ${t.ai_reasoning}` : t.description;
          return `<tr class="tx-row">
            <td class="txt-muted date">${E(t.date || '—')}</td>
            <td class="desc" title="${E(tip)}">${E(t.description)}</td>
            <td class="amt ${credit ? 'txt-green' : ''}">${E(amt)}</td>
            <td><span class="badge-pill badge-sm ${credit ? 'badge-green' : 'badge-blue'}">${credit ? 'Deposit (+)' : 'Debit (-)'}</span></td>
            <td><select class="compact" data-tx="${E(t.tx_id)}">${t.options.map(o => `<option value="${E(o)}" ${o === modal.classifications[t.tx_id] ? 'selected' : ''}>${E(o)}</option>`).join('')}</select></td>
          </tr>`;
        }).join('')}</tbody>
      </table></div>`;
  }

  function bindBankTab() {
    modalBody.querySelectorAll('select[data-tx]').forEach(sel => sel.addEventListener('change', () => {
      modal.classifications[sel.getAttribute('data-tx')] = sel.value;
      markRatiosStale();
    }));
    const reset = $('btn-reset-heuristic');
    if (reset) reset.addEventListener('click', async () => {
      // The server (loan_agent.classification) is the single source of truth for the heuristic rules.
      reset.disabled = true;
      try {
        const heuristic = await ApexAPI.heuristicClassifications(modal.id);
        Object.assign(modal.classifications, heuristic);
        $('bank-tab').innerHTML = renderBankTab(modal.dossier.bank);
        bindBankTab();
        markRatiosStale();
      } catch (ex) { ApexUI.showAlert(alertBox, 'error', ex.message); }
      finally { const r = $('btn-reset-heuristic'); if (r) r.disabled = false; }
    });
  }

  function renderRatiosTab(r) {
    const x = r.ratios;
    const stale = r.source === 'STALE';
    return `
      <div class="toolbar">
        <div style="font-size:0.75rem; padding:6px 10px; border-radius:6px; border:1px solid;" class="${stale ? 'alert-warning' : 'alert-success'}">
          ${stale ? '⚠️ <strong>Classifications changed.</strong> Recalculate to refresh deterministic ratios.' : (r.source === 'RECALCULATED' ? '🔄 <strong>Recalculated</strong> from officer classifications (deterministic SR 11-7 engine).' : '✅ <strong>Auto-populated from the automated underwriting run.</strong> Deterministic financial ratios.')}
        </div>
        <button class="btn btn-sm" id="btn-recalc">🔄 Recalculate Ratios</button>
      </div>
      <div class="metric-grid">
        <div class="fin-metric"><div class="fin-metric-label">Post-Loan DTI</div><div class="fin-metric-value ${ApexFmt.dtiColor(x.post_loan_dti)}">${ApexFmt.pct(x.post_loan_dti)}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Verified Monthly Income</div><div class="fin-metric-value">${ApexFmt.currency(x.verified_monthly_income)}</div></div>
        <div class="fin-metric"><div class="fin-metric-label">Payment Cushion</div><div class="fin-metric-value txt-blue">${ApexFmt.mult(x.payment_cushion_ratio)}</div></div>
      </div>
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Underwriting Metric</th><th>Calculated Value</th><th>Policy Benchmark</th></tr></thead>
        <tbody>${r.rows.map(row => `<tr><td>${E(row.metric)}</td><td class="mono">${E(row.value)}</td><td class="txt-muted">${E(row.benchmark)}</td></tr>`).join('')}</tbody>
      </table></div>`;
  }

  function markRatiosStale() {
    const current = modal.dossier.ratios;
    $('ratios-tab').innerHTML = renderRatiosTab({ ...current, source: 'STALE' });
    bindRatiosTab();
  }

  function bindRatiosTab() {
    const btn = $('btn-recalc');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Calculating…';
      try {
        const res = await ApexAPI.recalculateRatios(modal.id, modal.classifications);
        modal.dossier.ratios = res;
        $('ratios-tab').innerHTML = renderRatiosTab(res);
        bindRatiosTab();
      } catch (ex) {
        ApexUI.showAlert(alertBox, 'error', ex.message);
        btn.disabled = false;
        btn.textContent = '🔄 Recalculate Ratios';
      }
    });
  }

  async function submitDecision(decision) {
    const notes = $('notes').value.trim();
    const errBox = $('decision-error');
    if (!notes) {
      const word = decision === 'APPROVE' ? 'approving' : 'rejecting';
      errBox.innerHTML = `<div class="alert alert-error" style="text-align:center; font-weight:600; margin:0;">⚠️ Mandatory: Enter underwriter justification memorandum before ${word}.</div>`;
      return;
    }
    errBox.innerHTML = '';
    const buttons = modalBody.querySelectorAll('[data-decision]');
    buttons.forEach(b => b.disabled = true);
    const active = modalBody.querySelector(`[data-decision="${decision}"]`);
    active.innerHTML = `<span class="spinner"></span> Recording determination (${decision})…`;
    try {
      const updated = await ApexAPI.recordDecision(modal.id, { decision, officer: $('officer').value, notes });
      closeModal();
      ApexUI.showAlert(alertBox, 'success', `✅ ${updated.application_id} finalized as ${updated.status} by ${updated.adjudication.officer}.`);
      await loadAll();
    } catch (ex) {
      errBox.innerHTML = `<div class="alert alert-error" style="margin:0;">${E(ex.message)}</div>`;
      buttons.forEach(b => b.disabled = false);
      active.textContent = decision === 'APPROVE' ? '✅ Approve' : '❌ Reject';
    }
  }

  /* ==================================================================
     Boot + polling (keeps queues live while background pipelines settle)
     ================================================================== */
  loadAll();
  setInterval(() => {
    if (modalEl.classList.contains('hidden')) loadAll();
  }, POLL_MS);
})();
