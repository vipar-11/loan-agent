"""
calculator.py
Deterministic Python Financial Ratio & Underwriting Metrics Calculator (SR 11-7 Compliant).

Provides modular, individual calculation functions for loan underwriting:
- FICO score & adverse public records checks (Active bankruptcy)
- 12-Month delinquency tallying
- 90-Day punitive NSF / overdraft event counting
- Verified payroll income & income variance percentage
- New loan monthly installment sizing
- Bureau monthly debt obligation
- Undisclosed / hidden debt obligation (BNPL / payday loans)
- Reconciled post-loan Debt-to-Income (DTI) ratio
- Liquidity cushion / payment reserve ratio
"""
from typing import Dict, Any, List, Optional, Tuple


def get_fico_score(credit_report: Dict[str, Any]) -> int:
    """Extracts the consumer FICO credit score from the credit bureau report."""
    return int(credit_report.get("fico_score", 0))


def has_active_bankruptcy(credit_report: Dict[str, Any]) -> bool:
    """
    Checks for any active bankruptcy filings on file.
    Note: Historical bankruptcies that are DISCHARGED do not trigger an active bankruptcy flag.
    """
    for rec in credit_report.get("public_records", []):
        rec_type = str(rec.get("type", "")).upper()
        rec_status = str(rec.get("status", "")).upper()
        if "BANKRUPTCY" in rec_type and rec_status == "ACTIVE":
            return True
    return False


def get_total_delinquencies_12m(credit_report: Dict[str, Any]) -> int:
    """Sums all 30-day, 60-day, and 90+ day delinquencies across all active tradelines in the past 12 months."""
    total = 0
    for tl in credit_report.get("tradelines", []):
        delinq = tl.get("delinquencies_12m", {})
        d30 = int(delinq.get("30_days", 0))
        d60 = int(delinq.get("60_days", 0))
        d90 = int(delinq.get("90_plus_days", 0))
        total += (d30 + d60 + d90)
    return total


def count_nsf_overdraft_events(categorized_txs: List[Dict[str, Any]]) -> int:
    """Counts the number of punitive NSF / Overdraft fee events in the bank statement period."""
    nsf_categories = {"NSF_OVERDRAFT_FEE", "NSF / Overdraft Fee"}
    return sum(
        1 for tx in categorized_txs
        if tx.get("category") in nsf_categories or tx.get("classification") in nsf_categories
    )


def calculate_verified_monthly_income(
    categorized_txs: List[Dict[str, Any]], statement_months: float = 3.0
) -> float:
    """
    Calculates verified average monthly payroll income from bank ledger deposits.
    Only inflows categorized as verified payroll are included.
    """
    payroll_categories = {"VERIFIED_PAYROLL_INCOME", "Verified Payroll Income"}
    months = max(statement_months, 1.0)
    total_payroll = sum(
        float(tx.get("amount", 0.0))
        for tx in categorized_txs
        if (tx.get("category") in payroll_categories or tx.get("classification") in payroll_categories)
        and float(tx.get("amount", 0.0)) > 0
    )
    return round(total_payroll / months, 2)


def calculate_stated_monthly_income(stated_annual_income: float) -> float:
    """Converts stated annual gross income to monthly stated gross income."""
    if not stated_annual_income or stated_annual_income <= 0:
        return 0.0
    return round(float(stated_annual_income) / 12.0, 2)


def calculate_income_variance_pct(
    stated_annual_income: float, verified_monthly_income: float
) -> float:
    """
    Calculates the percentage variance between stated monthly income and verified monthly income.
    Formula: |stated_monthly - verified_monthly| / stated_monthly * 100
    """
    stated_monthly = calculate_stated_monthly_income(stated_annual_income)
    if stated_monthly <= 0:
        return 100.0 if verified_monthly_income <= 0 else 0.0
    variance = abs(stated_monthly - verified_monthly_income) / stated_monthly * 100.0
    return round(variance, 2)


def calculate_monthly_installment(
    loan_amount: Optional[float], annual_rate: float = 0.12, term_months: int = 36
) -> float:
    """Calculates standard fixed-rate monthly principal & interest payment."""
    if loan_amount is None or loan_amount <= 0:
        return 0.0
    r = annual_rate / 12.0
    n = term_months
    factor = (1.0 + r) ** n
    payment = loan_amount * (r * factor) / (factor - 1.0)
    return round(payment, 2)


def calculate_bureau_monthly_debt(credit_report: Dict[str, Any]) -> float:
    """Calculates total existing monthly debt obligations declared across credit bureau tradelines."""
    tradelines = credit_report.get("tradelines", [])
    total_debt = sum(float(tl.get("monthly_payment", 0.0)) for tl in tradelines)
    return round(total_debt, 2)


def calculate_undisclosed_monthly_debt(
    categorized_txs: List[Dict[str, Any]], statement_months: float = 3.0
) -> float:
    """
    Calculates undisclosed / hidden monthly recurring debt obligations (e.g. BNPL, Payday installments)
    detected in bank ledger transactions.
    """
    hidden_categories = {"UNDISCLOSED_DEBT_PAYMENT", "Undisclosed Debt / BNPL"}
    months = max(statement_months, 1.0)
    total_hidden = sum(
        abs(float(tx.get("amount", 0.0)))
        for tx in categorized_txs
        if tx.get("category") in hidden_categories or tx.get("classification") in hidden_categories
    )
    return round(total_hidden / months, 2)


def calculate_total_post_loan_debt(
    bureau_monthly_debt: float,
    undisclosed_monthly_debt: float,
    estimated_new_payment: float,
) -> float:
    """Calculates aggregate post-loan monthly debt: bureau debt + undisclosed debt + new loan payment."""
    return round(bureau_monthly_debt + undisclosed_monthly_debt + estimated_new_payment, 2)


def calculate_post_loan_dti(
    total_post_loan_debt: float, verified_monthly_income: float
) -> float:
    """
    Calculates post-loan Debt-to-Income (DTI) ratio percentage.
    Formula: (total_post_loan_debt / verified_monthly_income) * 100
    """
    if verified_monthly_income <= 0:
        return 999.0
    dti = (total_post_loan_debt / verified_monthly_income) * 100.0
    return round(dti, 2)


def calculate_avg_ending_balance(bank_statement: Dict[str, Any]) -> float:
    """Calculates the average end-of-month checking account balance across the statement period."""
    eoms = bank_statement.get("eom_balances", [])
    if not eoms:
        return 0.0
    balances = [float(b.get("ending_balance", 0.0)) for b in eoms]
    return round(sum(balances) / len(balances), 2)


def calculate_payment_cushion_ratio(
    avg_ending_balance: float, estimated_new_payment: float
) -> float:
    """
    Calculates liquidity cushion ratio: average checking balance divided by the new monthly installment.
    Indicates how many months of loan payments the applicant retains in cash reserves.
    """
    if estimated_new_payment <= 0:
        return 0.0
    return round(avg_ending_balance / estimated_new_payment, 2)


def compute_financial_ratios(
    credit_report: Dict[str, Any],
    bank_raw: Dict[str, Any],
    categorized_txs: List[Dict[str, Any]],
    stated_annual_income: float,
    loan_amount_requested: float,
    annual_rate: float = 0.12,
    term_months: int = 36,
) -> Dict[str, Any]:
    """
    Comprehensive financial ratio engine. Computes all 10 core underwriting metrics
    from credit bureau data, bank statement raw data, and categorized bank transactions.
    """
    months = float(bank_raw.get("statement_period_months", 3))

    fico_score = get_fico_score(credit_report)
    active_bankruptcy = has_active_bankruptcy(credit_report)
    delinquencies_12m = get_total_delinquencies_12m(credit_report)
    nsf_count = count_nsf_overdraft_events(categorized_txs)

    verified_income = calculate_verified_monthly_income(categorized_txs, months)
    stated_monthly = calculate_stated_monthly_income(stated_annual_income)
    income_variance = calculate_income_variance_pct(stated_annual_income, verified_income)

    new_payment = calculate_monthly_installment(loan_amount_requested, annual_rate, term_months)
    bureau_debt = calculate_bureau_monthly_debt(credit_report)
    undisclosed_debt = calculate_undisclosed_monthly_debt(categorized_txs, months)
    total_post_debt = calculate_total_post_loan_debt(bureau_debt, undisclosed_debt, new_payment)

    post_loan_dti = calculate_post_loan_dti(total_post_debt, verified_income)
    avg_eom = calculate_avg_ending_balance(bank_raw)
    payment_cushion = calculate_payment_cushion_ratio(avg_eom, new_payment)

    return {
        "fico_score": fico_score,
        "has_active_bankruptcy": active_bankruptcy,
        "total_delinquencies_12m": delinquencies_12m,
        "nsf_count_90d": nsf_count,
        "verified_monthly_income": verified_income,
        "stated_monthly_income": stated_monthly,
        "income_variance_pct": income_variance,
        "estimated_new_loan_payment": new_payment,
        "bureau_monthly_debt": bureau_debt,
        "undisclosed_monthly_debt": undisclosed_debt,
        "total_post_loan_debt": total_post_debt,
        "post_loan_dti": post_loan_dti,
        "avg_ending_balance": avg_eom,
        "payment_cushion_ratio": payment_cushion,
    }
