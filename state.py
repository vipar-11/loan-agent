"""
state_and_schemas.py
Defines the LangGraph LoanApplicationState (TypedDict) and the Pydantic Structured
Output schemas for the two LLM Agents (Bank Statement Analyzer & Policy Underwriter).
"""
from typing import TypedDict, Optional, List, Dict, Any, Literal
from pydantic import Field

# ---------------------------------------------------------------------------
# LANGGRAPH CENTRAL STATE (TypedDict)
# ---------------------------------------------------------------------------
class LoanApplicationState(TypedDict, total=False):
    # --- A. Web App Input Fields ---
    application_id: str
    applicant_name: str
    ssn_last_4: str
    stated_annual_income: float
    loan_amount_requested: float
    loan_purpose: str
    fcra_consent: bool

    # --- B. Raw External API Data (Populated in Parallel) ---
    credit_report: Optional[Dict[str, Any]]
    bank_statement_raw: Optional[Dict[str, Any]]
    prefiltered_transactions: Optional[List[Dict[str, Any]]]

    # --- C. Agent #1 Output: Semantic Bank Reconciliation ---
    categorized_transactions: Optional[List[Dict[str, Any]]]
    bank_analyst_summary: Optional[str]

    # --- D. Deterministic Python Math Output (The 7 Core Metrics + Supporting Numbers) ---
    fico_score: Optional[int]
    has_active_bankruptcy: Optional[bool]
    total_delinquencies_12m: Optional[int]
    nsf_count_90d: Optional[int]
    bureau_monthly_debt: Optional[float]
    undisclosed_monthly_debt: Optional[float]
    verified_monthly_income: Optional[float]
    stated_monthly_income: Optional[float]
    income_variance_pct: Optional[float]
    estimated_new_Loan_payment: Optional[float]
    post_loan_dti: Optional[float]
    avg_ending_balance: Optional[float]
    payment_cushion_ratio: Optional[float]

    # --- E. Agent #2 Output: Policy Underwriting Decision ---
    underwriting_decision: Optional[Literal["APPROVE", "DENY", "ESCALATE", "REJECT_NO_CONSENT"]]
    confidence_score: Optional[float]
    policy_citations: Optional[List[str]]
    adverse_action_reasons: Optional[List[str]]
    underwriter_memo: Optional[str]

    # --- F. Error Tracking, Human-in-the-Loop Override & Final Status ---
    error_message: Optional[str]
    human_reviewer_decision: Optional[Literal["APPROVE", "DENY"]]
    human_reviewer_notes: Optional[str]
    final_status: Optional[str]
    execution_log: Optional[List[str]]