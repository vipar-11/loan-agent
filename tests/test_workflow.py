"""
test_workflow.py
Unit tests for LangGraph 6-node underwriting workflow, routing, and error handling.
"""
import pytest
from loan_agent.state import LoanApplicationState
from loan_agent.graph_workflow import (
    load_policy_document,
    validate_consent_node,
    route_after_consent,
    pull_credit_bureau_node,
    pull_raw_bank_transactions_node,
    calculate_financial_ratios_node,
    route_after_underwriter,
    human_review_node,
    finalize_application_node,
    build_underwriting_graph,
)


class TestPolicyDocument:
    def test_load_policy_document(self):
        doc = load_policy_document()
        assert len(doc) > 0
        assert "APEX FINANCIAL" in doc
        assert "Section 1: Mandatory Compliance & Consent" in doc
        assert "Section 2: Hard Knockout Criteria" in doc
        assert "Section 3: Standard Auto-Approval Criteria" in doc
        assert "Section 4: Manual Underwriting Escalation" in doc


class TestConsentGatekeeperNode:
    def test_validate_consent_node_with_consent(self):
        state: LoanApplicationState = {
            "application_id": "APP-001",
            "fcra_consent": True,
            "execution_log": [],
        }
        res = validate_consent_node(state)
        assert "execution_log" in res
        assert any("consent verified" in log.lower() for log in res["execution_log"])
        assert "underwriting_decision" not in res

    def test_validate_consent_node_without_consent(self):
        state: LoanApplicationState = {
            "application_id": "APP-002",
            "fcra_consent": False,
            "execution_log": [],
        }
        res = validate_consent_node(state)
        assert res["underwriting_decision"] == "REJECT_NO_CONSENT"
        assert res["final_status"] == "REJECTED_NO_CONSENT"
        assert len(res["adverse_action_reasons"]) > 0

    def test_route_after_consent(self):
        # Consent granted -> fan-out parallel nodes
        assert route_after_consent({"fcra_consent": True}) == [
            "pull_credit_bureau_node",
            "pull_raw_bank_transactions_node",
        ]
        # Consent denied -> route straight to finalize
        assert route_after_consent({"fcra_consent": False}) == "finalize_application_node"


class TestExternalDataHarvesters:
    def test_pull_credit_bureau_node_success(self):
        state: LoanApplicationState = {"ssn_last_4": "4401"}
        res = pull_credit_bureau_node(state)
        assert "credit_report" in res
        assert res["credit_report"]["fico_score"] == 745

    def test_pull_credit_bureau_node_failure(self):
        state: LoanApplicationState = {"ssn_last_4": "0000"}
        res = pull_credit_bureau_node(state)
        assert res["credit_report"] is None
        assert "error_message" in res
        assert "Credit Bureau API Error" in res["error_message"]

    def test_pull_raw_bank_transactions_node_success(self):
        state: LoanApplicationState = {"ssn_last_4": "4401"}
        res = pull_raw_bank_transactions_node(state)
        assert "bank_statement_raw" in res
        assert "prefiltered_transactions" in res
        assert len(res["prefiltered_transactions"]) > 0

    def test_pull_raw_bank_transactions_node_failure(self):
        state: LoanApplicationState = {"ssn_last_4": "0000"}
        res = pull_raw_bank_transactions_node(state)
        assert res["bank_statement_raw"] is None
        assert res["prefiltered_transactions"] == []
        assert "error_message" in res
        assert "Bank Transactions API Error" in res["error_message"]


class TestRatioCalculationNode:
    def test_calculate_financial_ratios_node_skips_on_upstream_error(self):
        state: LoanApplicationState = {
            "error_message": "Pre-existing failure",
            "execution_log": [],
        }
        res = calculate_financial_ratios_node(state)
        assert "execution_log" in res
        assert any("skipping math" in log.lower() for log in res["execution_log"])

    def test_calculate_financial_ratios_node_computes_correctly(self, clean_credit_report, clean_bank_statement, categorized_transactions_clean):
        state: LoanApplicationState = {
            "credit_report": clean_credit_report,
            "bank_statement_raw": clean_bank_statement,
            "categorized_transactions": categorized_transactions_clean,
            "stated_annual_income": 96000.0,
            "loan_amount_requested": 15000.0,
            "execution_log": [],
        }
        res = calculate_financial_ratios_node(state)
        assert res["fico_score"] == 750
        assert res["has_active_bankruptcy"] is False
        assert res["verified_monthly_income"] == 8000.00
        assert res["bureau_monthly_debt"] == 1400.00
        assert res["estimated_new_Loan_payment"] == 498.21


class TestRoutingAndFinalization:
    def test_route_after_underwriter(self):
        assert route_after_underwriter({"underwriting_decision": "ESCALATE"}) == "NEEDS_HUMAN_REVIEW"
        assert route_after_underwriter({"underwriting_decision": "APPROVE"}) == "READY_TO_FINALIZE"
        assert route_after_underwriter({"underwriting_decision": "DENY"}) == "READY_TO_FINALIZE"

    def test_human_review_node(self):
        state: LoanApplicationState = {
            "human_reviewer_decision": "APPROVE",
            "human_reviewer_notes": "Senior underwriter approved with compensating factor.",
            "execution_log": [],
        }
        res = human_review_node(state)
        assert res["final_status"] == "HUMAN_APPROVED"

    def test_finalize_application_node(self):
        assert finalize_application_node({"underwriting_decision": "APPROVE"})["final_status"] == "AUTO_APPROVED"
        assert finalize_application_node({"underwriting_decision": "DENY"})["final_status"] == "AUTO_DENIED"
        assert finalize_application_node({"final_status": "CUSTOM_STATUS"})["final_status"] == "CUSTOM_STATUS"


class TestGraphCompilationAndExecution:
    def test_build_underwriting_graph(self):
        graph = build_underwriting_graph()
        assert graph is not None

    def test_end_to_end_rejection_no_consent(self):
        """Graph executes deterministically to completion when consent is False without needing LLM."""
        graph = build_underwriting_graph()
        initial_state: LoanApplicationState = {
            "application_id": "APP-TEST-999",
            "applicant_name": "Test Borrower",
            "ssn_last_4": "4401",
            "stated_annual_income": 80000.0,
            "loan_amount_requested": 10000.0,
            "loan_purpose": "Home Improvement",
            "fcra_consent": False,
        }
        config = {"configurable": {"thread_id": "test-thread-no-consent"}}
        result = graph.invoke(initial_state, config=config)

        assert result["underwriting_decision"] == "REJECT_NO_CONSENT"
        assert result["final_status"] == "REJECTED_NO_CONSENT"
