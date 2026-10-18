"""
state_and_schemas.py
Defines the LangGraph LoanApplicationState (TypedDict) and the Pydantic Structured
Output schemas for the two LLM Agents (Bank Statement Analyzer & Policy Underwriter).
"""
from typing import TypedDict, Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 1. PYDANTIC SCHEMA FOR AGENT #1: BANK STATEMENT ANALYZER & RECONCILER
# ---------------------------------------------------------------------------
TransactionCategory = Literal[
    "VERIFIED_PAYROLL_INCOME",
    "ONE_OFF_GIFT_OR_TRANSFER_IGNORE",
    "INTERNAL_TRANSFER_IGNORE",
    "MATCHED_BUREAU_DEBT",
    "UNDISCLOSED_DEBT_PAYMENT",
    "NSF_OVERDRAFT_FEE",
    "BANK_MAINTENANCE_FEE_IGNORE",
    "OTHER_EXPENSE_IGNORE",
]


class CategorizedTransaction(BaseModel):
    tx_id: str = Field(description="The exact transaction ID from the input list (e.g., D01, M03).")
    date: str = Field(description="Transaction date YYYY-MM-DD.")
    description: str = Field(description="Raw transaction description.")
    amount: float = Field(description="Exact dollar amount (positive for inflows, negative for outflows).")
    category: TransactionCategory = Field(
        description="Strict underwriting classification category for this transaction."
    )
    matched_tradeline_id: Optional[str] = Field(
        default=None,
        description="If category is MATCHED_BUREAU_DEBT, provide the matching Experian tradeline_id (e.g., TL_301). Otherwise null.",
    )
    reasoning: str = Field(
        description="Brief 1-sentence explanation of why this category was chosen."
    )


class BankStatementAnalysisOutput(BaseModel):
    categorized_transactions: List[CategorizedTransaction] = Field(
        description="Every transaction from the pre-filtered input list, classified into underwriting buckets."
    )
    analyst_summary: str = Field(
        description="2-3 sentence summary of cash flow findings, noting any excluded transfers/gifts, undisclosed debt obligations, or NSF events."
    )


# ---------------------------------------------------------------------------
# 2. PYDANTIC SCHEMA FOR AGENT #2: POLICY UNDERWRITER AGENT
# ---------------------------------------------------------------------------
class UnderwritingOutput(BaseModel):
    underwriting_decision: Literal["APPROVE", "DENY", "ESCALATE"] = Field(
        description="The underwriting decision strictly adhering to the provided policy document."
    )
    confidence_score: float = Field(
        description="Confidence score between 0.0 and 1.0 in this policy evaluation."
    )
    policy_citations: List[str] = Field(
        description="Exact rule identifiers triggered from the provided policy document."
    )
    adverse_action_reasons: List[str] = Field(
        default_factory=list,
        description="Required under ECOA ONLY if underwriting_decision is DENY. Specific consumer-facing reasons for denial. Empty list if APPROVE or ESCALATE.",
    )
    underwriter_memo: str = Field(
        description="Comprehensive executive memo explaining the decision, citing exact calculated numbers, and (if ESCALATE) stating what the human underwriter needs to verify."
    )
