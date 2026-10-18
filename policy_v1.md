# APEX FINANCIAL — UNSECURED PERSONAL LOAN UNDERWRITING POLICY (US-2026)
**Product Scope:** Unsecured Personal Loans from $2,000 to $50,000 (Standard 36-Month Term, 12.0% Representative APR).

## Section 1: Mandatory Compliance & Consent (FCRA / GLBA)
* **Rule 1.1 (Explicit Consent):** Explicit applicant consent (`fcra_consent = True`) is strictly required prior to invoking any external credit bureau or bank cash-flow APIs. Any application submitted without consent must be immediately rejected (`REJECT_NO_CONSENT`).

## Section 2: Hard Knockout Criteria (Automatic Denial)
An application must be **DENIED** immediately if ANY of the following conditions are triggered:
* **Rule 2.1 (Credit Score Floor):** Applicant FICO Score is less than 620.
* **Rule 2.2 (Maximum DTI Cap):** Reconciled Post-Loan Debt-to-Income (`post_loan_dti`) ratio exceeds 45.0%.
* **Rule 2.3 (Derogatory Credit Marks):** Applicant has an active bankruptcy (`has_active_bankruptcy = True`) on file, OR has more than 1 delinquency (30+ days past due) across all tradelines in the last 12 months (`total_delinquencies_12m > 1`).
* **Rule 2.4 (Cash Flow Distress):** Applicant has more than 2 punitive Non-Sufficient Funds (NSF) or overdraft fee events in the past 90 days (`nsf_count_90d > 2`).

## Section 3: Standard Auto-Approval Criteria
An application is eligible for immediate **AUTO-APPROVAL** (`APPROVE`) if ALL of the following criteria are met:
* **Rule 3.1 (Prime Credit Tier):** FICO Score is 680 or higher.
* **Rule 3.2 (Healthy Debt Load):** Reconciled Post-Loan DTI (`post_loan_dti`) is 36.0% or lower.
* **Rule 3.3 (Loan Sizing Cap):** Requested loan amount is $25,000 or less, AND does not exceed 30% of verified annualized income (`verified_monthly_income * 12`).
* **Rule 3.4 (Liquidity & Buffer):** Zero NSF/overdraft events in the last 90 days (`nsf_count_90d == 0`), AND the Payment Cushion Ratio (`payment_cushion_ratio` = Average Ending Monthly Balance / New Monthly Loan Payment) is at least 2.0x.
* **Rule 3.5 (Income Consistency):** Income Variance (`income_variance_pct`) between stated monthly income and bank-verified monthly income does not exceed 15.0%.

## Section 4: Manual Underwriting Escalation (Grey Area / Human-in-the-Loop)
If an application passes all Section 2 Hard Knockouts (zero knockout violations) but fails one or more Section 3 Auto-Approval rules, it must be routed to **ESCALATE** for Human Underwriter review under the following guidelines:
* **Rule 4.1 (Near-Prime Credit or Elevated DTI):** FICO Score is between 620 and 679, OR Reconciled Post-Loan DTI is between 36.01% and 45.00%, OR `nsf_count_90d` is 1 or 2.
* **Rule 4.2 (High-Value Loan Gate):** Requested loan amount exceeds $25,000 (up to the $50,000 product maximum), requiring manual executive sign-off even if credit metrics are prime.
* **Rule 4.3 (Stated vs. Verified Net Income Mismatch):** `income_variance_pct` exceeds 15.0% (e.g., due to tax/401k withholding between gross stated salary and net direct deposits), requiring the human underwriter to verify a W-2 or paystub before final approval.
