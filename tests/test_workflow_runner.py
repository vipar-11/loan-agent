"""
Unit tests for loan_agent/workflow_runner.py and initiate_workflow CLI.
"""
from unittest.mock import patch
import pytest

from loan_agent.db import ApplicationDatabase
from loan_agent.checkpointer import get_langgraph_checkpointer
from loan_agent.workflow_runner import (
    normalize_cli_args,
    run_initiate_workflow,
    main as workflow_main,
)
from loan_agent.mock_apis import APPLICANT_PROFILES


class TestNormalizeCliArgs:
    def test_normalize_dashes(self):
        raw = ["—dry-run", "–limit", "10", "--config", "dbconfig.ini"]
        res = normalize_cli_args(raw)
        assert res == ["--dry-run", "--limit", "10", "--config", "dbconfig.ini"]


class TestInitiateWorkflow:
    def test_run_initiate_workflow_empty(self, test_app_db, test_checkpointer):
        summary = run_initiate_workflow(
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )
        assert summary["found"] == 0
        assert summary["selected"] == 0
        assert summary["approved"] == 0

    def test_run_initiate_workflow_dry_run(self, test_app_db, test_checkpointer):
        # Seed applications
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

        summary = run_initiate_workflow(
            dry_run=True,
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )
        assert summary["found"] == 1
        assert summary["selected"] == 1
        assert summary["dry_run"] is True

        # Database record must still be in NEW status
        app = test_app_db.get_application(sarah["application_id"])
        assert app["status"] == "NEW"

    def test_run_initiate_workflow_limit_and_execution(self, test_app_db, test_checkpointer):
        # Seed 3 applicant personas
        sarah = APPLICANT_PROFILES["SARAH_JENKINS"]
        marcus = APPLICANT_PROFILES["MARCUS_VANCE"]
        david = APPLICANT_PROFILES["DAVID_CHEN"]

        for idx, p in enumerate([sarah, marcus, david], 1):
            test_app_db.save_application({
                "application_id": p["application_id"],
                "applicant_name": p["applicant_name"],
                "ssn_last_4": p["ssn_last_4"],
                "stated_annual_income": float(p["stated_annual_income"]),
                "loan_amount_requested": float(p["loan_amount_requested"]),
                "loan_purpose": p["loan_purpose"],
                "fcra_consent": True,
                "status": "NEW",
                "submitted_at": f"2026-10-30 10:0{idx}:00",
            })

        # Test with limit=2 (should process oldest 2: sarah and marcus)
        summary = run_initiate_workflow(
            limit=2,
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )

        assert summary["found"] == 3
        assert summary["selected"] == 2
        assert summary["approved"] == 1
        assert summary["rejected"] == 1

        # Sarah should be AUTO_APPROVED
        s_app = test_app_db.get_application(sarah["application_id"])
        assert s_app["status"] == "AUTO_APPROVED"
        assert s_app["adjudication"] is not None

        # Marcus should be AUTO_DENIED
        m_app = test_app_db.get_application(marcus["application_id"])
        assert m_app["status"] == "AUTO_DENIED"

        # David was not processed due to limit=2, so should still be NEW
        d_app = test_app_db.get_application(david["application_id"])
        assert d_app["status"] == "NEW"

        # Now run again for remaining (David -> ESCALATE -> PENDING_REVIEW)
        summary2 = run_initiate_workflow(
            app_db=test_app_db,
            checkpointer=test_checkpointer,
        )
        assert summary2["found"] == 1
        assert summary2["escalated"] == 1

        d_app2 = test_app_db.get_application(david["application_id"])
        assert d_app2["status"] == "PENDING_REVIEW"

        # Verify audit trails
        trail = test_app_db.get_audit_trail(sarah["application_id"])
        event_types = [t["event_type"] for t in trail]
        assert "WORKFLOW_INITIATED" in event_types
        assert "WORKFLOW_APPROVED" in event_types

    def test_run_initiate_workflow_exception_handling(self, test_app_db, test_checkpointer):
        test_app_db.save_application({
            "application_id": "APP-FAIL-01",
            "applicant_name": "Error Subject",
            "ssn_last_4": "0000",
            "stated_annual_income": 50000.0,
            "loan_amount_requested": 10000.0,
            "loan_purpose": "Other",
            "fcra_consent": True,
            "status": "NEW",
        })

        with patch("loan_agent.workflow_runner.build_underwriting_graph") as mock_build:
            mock_graph = mock_build.return_value
            mock_graph.invoke.side_effect = RuntimeError("Graph engine simulated failure")

            summary = run_initiate_workflow(
                app_db=test_app_db,
                checkpointer=test_checkpointer,
            )
            assert summary["errors"] == 1

            app = test_app_db.get_application("APP-FAIL-01")
            assert app["status"] == "PENDING_REVIEW"
            assert "Graph engine simulated failure" in app["graph_state"]["error_message"]

    def test_main_cli_entrypoint(self, tmp_path, monkeypatch):
        # Test CLI invocations with em-dash and regular flags
        ret = workflow_main(["—dry-run", "—limit", "5"])
        assert ret == 0

        cfg_file = tmp_path / "custom.ini"
        cfg_file.write_text(
            f"[storage]\nmode=local\nlocal_app_db_path={tmp_path / 'app.db'}\nlocal_langgraph_db_path={tmp_path / 'lg.db'}\n",
            encoding="utf-8",
        )
        ret_cfg = workflow_main(["--config", str(cfg_file), "--dry-run"])
        assert ret_cfg == 0
