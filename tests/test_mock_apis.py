"""
test_mock_apis.py
Unit tests for Experian and Plaid mock data providers and noise filtering.
"""
import pytest
from loan_agent.mock_apis import (
    APPLICANT_PROFILES,
    fetch_experian_credit_report,
    fetch_plaid_bank_statement,
    prefilter_underwriting_transactions,
)


class TestApplicantProfiles:
    def test_applicant_profiles_contain_required_keys(self):
        assert "SARAH_JENKINS" in APPLICANT_PROFILES
        assert "MARCUS_VANCE" in APPLICANT_PROFILES
        assert "DAVID_CHEN" in APPLICANT_PROFILES

        required_keys = {
            "application_id",
            "applicant_name",
            "ssn_last_4",
            "stated_annual_income",
            "loan_amount_requested",
            "loan_purpose",
            "persona_label",
        }
        for key, profile in APPLICANT_PROFILES.items():
            assert required_keys.issubset(profile.keys()), f"Missing keys in {key}"
            assert profile["loan_amount_requested"] > 0
            assert profile["stated_annual_income"] > 0


class TestExperianCreditReportAPI:
    @pytest.mark.parametrize("ssn,expected_fico", [
        ("4401", 745),
        ("8829", 595),
        ("9941", 710),
    ])
    def test_fetch_experian_credit_report_valid(self, ssn, expected_fico):
        report = fetch_experian_credit_report(ssn)
        assert report["bureau"] == "Experian"
        assert report["fico_score"] == expected_fico
        assert "tradelines" in report
        assert isinstance(report["tradelines"], list)

    def test_fetch_experian_credit_report_invalid(self):
        with pytest.raises(ValueError, match="No Experian record found"):
            fetch_experian_credit_report("0000")


class TestPlaidBankStatementAPI:
    @pytest.mark.parametrize("ssn,expected_account", [
        ("4401", "1104"),
        ("8829", "7732"),
        ("9941", "4402"),
    ])
    def test_fetch_plaid_bank_statement_valid(self, ssn, expected_account):
        statement = fetch_plaid_bank_statement(ssn)
        assert statement["account_mask"] == expected_account
        assert len(statement["transactions"]) > 0
        assert len(statement["eom_balances"]) == 3

    def test_fetch_plaid_bank_statement_invalid(self):
        with pytest.raises(ValueError, match="No Plaid bank record found"):
            fetch_plaid_bank_statement("9999")


class TestPrefilterTransactions:
    def test_prefilter_retains_positive_deposits(self):
        txs = [
            {"amount": 50.0, "description": "VENMO CASH IN"},
            {"amount": 10.0, "description": "SMALL DEPOSIT"},
        ]
        result = prefilter_underwriting_transactions(txs)
        assert len(result) == 2

    def test_prefilter_retains_large_outflows(self):
        txs = [
            {"amount": -150.0, "description": "BEST BUY STORE"},
            {"amount": -500.0, "description": "RENT PAYMENT"},
        ]
        result = prefilter_underwriting_transactions(txs)
        assert len(result) == 2

    def test_prefilter_retains_keyword_matches_regardless_of_size(self):
        txs = [
            {"amount": -35.0, "description": "RETURNED ITEM NSF FEE"},
            {"amount": -12.0, "description": "MONTHLY MAINTENANCE SERVICE FEE"},
            {"amount": -25.0, "description": "KLARNA INSTALLMENT 1 OF 4"},
            {"amount": -45.0, "description": "AFFIRM PAYMENT"},
        ]
        result = prefilter_underwriting_transactions(txs)
        assert len(result) == 4

    def test_prefilter_strips_small_everyday_retail_noise(self):
        txs = [
            {"amount": -4.50, "description": "STARBUCKS STORE 1042"},
            {"amount": -12.99, "description": "NETFLIX SUBSCRIPTION"},
            {"amount": -45.00, "description": "SHELL GAS STATION"},
        ]
        result = prefilter_underwriting_transactions(txs)
        assert len(result) == 0
