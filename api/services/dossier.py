"""
Review dossier assembly for the Underwriter Desk.

Projects a persisted application (and its persisted workflow state) onto the sections a credit officer
reviews: AI assessment, bureau tradelines, bank cash flow (AI-classified), financial ratios.
Also powers on-demand ratio recalculation after an officer re-classifies transactions.
"""
from typing import Any, Dict, List, Optional

from api.schemas import (
    AIAssessment,
    BankSection,
    BankTransaction,
    CreditSection,
    Dossier,
    FinancialRatios,
    LoanSizing,
    RatioRow,
    RatiosResponse,
    TradelineRow,
)
from api.services.applications import to_summary
from loan_agent.calculator import calculate_monthly_installment, compute_financial_ratios
from loan_agent.classification import (
    auto_classify_transaction,
    is_classified,
    options_for_transaction,
    resolve_classifications,
)
from loan_agent.mock_apis import fetch_experian_credit_report, fetch_plaid_bank_statement


def format_currency(val: Any) -> str:
    if val is None or val == "":
        return "—"
    return f"${float(val):,.2f}"


# ---------------------------------------------------------------------------
# External data resolution (graph state first, mock provider fallback second)
# ---------------------------------------------------------------------------
def resolve_credit_report(record: Dict[str, Any]) -> Dict[str, Any]:
    state = record.get("graph_state") or {}
    report = state.get("credit_report")
    if not report and record.get("fcra_consent"):
        try:
            report = fetch_experian_credit_report(str(record["ssn_last_4"]))
        except Exception:
            report = {}
    return report or {}


def resolve_bank_statement(record: Dict[str, Any]) -> Dict[str, Any]:
    state = record.get("graph_state") or {}
    bank = state.get("bank_statement_raw")
    if not bank and record.get("fcra_consent"):
        try:
            bank = fetch_plaid_bank_statement(str(record["ssn_last_4"]))
        except Exception:
            bank = {}
    return bank or {}


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def build_tradeline_rows(credit_report: Dict[str, Any]) -> List[TradelineRow]:
    rows: List[TradelineRow] = []
    for tl in credit_report.get("tradelines", []):
        delinq = tl.get("delinquencies_12m", {})
        d30, d60, d90 = delinq.get("30_days", 0), delinq.get("60_days", 0), delinq.get("90_plus_days", 0)
        total = d30 + d60 + d90
        rows.append(
            TradelineRow(
                creditor=tl.get("creditor", "—"),
                account_type=str(tl.get("account_type", "—")).replace("_", " "),
                monthly_payment=float(tl.get("monthly_payment") or 0.0),
                delinquencies="Clean (0)" if total == 0 else f"{d30}x30d, {d60}x60d, {d90}x90d+",
                standing="Current" if total == 0 else "Delinquent",
            )
        )
    return rows


def build_credit_section(credit_report: Dict[str, Any]) -> CreditSection:
    tradelines = credit_report.get("tradelines", [])
    return CreditSection(
        fico_score=int(credit_report.get("fico_score") or 0),
        bureau_monthly_debt=round(sum(float(t.get("monthly_payment") or 0.0) for t in tradelines), 2),
        public_records=credit_report.get("public_records", []),
        tradelines=build_tradeline_rows(credit_report),
    )


def build_bank_section(record: Dict[str, Any], bank_raw: Dict[str, Any]) -> BankSection:
    state = record.get("graph_state") or {}
    txs = bank_raw.get("transactions", [])
    categorized = state.get("categorized_transactions") or []
    classifications = resolve_classifications(txs, categorized)
    reasoning = {t["tx_id"]: t.get("reasoning", "") for t in categorized}
    eoms = bank_raw.get("eom_balances", [])

    return BankSection(
        bank_name=bank_raw.get("bank_name"),
        account_mask=bank_raw.get("account_mask"),
        statement_period_months=float(bank_raw.get("statement_period_months", 3)),
        avg_eom_balance=round(sum(float(b.get("ending_balance", 0)) for b in eoms) / len(eoms), 2) if eoms else 0.0,
        total_inflows=round(sum(float(t["amount"]) for t in txs if float(t["amount"]) > 0), 2),
        total_outflows=round(sum(abs(float(t["amount"])) for t in txs if float(t["amount"]) < 0), 2),
        transactions=[
            BankTransaction(
                tx_id=t["tx_id"],
                date=t.get("date"),
                description=t.get("description", "—"),
                amount=float(t.get("amount", 0.0)),
                flow="CREDIT" if float(t.get("amount", 0.0)) > 0 else "DEBIT",
                classification=classifications[t["tx_id"]],
                ai_reasoning=reasoning.get(t["tx_id"]) or None,
                options=options_for_transaction(t),
            )
            for t in txs
        ],
        classified_by_ai=bool(categorized),
    )


def build_ai_assessment(record: Dict[str, Any]) -> AIAssessment:
    state = record.get("graph_state") or {}
    confidence = float(state.get("confidence_score") or 0.0)
    return AIAssessment(
        underwriting_decision=state.get("underwriting_decision") or "ESCALATE",
        confidence_score=confidence,
        confidence_pct=f"{confidence * 100:.0f}%" if confidence <= 1.0 else f"{confidence:.0f}%",
        underwriter_memo=state.get("underwriter_memo") or "No memorandum generated.",
        policy_citations=list(state.get("policy_citations") or []),
        adverse_action_reasons=list(state.get("adverse_action_reasons") or []),
        bank_analyst_summary=state.get("bank_analyst_summary") or "No bank analysis summary generated.",
        execution_log=list(state.get("execution_log") or []),
        error_message=state.get("error_message"),
    )


def ratios_from_graph_state(record: Dict[str, Any], credit_report: Dict[str, Any]) -> FinancialRatios:
    """Auto-populates ratios from the deterministic calculation output persisted in graph state (with sensible fallbacks)."""
    state = record.get("graph_state") or {}
    loan_amount = float(record.get("loan_amount_requested") or 0.0)
    stated_monthly = round(float(record.get("stated_annual_income") or 0.0) / 12.0, 2)
    bureau_debt = float(state.get("bureau_monthly_debt") or 0.0)
    undisclosed = float(state.get("undisclosed_monthly_debt") or 0.0)
    new_pmt = float(state.get("estimated_new_Loan_payment") or (calculate_monthly_installment(loan_amount) if loan_amount > 0 else 0.0))
    return FinancialRatios(
        fico_score=int(state.get("fico_score") or credit_report.get("fico_score") or 0),
        has_active_bankruptcy=bool(state.get("has_active_bankruptcy", False)),
        total_delinquencies_12m=int(state.get("total_delinquencies_12m") or 0),
        nsf_count_90d=int(state.get("nsf_count_90d") or 0),
        bureau_monthly_debt=bureau_debt,
        undisclosed_monthly_debt=undisclosed,
        estimated_new_loan_payment=new_pmt,
        total_post_loan_debt=round(bureau_debt + undisclosed + new_pmt, 2),
        verified_monthly_income=float(state.get("verified_monthly_income") or 0.0),
        stated_monthly_income=float(state.get("stated_monthly_income") or stated_monthly),
        income_variance_pct=float(state.get("income_variance_pct") or 0.0),
        post_loan_dti=float(state.get("post_loan_dti") or 0.0),
        avg_ending_balance=float(state.get("avg_ending_balance") or 0.0),
        payment_cushion_ratio=float(state.get("payment_cushion_ratio") or 0.0),
    )


def build_ratio_rows(r: FinancialRatios) -> List[RatioRow]:
    """Policy-benchmarked ratio worksheet (presentation strings computed server-side for auditability)."""
    return [
        RatioRow(metric="FICO Credit Score", value=f"{r.fico_score}", benchmark=">= 680 Tier 1 Prime"),
        RatioRow(metric="Active Bankruptcy Record", value="ACTIVE" if r.has_active_bankruptcy else "None (Clean)", benchmark="Active is Knockout"),
        RatioRow(metric="12-Month Tradeline Delinquencies", value=f"{r.total_delinquencies_12m} events", benchmark="<= 1 allowed for Tier 1"),
        RatioRow(metric="90-Day NSF / Overdraft Fees", value=f"{r.nsf_count_90d} fee events", benchmark="<= 2 allowed for Tier 1"),
        RatioRow(metric="Stated vs. Verified Monthly Income", value=f"{format_currency(r.stated_monthly_income)} stated vs {format_currency(r.verified_monthly_income)} verified", benchmark="Verified Bank Payroll"),
        RatioRow(metric="Income Variance %", value=f"{r.income_variance_pct:.2f}%", benchmark="<= 10% Tier 1, <= 25% Tier 2"),
        RatioRow(metric="Bureau Monthly Debt", value=format_currency(r.bureau_monthly_debt), benchmark="Declared Tradelines"),
        RatioRow(metric="Undisclosed / Hidden Debt (BNPL)", value=format_currency(r.undisclosed_monthly_debt), benchmark="Detected Recurring Outflows"),
        RatioRow(metric="Estimated New Loan Payment", value=format_currency(r.estimated_new_loan_payment), benchmark="36 Mo @ 12.0% APR"),
        RatioRow(metric="Total Aggregate Post-Loan Debt", value=format_currency(r.total_post_loan_debt), benchmark="Bureau + Hidden + New Loan"),
        RatioRow(metric="Reconciled Post-Loan DTI", value=f"{r.post_loan_dti:.2f}%", benchmark="<= 38% Tier 1, <= 45% Near-Prime"),
        RatioRow(metric="Average Ending Checking Balance", value=format_currency(r.avg_ending_balance), benchmark="90-Day EOM Average"),
        RatioRow(metric="Liquidity Payment Cushion Ratio", value=f"{r.payment_cushion_ratio:.2f}x", benchmark=">= 2.0x required for Tier 1"),
    ]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def build_dossier(record: Dict[str, Any]) -> Dossier:
    credit_report = resolve_credit_report(record)
    bank_raw = resolve_bank_statement(record)
    loan_amount = float(record.get("loan_amount_requested") or 0.0)
    ratios = ratios_from_graph_state(record, credit_report)

    return Dossier(
        application=to_summary(record),
        stated_monthly_income=round(float(record.get("stated_annual_income") or 0.0) / 12.0, 2),
        sizing=LoanSizing(
            loan_amount_requested=loan_amount,
            monthly_payment=calculate_monthly_installment(loan_amount) if loan_amount > 0 else None,
        ),
        ai_assessment=build_ai_assessment(record),
        credit=build_credit_section(credit_report),
        bank=build_bank_section(record, bank_raw),
        ratios=RatiosResponse(ratios=ratios, rows=build_ratio_rows(ratios), source="GRAPH_STATE"),
    )


def heuristic_classifications(record: Dict[str, Any]) -> Dict[str, str]:
    """Deterministic rule-based {tx_id: label} map (officer "Reset to Heuristic" action)."""
    bank_raw = resolve_bank_statement(record)
    return {t["tx_id"]: auto_classify_transaction(t) for t in bank_raw.get("transactions", [])}


def recalculate_ratios(record: Dict[str, Any], classifications: Optional[Dict[str, str]] = None) -> RatiosResponse:
    """
    Re-runs the deterministic SR 11-7 calculator with officer-supplied classifications.
    Unknown / unclassified entries fall back to the heuristic classifier.
    """
    credit_report = resolve_credit_report(record)
    bank_raw = resolve_bank_statement(record)
    supplied = classifications or {}
    categorized = []
    for t in bank_raw.get("transactions", []):
        label = supplied.get(t["tx_id"])
        categorized.append(
            {
                "tx_id": t["tx_id"],
                "date": t.get("date"),
                "description": t.get("description"),
                "amount": float(t.get("amount", 0.0)),
                "category": label if is_classified(label) else auto_classify_transaction(t),
            }
        )
    raw = compute_financial_ratios(
        credit_report=credit_report,
        bank_raw=bank_raw,
        categorized_txs=categorized,
        stated_annual_income=float(record.get("stated_annual_income") or 0.0),
        loan_amount_requested=float(record.get("loan_amount_requested") or 0.0),
    )
    ratios = FinancialRatios(**raw)
    return RatiosResponse(ratios=ratios, rows=build_ratio_rows(ratios), source="RECALCULATED")
