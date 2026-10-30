"""
Unit tests for loan_agent/admin.py, list_loans, and trace_loan CLI scripts.
"""
import pytest
from unittest.mock import MagicMock, patch

from loan_agent.admin import (
    normalize_cli_args,
    list_loans_action,
    trace_loan_action,
    main_list_loans,
    main_trace_loan,
)
from loan_agent.mock_apis import APPLICANT_PROFILES
from loan_agent.workflow_runner import run_initiate_workflow


class TestNormalizeCliArgs:
    def test_normalize_args_dashes_and_quotes(self):
        raw = ["—status", "“pending review”", "—count", "—app_id=APP-01"]
        cleaned = normalize_cli_args(raw)
        assert cleaned == ["--status", "pending review", "--count", "--app_id=APP-01"]

    def test_normalize_args_split_curly_quotes(self):
        raw = ["--status", "“pending", "review”"]
        cleaned = normalize_cli_args(raw)
        assert cleaned == ["--status", "pending review"]

    def test_normalize_args_unclosed_quotes(self):
        raw = ["--status", "“pending"]
        cleaned = normalize_cli_args(raw)
        assert cleaned == ["--status", "pending"]


class TestListLoans:
    def test_list_loans_empty(self, test_app_db, capsys):
        count = list_loans_action(app_db=test_app_db)
        assert count == 0
        captured = capsys.readouterr()
        assert "No loan applications found" in captured.out

    def test_list_loans_empty_with_status(self, test_app_db, capsys):
        count = list_loans_action(status="new", app_db=test_app_db)
        assert count == 0
        captured = capsys.readouterr()
        assert "No loan applications found matching status: 'new'" in captured.out

    def test_list_loans_count_only(self, test_app_db, capsys):
        test_app_db.save_application({
            "application_id": "APP-001",
            "applicant_name": "Applicant 1",
            "ssn_last_4": "1111",
            "stated_annual_income": 80000.0,
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Other",
            "fcra_consent": True,
            "status": "NEW",
        })
        count = list_loans_action(count_only=True, app_db=test_app_db)
        assert count == 1
        captured = capsys.readouterr()
        assert "Count: 1" in captured.out

    def test_list_loans_tabular_output(self, test_app_db, capsys):
        test_app_db.save_application({
            "application_id": "APP-001",
            "applicant_name": "Sarah Jenkins",
            "ssn_last_4": "4401",
            "stated_annual_income": 96000.0,
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Debt Consolidation",
            "fcra_consent": True,
            "status": "AUTO_APPROVED",
        })
        count = list_loans_action(status="approved", app_db=test_app_db)
        assert count == 1
        captured = capsys.readouterr()
        assert "Sarah Jenkins" in captured.out
        assert "APP-001" in captured.out
        assert "$15,000.00" in captured.out

    def test_main_list_loans_cli(self, test_app_db, monkeypatch):
        # Seed test loan
        test_app_db.save_application({
            "application_id": "APP-2026-001",
            "applicant_name": "Sarah Jenkins",
            "ssn_last_4": "4401",
            "stated_annual_income": 96000.0,
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Debt Consolidation",
            "fcra_consent": True,
            "status": "NEW",
        })
        # Mock get_application_database to return our test DB
        monkeypatch.setattr("loan_agent.admin.get_application_database", lambda cfg=None: test_app_db)

        # 1. python -m list_loans
        assert main_list_loans([]) == 0

        # 2. python -m list_loans —count
        assert main_list_loans(["—count"]) == 0

        # 3. python -m list_loans --status new
        assert main_list_loans(["--status", "new"]) == 0

        # 4. python -m list_loans --status new —count
        assert main_list_loans(["--status", "new", "—count"]) == 0

        # 5. python -m list_loans --status “pending review”
        assert main_list_loans(["--status", "“pending review”"]) == 0

        # 6. python -m list_loans --status pending-review
        assert main_list_loans(["--status", "pending-review"]) == 0

        # 7. python -m list_loans --status pending_review
        assert main_list_loans(["--status", "pending_review"]) == 0


class TestTraceLoan:
    def test_trace_loan_not_found(self, test_app_db, test_checkpointer, capsys):
        success = trace_loan_action(
            app_id="APP-MISSING",
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )
        assert success is False
        captured = capsys.readouterr()
        assert "not found" in captured.out

    def test_trace_loan_case_insensitive_and_consolidated(self, test_app_db, test_checkpointer, capsys):
        sarah = APPLICANT_PROFILES["SARAH_JENKINS"]
        test_app_db.save_application({
            "application_id": sarah["application_id"],
            "applicant_name": sarah["applicant_name"],
            "ssn_last_4": sarah["ssn_last_4"],
            "stated_annual_income": float(sarah["stated_annual_income"]),
            "loan_amount_requested": float(sarah["loan_amount_requested"]),
            "loan_purpose": sarah["loan_purpose"],
            "fcra_consent": True,
            "status": "NEW",
        })

        # Run workflow so checkpointer has state and audit trail has events
        run_initiate_workflow(app_db=test_app_db, checkpointer=test_checkpointer)

        # Query trace using lowercase app-2026-001 (case-insensitive)
        lower_id = sarah["application_id"].lower()
        success = trace_loan_action(
            app_id=lower_id,
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )
        assert success is True
        captured = capsys.readouterr()

        # Verify consolidated information from both DB and Langgraph
        assert sarah["applicant_name"] in captured.out
        assert sarah["application_id"] in captured.out
        assert "CONSOLIDATED AUDIT & EXECUTION TRACE" in captured.out
        assert "APPLICATION METADATA" in captured.out
        assert "ADJUDICATION OUTCOME" in captured.out
        assert "UNDERWRITING FINANCIAL RATIOS" in captured.out
        assert "REGULATORY AUDIT TRAIL" in captured.out
        assert "LANGGRAPH EXECUTION LOG" in captured.out

    def test_main_trace_loan_cli(self, test_app_db, test_checkpointer, monkeypatch):
        test_app_db.save_application({
            "application_id": "APP-2026-001",
            "applicant_name": "Sarah Jenkins",
            "ssn_last_4": "4401",
            "stated_annual_income": 96000.0,
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Debt Consolidation",
            "fcra_consent": True,
            "status": "NEW",
        })
        monkeypatch.setattr("loan_agent.admin.get_application_database", lambda cfg=None: test_app_db)
        monkeypatch.setattr("loan_agent.admin.get_langgraph_checkpointer", lambda cfg=None: test_checkpointer)

        # Successful trace (case-insensitive)
        ret_ok = main_trace_loan(["—app_id", "app-2026-001"])
        assert ret_ok == 0

        # Non-existent trace returns 1
        ret_fail = main_trace_loan(["--app-id", "APP-NONEXISTENT"])
        assert ret_fail == 1

    def test_trace_loan_adverse_actions_and_missing_branches(self, test_app_db, test_checkpointer, capsys):
        # 1. App with adverse action reasons
        test_app_db.save_application({
            "application_id": "APP-2026-999",
            "applicant_name": "Test Adverse",
            "ssn_last_4": "9999",
            "stated_annual_income": 30000.0,
            "loan_amount_requested": 50000.0,
            "status": "REJECTED",
            "adjudication": {
                "decision": "DENY",
                "officer": "SYSTEM_AGENT",
                "adjudicated_at": "2026-10-30T10:00:00",
                "confidence_score": 0.98,
                "adverse_action_reasons": ["DTI exceeds maximum threshold", "Insufficient liquidity cushion"],
                "reason": "Risk threshold exceeded",
            },
        })
        success = trace_loan_action(
            app_id="APP-2026-999",
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )
        assert success is True
        out = capsys.readouterr().out
        assert "Adverse Action:" in out
        assert "DTI exceeds maximum threshold" in out

        # 2. Checkpointer has state for an app ID that does NOT exist in application DB
        app_mock = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.values = {
            "applicant_name": "Ghost Applicant",
            "execution_log": ["Step 1", "Step 2"],
        }
        mock_snapshot.next = ()
        app_mock.get_state.return_value = mock_snapshot
        app_mock.get_state_history.return_value = [mock_snapshot]

        with patch("loan_agent.admin.build_underwriting_graph", return_value=app_mock):
            success2 = trace_loan_action(
                app_id="APP-2026-GHOST",
                app_db=test_app_db,
                checkpointer=test_checkpointer,
            )
            assert success2 is True
            out2 = capsys.readouterr().out
            assert "Record not found in Application Database" in out2
            assert "Step 1" in out2
            assert "Step 2" in out2

