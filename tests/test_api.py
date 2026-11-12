"""
test_api.py
End-to-end tests for the FastAPI Autonomous Underwriting service (api/).

Uses isolated in-memory SQLite for both the application database and the workflow checkpointer,
and the deterministic mock LLM installed by conftest.py (no live API calls).
"""
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from loan_agent.db import ApplicationDatabase
from loan_agent.mock_apis import APPLICANT_PROFILES
from tests.conftest import sign_in_as


@pytest.fixture
def client(test_app_db: ApplicationDatabase, test_checkpointer) -> Generator[TestClient, None, None]:
    app = create_app(scheduler_enabled=False)
    app.dependency_overrides[get_db] = lambda: test_app_db
    app.dependency_overrides[get_checkpointer] = lambda: test_checkpointer
    sign_in_as(app)  # functional tests run as the all-permissions test operator (see conftest.py)
    # Lifespan warm-up is bypassed deliberately (it would touch the real singleton factories).
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def persona_payload(key: str, **overrides) -> dict:
    p = APPLICANT_PROFILES[key]
    payload = {
        "applicant_name": p["applicant_name"],
        "ssn_last_4": p["ssn_last_4"],
        "stated_annual_income": p["stated_annual_income"],
        "loan_amount_requested": p["loan_amount_requested"],
        "loan_purpose": p["loan_purpose"],
        "fcra_consent": True,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Reference & system
# ---------------------------------------------------------------------------
class TestReferenceEndpoints:
    def test_health(self, client):
        assert client.get("/api/v1/health").json() == {"status": "ok"}

    def test_system_info(self, client):
        data = client.get("/api/v1/system").json()
        assert {"new", "processing", "pending_review", "adjudicated", "total", "by_status", "main_statuses"} <= set(data["stats"])
        for leaked in ("engine", "hitl"):
            assert leaked not in data
        assert "Home Improvement" in data["loan_purposes"]

    def test_personas(self, client):
        personas = client.get("/api/v1/personas").json()
        assert {p["key"] for p in personas} == {"MRS_SOME_NAME", "MR_SOME_NAME", "MS_SOME_NAME"}
        assert personas[0]["ssn_last_4"] == "1111"

    def test_installment_quote(self, client):
        q = client.get("/api/v1/calculator/installment", params={"loan_amount": 15000}).json()
        assert q["monthly_payment"] == 498.21
        assert q["term_months"] == 36
        assert client.get("/api/v1/calculator/installment", params={"loan_amount": 500}).status_code == 422

    def test_policy_document(self, client):
        doc = client.get("/api/v1/policy").json()
        assert "APEX FINANCIAL" in doc["markdown"]
        assert "<h" in doc["html"]

    def test_workbench_pages_render(self, client):
        assert client.get("/", follow_redirects=False).status_code == 307
        portal = client.get("/portal")
        assert portal.status_code == 200 and "Personal Installment Loan Application" in portal.text
        desk = client.get("/desk")
        assert desk.status_code == 200 and "Underwriting Review & Adjudication Desk" in desk.text
        # Applicants cannot launch underwriting; the desk has no dispatch tab and nothing deletes data.
        assert "run_workflow" not in portal.text
        assert "New Applications" not in desk.text
        assert "Reset Application Database" not in desk.text
        assert client.post("/api/v1/admin/reset").status_code in (404, 405)
        admin = client.get("/admin")
        assert admin.status_code == 200 and "Administration & Operations Console" in admin.text
        # No internal architecture or framework names on any screen; no queue counts for applicants
        for page in (portal.text, desk.text, admin.text):
            for leaked in ("LangGraph", "System Architecture", "/docs", "gpt-oss"):
                assert leaked not in page
        assert "QUEUED" not in portal.text and "PIPELINE" not in portal.text
        for js in ("/static/js/portal.js", "/static/js/desk.js", "/static/js/admin.js"):
            assert "LangGraph" not in client.get(js).text
        assert client.get("/static/css/apex.css").status_code == 200
        assert client.get("/docs").status_code == 200


# ---------------------------------------------------------------------------
# Application submission & listing
# ---------------------------------------------------------------------------
class TestApplicationSubmission:
    def test_submit_records_new_application(self, client, test_app_db):
        res = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME"))
        assert res.status_code == 201
        body = res.json()
        assert "workflow_scheduled" not in body
        # Borrower-facing confirmation: reference number, no internal/technical instructions
        assert body["application"]["application_id"] in body["message"]
        assert "underwriting team" in body["message"]
        for leaked in ("initiate_workflow", "NEW", "cron", "pipeline", "LangGraph"):
            assert leaked not in body["message"]
        app = body["application"]
        assert app["status"] == "NEW" and app["stage"] == "NEW"
        assert app["application_id"].startswith("APP-")
        assert app["ssn_masked"] == "••••-1111"
        assert app["estimated_monthly_payment"] == 498.21

        trail = test_app_db.get_audit_trail(app["application_id"])
        assert [t["event_type"] for t in trail] == ["APPLICATION_SUBMITTED"]

    def test_submit_validation_errors(self, client):
        bad = persona_payload("MRS_SOME_NAME", ssn_last_4="12", loan_amount_requested=500.0)
        res = client.post("/api/v1/applications", json=bad)
        assert res.status_code == 422
        fields = {e["loc"][-1] for e in res.json()["detail"]}
        assert {"ssn_last_4", "loan_amount_requested"} <= fields

        res = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME", applicant_name="   "))
        assert res.status_code == 422

        res = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME", loan_purpose="Yacht"))
        assert res.status_code == 422

    def test_unique_ids_and_listing_filters(self, client):
        ids = [client.post("/api/v1/applications", json=persona_payload(k)).json()["application"]["application_id"]
               for k in ("MRS_SOME_NAME", "MR_SOME_NAME", "MS_SOME_NAME")]
        assert len(set(ids)) == 3

        assert len(client.get("/api/v1/applications").json()) == 3
        assert len(client.get("/api/v1/applications", params={"status": "new"}).json()) == 3
        assert client.get("/api/v1/applications", params={"status": "pending_review"}).json() == []
        assert client.get("/api/v1/applications", params={"status": "adjudicated"}).json() == []

        stats = client.get("/api/v1/stats").json()
        assert {k: stats[k] for k in ("new", "processing", "pending_review", "adjudicated", "total", "by_status")} == {
            "new": 3, "processing": 0, "pending_review": 0, "adjudicated": 0, "total": 3, "by_status": {"NEW": 3}}
        assert [g["status"] for g in stats["main_statuses"]] == ["new", "pending processing", "pending review", "approved", "rejected"]
        assert stats["main_statuses"][0]["count"] == 3

    def test_get_unknown_application_404(self, client):
        assert client.get("/api/v1/applications/APP-NOPE").status_code == 404
        assert client.get("/api/v1/applications/APP-NOPE/dossier").status_code == 404
        assert client.get("/api/v1/applications/APP-NOPE/audit-trail").status_code == 404

    def test_case_insensitive_lookup(self, client):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME")).json()["application"]["application_id"]
        assert client.get(f"/api/v1/applications/{app_id.lower()}").status_code == 200


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------
class TestWorkflowExecution:
    def test_applicant_cannot_trigger_workflow_on_submit(self, client, test_app_db):
        """The portal only records the file; `run_workflow` is not part of the contract and is ignored."""
        res = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME", run_workflow=True))
        assert res.status_code == 201
        app_id = res.json()["application"]["application_id"]
        assert test_app_db.get_application(app_id)["status"] == "NEW"
        assert [t["event_type"] for t in test_app_db.get_audit_trail(app_id)] == ["APPLICATION_SUBMITTED"]

    def test_sync_run_auto_approves_prime(self, client, test_app_db):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME")).json()["application"]["application_id"]
        res = client.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"})
        assert res.status_code == 202
        assert res.json()["status"] == "AUTO_APPROVED"

        final = client.get(f"/api/v1/applications/{app_id}").json()
        assert final["stage"] == "ADJUDICATED"
        assert final["adjudicator_type"] == "AI_AGENT"
        assert final["adjudication"]["officer"] == "AI Agent (Policy Underwriter)"
        assert final["graph_state"]["final_status"] == "AUTO_APPROVED"

        events = [t["event_type"] for t in test_app_db.get_audit_trail(app_id)]
        assert events == ["APPLICATION_SUBMITTED", "WORKFLOW_INITIATED", "WORKFLOW_APPROVED"]

    def test_sync_run_auto_denies_subprime_with_adverse_action(self, client):
        app_id = client.post("/api/v1/applications", json=persona_payload("MR_SOME_NAME")).json()["application"]["application_id"]
        res = client.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"})
        assert res.status_code == 202
        assert res.json()["status"] == "AUTO_DENIED"
        assert res.json()["decision"] == "DENY"

        final = client.get(f"/api/v1/applications/{app_id}").json()
        assert final["adjudication"]["adverse_action_reasons"]

    def test_no_consent_rejected_without_external_calls(self, client):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME", fcra_consent=False)).json()["application"]["application_id"]
        res = client.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"}).json()
        assert res["status"] == "REJECTED_NO_CONSENT"
        detail = client.get(f"/api/v1/applications/{app_id}").json()
        assert detail["graph_state"].get("credit_report") is None

    def test_dispatch_guard_rejects_non_new(self, client):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME")).json()["application"]["application_id"]
        assert client.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"}).status_code == 202
        # Already adjudicated -> 409
        assert client.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"}).status_code == 409
        assert client.post("/api/v1/applications/APP-NOPE/workflow").status_code == 404

    def test_background_dispatch_marks_processing_then_settles(self, client, test_app_db):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME")).json()["application"]["application_id"]
        res = client.post(f"/api/v1/applications/{app_id}/workflow")
        assert res.status_code == 202
        assert res.json()["scheduled"] is True
        assert res.json()["status"] == "PENDING_PROCESSING"
        assert test_app_db.get_application(app_id)["status"] == "AUTO_APPROVED"
        events = [t["event_type"] for t in test_app_db.get_audit_trail(app_id)]
        assert events == ["APPLICATION_SUBMITTED", "WORKFLOW_SCHEDULED", "WORKFLOW_INITIATED", "WORKFLOW_APPROVED"]

    def test_batch_run_processes_all_new(self, client):
        for k in ("MRS_SOME_NAME", "MR_SOME_NAME", "MS_SOME_NAME"):
            client.post("/api/v1/applications", json=persona_payload(k))

        dry = client.post("/api/v1/workflows/run", json={"dry_run": True}).json()
        assert dry["found"] == 3 and dry["dry_run"] is True
        assert client.get("/api/v1/stats").json()["new"] == 3

        res = client.post("/api/v1/workflows/run", params={"sync": "true"}, json={"limit": None, "dry_run": False})
        assert res.status_code == 202
        summary = res.json()
        assert (summary["approved"], summary["rejected"], summary["escalated"], summary["errors"]) == (1, 1, 1, 0)

        stats = client.get("/api/v1/stats").json()
        assert stats["by_status"] == {"AUTO_APPROVED": 1, "AUTO_DENIED": 1, "PENDING_REVIEW": 1}
        assert (stats["new"], stats["pending_review"], stats["adjudicated"], stats["total"]) == (0, 1, 2, 3)

    def test_batch_run_is_fire_and_forget_by_default(self, client):
        client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME"))
        res = client.post("/api/v1/workflows/run", params={"trigger": "cli"}, json={})
        assert res.status_code == 202
        body = res.json()
        assert body["status"] == "REQUESTED" and body["found"] == 1 and body["run_id"] >= 1
        # TestClient executes the background task before returning -> the run has settled and is logged
        run = client.get(f"/api/v1/workflows/runs/{body['run_id']}").json()
        assert run["status"] == "COMPLETED" and run["trigger_source"] == "CLI"
        assert run["approved"] == 1 and run["started_at"] and run["finished_at"]
        assert client.get("/api/v1/stats").json()["adjudicated"] == 1
        assert client.get("/api/v1/workflows/runs/999").status_code == 404

    def test_dry_run_is_not_logged(self, client):
        client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME"))
        client.post("/api/v1/workflows/run", json={"dry_run": True})
        assert client.get("/api/v1/admin/workflow-runs").json() == []


# ---------------------------------------------------------------------------
# Human-in-the-loop: dossier, ratios, decision
# ---------------------------------------------------------------------------
@pytest.fixture
def escalated_app_id(client) -> str:
    app_id = client.post("/api/v1/applications", json=persona_payload("MS_SOME_NAME")).json()["application"]["application_id"]
    res = client.post(f"/api/v1/applications/{app_id}/workflow", params={"sync": "true"}).json()
    assert res["status"] == "PENDING_REVIEW"
    return app_id


class TestHumanInTheLoop:
    def test_dossier_is_auto_populated_from_graph_state(self, client, escalated_app_id):
        d = client.get(f"/api/v1/applications/{escalated_app_id}/dossier").json()
        assert d["application"]["stage"] == "PENDING_REVIEW"
        assert d["ai_assessment"]["underwriting_decision"] == "ESCALATE"
        assert d["ai_assessment"]["execution_log"]
        assert d["sizing"]["monthly_payment"] == 1162.5

        assert d["credit"]["fico_score"] == 710
        assert len(d["credit"]["tradelines"]) == 3
        assert d["credit"]["public_records"][0]["status"] == "DISCHARGED"
        assert {"creditor", "account_type", "monthly_payment", "delinquencies", "standing"} <= set(d["credit"]["tradelines"][0])

        bank = d["bank"]
        assert bank["classified_by_ai"] is True
        assert bank["transactions"]
        for tx in bank["transactions"]:
            assert tx["classification"] in tx["options"]
            assert tx["classification"] != "— Select Classification —"
            assert (tx["flow"] == "CREDIT") == (tx["amount"] > 0)
        # AI reasoning from the Bank Statement Analyzer agent is surfaced per transaction
        assert any(tx["ai_reasoning"] for tx in bank["transactions"])
        assert any(tx["classification"] == "Undisclosed Debt / BNPL" for tx in bank["transactions"])

        ratios = d["ratios"]
        assert ratios["source"] == "GRAPH_STATE"
        assert ratios["ratios"]["post_loan_dti"] > 0
        assert len(ratios["rows"]) == 13

    def test_dossier_for_unprocessed_application_falls_back_to_heuristics(self, client):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME")).json()["application"]["application_id"]
        d = client.get(f"/api/v1/applications/{app_id}/dossier").json()
        assert d["bank"]["classified_by_ai"] is False
        assert d["credit"]["fico_score"] == 745
        assert d["ratios"]["ratios"]["estimated_new_loan_payment"] == 498.21

    def test_recalculate_ratios_with_officer_classifications(self, client, escalated_app_id):
        d = client.get(f"/api/v1/applications/{escalated_app_id}/dossier").json()
        baseline = d["ratios"]["ratios"]
        classifications = {tx["tx_id"]: tx["classification"] for tx in d["bank"]["transactions"]}

        # Same classifications -> same deterministic DTI as the pipeline produced
        same = client.post(f"/api/v1/applications/{escalated_app_id}/ratios/recalculate", json={"classifications": classifications}).json()
        assert same["source"] == "RECALCULATED"
        assert same["ratios"]["post_loan_dti"] == pytest.approx(baseline["post_loan_dti"], abs=0.05)

        # Officer reclassifies all BNPL outflows as ordinary living expenses -> hidden debt drops, DTI falls
        relaxed = {k: ("Other Living Expense / Standard" if v == "Undisclosed Debt / BNPL" else v) for k, v in classifications.items()}
        res = client.post(f"/api/v1/applications/{escalated_app_id}/ratios/recalculate", json={"classifications": relaxed}).json()
        assert res["ratios"]["undisclosed_monthly_debt"] == 0.0
        assert res["ratios"]["post_loan_dti"] < baseline["post_loan_dti"]

        # Persisted graph state is NOT mutated by what-if recalculation
        assert client.get(f"/api/v1/applications/{escalated_app_id}").json()["graph_state"]["post_loan_dti"] == baseline["post_loan_dti"]

    def test_heuristic_classifications_endpoint(self, client, escalated_app_id):
        res = client.get(f"/api/v1/applications/{escalated_app_id}/classifications/heuristic")
        assert res.status_code == 200
        labels = res.json()
        assert labels and all(v != "— Select Classification —" for v in labels.values())

    def test_human_approval_resumes_graph(self, client, escalated_app_id, test_app_db):
        # A caller-supplied officer name (legacy field) is ignored: the determination belongs to the signed-in user.
        payload = {"decision": "APPROVE", "officer": "Somebody Else", "notes": "Verified W-2; bonus structure explains variance."}
        res = client.post(f"/api/v1/applications/{escalated_app_id}/decision", json=payload)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "HUMAN_APPROVED"
        assert body["stage"] == "ADJUDICATED"
        assert body["adjudicator_type"] == "HUMAN_OFFICER"
        assert body["adjudication"]["officer"] == "Test Operator"
        assert body["adjudication"]["officer_user_id"] == "test.operator"
        assert "Somebody Else" not in res.text
        assert body["adjudication"]["reason"] == payload["notes"]
        assert body["graph_state"]["human_reviewer_decision"] == "APPROVE"
        assert any("Human Underwriter Override" in line for line in body["graph_state"]["execution_log"])
        decision_event = next(t for t in test_app_db.get_audit_trail(escalated_app_id)
                              if t["event_type"] == "HUMAN_REVIEW_DECISION")
        assert decision_event["actor"] == "Test Operator (test.operator)"

        # Queue moved: pending -> adjudicated
        assert client.get("/api/v1/applications", params={"status": "pending_review"}).json() == []
        assert client.get("/api/v1/stats").json()["adjudicated"] == 1

    def test_human_denial(self, client, escalated_app_id):
        res = client.post(f"/api/v1/applications/{escalated_app_id}/decision", json={"decision": "DENY", "notes": "Unable to verify income documentation."})
        assert res.status_code == 200
        assert res.json()["status"] == "HUMAN_DENIED"

    def test_decision_requires_notes(self, client, escalated_app_id):
        res = client.post(f"/api/v1/applications/{escalated_app_id}/decision", json={"decision": "APPROVE", "notes": "   "})
        assert res.status_code == 422
        assert client.get(f"/api/v1/applications/{escalated_app_id}").json()["status"] == "PENDING_REVIEW"

    def test_decision_rejected_when_not_pending_review(self, client):
        app_id = client.post("/api/v1/applications", json=persona_payload("MRS_SOME_NAME")).json()["application"]["application_id"]
        res = client.post(f"/api/v1/applications/{app_id}/decision", json={"decision": "APPROVE", "notes": "ok"})
        assert res.status_code == 409
        assert client.post("/api/v1/applications/APP-NOPE/decision", json={"decision": "APPROVE", "notes": "ok"}).status_code == 404

    def test_decision_is_idempotent_guarded(self, client, escalated_app_id):
        client.post(f"/api/v1/applications/{escalated_app_id}/decision", json={"decision": "APPROVE", "notes": "first"})
        second = client.post(f"/api/v1/applications/{escalated_app_id}/decision", json={"decision": "DENY", "notes": "second"})
        assert second.status_code == 409


# ---------------------------------------------------------------------------
# Concurrency: the underwriter must never wait on the periodic initiate_workflow job
# ---------------------------------------------------------------------------
class TestDecisionConcurrency:
    def test_decision_not_blocked_by_in_progress_workflow_run(
        self, client, escalated_app_id, test_app_db, test_checkpointer, monkeypatch
    ):
        """
        Regression: Approve/Reject used to hang until the whole initiate_workflow batch finished, because
        the HitL decision waited on the same global lock the batch held for its entire duration.
        """
        import threading
        from langchain_core.runnables import RunnableLambda

        from tests import conftest
        from api.dependencies import PIPELINE_LOCK
        from api.services import scheduler as scheduler_service

        llm_entered, release_llm = threading.Event(), threading.Event()

        class BlockingLLM(conftest.TestMockLLM):
            """Simulates a slow LLM call inside the batch: parks until the test releases it."""
            def with_structured_output(self, schema):
                inner = super().with_structured_output(schema)

                def _call(x):
                    llm_entered.set()
                    release_llm.wait(timeout=30)
                    return inner.invoke(x)
                return RunnableLambda(_call)

        monkeypatch.setattr("loan_agent.graph_workflow.get_llm", lambda *a, **kw: BlockingLLM())

        client.post("/api/v1/applications", json=persona_payload("MS_SOME_NAME"))  # queued NEW file
        run = scheduler_service.request_run(test_app_db, trigger_source="SCHEDULER")
        batch = threading.Thread(
            target=scheduler_service.execute_run, args=(test_app_db, test_checkpointer, run["id"]), daemon=True
        )
        batch.start()
        try:
            assert llm_entered.wait(timeout=10), "batch never reached the LLM"
            assert PIPELINE_LOCK.locked()
            assert test_app_db.get_workflow_run(run["id"])["status"] == "RUNNING"

            result: dict = {}

            def decide():
                result["res"] = client.post(
                    f"/api/v1/applications/{escalated_app_id}/decision",
                    json={"decision": "APPROVE", "notes": "Verified W-2 during batch."},
                )

            officer = threading.Thread(target=decide, daemon=True)
            officer.start()
            officer.join(timeout=10)
            assert not officer.is_alive(), "decision blocked behind the in-progress initiate_workflow run"
            assert result["res"].status_code == 200
            assert result["res"].json()["status"] == "HUMAN_APPROVED"
            assert test_app_db.get_workflow_run(run["id"])["status"] == "RUNNING"  # batch still in flight
        finally:
            release_llm.set()
            batch.join(timeout=30)

        finished = test_app_db.get_workflow_run(run["id"])
        assert finished["status"] == "COMPLETED" and finished["errors"] == 0 and finished["selected"] == 1
        assert test_app_db.get_application(escalated_app_id)["status"] == "HUMAN_APPROVED"

    def test_concurrent_decisions_on_same_file_only_one_wins(self, client, escalated_app_id, monkeypatch):
        """Same tab submitting twice at once (double-click): the checkout claim lets only one through."""
        import threading
        import time

        from api.services import workflows as workflow_service

        original = workflow_service.resume_with_human_decision

        def slow_resume(*args, **kwargs):
            time.sleep(0.3)  # widen the check-then-act window
            return original(*args, **kwargs)

        monkeypatch.setattr(workflow_service, "resume_with_human_decision", slow_resume)

        codes: list = []
        barrier = threading.Barrier(2)
        tab = {"X-Apex-Session-Id": client.get("/api/v1/session").headers["X-Apex-Session-Id"]}  # same browser tab

        def decide(decision, app_id):
            barrier.wait()
            codes.append(client.post(
                f"/api/v1/applications/{app_id}/decision", headers=tab, json={"decision": decision, "notes": decision}
            ).status_code)

        threads = [
            threading.Thread(target=decide, args=("APPROVE", escalated_app_id)),
            threading.Thread(target=decide, args=("DENY", escalated_app_id.lower())),  # IDs are case-insensitive
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert sorted(codes) == [200, 409]
        trail = client.get(f"/api/v1/applications/{escalated_app_id}/audit-trail").json()
        assert [e["event_type"] for e in trail].count("HUMAN_REVIEW_DECISION") == 1
