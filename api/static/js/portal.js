/* Applicant Portal: live installment sizing, persona autofill, submission. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const form = $('application-form');
  const alertBox = $('portal-alert');
  const preview = $('installment-preview');

  const PERSONA_HINTS = {
    MRS_SOME_NAME: 'Prime $15,000 → Auto-Approve',
    MR_SOME_NAME: 'Subprime $20,000 → Auto-Deny',
    MS_SOME_NAME: 'Nuanced $35,000 → Escalate',
  };

  /* ---------------- Live installment preview ---------------- */
  let quoteTimer = null;
  function refreshInstallment() {
    clearTimeout(quoteTimer);
    const amount = parseFloat($('loan_amount_requested').value);
    if (!amount || amount <= 0) { preview.textContent = '—'; return; }
    quoteTimer = setTimeout(async () => {
      try {
        const q = await ApexAPI.installment(amount);
        preview.textContent = ApexFmt.currency(q.monthly_payment);
      } catch (_) {
        preview.textContent = '—';
      }
    }, 120);
  }
  $('loan_amount_requested').addEventListener('input', refreshInstallment);

  /* ---------------- Persona dialog ---------------- */
  const personaModal = $('persona-modal');
  function openPersonas() { personaModal.classList.remove('hidden'); }
  function closePersonas() { personaModal.classList.add('hidden'); }
  $('btn-autofill').addEventListener('click', async () => {
    const grid = $('persona-grid');
    if (!grid.childElementCount) {
      const personas = await ApexAPI.personas();
      grid.innerHTML = personas.map(p => `
        <button type="button" class="btn persona-btn" data-persona='${ApexFmt.escape(JSON.stringify(p))}'>
          ${ApexFmt.escape(p.applicant_name)}
          <small>${ApexFmt.escape(PERSONA_HINTS[p.key] || p.label)}</small>
        </button>`).join('');
      grid.querySelectorAll('[data-persona]').forEach(btn => btn.addEventListener('click', () => {
        applyPersona(JSON.parse(btn.getAttribute('data-persona')));
        closePersonas();
      }));
    }
    openPersonas();
  });
  personaModal.querySelectorAll('[data-close-persona]').forEach(b => b.addEventListener('click', closePersonas));
  personaModal.addEventListener('click', (e) => { if (e.target === personaModal) closePersonas(); });
  $('btn-clear-form').addEventListener('click', () => { clearForm(); closePersonas(); });

  function applyPersona(p) {
    $('applicant_name').value = p.applicant_name;
    $('ssn_last_4').value = p.ssn_last_4;
    $('stated_annual_income').value = p.stated_annual_income;
    $('loan_amount_requested').value = p.loan_amount_requested;
    $('loan_purpose').value = p.loan_purpose;
    refreshInstallment();
  }
  function clearForm() {
    form.reset();
    $('fcra_consent').checked = true;
    preview.textContent = '—';
    alertBox.innerHTML = '';
  }

  /* ---------------- Submission ---------------- */
  function validate(payload) {
    if (!payload.applicant_name) return 'Please enter borrower legal name.';
    if (!/^\d{4}$/.test(payload.ssn_last_4)) return 'Please enter a valid 4-digit SSN.';
    if (!(payload.loan_amount_requested > 0)) return 'Please enter a valid loan amount requested.';
    if (!(payload.stated_annual_income > 0)) return 'Please enter stated annual income.';
    if (!payload.loan_purpose) return 'Please select a loan purpose.';
    return null;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
      applicant_name: $('applicant_name').value.trim(),
      ssn_last_4: $('ssn_last_4').value.trim(),
      stated_annual_income: parseFloat($('stated_annual_income').value),
      loan_amount_requested: parseFloat($('loan_amount_requested').value),
      loan_purpose: $('loan_purpose').value,
      fcra_consent: $('fcra_consent').checked,
    };
    const err = validate(payload);
    if (err) { ApexUI.showAlert(alertBox, 'error', err); return; }

    const btn = $('btn-submit');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Submitting...';
    try {
      const res = await ApexAPI.submitApplication(payload);
      ApexUI.showAlert(alertBox, 'success', '✅ ' + res.message);
      alertBox.querySelector('.alert').insertAdjacentHTML('beforeend',
        ' <a href="/status">Go to My Applications ↗</a>');
      form.reset();
      $('fcra_consent').checked = true;
        preview.textContent = '—';
    } catch (ex) {
      ApexUI.showAlert(alertBox, 'error', ex.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Submit Loan Application';
    }
  });
})();
