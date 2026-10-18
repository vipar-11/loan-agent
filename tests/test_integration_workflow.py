"""
test_integration_workflow.py
Live integration test suite running the 6-node LangGraph agentic workflow
with real LLM reasoning across the three pre-configured applicant personas:
1. Sarah Jenkins: Auto-Approval (Prime Tier)
2. Marcus Vance: Hard Knockout Auto-Denial (Subprime Knockouts)
3. David Chen: Manual Escalation with Human-in-the-Loop Interruption and Resumption
"""
import os
import pytest
from dotenv import load_dotenv

from loan_agent.graph_workflow import build_underwriting_graph
from loan_agent.mock_apis import APPLICANT_PROFILES
from loan_agent.state import LoanApplicationState

# Ensure environment variables (.env) are loaded
load_dotenv()

# Skip integration tests if no API key is available in the environment
HAS_API_KEY = bool(
    os.environ.get("LLM_API_KEY")
    or os.environ.get("GROQ_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
)
skip_if_no_key = pytest.mark.skipif(
    not HAS_API_KEY,
    reason="Live LLM API key not configured (requires GROQ_API_KEY or OPENAI_API_KEY).",
)


@pytest.mark.integration
@skip_if_no_key
class TestLiveWorkflowPersonas:
    def test_sarah_jenkins_auto_approval(self):
        """
        Persona 1 (Sarah Jenkins):
        - FICO 745, DTI ~24.98%, Discharged bankruptcy from 2018 (not active), 0 NSF fees, 0 delinquencies.
        - Expected: LLM policy underwriter issues APPROVE per Section 3 rules.
        - Graph reaches finalize_application_node with AUTO_APPROVED final status.
        """
        profile = APPLICANT_PROFILES["SARAH_JENKINS"]
        initial_state: LoanApplicationState = {
            "application_id": profile["application_id"],
            "applicant_name": profile["applicant_name"],
            "ssn_last_4": profile["ssn_last_4"],
            "stated_annual_income": float(profile["stated_annual_income"]),
            "loan_amount_requested": float(profile["loan_amount_requested"]),
            "loan_purpose": profile["loan_purpose"],
            "fcra_consent": True,
        }

        graph = build_underwriting_graph()
        config = {"configurable": {"thread_id": "test-integration-sarah"}}

        result = graph.invoke(initial_state, config=config)

        # 1. Underwriter Decision & Final Status
        assert result.get("underwriting_decision") == "APPROVE"
        assert result.get("final_status") == "AUTO_APPROVED"

        # 2. Policy Citations & Confidence
        citations = result.get("policy_citations", [])
        assert len(citations) > 0
        assert any("3." in c for c in citations), f"Expected Section 3 auto-approval citations, got: {citations}"
        assert result.get("confidence_score", 0.0) >= 0.7

        # 3. Adverse Action & Audit Memo
        assert result.get("adverse_action_reasons") == []
        assert len(result.get("underwriter_memo", "")) > 20

        # 4. Bank Analyst Agent Output
        assert result.get("bank_analyst_summary") is not None
        assert len(result.get("categorized_transactions", [])) > 0

        # 5. Financial Math Verification
        assert result.get("fico_score") == 745
        assert result.get("post_loan_dti") is not None
        assert result.get("post_loan_dti") <= 36.0

    def test_marcus_vance_auto_denial(self):
        """
        Persona 2 (Marcus Vance):
        - FICO 595 (<620 knockout), 3 delinquencies (>1 knockout), 4 NSF fees (>2 knockout), DTI ~63.96% (>45% knockout).
        - Expected: LLM policy underwriter triggers Section 2 knockout rules and issues DENY.
        - Must include ECOA-compliant adverse action reasons.
        - Graph reaches finalize_application_node with AUTO_DENIED final status.
        """
        profile = APPLICANT_PROFILES["MARCUS_VANCE"]
        initial_state: LoanApplicationState = {
            "application_id": profile["application_id"],
            "applicant_name": profile["applicant_name"],
            "ssn_last_4": profile["ssn_last_4"],
            "stated_annual_income": float(profile["stated_annual_income"]),
            "loan_amount_requested": float(profile["loan_amount_requested"]),
            "loan_purpose": profile["loan_purpose"],
            "fcra_consent": True,
        }

        graph = build_underwriting_graph()
        config = {"configurable": {"thread_id": "test-integration-marcus"}}

        result = graph.invoke(initial_state, config=config)

        # 1. Underwriter Decision & Final Status
        assert result.get("underwriting_decision") == "DENY"
        assert result.get("final_status") == "AUTO_DENIED"

        # 2. Knockout Citations & Confidence
        citations = result.get("policy_citations", [])
        assert len(citations) > 0
        assert any("2." in c for c in citations), f"Expected Section 2 knockout citations, got: {citations}"
        assert result.get("confidence_score", 0.0) >= 0.7

        # 3. Mandatory ECOA Adverse Action Notice
        adverse_reasons = result.get("adverse_action_reasons", [])
        assert len(adverse_reasons) > 0, "ECOA mandates adverse action reasons for denied applications."

        # 4. Underwriter Memo
        assert len(result.get("underwriter_memo", "")) > 20

    def test_david_chen_escalate_human_review_and_resume(self):
        """
        Persona 3 (David Chen):
        - FICO 710, loan $35,000 (exceeds $25,000 threshold), DTI ~41.84% (near-prime 36.01%-45.00%),
          income variance ~21.45% (>15% stated gross vs net direct deposits).
        - Expected: Passes all Section 2 knockouts, but triggers Section 4 Escalation gates.
        - Decision is ESCALATE.
        - Graph intercepts and PAUSES execution before `human_review_node`.
        - Resuming with human underwriter approval finishes the workflow with HUMAN_APPROVED.
        """
        profile = APPLICANT_PROFILES["DAVID_CHEN"]
        initial_state: LoanApplicationState = {
            "application_id": profile["application_id"],
            "applicant_name": profile["applicant_name"],
            "ssn_last_4": profile["ssn_last_4"],
            "stated_annual_income": float(profile["stated_annual_income"]),
            "loan_amount_requested": float(profile["loan_amount_requested"]),
            "loan_purpose": profile["loan_purpose"],
            "fcra_consent": True,
        }

        graph = build_underwriting_graph()
        config = {"configurable": {"thread_id": "test-integration-david"}}

        # Step 1: Initial invocation triggers workflow and halts before human review
        paused_result = graph.invoke(initial_state, config=config)

        # Verify AI Agent decision is ESCALATE
        assert paused_result.get("underwriting_decision") == "ESCALATE"

        # Verify LangGraph StateGraph halted at the breakpoint before human_review_node
        state_snapshot = graph.get_state(config)
        assert state_snapshot.next == ("human_review_node",), (
            f"Expected graph to be paused before ('human_review_node',), but next is {state_snapshot.next}"
        )

        # Verify Section 4 escalation policy citations
        citations = paused_result.get("policy_citations", [])
        assert len(citations) > 0
        assert any("4." in c for c in citations), f"Expected Section 4 escalation citations, got: {citations}"

        # Step 2: Human Credit Officer reviews the file and updates state with decision
        officer_notes = "Senior Underwriter verified W-2 and pay stubs; tax withholding explains 21% variance. Approved 41.8% DTI."
        graph.update_state(
            config,
            {
                "human_reviewer_decision": "APPROVE",
                "human_reviewer_notes": officer_notes,
            },
        )

        # Step 3: Resume LangGraph execution by passing None as input
        final_result = graph.invoke(None, config=config)

        # Verify Final Status and Execution Audit Log
        assert final_result.get("final_status") == "HUMAN_APPROVED"
        logs = final_result.get("execution_log", [])
        assert any("Human Underwriter Override" in log for log in logs)
        assert any(officer_notes in log for log in logs)
