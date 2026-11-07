"""
classification.py
UI-agnostic bank transaction classification vocabulary and heuristic fallback classifier.

This module is the single source of truth for:
- The human-readable classification labels offered to credit officers (credit vs. debit entries).
- The bidirectional mapping between LLM schema categories (see `schema.TransactionCategory`)
  and those human-readable labels.
- A deterministic, rule-based heuristic classifier used when no AI classification is available
  (or when an officer explicitly resets to heuristics).

Keeping this out of any presentation layer lets the FastAPI service layer, CLI tools, and tests
share one vocabulary without importing UI frameworks.
"""
from typing import Any, Dict, List

UNCLASSIFIED_LABEL = "— Select Classification —"

CREDIT_CLASSIFICATION_OPTIONS: List[str] = [
    UNCLASSIFIED_LABEL,
    "Verified Payroll Income",
    "One-Off Gift / Transfer (Excluded)",
    "Internal Account Transfer (Excluded)",
]

DEBIT_CLASSIFICATION_OPTIONS: List[str] = [
    UNCLASSIFIED_LABEL,
    "Known Bureau Debt Payment",
    "Undisclosed Debt / BNPL",
    "NSF / Overdraft Fee",
    "Bank Maintenance Fee (Excluded)",
    "Other Living Expense / Standard",
]

SCHEMA_CATEGORY_TO_UI_MAP: Dict[str, str] = {
    "VERIFIED_PAYROLL_INCOME": "Verified Payroll Income",
    "ONE_OFF_GIFT_OR_TRANSFER_IGNORE": "One-Off Gift / Transfer (Excluded)",
    "INTERNAL_TRANSFER_IGNORE": "Internal Account Transfer (Excluded)",
    "MATCHED_BUREAU_DEBT": "Known Bureau Debt Payment",
    "UNDISCLOSED_DEBT_PAYMENT": "Undisclosed Debt / BNPL",
    "NSF_OVERDRAFT_FEE": "NSF / Overdraft Fee",
    "BANK_MAINTENANCE_FEE_IGNORE": "Bank Maintenance Fee (Excluded)",
    "OTHER_EXPENSE_IGNORE": "Other Living Expense / Standard",
}

UI_TO_SCHEMA_CATEGORY_MAP: Dict[str, str] = {v: k for k, v in SCHEMA_CATEGORY_TO_UI_MAP.items()}


def map_category_to_ui(category: str) -> str:
    """Translates a raw Pydantic schema category into its human-readable label (pass-through if unknown)."""
    return SCHEMA_CATEGORY_TO_UI_MAP.get(category, category)


def map_ui_to_category(label: str) -> str:
    """Translates a human-readable label back into the schema category (pass-through if unknown)."""
    return UI_TO_SCHEMA_CATEGORY_MAP.get(label, label)


def options_for_transaction(tx: Dict[str, Any]) -> List[str]:
    """Returns the valid classification options for a transaction based on its cash-flow direction."""
    return CREDIT_CLASSIFICATION_OPTIONS if float(tx.get("amount", 0.0)) > 0 else DEBIT_CLASSIFICATION_OPTIONS


def is_classified(label: Any) -> bool:
    """True when a label represents an actual classification rather than the empty placeholder."""
    return label not in (None, "", UNCLASSIFIED_LABEL)


def auto_classify_transaction(tx: Dict[str, Any]) -> str:
    """Heuristic rule-based auto-classification fallback (deterministic, no LLM)."""
    amt = float(tx.get("amount", 0.0))
    desc_upper = str(tx.get("description", "")).upper()
    if amt > 0:
        if "PAYROLL" in desc_upper or "DIR DEP" in desc_upper:
            return "Verified Payroll Income"
        if "XFER" in desc_upper or "TRANSFER" in desc_upper:
            return "Internal Account Transfer (Excluded)"
        return "One-Off Gift / Transfer (Excluded)"

    if "NSF" in desc_upper or "OVERDRAFT" in desc_upper or "RETURNED" in desc_upper:
        return "NSF / Overdraft Fee"
    if any(k in desc_upper for k in ("AFFIRM", "KLARNA", "PAYDAY", "SPEEDYCASH")):
        return "Undisclosed Debt / BNPL"
    if any(k in desc_upper for k in ("AUTOPAY", "LOAN", "FIN", "MTG", "EPAY")):
        return "Known Bureau Debt Payment"
    if any(k in desc_upper for k in ("FEE", "MAINTENANCE", "SERVICE")):
        return "Bank Maintenance Fee (Excluded)"
    return "Other Living Expense / Standard"


def resolve_classifications(
    transactions: List[Dict[str, Any]],
    categorized_transactions: List[Dict[str, Any]] | None,
) -> Dict[str, str]:
    """
    Builds the {tx_id: ui_label} map for a bank ledger, preferring the AI agent's classification
    (from graph state) and falling back to the heuristic classifier for any unclassified entry.
    """
    ai_map = {tx["tx_id"]: map_category_to_ui(tx.get("category", "")) for tx in (categorized_transactions or [])}
    return {
        t["tx_id"]: ai_map.get(t["tx_id"]) or auto_classify_transaction(t)
        for t in transactions
    }
