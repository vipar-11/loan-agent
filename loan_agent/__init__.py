"""
loan_agent
Apex Financial — Institutional Loan Underwriting & Approval Workbench.
"""

__version__ = "0.1.0"

from loan_agent.state import LoanApplicationState
from loan_agent.schema import (
    CategorizedTransaction,
    BankStatementAnalysisOutput,
    UnderwritingOutput,
    TransactionCategory,
)
from loan_agent.calculator import (
    get_fico_score,
    has_active_bankruptcy,
    get_total_delinquencies_12m,
    count_nsf_overdraft_events,
    calculate_verified_monthly_income,
    calculate_stated_monthly_income,
    calculate_income_variance_pct,
    calculate_monthly_installment,
    calculate_bureau_monthly_debt,
    calculate_undisclosed_monthly_debt,
    calculate_total_post_loan_debt,
    calculate_post_loan_dti,
    calculate_avg_ending_balance,
    calculate_payment_cushion_ratio,
    compute_financial_ratios,
)
from loan_agent.mock_apis import (
    APPLICANT_PROFILES,
    fetch_experian_credit_report,
    fetch_plaid_bank_statement,
    prefilter_underwriting_transactions,
)
from loan_agent.graph_workflow import (
    build_underwriting_graph,
    load_policy_document,
)

__all__ = [
    "LoanApplicationState",
    "CategorizedTransaction",
    "BankStatementAnalysisOutput",
    "UnderwritingOutput",
    "TransactionCategory",
    "get_fico_score",
    "has_active_bankruptcy",
    "get_total_delinquencies_12m",
    "count_nsf_overdraft_events",
    "calculate_verified_monthly_income",
    "calculate_stated_monthly_income",
    "calculate_income_variance_pct",
    "calculate_monthly_installment",
    "calculate_bureau_monthly_debt",
    "calculate_undisclosed_monthly_debt",
    "calculate_total_post_loan_debt",
    "calculate_post_loan_dti",
    "calculate_avg_ending_balance",
    "calculate_payment_cushion_ratio",
    "compute_financial_ratios",
    "APPLICANT_PROFILES",
    "fetch_experian_credit_report",
    "fetch_plaid_bank_statement",
    "prefilter_underwriting_transactions",
    "build_underwriting_graph",
    "load_policy_document",
]
