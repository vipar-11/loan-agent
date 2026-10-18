"""
conftest.py
Shared pytest fixtures and test data for loan_agent test suite.
"""
import pytest
from typing import Dict, Any, List


@pytest.fixture
def clean_credit_report() -> Dict[str, Any]:
    """Experian credit report with prime score, 0 delinquencies, 0 active bankruptcies."""
    return {
        "bureau": "Experian",
        "fico_score": 750,
        "public_records": [
            {"type": "CHAPTER_7_BANKRUPTCY", "status": "DISCHARGED", "filed_date": "2018-05-10"}
        ],
        "tradelines": [
            {
                "tradeline_id": "TL_001",
                "creditor": "HONDA FINANCIAL",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 450.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_002",
                "creditor": "WELLS FARGO MORTGAGE",
                "account_type": "MORTGAGE",
                "monthly_payment": 950.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
        ],
    }


@pytest.fixture
def subprime_credit_report() -> Dict[str, Any]:
    """Experian credit report with subprime score, active bankruptcy, and multiple delinquencies."""
    return {
        "bureau": "Experian",
        "fico_score": 580,
        "public_records": [
            {"type": "CHAPTER_13_BANKRUPTCY", "status": "ACTIVE", "filed_date": "2025-01-15"}
        ],
        "tradelines": [
            {
                "tradeline_id": "TL_101",
                "creditor": "CAPITAL ONE",
                "account_type": "CREDIT_CARD",
                "monthly_payment": 250.00,
                "delinquencies_12m": {"30_days": 2, "60_days": 1, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_102",
                "creditor": "SANTANDER CONSUMER",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 550.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 1},
            },
        ],
    }


@pytest.fixture
def clean_bank_statement() -> Dict[str, Any]:
    """Plaid 90-day bank ledger with consistent payroll deposits and strong liquidity."""
    return {
        "bank_name": "Chase Checking",
        "account_mask": "1234",
        "statement_period_months": 3,
        "eom_balances": [
            {"month": "2026-07", "ending_balance": 8500.00},
            {"month": "2026-08", "ending_balance": 9200.00},
            {"month": "2026-09", "ending_balance": 9000.00},
        ],
        "transactions": [
            {"tx_id": "T01", "date": "2026-07-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T02", "date": "2026-07-05", "description": "HONDA FINANCIAL ACH", "amount": -450.00},
            {"tx_id": "T03", "date": "2026-07-15", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T04", "date": "2026-08-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T05", "date": "2026-08-15", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T06", "date": "2026-09-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T07", "date": "2026-09-15", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
        ],
    }


@pytest.fixture
def categorized_transactions_clean() -> List[Dict[str, Any]]:
    """Sample transactions categorized into verified income and debt."""
    return [
        {"tx_id": "T01", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T02", "category": "MATCHED_BUREAU_DEBT", "amount": -450.00},
        {"tx_id": "T03", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T04", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T05", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T06", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T07", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
    ]


@pytest.fixture
def categorized_transactions_distressed() -> List[Dict[str, Any]]:
    """Sample transactions with NSF fees and undisclosed BNPL debt."""
    return [
        {"tx_id": "M01", "category": "VERIFIED_PAYROLL_INCOME", "amount": 2500.00},
        {"tx_id": "M02", "category": "VERIFIED_PAYROLL_INCOME", "amount": 2500.00},
        {"tx_id": "M03", "category": "NSF_OVERDRAFT_FEE", "amount": -35.00},
        {"tx_id": "M04", "category": "NSF_OVERDRAFT_FEE", "amount": -35.00},
        {"tx_id": "M05", "category": "NSF_OVERDRAFT_FEE", "amount": -35.00},
        {"tx_id": "M06", "category": "UNDISCLOSED_DEBT_PAYMENT", "amount": -450.00},
        {"tx_id": "M07", "category": "ONE_OFF_GIFT_OR_TRANSFER_IGNORE", "amount": 500.00},
    ]
