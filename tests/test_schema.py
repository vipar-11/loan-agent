"""
test_schema.py
Unit tests for Pydantic Structured Output schemas and LangGraph state.
"""
import pytest
from pydantic import ValidationError
from loan_agent.schema import (
    CategorizedTransaction,
    BankStatementAnalysisOutput,
    UnderwritingOutput,
)
from loan_agent.state import LoanApplicationState


class TestCategorizedTransactionSchema:
    def test_valid_categorized_transaction(self):
        tx = CategorizedTransaction(
            tx_id="D01",
            date="2026-07-01",
            description="INITECH CORP PAYROLL",
            amount=3600.0,
            category="VERIFIED_PAYROLL_INCOME",
            matched_tradeline_id=None,
            reasoning="Regular recurring wage deposit from employer.",
        )
        assert tx.tx_id == "D01"
        assert tx.category == "VERIFIED_PAYROLL_INCOME"
        assert tx.matched_tradeline_id is None

    def test_matched_bureau_debt_with_tradeline_id(self):
        tx = CategorizedTransaction(
            tx_id="D02",
            date="2026-07-05",
            description="CHASE AUTO FIN PYMT",
            amount=-500.0,
            category="MATCHED_BUREAU_DEBT",
            matched_tradeline_id="TL_301",
            reasoning="Matches active auto loan tradeline.",
        )
        assert tx.matched_tradeline_id == "TL_301"

    def test_invalid_category_raises_validation_error(self):
        with pytest.raises(ValidationError):
            CategorizedTransaction(
                tx_id="D03",
                date="2026-07-05",
                description="TEST",
                amount=-10.0,
                category="NON_EXISTENT_CATEGORY",  # type: ignore
                reasoning="Invalid category.",
            )


class TestBankStatementAnalysisOutputSchema:
    def test_valid_analysis_output(self):
        output = BankStatementAnalysisOutput(
            categorized_transactions=[
                CategorizedTransaction(
                    tx_id="T01",
                    date="2026-07-01",
                    description="PAYROLL",
                    amount=4000.0,
                    category="VERIFIED_PAYROLL_INCOME",
                    reasoning="Payroll deposit",
                )
            ],
            analyst_summary="Clean income detected with no NSF fees.",
        )
        assert len(output.categorized_transactions) == 1
        assert "Clean income" in output.analyst_summary


class TestUnderwritingOutputSchema:
    def test_valid_approve_decision(self):
        decision = UnderwritingOutput(
            underwriting_decision="APPROVE",
            confidence_score=0.95,
            policy_citations=["Rule 3.1", "Rule 3.2", "Rule 3.3"],
            adverse_action_reasons=[],
            underwriter_memo="Applicant satisfies all prime auto-approval criteria.",
        )
        assert decision.underwriting_decision == "APPROVE"
        assert decision.confidence_score == 0.95
        assert len(decision.adverse_action_reasons) == 0

    def test_valid_deny_decision_with_adverse_action(self):
        decision = UnderwritingOutput(
            underwriting_decision="DENY",
            confidence_score=0.99,
            policy_citations=["Rule 2.1", "Rule 2.4"],
            adverse_action_reasons=[
                "Credit score falls below minimum 620 requirement.",
                "Excessive punitive overdraft events in the past 90 days.",
            ],
            underwriter_memo="Application rejected per mandatory knockout policy.",
        )
        assert decision.underwriting_decision == "DENY"
        assert len(decision.adverse_action_reasons) == 2

    def test_invalid_decision_string_rejected(self):
        with pytest.raises(ValidationError):
            UnderwritingOutput(
                underwriting_decision="MAYBE",  # type: ignore
                confidence_score=0.5,
                policy_citations=[],
                underwriter_memo="Undecided",
            )


class TestLoanApplicationState:
    def test_state_typeddict_fields(self):
        state: LoanApplicationState = {
            "application_id": "APP-2026-101",
            "applicant_name": "Sarah Jenkins",
            "ssn_last_4": "4401",
            "stated_annual_income": 96000.0,
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Home Improvement",
            "fcra_consent": True,
        }
        assert state["application_id"] == "APP-2026-101"
        assert state["fcra_consent"] is True
