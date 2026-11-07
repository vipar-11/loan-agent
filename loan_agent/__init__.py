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
from loan_agent.config import (
    DatabaseConfig,
    load_db_config,
)
from loan_agent.db import (
    ApplicationDatabase,
    get_application_database,
)
from loan_agent.checkpointer import (
    get_langgraph_checkpointer,
)
from loan_agent.workflow_runner import (
    run_initiate_workflow,
    process_application,
    resume_with_human_decision,
)
from loan_agent.classification import (
    CREDIT_CLASSIFICATION_OPTIONS,
    DEBIT_CLASSIFICATION_OPTIONS,
    SCHEMA_CATEGORY_TO_UI_MAP,
    map_category_to_ui,
    map_ui_to_category,
    auto_classify_transaction,
    resolve_classifications,
)
from loan_agent.admin import (
    list_loans_action,
    trace_loan_action,
)
from loan_agent.api_cli import (
    ApiClient,
    main_list_loans,
    main_trace_loan,
    main_initiate_workflow,
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
    "DatabaseConfig",
    "load_db_config",
    "ApplicationDatabase",
    "get_application_database",
    "get_langgraph_checkpointer",
    "run_initiate_workflow",
    "process_application",
    "resume_with_human_decision",
    "CREDIT_CLASSIFICATION_OPTIONS",
    "DEBIT_CLASSIFICATION_OPTIONS",
    "SCHEMA_CATEGORY_TO_UI_MAP",
    "map_category_to_ui",
    "map_ui_to_category",
    "auto_classify_transaction",
    "resolve_classifications",
    "list_loans_action",
    "trace_loan_action",
    "ApiClient",
    "main_list_loans",
    "main_trace_loan",
    "main_initiate_workflow",
]

