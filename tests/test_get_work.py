"""
test_get_work.py
"Get Work" (POST /api/v1/applications/next-for-review): the server hands an underwriter the next highest-priority
PENDING_REVIEW file — one they already hold first, otherwise the oldest one nobody else has open — checked out to them
atomically, so concurrent reviewers never receive the same file. Real accounts and sessions (no overrides).
"""
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from api.schemas import UserIdentity
from api.services import checkout as checkout_service
from loan_agent.auth import Principal, Role
from loan_agent.db import ApplicationDatabase
from tests.conftest import bearer_headers, create_user_with_password
from tests.test_auth import make_app
from tests.test_queue_filters import record

PASSWORD = "fake-password"
NOW = datetime(2026, 1, 9, 9, 0, 0)


def reviewer(user_id: str, tab: Optional[str] = None) -> UserIdentity:
    principal = Principal(user_id=user_id, display_name=user_id.title(), role=Role.UNDERWRITER)
    return UserIdentity.from_principal(principal, tab_session=tab or str(uuid.uuid4()))


@pytest.fixture
def queue(test_app_db):
    """Three pending files, inserted newest first; plus one NEW and one decided file that must never be handed out."""
    for r in (
        record("APP-P3", "2026-01-07 09:00:00", name="Ms. Third Some Name"),
        record("APP-P2", "2026-01-06 09:00:00", name="Mr. Second Some Name"),
        record("APP-P1", "2026-01-05 09:00:00", name="Mrs. First Some Name"),
        record("APP-NEW", "2026-01-01 09:00:00", status="NEW"),
        record("APP-DONE", "2026-01-01 09:00:00", status="AUTO_APPROVED"),
    ):
        test_app_db.insert_new_application(r, actor="TEST")
    return test_app_db


class TestPriority:
    def test_oldest_pending_file_first(self, queue):
        info = checkout_service.checkout_next(queue, reviewer("uw.one"), now=NOW)
        assert info.application_id == "APP-P1" and info.held_by_you

    def test_files_open_with_someone_else_are_skipped(self, queue):
        checkout_service.checkout(queue, "APP-P1", reviewer("uw.two"), now=NOW)
        assert checkout_service.checkout_next(queue, reviewer("uw.one"), now=NOW).application_id == "APP-P2"

    def test_successive_reviewers_get_successive_files(self, queue):
        got = [checkout_service.checkout_next(queue, reviewer(f"uw.{n}"), now=NOW).application_id for n in "abc"]
        assert got == ["APP-P1", "APP-P2", "APP-P3"]
        assert checkout_service.checkout_next(queue, reviewer("uw.d"), now=NOW) is None  # everything is taken

    def test_a_file_the_reviewer_already_holds_comes_back_first(self, queue):
        """No hoarding: asking again (e.g. after a reload) returns the held file, not a second one."""
        first_uw = reviewer("uw.one")
        checkout_service.checkout(queue, "APP-P3", first_uw, now=NOW)  # cherry-picked the newest
        assert checkout_service.checkout_next(queue, first_uw, now=NOW).application_id == "APP-P3"
        assert checkout_service.checkout_next(queue, first_uw, now=NOW).application_id == "APP-P3"
        assert queue.get_checkout("APP-P1") is None

    def test_same_user_in_another_tab_is_another_reviewer(self, queue):
        first_tab, second_tab = reviewer("uw.one", "tab-1"), reviewer("uw.one", "tab-2")
        assert checkout_service.checkout_next(queue, first_tab, now=NOW).application_id == "APP-P1"
        assert checkout_service.checkout_next(queue, second_tab, now=NOW).application_id == "APP-P2"

    def test_a_lapsed_checkout_is_available_again(self, queue):
        checkout_service.checkout(queue, "APP-P1", reviewer("uw.two"), now=NOW - timedelta(hours=1))
        assert checkout_service.checkout_next(queue, reviewer("uw.one"), now=NOW).application_id == "APP-P1"

    def test_a_determination_in_flight_is_skipped(self, queue):
        second_uw = reviewer("uw.two")
        checkout_service.begin_decision(queue, "APP-P1", second_uw, now=NOW)  # ACTIVE -> DECIDING
        assert checkout_service.checkout_next(queue, reviewer("uw.one"), now=NOW).application_id == "APP-P2"

    def test_many_files_open_with_others_do_not_hide_an_available_one(self, test_app_db, monkeypatch):
        """Files other reviewers hold are excluded by the query itself, so they never use up the candidate batches."""
        monkeypatch.setattr(checkout_service, "GET_WORK_BATCH", 2)
        for i in range(8):
            test_app_db.insert_new_application(record(f"APP-{i}", f"2026-01-05 09:0{i}:00"), actor="TEST")
        for i in range(7):  # the seven oldest are open with other reviewers (more than BATCH x ROUNDS)
            checkout_service.checkout(test_app_db, f"APP-{i}", reviewer(f"uw.other{i}"), now=NOW)
        assert checkout_service.checkout_next(test_app_db, reviewer("uw.one"), now=NOW).application_id == "APP-7"

    def test_empty_queue(self, test_app_db):
        assert checkout_service.checkout_next(test_app_db, reviewer("uw.one"), now=NOW) is None

    def test_audit_trail_notes_the_assignment(self, queue):
        checkout_service.checkout_next(queue, reviewer("uw.one"), now=NOW)
        event = [e for e in queue.get_audit_trail("APP-P1") if e["event_type"] == "CHECKOUT_ACQUIRED"][-1]
        assert "via Get Work" in event["details"]


class TestConcurrency:
    def test_concurrent_reviewers_on_separate_connections_never_share_a_file(self, tmp_path):
        """
        Two ApplicationDatabase instances on one file stand in for two API worker processes (independent in-process
        locks): only the SQL itself can keep them from handing out the same file.
        """
        path = tmp_path / "shared.db"
        seed = ApplicationDatabase(db_path=path)
        for i in range(12):
            seed.insert_new_application(record(f"APP-{i:02d}", f"2026-01-05 09:{i:02d}:00"), actor="TEST")
        seed.close()
        dbs = [ApplicationDatabase(db_path=path) for _ in range(2)]
        for db in dbs:
            db.conn.execute("PRAGMA busy_timeout = 5000")
        results, barrier = {}, threading.Barrier(6)

        # six reviewers each take one file and keep it: nobody may receive a file twice
        def work_and_keep(index: int):
            barrier.wait()
            info = checkout_service.checkout_next(dbs[index % 2], reviewer(f"uw.{index}"), now=NOW)
            results[index] = info.application_id if info else None

        threads = [threading.Thread(target=work_and_keep, args=(i,)) for i in range(6)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            handed_out = [v for v in results.values() if v]
            assert len(handed_out) == 6 and len(set(handed_out)) == 6, results
            assert sorted(handed_out) == [f"APP-{i:02d}" for i in range(6)]  # and they are the six oldest
        finally:
            for db in dbs:
                db.close()


class TestEndpoint:
    @pytest.fixture
    def api(self, queue, test_checkpointer):
        app = make_app(queue, test_checkpointer)
        for user_id, role in (("uw.one", Role.UNDERWRITER), ("uw.two", Role.UNDERWRITER),
                              ("admin.one", Role.ADMINISTRATOR), ("applicant.one", Role.APPLICANT)):
            create_user_with_password(queue, user_id, role, PASSWORD)
        with TestClient(app) as client:
            yield client
        app.dependency_overrides.clear()

    def test_hands_out_the_next_file_checked_out_to_the_caller(self, api):
        h = bearer_headers(api, "uw.one", PASSWORD)
        body = api.post("/api/v1/applications/next-for-review", headers=h).json()
        assert body["application"]["application_id"] == "APP-P1" and body["checkout"]["held_by_you"] is True
        assert api.post("/api/v1/applications/APP-P1/checkout", headers=bearer_headers(api, "uw.two", PASSWORD)
                        ).status_code == 409

    def test_decide_then_get_the_next_one(self, api):
        h = bearer_headers(api, "uw.one", PASSWORD)
        first = api.post("/api/v1/applications/next-for-review", headers=h).json()["application"]["application_id"]
        decided = api.post(f"/api/v1/applications/{first}/decision", headers=h,
                           json={"decision": "APPROVE", "notes": "Verified W-2 and pay stubs."})
        assert decided.status_code == 200
        nxt = api.post("/api/v1/applications/next-for-review", headers=h).json()["application"]["application_id"]
        assert (first, nxt) == ("APP-P1", "APP-P2")

    def test_queue_filters_do_not_apply(self, api):
        """Filters are for cherry-picking; Get Work always serves the queue in priority order."""
        h = bearer_headers(api, "uw.one", PASSWORD)
        res = api.post("/api/v1/applications/next-for-review", params={"q": "third"}, headers=h)
        assert res.json()["application"]["application_id"] == "APP-P1"

    def test_empty_queue_is_not_an_error(self, test_checkpointer):
        empty_db = ApplicationDatabase(in_memory=True)
        try:
            app = make_app(empty_db, test_checkpointer)
            create_user_with_password(empty_db, "uw.one", Role.UNDERWRITER, PASSWORD)
            client = TestClient(app)
            res = client.post("/api/v1/applications/next-for-review", headers=bearer_headers(client, "uw.one", PASSWORD))
            assert res.status_code == 200
            assert res.json()["application"] is None and res.json()["checkout"] is None
            assert "No files are waiting" in res.json()["message"]
        finally:
            empty_db.close()

    @pytest.mark.parametrize("user_id", ["admin.one", "applicant.one"])
    def test_only_underwriters(self, api, user_id):
        res = api.post("/api/v1/applications/next-for-review", headers=bearer_headers(api, user_id, PASSWORD))
        assert res.status_code == 403


class TestDeskWiring:
    def test_get_work_button_and_checkbox_gating(self, test_app_db, test_checkpointer):
        app = make_app(test_app_db, test_checkpointer)
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER, PASSWORD)
        browser = TestClient(app)
        browser.post("/api/v1/auth/login", json={"user_id": "uw.one", "password": PASSWORD})
        html = browser.get("/desk").text
        assert 'id="btn-get-work"' in html and "Get Work" in html
        assert 'id="auto-next"' not in html  # rendered by desk.js, and only for reviews assigned by Get Work
        js = browser.get("/static/js/desk.js").text
        assert "modal.mode === 'get-work' ? `" in js          # checkbox only in get-work mode
        assert "modal.mode === 'get-work' && !!($('auto-next')" in js  # auto-continue only in get-work mode
        assert "openModal(btn.getAttribute('data-review'))" in js      # queue click = cherry-pick (default mode)
