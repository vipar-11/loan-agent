"""
mock_apis.py
Simulates external REST API calls to Experian (Credit Bureau) and Plaid (90-Day Bank Ledger).
Includes realistic tradelines, payroll deposits, internal transfers, BNPL hidden debt,
NSF fee reversals, and everyday small-dollar noise.
"""
from typing import Dict, Any, List

# ---------------------------------------------------------------------------
# 1. PRE-CONFIGURED WEB APP APPLICANT PROFILES (For 1-Click Demo Selection)
# ---------------------------------------------------------------------------
APPLICANT_PROFILES: Dict[str, Dict[str, Any]] = {
    "SARAH_JENKINS": {
        "application_id": "APP-2026-101",
        "applicant_name": "Sarah Jenkins",
        "ssn_last_4": "4401",
        "stated_annual_income": 96000.0,   # $8,000.00 / month
        "loan_amount_requested": 15000.0,  # Est. payment ~$498.21/mo
        "loan_purpose": "Home Improvement",
        "persona_label": "Persona 1: Sarah Jenkins (Clean Auto-Approve)",
    },
    "MARCUS_VANCE": {
        "application_id": "APP-2026-102",
        "applicant_name": "Marcus Vance",
        "ssn_last_4": "8829",
        "stated_annual_income": 60000.0,   # $5,000.00 / month
        "loan_amount_requested": 20000.0,  # Est. payment ~$664.29/mo
        "loan_purpose": "Debt Consolidation",
        "persona_label": "Persona 2: Marcus Vance (Hard Knockout Auto-Deny)",
    },
    "DAVID_CHEN": {
        "application_id": "APP-2026-103",
        "applicant_name": "David Chen",
        "ssn_last_4": "9941",
        "stated_annual_income": 110000.0,  # $9,166.67 / month (Gross)
        "loan_amount_requested": 35000.0,  # Est. payment ~$1,162.50/mo
        "loan_purpose": "Medical Expenses",
        "persona_label": "Persona 3: David Chen (Nuanced Grey-Area Escalation)",
    },
}

# ---------------------------------------------------------------------------
# 2. MOCK EXPERIAN CREDIT BUREAU DATABASE (Keyed by ssn_last_4)
# ---------------------------------------------------------------------------
MOCK_EXPERIAN_DB: Dict[str, Dict[str, Any]] = {
    # Sarah: 745 FICO, $1,400/mo bureau debt, 0 delinquencies, Discharged old bankruptcy (Not Active!)
    "4401": {
        "bureau": "Experian",
        "fico_score": 745,
        "public_records": [],
        "tradelines": [
            {
                "tradeline_id": "TL_101",
                "creditor": "HONDA FINANCIAL SERVICES",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 450.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_102",
                "creditor": "WELLS FARGO HOME MTG",
                "account_type": "MORTGAGE",
                "monthly_payment": 950.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
        ],
    },
    # Marcus: 595 FICO (<620 knockout), $1,850/mo bureau debt, 3 delinquencies (>1 knockout)
    "8829": {
        "bureau": "Experian",
        "fico_score": 595,
        "public_records": [],
        "tradelines": [
            {
                "tradeline_id": "TL_201",
                "creditor": "ALLY AUTO FINANCE",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 650.00,
                "delinquencies_12m": {"30_days": 1, "60_days": 0, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_202",
                "creditor": "CAPITAL ONE REVOLVING",
                "account_type": "CREDIT_CARD",
                "monthly_payment": 400.00,
                "delinquencies_12m": {"30_days": 1, "60_days": 1, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_203",
                "creditor": "SOFI PERSONAL LOAN",
                "account_type": "UNSECURED_LOAN",
                "monthly_payment": 800.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
        ],
    },
    # David: 710 FICO, $1,600/mo bureau debt, 0 delinquencies, 0 active bankruptcies
    "9941": {
        "bureau": "Experian",
        "fico_score": 710,
        "public_records": [
            # Teaching nuance: A DISCHARGED bankruptcy from 2019 does NOT trigger Rule 2.3 (Active Bankruptcy)
            {"type": "CHAPTER_7_BANKRUPTCY", "status": "DISCHARGED", "filed_date": "2019-03-14"}
        ],
        "tradelines": [
            {
                "tradeline_id": "TL_301",
                "creditor": "JPMCB - CHASE AUTO",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 500.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_302",
                "creditor": "NAVIENT STUDENT LOAN",
                "account_type": "STUDENT_LOAN",
                "monthly_payment": 400.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_303",
                "creditor": "AMEX REVOLVING",
                "account_type": "CREDIT_CARD",
                "monthly_payment": 700.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
        ],
    },
}

# ---------------------------------------------------------------------------
# 3. MOCK PLAID 90-DAY BANK TRANSACTION LEDGERS (Keyed by ssn_last_4)
# ---------------------------------------------------------------------------
MOCK_PLAID_DB: Dict[str, Dict[str, Any]] = {
    # SARAH JENKINS: $7,600/mo verified payroll (4.9% variance vs $8k stated), 
    # matches Honda ($450) and Wells Fargo ($950), 0 hidden debt, 0 NSF, ~$6,500 avg EOM balance.
    # Post-Loan DTI = ($1400 + $498.21) / $7600 = 24.98% (Auto-Approve!)
    "4401": {
        "bank_name": "Wells Fargo Checking",
        "account_mask": "1104",
        "statement_period_months": 3,
        "eom_balances": [
            {"month": "2026-07", "ending_balance": 6200.00},
            {"month": "2026-08", "ending_balance": 6500.00},
            {"month": "2026-09", "ending_balance": 6800.00},
        ],
        "transactions": [
            # Month 1 (July)
            {"tx_id": "S01", "date": "2026-07-01", "description": "ACME HEALTHCARE DIR DEP PAYROLL", "amount": 3800.00},
            {"tx_id": "S02", "date": "2026-07-03", "description": "WELLS FARGO HOME MTG AUTOPAY", "amount": -950.00},
            {"tx_id": "S03", "date": "2026-07-05", "description": "STARBUCKS STORE 1042", "amount": -14.50},
            {"tx_id": "S04", "date": "2026-07-10", "description": "HONDA FIN SVC ACH PYMT", "amount": -450.00},
            {"tx_id": "S05", "date": "2026-07-15", "description": "ACME HEALTHCARE DIR DEP PAYROLL", "amount": 3800.00},
            # Month 2 (August)
            {"tx_id": "S06", "date": "2026-08-01", "description": "ACME HEALTHCARE DIR DEP PAYROLL", "amount": 3800.00},
            {"tx_id": "S07", "date": "2026-08-03", "description": "WELLS FARGO HOME MTG AUTOPAY", "amount": -950.00},
            {"tx_id": "S08", "date": "2026-08-10", "description": "HONDA FIN SVC ACH PYMT", "amount": -450.00},
            {"tx_id": "S09", "date": "2026-08-15", "description": "ACME HEALTHCARE DIR DEP PAYROLL", "amount": 3800.00},
            {"tx_id": "S10", "date": "2026-08-20", "description": "WHOLE FOODS MARKET", "amount": -82.30},
            # Month 3 (September)
            {"tx_id": "S11", "date": "2026-09-01", "description": "ACME HEALTHCARE DIR DEP PAYROLL", "amount": 3800.00},
            {"tx_id": "S12", "date": "2026-09-03", "description": "WELLS FARGO HOME MTG AUTOPAY", "amount": -950.00},
            {"tx_id": "S13", "date": "2026-09-10", "description": "HONDA FIN SVC ACH PYMT", "amount": -450.00},
            {"tx_id": "S14", "date": "2026-09-15", "description": "ACME HEALTHCARE DIR DEP PAYROLL", "amount": 3800.00},
        ],
    },

    # MARCUS VANCE: $4,400/mo verified payroll, 4 NSF fees (>2 knockout), 
    # plus $300/mo undisclosed Klarna/Payday debt. Post-Loan DTI = ($1850 + $300 + $664.29) / $4400 = 63.96% (Auto-Deny!)
    "8829": {
        "bank_name": "Bank of America Core Checking",
        "account_mask": "7732",
        "statement_period_months": 3,
        "eom_balances": [
            {"month": "2026-07", "ending_balance": 410.00},
            {"month": "2026-08", "ending_balance": 290.00},
            {"month": "2026-09", "ending_balance": 350.00},
        ],
        "transactions": [
            # Month 1 (July)
            {"tx_id": "M01", "date": "2026-07-01", "description": "METRO LOGISTICS PAYROLL ACH", "amount": 2200.00},
            {"tx_id": "M02", "date": "2026-07-05", "description": "ALLY AUTO FIN PYMT", "amount": -650.00},
            {"tx_id": "M03", "date": "2026-07-08", "description": "RETURNED ITEM UNPAID NSF FEE", "amount": -35.00},
            {"tx_id": "M04", "date": "2026-07-12", "description": "SPEEDYCASH PAYDAY LOAN INSTALLMENT", "amount": -300.00},
            {"tx_id": "M05", "date": "2026-07-15", "description": "METRO LOGISTICS PAYROLL ACH", "amount": 2200.00},
            {"tx_id": "M06", "date": "2026-07-20", "description": "SOFI LENDING AUTOPAY", "amount": -800.00},
            # Month 2 (August)
            {"tx_id": "M07", "date": "2026-08-01", "description": "METRO LOGISTICS PAYROLL ACH", "amount": 2200.00},
            {"tx_id": "M08", "date": "2026-08-05", "description": "ALLY AUTO FIN PYMT", "amount": -650.00},
            {"tx_id": "M09", "date": "2026-08-11", "description": "OVERDRAFT ITEM FEE CHARGE", "amount": -35.00},
            {"tx_id": "M10", "date": "2026-08-12", "description": "SPEEDYCASH PAYDAY LOAN INSTALLMENT", "amount": -300.00},
            {"tx_id": "M11", "date": "2026-08-15", "description": "METRO LOGISTICS PAYROLL ACH", "amount": 2200.00},
            {"tx_id": "M12", "date": "2026-08-19", "description": "OVERDRAFT ITEM FEE CHARGE", "amount": -35.00},
            {"tx_id": "M13", "date": "2026-08-25", "description": "CAP ONE CRCARD EPAY", "amount": -400.00},
            # Month 3 (September)
            {"tx_id": "M14", "date": "2026-09-01", "description": "METRO LOGISTICS PAYROLL ACH", "amount": 2200.00},
            {"tx_id": "M15", "date": "2026-09-12", "description": "SPEEDYCASH PAYDAY LOAN INSTALLMENT", "amount": -300.00},
            {"tx_id": "M16", "date": "2026-09-15", "description": "METRO LOGISTICS PAYROLL ACH", "amount": 2200.00},
            {"tx_id": "M17", "date": "2026-09-22", "description": "RETURNED ITEM UNPAID NSF FEE", "amount": -35.00},
        ],
    },

    # DAVID CHEN: $7,200/mo verified payroll (21.45% variance vs $9,166.67 stated gross),
    # has $1,500 savings transfer & $2,000 Zelle gift from Mom (must be excluded!),
    # matches Chase Auto ($500), Navient ($400), Amex ($700),
    # has $250/mo undisclosed Affirm BNPL debt,
    # has 1 NSF fee that was REVERSED/REFUNDED the next day (net 0 punitive NSF events!),
    # Post-Loan DTI = ($1600 + $250 + $1162.50) / $7200 = 41.84% -> ESCALATE (Rules 4.1, 4.2, 4.3)!
    "9941": {
        "bank_name": "JPMorgan Chase Total Checking",
        "account_mask": "4402",
        "statement_period_months": 3,
        "eom_balances": [
            {"month": "2026-07", "ending_balance": 11500.00},
            {"month": "2026-08", "ending_balance": 12500.00},
            {"month": "2026-09", "ending_balance": 12000.00},
        ],
        "transactions": [
            # Month 1 (July)
            {"tx_id": "D01", "date": "2026-07-01", "description": "INITECH CORP DIR DEP 9941", "amount": 3600.00},
            {"tx_id": "D02", "date": "2026-07-05", "description": "CHASE AUTO FIN PYMT", "amount": -500.00},
            {"tx_id": "D03", "date": "2026-07-08", "description": "BLUE BOTTLE COFFEE", "amount": -6.75},
            {"tx_id": "D04", "date": "2026-07-10", "description": "NAVIENT ED SERV ACH", "amount": -400.00},
            {"tx_id": "D05", "date": "2026-07-15", "description": "INITECH CORP DIR DEP 9941", "amount": 3600.00},
            {"tx_id": "D06", "date": "2026-07-18", "description": "AMEX EPAYMENT ACH", "amount": -700.00},
            {"tx_id": "D07", "date": "2026-07-22", "description": "AFFIRM *PYMT 8821", "amount": -250.00},
            # Month 2 (August)
            {"tx_id": "D08", "date": "2026-08-01", "description": "INITECH CORP DIR DEP 9941", "amount": 3600.00},
            {"tx_id": "D09", "date": "2026-08-05", "description": "CHASE AUTO FIN PYMT", "amount": -500.00},
            {"tx_id": "D10", "date": "2026-08-10", "description": "NAVIENT ED SERV ACH", "amount": -400.00},
            {"tx_id": "D11", "date": "2026-08-15", "description": "INITECH CORP DIR DEP 9941", "amount": 3600.00},
            {"tx_id": "D12", "date": "2026-08-18", "description": "ONLINE XFER FROM CHASE SAV 8810", "amount": 1500.00},
            {"tx_id": "D13", "date": "2026-08-19", "description": "AMEX EPAYMENT ACH", "amount": -700.00},
            {"tx_id": "D14", "date": "2026-08-22", "description": "AFFIRM *PYMT 8821", "amount": -250.00},
            {"tx_id": "D15", "date": "2026-08-25", "description": "MONTHLY SERVICE MAINTENANCE FEE", "amount": -12.00},
            # Month 3 (September)
            {"tx_id": "D16", "date": "2026-09-01", "description": "INITECH CORP DIR DEP 9941", "amount": 3600.00},
            {"tx_id": "D17", "date": "2026-09-05", "description": "CHASE AUTO FIN PYMT", "amount": -500.00},
            {"tx_id": "D18", "date": "2026-09-10", "description": "ZELLE FROM MOM MEDICAL HELP", "amount": 2000.00},
            {"tx_id": "D19", "date": "2026-09-11", "description": "NAVIENT ED SERV ACH", "amount": -400.00},
            {"tx_id": "D20", "date": "2026-09-15", "description": "INITECH CORP DIR DEP 9941", "amount": 3600.00},
            {"tx_id": "D21", "date": "2026-09-18", "description": "AMEX EPAYMENT ACH", "amount": -700.00},
            {"tx_id": "D22", "date": "2026-09-22", "description": "AFFIRM *PYMT 8821", "amount": -250.00},
        ],
    },
}


# ---------------------------------------------------------------------------
# 4. SIMULATED API FUNCTIONS & DETERMINISTIC PRE-FILTER
# ---------------------------------------------------------------------------
def fetch_experian_credit_report(ssn_last_4: str) -> Dict[str, Any]:
    """Simulates calling the Experian Credit Bureau API."""
    if ssn_last_4 not in MOCK_EXPERIAN_DB:
        raise ValueError(f"No Experian record found for SSN ending in {ssn_last_4}")
    return MOCK_EXPERIAN_DB[ssn_last_4]


def fetch_plaid_bank_statement(ssn_last_4: str) -> Dict[str, Any]:
    """Simulates calling the Plaid Transactions & Balance API."""
    if ssn_last_4 not in MOCK_PLAID_DB:
        raise ValueError(f"No Plaid bank record found for SSN ending in {ssn_last_4}")
    return MOCK_PLAID_DB[ssn_last_4]


def prefilter_underwriting_transactions(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deterministic Pre-Filter before LLM Batch Call:
    Keeps all deposits (amount > 0), all large outflows (abs(amount) >= 100),
    and any small outflows containing fee/NSF/loan/affirm/klarna keywords.
    Strips out everyday small retail noise (coffee, groceries under $100).
    """
    keywords = ("NSF", "OVERDRAFT", "FEE", "RETURNED", "AFFIRM", "KLARNA", "LOAN", "PYMT", "ACH")
    filtered = []
    for tx in transactions:
        amt = tx["amount"]
        desc_upper = tx["description"].upper()
        if amt > 0 or abs(amt) >= 100.0 or any(k in desc_upper for k in keywords):
            filtered.append(tx)
    return filtered