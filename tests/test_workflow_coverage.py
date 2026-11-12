"""
Targeted tests to maximize test coverage across loan_agent/graph_workflow.py.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
import os
import pytest

from loan_agent.graph_workflow import (
    load_policy_document,
    get_llm,
    bank_statement_analyzer_agent,
    calculate_financial_ratios_node,
    underwriter_agent_node,
    finalize_application_node,
)
from loan_agent.schema import BankStatementAnalysisOutput, CategorizedTransaction, UnderwritingOutput
from loan_agent.state import LoanApplicationState


class TestWorkflowCoverage:
    def test_load_policy_document_not_found(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: False)
        with pytest.raises(FileNotFoundError, match="not found"):
            load_policy_document()

    def test_get_llm_valid_and_missing_api_key(self, monkeypatch):
        # Missing API key raises ValueError
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key is missing"):
            get_llm(api_key="")

        # Valid explicit key returns client
        client = get_llm(api_key="test-dummy-key")
        assert client is not None
        assert client.openai_api_key.get_secret_value() == "test-dummy-key"

    def test_bank_statement_analyzer_mock_categories(self):
        # Exercise all mock categorization branches
        state: LoanApplicationState = {
            "credit_report": {
                "tradelines": [
                    {"tradeline_id": "TL_99", "creditor_name": "HONDA FINANCIAL"}
                ]
            },
            "bank_statement_raw": {"transactions": []},
            "prefiltered_transactions": [
                {"date": "2026-07-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4500.0},
                {"date": "2026-07-02", "description": "HONDA FINANCIAL ACH", "amount": -450.0},
                {"date": "2026-07-03", "description": "OVERDRAFT ITEM FEE", "amount": -35.0},
                {"date": "2026-07-04", "description": "KLARNA INSTALLMENT", "amount": -65.0},
                {"date": "2026-07-05", "description": "MONTHLY SERVICE FEE", "amount": -12.0},
                {"date": "2026-07-06", "description": "XFER FROM SAVINGS", "amount": 1000.0},
                {"date": "2026-07-07", "description": "ZELLE FROM SISTER", "amount": 200.0},
                {"date": "2026-07-08", "description": "GROCERY STORE", "amount": -85.0},
            ],
            "execution_log": [],
        }

        res = bank_statement_analyzer_agent(state)
        assert len(res["categorized_transactions"]) == 8
        categories = [tx["category"] for tx in res["categorized_transactions"]]
        assert "VERIFIED_PAYROLL_INCOME" in categories
        assert "MATCHED_BUREAU_DEBT" in categories
        assert "NSF_OVERDRAFT_FEE" in categories
        assert "UNDISCLOSED_DEBT_PAYMENT" in categories
        assert "BANK_MAINTENANCE_FEE_IGNORE" in categories
        assert "INTERNAL_TRANSFER_IGNORE" in categories
        assert "ONE_OFF_GIFT_OR_TRANSFER_IGNORE" in categories
        assert "OTHER_EXPENSE_IGNORE" in categories

    def test_bank_statement_analyzer_live_llm_path_and_exception(self, monkeypatch):
        # Test real invocation branch using mocked ChatOpenAI
        mock_output = BankStatementAnalysisOutput(
            categorized_transactions=[
                CategorizedTransaction(
                    tx_id="TX_1",
                    date="2026-08-01",
                    description="SALARY",
                    amount=5000.0,
                    category="VERIFIED_PAYROLL_INCOME",
                    matched_tradeline_id=None,
                    reasoning="Verified direct deposit payroll",
                )
            ],
            analyst_summary="Mocked live LLM analysis complete.",
        )

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = mock_output
        mock_llm.with_structured_output.return_value = mock_structured

        monkeypatch.setattr("loan_agent.graph_workflow.get_llm", lambda: mock_llm)

        state: LoanApplicationState = {
            "applicant_name": "Test User",
            "credit_report": {"tradelines": []},
            "bank_statement_raw": {"transactions": []},
            "prefiltered_transactions": [{"date": "2026-08-01", "description": "SALARY", "amount": 5000.0}],
            "execution_log": [],
        }

        # Run invocation
        res = bank_statement_analyzer_agent(state)
        assert res["bank_analyst_summary"] == "Mocked live LLM analysis complete."
        assert len(res["categorized_transactions"]) == 1

        # Test exception handling in bank analyzer
        mock_structured.invoke.side_effect = RuntimeError("Simulated API failure")
        err_res = bank_statement_analyzer_agent(state)
        assert "error_message" in err_res
        assert "Simulated API failure" in err_res["error_message"]

    def test_calculate_financial_ratios_exception(self):
        state: LoanApplicationState = {
            "execution_log": [],
            "credit_report": {"fico_score": "not_an_int"},
            "categorized_transactions": [{"category": "VERIFIED_PAYROLL_INCOME", "amount": 1000.0}],
            "bank_statement_raw": {},
        }
        res = calculate_financial_ratios_node(state)
        assert "error_message" in res
        assert "invalid literal for int()" in res["error_message"]

    def test_underwriter_agent_upstream_error_and_exception(self):
        # Test upstream error branch
        state_err: LoanApplicationState = {
            "error_message": "Prior node failure",
            "execution_log": [],
        }
        res_err = underwriter_agent_node(state_err)
        assert res_err["underwriting_decision"] == "ESCALATE"

    def test_underwriter_agent_live_lcel_path_and_exception(self, monkeypatch):
        # Mock structured LLM and chain invocation for live LCEL branch
        mock_decision = UnderwritingOutput(
            underwriting_decision="APPROVE",
            confidence_score=0.92,
            policy_citations=["POLICY_GUIDELINE_A"],
            adverse_action_reasons=[],
            underwriter_memo="Compliant application approved.",
        )

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        monkeypatch.setattr("loan_agent.graph_workflow.get_llm", lambda: mock_llm)
        monkeypatch.setattr(
            "loan_agent.graph_workflow.load_policy_document",
            lambda: "Sample Policy Document",
        )

        state: LoanApplicationState = {
            "application_id": "APP-TEST-LCEL",
            "applicant_name": "Test Persona",
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Other",
            "stated_annual_income": 90000.0,
            "stated_monthly_income": 7500.0,
            "fcra_consent": True,
            "fico_score": 750,
            "has_active_bankruptcy": False,
            "total_delinquencies_12m": 0,
            "nsf_count_90d": 0,
            "verified_monthly_income": 6500.0,
            "income_variance_pct": 13.3,
            "bureau_monthly_debt": 500.0,
            "undisclosed_monthly_debt": 0.0,
            "estimated_new_Loan_payment": 498.0,
            "post_loan_dti": 15.3,
            "avg_ending_balance": 8000.0,
            "payment_cushion_ratio": 16.0,
            "bank_analyst_summary": "Clean profile",
            "execution_log": [],
        }

        # Mock the Runnable pipe: prompt | structured_llm
        with patch("loan_agent.graph_workflow.ChatPromptTemplate") as mock_prompt_cls:
            mock_prompt = mock_prompt_cls.from_messages.return_value
            mock_chain = MagicMock()
            mock_chain.invoke.return_value = mock_decision
            mock_prompt.__or__.return_value = mock_chain

            # Run LCEL invocation
            res = underwriter_agent_node(state)
            assert res["underwriting_decision"] == "APPROVE"
            assert res["confidence_score"] == 0.92

            # Test exception branch in underwriter
            mock_chain.invoke.side_effect = RuntimeError("OpenAI rate limit")
            res_exc = underwriter_agent_node(state)
            assert res_exc["underwriting_decision"] == "ESCALATE"
            assert "OpenAI rate limit" in res_exc["error_message"]

    def test_finalize_application_node_variations(self):
        # Decision is neither APPROVE nor DENY
        state_custom: LoanApplicationState = {
            "underwriting_decision": "CUSTOM_STATUS",
            "execution_log": [],
        }
        res = finalize_application_node(state_custom)
        assert res["final_status"] == "CUSTOM_STATUS"

    def test_bank_statement_analyzer_upstream_error_skip(self):
        state_err: LoanApplicationState = {
            "error_message": "Bureau API offline",
            "execution_log": [],
        }
        res = bank_statement_analyzer_agent(state_err)
        assert "execution_log" in res
        assert any("Skipping due to upstream error" in log for log in res["execution_log"])

    def test_calculate_financial_ratios_zero_incomes(self):
        state: LoanApplicationState = {
            "execution_log": [],
            "credit_report": {
                "fico_score": 700,
                "public_records": [],
                "tradelines": [],
            },
            "bank_statement_raw": {
                "statement_period_months": 3,
                "eom_balances": [],
            },
            "categorized_transactions": [
                {"category": "OTHER_EXPENSE_IGNORE", "amount": -100.0}
            ],
            "stated_annual_income": 0.0,
            "loan_amount_requested": 10000.0,
        }
        res = calculate_financial_ratios_node(state)
        assert res["income_variance_pct"] == 100.0
        assert res["post_loan_dti"] == 999.0

