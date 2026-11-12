"""
test_api_cli.py
Tests for the REST-backed admin CLI (loan_agent/api_cli.py) that powers the
list_loans / trace_loan / initiate_workflow scripts.

The FastAPI TestClient is injected as the HTTP session, so the CLI exercises the real
service endpoints end-to-end without a live server (and without live LLM calls — the
deterministic mock from conftest.py is active). Authentication is real as well: the tools
sign in as an Administrator through POST /api/v1/auth/token and sign out afterwards.
"""
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from loan_agent.api_cli import (
    ApiClient,
    main_initiate_workflow,
    main_list_loans,
    main_trace_loan,
)
from loan_agent.auth import Role
from loan_agent.auth import users as user_directory
from loan_agent.mock_apis import APPLICANT_PROFILES
from tests.conftest import bearer_headers, create_user_with_password

ADMIN, APPLICANT, UNDERWRITER, PASSWORD = "ops.admin", "applicant.one", "uw.one", "fake-password"


@pytest.fixture
def http(test_app_db, test_checkpointer) -> Generator[TestClient, None, None]:
    app = create_app(scheduler_enabled=False)
    app.dependency_overrides[get_db] = lambda: test_app_db
    app.dependency_overrides[get_checkpointer] = lambda: test_checkpointer
    create_user_with_password(test_app_db, ADMIN, Role.ADMINISTRATOR, PASSWORD, "Ops Admin")
    create_user_with_password(test_app_db, APPLICANT, Role.APPLICANT, PASSWORD, "Mrs. Some Applicant")
    create_user_with_password(test_app_db, UNDERWRITER, Role.UNDERWRITER, PASSWORD, "Mr. Some Underwriter")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def admin_credentials(monkeypatch):
    """How an operator (or cron job) runs the tools: user ID and password in the environment."""
    monkeypatch.setenv("LOAN_AGENT_USER", ADMIN)
    monkeypatch.setenv("LOAN_AGENT_PASSWORD", PASSWORD)


@pytest.fixture
def cli(http) -> ApiClient:
    return ApiClient(base_url="http://testserver", session=http)


def as_role(http: TestClient, user_id: str) -> dict:
    return bearer_headers(http, user_id, PASSWORD)


def submit(http: TestClient, key: str) -> str:
    p = APPLICANT_PROFILES[key]
    res = http.post("/api/v1/applications", headers=as_role(http, APPLICANT), json={
        "applicant_name": p["applicant_name"],
        "ssn_last_4": p["ssn_last_4"],
        "stated_annual_income": p["stated_annual_income"],
        "loan_amount_requested": p["loan_amount_requested"],
        "loan_purpose": p["loan_purpose"],
        "fcra_consent": True,
    })
    assert res.status_code == 201
    return res.json()["application"]["application_id"]


def run_workflows(http: TestClient, **json) -> None:
    res = http.post("/api/v1/workflows/run", params={"sync": "true"}, json=json, headers=as_role(http, ADMIN))
    assert res.status_code == 202, res.text


class TestCliAuthentication:
    def test_tools_sign_in_and_revoke_their_token_afterwards(self, http, cli, test_app_db):
        assert main_list_loans([], client=cli) == 0
        assert cli.token is None  # signed out again
        sessions = test_app_db.conn.execute("SELECT user_id, revoked_at FROM auth_sessions").fetchall()
        assert sessions and all(r["user_id"] == ADMIN and r["revoked_at"] for r in sessions)

    def test_missing_credentials_are_reported(self, cli, capsys, monkeypatch):
        monkeypatch.delenv("LOAN_AGENT_USER")
        assert main_list_loans([], client=cli) == 1
        assert "Sign-in required" in capsys.readouterr().out

    def test_user_flag_overrides_the_environment(self, http, cli, capsys):
        # an underwriter may read the queue but may not run the workflow
        assert main_list_loans(["--user", UNDERWRITER], client=cli) == 0
        assert main_initiate_workflow(["--user", UNDERWRITER], client=cli) == 1
        assert "HTTP 403" in capsys.readouterr().out

    def test_wrong_password_is_refused(self, cli, capsys, monkeypatch):
        monkeypatch.setenv("LOAN_AGENT_PASSWORD", "wrong-password")
        assert main_list_loans([], client=cli) == 1
        out = capsys.readouterr().out
        assert "HTTP 401" in out and "Sign-in failed" in out

    def test_applicant_cannot_use_the_admin_tools(self, cli, capsys):
        assert main_list_loans(["--user", APPLICANT], client=cli) == 1
        assert "HTTP 403" in capsys.readouterr().out

    def test_account_without_a_password_is_told_to_set_one(self, cli, capsys, test_app_db, monkeypatch):
        user_directory.create_user(test_app_db, "new.admin", Role.ADMINISTRATOR)
        monkeypatch.setenv("LOAN_AGENT_PASSWORD", "")
        assert main_list_loans(["--user", "new.admin"], client=cli) == 1
        assert "must choose a password first" in capsys.readouterr().out

    def test_pre_issued_token_is_used_as_is_and_not_revoked(self, http, capsys, monkeypatch):
        token = as_role(http, ADMIN)["Authorization"].split()[1]
        monkeypatch.delenv("LOAN_AGENT_USER")
        monkeypatch.setenv("LOAN_AGENT_TOKEN", token)
        client = ApiClient(base_url="http://testserver", session=http)
        assert main_list_loans(["--count"], client=client) == 0
        assert http.get("/api/v1/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200


class TestListLoansCli:
    def test_empty_listing(self, cli, capsys):
        assert main_list_loans([], client=cli) == 0
        assert "No loan applications found" in capsys.readouterr().out

    def test_table_listing_and_status_filter(self, http, cli, capsys):
        submit(http, "MRS_SOME_NAME")
        submit(http, "MR_SOME_NAME")
        assert main_list_loans([], client=cli) == 0
        out = capsys.readouterr().out
        assert "Found 2 loan application(s)" in out
        assert "Mrs. Some Name" in out and "Mr. Some Name" in out and "$15,000.00" in out

        assert main_list_loans(["--status", "pending review"], client=cli) == 0
        assert "No loan applications found matching status: 'pending review'" in capsys.readouterr().out

    def test_count_prints_summary_by_status(self, http, cli, capsys):
        for key in ("MRS_SOME_NAME", "MR_SOME_NAME", "MS_SOME_NAME"):
            submit(http, key)
        run_workflows(http, limit=2)  # Mrs. Some Name -> approved, Mr. Some Name -> denied
        capsys.readouterr()

        assert main_list_loans(["--count"], client=cli) == 0
        out = capsys.readouterr().out
        assert "Loan Application Status Summary" in out
        # Five main statuses always listed, in order, with sub-status detail indented beneath
        lines = [l.strip() for l in out.splitlines()]
        mains = [l for l in lines if l and not l.startswith("└") and not l.startswith("-") and "Summary" not in l]
        assert [m.split("  ")[0] for m in mains] == ["New", "Pending Processing", "Pending Review", "Approved", "Rejected", "TOTAL"]
        assert "New                                   1" in out
        assert "└ AUTO_APPROVED" in out and "└ AUTO_DENIED" in out
        assert "TOTAL                                 3" in out

    def test_count_with_status_filter(self, http, cli, capsys):
        for key in ("MRS_SOME_NAME", "MR_SOME_NAME"):
            submit(http, key)
        run_workflows(http)
        capsys.readouterr()  # discard the server-side batch-run console output

        # em-dash flags (pasted from docs) are normalized
        assert main_list_loans(["—count", "—status", "rejected"], client=cli) == 0
        out = capsys.readouterr().out
        assert "filter: 'rejected'" in out
        assert "Rejected" in out and "└ AUTO_DENIED" in out and "Approved" not in out

    def test_unreachable_service_is_reported(self, capsys):
        broken = ApiClient(base_url="http://127.0.0.1:9")  # nothing listens on port 9
        assert main_list_loans(["--count"], client=broken) == 2
        out = capsys.readouterr().out
        assert "Could not reach the underwriting service" in out
        assert "run_api.py" in out


class TestTraceLoanCli:
    def test_trace_unknown_application(self, cli, capsys):
        assert main_trace_loan(["--app_id", "APP-NOPE"], client=cli) == 1
        assert "was not found" in capsys.readouterr().out

    def test_trace_full_lifecycle(self, http, cli, capsys):
        app_id = submit(http, "MS_SOME_NAME")
        http.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"}, headers=as_role(http, ADMIN))
        decided = http.post(f"/api/v1/applications/{app_id}/decision", headers=as_role(http, UNDERWRITER),
                            json={"decision": "APPROVE", "notes": "Verified W-2 documentation."})
        assert decided.status_code == 200, decided.text

        # case-insensitive lookup, like the original tool
        assert main_trace_loan(["--app_id", app_id.lower()], client=cli) == 0
        out = capsys.readouterr().out
        assert f"CONSOLIDATED AUDIT & EXECUTION TRACE: {app_id}" in out
        assert "APPLICATION METADATA & PROFILE" in out and "Ms. Some Name" in out
        assert "ADJUDICATION OUTCOME" in out and "HUMAN_APPROVED" in out
        assert "WORKFLOW STATE & FINANCIAL METRICS" in out and "Post-Loan DTI" in out
        assert "Workflow Execution Log:" in out
        assert "APPLICATION DATABASE AUDIT TRAIL" in out and "APPLICATION_SUBMITTED" in out
        assert "HUMAN_REVIEW_DECISION" in out and "Mr. Some Underwriter (uw.one)" in out  # the signed-in officer

    def test_trace_before_workflow_runs(self, http, cli, capsys):
        app_id = submit(http, "MRS_SOME_NAME")
        assert main_trace_loan(["--app_id", app_id], client=cli) == 0
        out = capsys.readouterr().out
        assert "(Workflow has not been executed for this application yet)" in out
        assert "(No final adjudication recorded yet)" in out


class TestInitiateWorkflowCli:
    def test_dry_run_leaves_records_untouched(self, http, cli, capsys, test_app_db):
        app_id = submit(http, "MRS_SOME_NAME")
        assert main_initiate_workflow(["--dry-run"], client=cli) == 0
        out = capsys.readouterr().out
        assert "[DRY RUN] Found 1 new loan application(s)" in out
        assert test_app_db.get_application(app_id)["status"] == "NEW"

    def test_run_is_fire_and_forget(self, http, cli, capsys, test_app_db):
        ids = {key: submit(http, key) for key in ("MRS_SOME_NAME", "MR_SOME_NAME", "MS_SOME_NAME")}
        assert main_initiate_workflow([], client=cli) == 0
        out = capsys.readouterr().out
        assert "Approval workflow run #1 accepted (REQUESTED)" in out
        assert "New applications queued for underwriting: 3" in out
        assert "track it on the Administrator screen" in out  # the CLI returns before the outcome is known
        # The run is logged with the CLI as its trigger and (TestClient runs background tasks inline) completed
        run = http.get("/api/v1/workflows/runs/1", headers=as_role(http, ADMIN)).json()
        assert run["trigger_source"] == "CLI" and run["status"] == "COMPLETED"
        assert (run["approved"], run["rejected"], run["escalated"]) == (1, 1, 1)
        assert test_app_db.get_application(ids["MRS_SOME_NAME"])["status"] == "AUTO_APPROVED"
        assert test_app_db.get_application(ids["MR_SOME_NAME"])["status"] == "AUTO_DENIED"
        assert test_app_db.get_application(ids["MS_SOME_NAME"])["status"] == "PENDING_REVIEW"

    def test_limit_flag(self, http, cli, capsys, test_app_db):
        submit(http, "MRS_SOME_NAME")
        submit(http, "MR_SOME_NAME")
        assert main_initiate_workflow(["--limit", "1"], client=cli) == 0
        assert "(limit 1)" in capsys.readouterr().out
        assert len([a for a in test_app_db.list_applications(status="new")]) == 1
