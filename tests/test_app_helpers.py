"""
test_app_helpers.py
Unit tests for data formatting and classification helpers in the application layer.
"""
import pytest
import pandas as pd
from app import (
    format_currency,
    calculate_monthly_payment,
    build_tradelines_dataframe,
    auto_classify_transaction,
    get_policy_markdown,
)


class TestAppFormattingHelpers:
    def test_format_currency_valid(self):
        assert format_currency(15000) == "$15,000.00"
        assert format_currency(498.21) == "$498.21"
        assert format_currency(0) == "$0.00"

    def test_format_currency_none_or_empty(self):
        assert format_currency(None) == "—"
        assert format_currency("") == "—"

    def test_calculate_monthly_payment(self):
        assert calculate_monthly_payment(15000.0) == 498.21
        assert calculate_monthly_payment(0.0) is None
        assert calculate_monthly_payment(None) is None

    def test_build_tradelines_dataframe(self, clean_credit_report):
        df = build_tradelines_dataframe(clean_credit_report)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert "Creditor Name" in df.columns
        assert "Monthly Obligation" in df.columns
        assert "Standing" in df.columns
        assert "Tradeline ID" not in df.columns  # Clean audit view without technical ID

    def test_build_tradelines_dataframe_empty(self):
        df = build_tradelines_dataframe({})
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


class TestAutoClassifyTransaction:
    def test_classify_payroll_inflow(self):
        tx = {"amount": 3800.0, "description": "ACME CORP DIR DEP PAYROLL"}
        assert auto_classify_transaction(tx) == "Verified Payroll Income"

    def test_classify_transfer_inflow(self):
        tx = {"amount": 1500.0, "description": "ONLINE XFER FROM CHASE SAV"}
        assert auto_classify_transaction(tx) == "Internal Account Transfer (Excluded)"

    def test_classify_gift_inflow(self):
        tx = {"amount": 500.0, "description": "ZELLE FROM MOM"}
        assert auto_classify_transaction(tx) == "One-Off Gift / Transfer (Excluded)"

    def test_classify_nsf_fee_outflow(self):
        tx = {"amount": -35.0, "description": "RETURNED ITEM UNPAID NSF FEE"}
        assert auto_classify_transaction(tx) == "NSF / Overdraft Fee"

    def test_classify_bnpl_outflow(self):
        tx = {"amount": -250.0, "description": "AFFIRM *PYMT 8821"}
        assert auto_classify_transaction(tx) == "Undisclosed Debt / BNPL"

    def test_classify_bureau_debt_outflow(self):
        tx = {"amount": -950.0, "description": "WELLS FARGO HOME MTG AUTOPAY"}
        assert auto_classify_transaction(tx) == "Known Bureau Debt Payment"

    def test_classify_maintenance_fee_outflow(self):
        tx = {"amount": -12.0, "description": "MONTHLY SERVICE MAINTENANCE FEE"}
        assert auto_classify_transaction(tx) == "Bank Maintenance Fee (Excluded)"

    def test_classify_standard_living_expense(self):
        tx = {"amount": -82.30, "description": "WHOLE FOODS MARKET"}
        assert auto_classify_transaction(tx) == "Other Living Expense / Standard"


class TestPolicyMarkdownLoader:
    def test_get_policy_markdown_finds_document(self):
        text = get_policy_markdown()
        assert "APEX FINANCIAL" in text
        assert "Section 1: Mandatory Compliance & Consent" in text
