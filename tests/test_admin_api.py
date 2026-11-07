"""
test_admin_api.py
Administrator features: status monitoring by main status, initiate_workflow run logging & health,
schedule configuration, and the built-in periodic trigger.
"""
from datetime import datetime, timedelta
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from api.services import scheduler as sched
from loan_agent.db import main_status_for
from loan_agent.mock_apis import APPLICANT_PROFILES


@pytest.fixture
def client(test_app_db, test_checkpointer) -> Generator[TestClient, None, None]:
    app = create_app(scheduler_enabled=False)
    app.dependency_overrides[get_db] = lambda: test_app_db
    app.dependency_overrides[get_checkpointer] = lambda: test_checkpointer
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def submit(client: TestClient, key: str) -> str:
    p = APPLICANT_PROFILES[key]
    res = client.post("/api/v1/applications", json={
        "applicant_name": p["applicant_name"], "ssn_last_4": p["ssn_last_4"],
        "stated_annual_income": p["stated_annual_income"], "loan_amount_requested": p["loan_amount_requested"],
        "loan_purpose": p["loan_purpose"], "fcra_consent": True,
    })
    return res.json()["application"]["application_id"]


class TestMainStatusRollup:
    @pytest.mark.parametrize("raw,expected", [
        ("NEW", "new"), ("PENDING_PROCESSING", "pending processing"), ("PENDING_REVIEW", "pending review"),
        ("AUTO_APPROVED", "approved"), ("HUMAN_APPROVED", "approved"),
        ("AUTO_DENIED", "rejected"), ("HUMAN_DENIED", "rejected"), ("REJECTED_NO_CONSENT", "rejected"),
    ])
    def test_every_status_rolls_up(self, raw, expected):
        assert main_status_for(raw) == expected

    def test_status_summary_endpoint(self, client):
        for key in ("MRS_SOME_NAME", "MR_SOME_NAME", "MS_SOME_NAME"):
            submit(client, key)
        client.post("/api/v1/workflows/run", params={"sync": "true"}, json={})
        escalated = [a for a in client.get("/api/v1/applications", params={"status": "pending_review"}).json()][0]
        client.post(f"/api/v1/applications/{escalated['application_id']}/decision", json={"decision": "DENY", "notes": "Income unverifiable."})

        groups = {g["status"]: g for g in client.get("/api/v1/admin/status-summary").json()}
        assert list(groups) == ["new", "pending processing", "pending review", "approved", "rejected"]
        assert groups["approved"]["count"] == 1 and groups["approved"]["sub_statuses"] == {"AUTO_APPROVED": 1}
        assert groups["rejected"]["count"] == 2
        assert groups["rejected"]["sub_statuses"] == {"AUTO_DENIED": 1, "HUMAN_DENIED": 1}
        assert groups["new"]["count"] == 0 and groups["pending review"]["count"] == 0


class TestScheduleConfiguration:
    def test_defaults(self, client):
        cfg = client.get("/api/v1/admin/scheduler/config").json()
        assert cfg == {"interval_minutes": 2, "scheduler_enabled": True, "updated_at": None, "updated_by": None}

    def test_update_and_persist(self, client, test_app_db):
        res = client.put("/api/v1/admin/scheduler/config", json={"interval_minutes": 15, "updated_by": "Ops Admin"})
        assert res.status_code == 200
        cfg = res.json()
        assert cfg["interval_minutes"] == 15 and cfg["scheduler_enabled"] is True and cfg["updated_by"] == "Ops Admin"
        assert cfg["updated_at"]
        # Persisted in the database (survives a restart), not just in memory
        assert test_app_db.get_setting("initiate_workflow.interval_minutes") == "15"

        res = client.put("/api/v1/admin/scheduler/config", json={"scheduler_enabled": False})
        assert res.json() == {**res.json(), "interval_minutes": 15, "scheduler_enabled": False}

    def test_validation(self, client):
        assert client.put("/api/v1/admin/scheduler/config", json={"interval_minutes": 0}).status_code == 422
        assert client.put("/api/v1/admin/scheduler/config", json={"interval_minutes": 100000}).status_code == 422


class TestSchedulerHealth:
    def test_never_run(self, client):
        st = client.get("/api/v1/admin/scheduler").json()
        assert st["health"] == "NEVER_RUN" and st["last_run"] is None
        assert st["config"]["interval_minutes"] == 2

    def test_healthy_after_cli_run(self, client):
        submit(client, "MRS_SOME_NAME")
        client.post("/api/v1/workflows/run", params={"trigger": "CLI"}, json={})
        st = client.get("/api/v1/admin/scheduler").json()
        assert st["health"] == "HEALTHY"
        assert st["last_run"]["trigger_source"] == "CLI" and st["last_run"]["status"] == "COMPLETED"
        assert st["seconds_since_last_run"] is not None and st["next_due_at"]
        runs = client.get("/api/v1/admin/workflow-runs").json()
        assert len(runs) == 1 and runs[0]["approved"] == 1

    def test_overdue_detection(self, client, test_app_db):
        run = test_app_db.create_workflow_run("CRON")
        test_app_db.update_workflow_run(run["id"], {"status": "COMPLETED"})
        later = datetime.strptime(run["requested_at"], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=2)
        assert sched.scheduler_status(test_app_db, now=later).health == "HEALTHY"          # 2 min  <= 1.5 x 2
        assert sched.scheduler_status(test_app_db, now=later + timedelta(minutes=2)).health == "OVERDUE"  # 4 min > 3
        assert "Check the scheduler" in sched.scheduler_status(test_app_db, now=later + timedelta(minutes=2)).message

    def test_run_history_defaults_to_latest_ten(self, client, test_app_db):
        for i in range(12):
            run = test_app_db.create_workflow_run("CRON")
            test_app_db.update_workflow_run(run["id"], {"status": "COMPLETED"})
        runs = client.get("/api/v1/admin/workflow-runs").json()
        assert len(runs) == 10
        assert [r["id"] for r in runs] == list(range(12, 2, -1))  # newest first
        assert len(client.get("/api/v1/admin/workflow-runs", params={"limit": 3}).json()) == 3

    def test_running_state(self, client, test_app_db):
        test_app_db.create_workflow_run("API")  # REQUESTED, never finished
        assert client.get("/api/v1/admin/scheduler").json()["health"] == "RUNNING"


class TestBuiltInScheduler:
    def test_run_if_due_fires_then_waits_for_interval(self, client, test_app_db, test_checkpointer):
        submit(client, "MRS_SOME_NAME")
        first = sched.run_if_due(test_app_db, test_checkpointer)
        assert first and first["trigger_source"] == "SCHEDULER" and first["status"] == "COMPLETED"
        assert first["approved"] == 1

        # Within the interval -> nothing fires
        assert sched.run_if_due(test_app_db, test_checkpointer) is None
        # Once the interval has elapsed -> fires again (nothing new to process, but the run is logged)
        later = datetime.strptime(first["requested_at"], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=2, seconds=1)
        second = sched.run_if_due(test_app_db, test_checkpointer, now=later)
        assert second and second["id"] == first["id"] + 1 and second["found"] == 0

    def test_disabled_scheduler_never_fires(self, client, test_app_db, test_checkpointer):
        client.put("/api/v1/admin/scheduler/config", json={"scheduler_enabled": False})
        submit(client, "MRS_SOME_NAME")
        assert sched.run_if_due(test_app_db, test_checkpointer) is None
        assert client.get("/api/v1/admin/workflow-runs").json() == []

    def test_configured_interval_is_respected(self, client, test_app_db, test_checkpointer):
        client.put("/api/v1/admin/scheduler/config", json={"interval_minutes": 10})
        first = sched.run_if_due(test_app_db, test_checkpointer)
        base = datetime.strptime(first["requested_at"], "%Y-%m-%d %H:%M:%S")
        assert sched.run_if_due(test_app_db, test_checkpointer, now=base + timedelta(minutes=9)) is None
        assert sched.run_if_due(test_app_db, test_checkpointer, now=base + timedelta(minutes=10)) is not None

    def test_lifespan_starts_and_stops_scheduler_task(self, test_app_db, test_checkpointer, monkeypatch):
        """With the scheduler enabled, the loop runs on startup and is shut down cleanly on exit."""
        import api.main as main_mod
        ticks = []

        def fake_run_if_due(db, cp, now=None):
            ticks.append(1)
            return None
        monkeypatch.setattr(sched, "run_if_due", fake_run_if_due)
        monkeypatch.setattr("api.dependencies.get_db", lambda: test_app_db)
        monkeypatch.setattr("api.dependencies.get_checkpointer", lambda: test_checkpointer)
        app = main_mod.create_app(scheduler_enabled=True)
        with TestClient(app) as c:
            assert c.get("/api/v1/health").status_code == 200
        assert ticks  # at least one scheduler tick executed before shutdown
