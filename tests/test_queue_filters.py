"""
test_queue_filters.py
Underwriter Desk queues: the pending review queue is worked OLDEST first; both queues filter by loan application
metadata (name / reference, purpose, amount range, submission dates); the adjudicated archive can be narrowed to the
determinations recorded by the signed-in user. Service-level unit tests plus the API with real accounts (no overrides).
"""
from datetime import date
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from api.services.applications import ApplicationFilters, query_applications
from loan_agent.auth import Role
from tests.conftest import bearer_headers, create_user_with_password
from tests.test_auth import make_app

PASSWORD = "Correct-Horse-9"


def record(app_id: str, submitted_at: str, name: str = "Borrower", purpose: str = "Other", amount: float = 10000.0,
           status: str = "PENDING_REVIEW", officer_user_id: Optional[str] = None, officer: Optional[str] = None) -> Dict:
    adjudication = None
    if officer_user_id or officer:
        adjudication = {"decision": status, "officer": officer or "Someone", "officer_user_id": officer_user_id,
                        "adjudicated_at": submitted_at}
    return {
        "application_id": app_id, "applicant_name": name, "ssn_last_4": "1234", "stated_annual_income": 90000.0,
        "loan_amount_requested": amount, "loan_purpose": purpose, "fcra_consent": True, "status": status,
        "submitted_at": submitted_at, "adjudication": adjudication,
    }


# ===========================================================================
# Database-level filtering & ordering (api.services.applications.query_applications -> one SQL query)
# ===========================================================================
class TestDatabaseQuery:
    RECORDS = [
        record("APP-B", "2026-01-06 09:00:00", name="Mrs. Some Name", purpose="Home Improvement", amount=15000),
        record("APP-A", "2026-01-05 09:00:00", name="Mr. Some Name", purpose="Debt Consolidation", amount=20000),
        record("APP-C", "2026-01-07 09:00:00", name="Ms. Some Name", purpose="Medical Expenses", amount=35000),
        record("APP-D", "2026-01-06 09:00:00", name="Dr. Other", purpose="Home Improvement", amount=5000),
    ]

    @pytest.fixture
    def db(self, test_app_db):
        for r in self.RECORDS:
            test_app_db.insert_new_application(dict(r), actor="TEST")
        return test_app_db

    def ids(self, db, filters=None, sort="oldest", status="pending_review") -> List[str]:
        return [r["application_id"] for r in query_applications(db, status, filters, sort)]

    def test_oldest_first_with_a_stable_tie_break(self, db):
        assert self.ids(db) == ["APP-A", "APP-B", "APP-D", "APP-C"]

    def test_newest_first_is_the_exact_reverse(self, db):
        assert self.ids(db, sort="newest") == ["APP-C", "APP-D", "APP-B", "APP-A"]

    def test_unknown_sort_is_refused(self, db):
        with pytest.raises(ValueError):
            query_applications(db, sort="random")

    @pytest.mark.parametrize("q,expected", [
        ("some name", ["APP-A", "APP-B", "APP-C"]),
        ("MRS.", ["APP-B"]),
        ("app-d", ["APP-D"]),          # reference number, case-insensitive
        ("  other ", ["APP-D"]),       # surrounding whitespace ignored
        ("nobody", []),
    ])
    def test_search_by_name_or_reference(self, db, q, expected):
        assert self.ids(db, ApplicationFilters(q=q)) == expected

    def test_search_treats_sql_wildcards_literally(self, db):
        db.insert_new_application(dict(record("APP-E", "2026-01-08 09:00:00", name="100% Legit_Name")), actor="TEST")
        assert self.ids(db, ApplicationFilters(q="%")) == ["APP-E"]
        assert self.ids(db, ApplicationFilters(q="_")) == ["APP-E"]
        assert self.ids(db, ApplicationFilters(q="t_n")) == ["APP-E"]   # "_" is not "any character"
        assert self.ids(db, ApplicationFilters(q="!")) == []

    def test_loan_purpose_is_exact_and_case_insensitive(self, db):
        assert self.ids(db, ApplicationFilters(loan_purpose="home improvement")) == ["APP-B", "APP-D"]

    def test_amount_bounds_are_inclusive(self, db):
        assert self.ids(db, ApplicationFilters(min_amount=15000, max_amount=20000)) == ["APP-A", "APP-B"]

    def test_submission_dates_are_inclusive_calendar_days(self, db):
        day = date(2026, 1, 6)
        assert self.ids(db, ApplicationFilters(submitted_from=day, submitted_to=day)) == ["APP-B", "APP-D"]
        db.insert_new_application(dict(record("APP-LATE", "2026-01-06 23:59:59")), actor="TEST")
        assert "APP-LATE" in self.ids(db, ApplicationFilters(submitted_to=day))

    def test_criteria_combine_with_and(self, db):
        filters = ApplicationFilters(q="some", loan_purpose="Home Improvement", max_amount=16000)
        assert self.ids(db, filters) == ["APP-B"]

    def test_stage_and_outcome_filters(self, db):
        db.insert_new_application(dict(record("APP-X", "2026-01-01 09:00:00", status="AUTO_DENIED")), actor="TEST")
        db.insert_new_application(dict(record("APP-Y", "2026-01-02 09:00:00", status="NEW")), actor="TEST")
        assert self.ids(db, status="adjudicated") == ["APP-X"]
        assert self.ids(db, status="rejected") == ["APP-X"]
        assert self.ids(db, status="new") == ["APP-Y"]
        assert len(self.ids(db, status="all")) == 6

    def test_decided_by_matches_the_user_id_not_the_display_name(self, test_app_db):
        rows = [
            record("APP-1", "2026-01-05 09:00:00", status="HUMAN_APPROVED", officer_user_id="uw.one", officer="Mr. Some Underwriter"),
            record("APP-2", "2026-01-05 10:00:00", status="HUMAN_DENIED", officer_user_id="uw.two", officer="Mr. Some Underwriter"),
            record("APP-3", "2026-01-05 11:00:00", status="AUTO_APPROVED", officer="AI Agent (Policy Underwriter)"),
            record("APP-4", "2026-01-05 12:00:00", status="HUMAN_APPROVED", officer="Mr. Some Underwriter"),  # legacy: no user ID
        ]
        for r in rows:
            test_app_db.insert_new_application(r, actor="TEST")
        assert self.ids(test_app_db, ApplicationFilters(decided_by="uw.one"), status="all") == ["APP-1"]
        assert self.ids(test_app_db, ApplicationFilters(decided_by="uw.two"), status="all") == ["APP-2"]

    @pytest.mark.parametrize("filters", [
        ApplicationFilters(min_amount=20000, max_amount=10000),
        ApplicationFilters(submitted_from=date(2026, 2, 1), submitted_to=date(2026, 1, 1)),
    ])
    def test_contradictory_ranges_are_refused(self, filters):
        with pytest.raises(ValueError):
            filters.validate()


class TestFiltersRunInTheDatabase:
    """The filters are part of the SQL statement — rows that do not match are never read into memory."""

    def test_every_criterion_is_in_the_sql_and_only_matching_rows_are_fetched(self, test_app_db):
        for i in range(30):
            test_app_db.insert_new_application(
                dict(record(f"APP-{i:03d}", f"2026-01-{1 + i % 28:02d} 09:00:00", name=f"Borrower {i}",
                            purpose="Medical Expenses" if i % 3 else "Home Improvement", amount=1000 * (i + 1))),
                actor="TEST",
            )
        statements: List[str] = []
        test_app_db.conn.set_trace_callback(statements.append)
        try:
            rows = query_applications(
                test_app_db, "pending_review",
                ApplicationFilters(q="borrower 1", loan_purpose="Home Improvement", min_amount=5000,
                                   max_amount=25000, submitted_from=date(2026, 1, 2), submitted_to=date(2026, 1, 28),
                                   decided_by=None),
                sort="oldest",
            )
        finally:
            test_app_db.conn.set_trace_callback(None)
        selects = [q for q in statements if q.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 1, statements  # one query does it all
        sql = selects[0]
        for fragment in ("UPPER(status) IN", "LOWER(applicant_name) LIKE", "LOWER(loan_purpose) =",
                         "loan_amount_requested >=", "loan_amount_requested <=", "submitted_at >=", "submitted_at <",
                         "ORDER BY submitted_at ASC, application_id ASC"):
            assert fragment in sql, fragment
        assert [r["application_id"] for r in rows] == ["APP-012", "APP-015", "APP-018"]

    def test_the_listing_endpoint_never_loads_the_whole_table(self, test_app_db, test_checkpointer, monkeypatch):
        """The old path (list everything, then filter in Python) must not be used by the queues."""
        from loan_agent.db import ApplicationDatabase

        def forbidden(*args, **kwargs):
            raise AssertionError("queue listing loaded every application instead of querying with filters")

        app = make_app(test_app_db, test_checkpointer)
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER, PASSWORD)
        test_app_db.insert_new_application(dict(record("APP-P1", "2026-01-05 09:00:00")), actor="TEST")
        client = TestClient(app)
        headers = bearer_headers(client, "uw.one", PASSWORD)
        monkeypatch.setattr(ApplicationDatabase, "list_applications", forbidden)
        for params in ({"status": "pending_review", "sort": "oldest", "q": "borrower"},
                       {"status": "adjudicated", "decided_by_me": "true"}, {}):
            assert client.get("/api/v1/applications", params=params, headers=headers).status_code == 200
        assert client.get("/api/v1/stats", headers=headers).status_code == 200  # GROUP BY, not a full load


class TestDecidedByColumn:
    def test_kept_in_step_with_the_determination(self, test_app_db):
        test_app_db.insert_new_application(dict(record("APP-1", "2026-01-05 09:00:00")), actor="TEST")
        assert test_app_db.get_application("APP-1")["decided_by"] is None
        test_app_db.update_application("APP-1", {"status": "HUMAN_APPROVED", "adjudication": {
            "decision": "HUMAN_APPROVED", "officer": "Mr. Some Underwriter", "officer_user_id": "uw.one"}}, actor="TEST")
        assert test_app_db.get_application("APP-1")["decided_by"] == "uw.one"

    def test_backfilled_for_determinations_recorded_before_the_column_existed(self, tmp_path):
        import json
        import sqlite3

        from loan_agent.db import ApplicationDatabase

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE loan_applications (application_id TEXT PRIMARY KEY, applicant_name TEXT NOT NULL, "
            "ssn_last_4 TEXT NOT NULL, stated_annual_income REAL NOT NULL, loan_amount_requested REAL NOT NULL, "
            "loan_purpose TEXT NOT NULL, fcra_consent INTEGER NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, adjudication TEXT, graph_state TEXT)"
        )
        for app_id, adjudication in (("APP-H", {"officer": "U", "officer_user_id": "uw.one"}),
                                     ("APP-A", {"officer": "AI Agent"}), ("APP-N", None)):
            conn.execute(
                "INSERT INTO loan_applications VALUES (?, 'X', '0000', 1, 1, 'Other', 1, 'HUMAN_APPROVED', "
                "'2025-01-01 00:00:00', '2025-01-01 00:00:00', ?, NULL)",
                (app_id, json.dumps(adjudication) if adjudication else None),
            )
        conn.commit()
        conn.close()
        db = ApplicationDatabase(db_path=path)
        try:
            assert {a: db.get_application(a)["decided_by"] for a in ("APP-H", "APP-A", "APP-N")} == \
                {"APP-H": "uw.one", "APP-A": None, "APP-N": None}
            assert [r["application_id"] for r in query_applications(db, "all", ApplicationFilters(decided_by="uw.one"))] \
                == ["APP-H"]
        finally:
            db.close()


# ===========================================================================
# API (real accounts, bearer tokens)
# ===========================================================================
@pytest.fixture
def api(test_app_db, test_checkpointer):
    app = make_app(test_app_db, test_checkpointer)
    create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER, PASSWORD, "Mr. Some Underwriter")
    create_user_with_password(test_app_db, "uw.two", Role.UNDERWRITER, PASSWORD, "Mr. Some Underwriter")  # same display name
    create_user_with_password(test_app_db, "admin.one", Role.ADMINISTRATOR, PASSWORD, "Ms. Some Admin")
    rows = [
        # pending review, deliberately inserted newest-first (the order a naive listing would show them)
        record("APP-P3", "2026-01-07 09:00:00", name="Ms. Newest Some Name", purpose="Medical Expenses", amount=35000),
        record("APP-P2", "2026-01-06 09:00:00", name="Mr. Middle Some Name", purpose="Home Improvement", amount=15000),
        record("APP-P1", "2026-01-05 09:00:00", name="Mrs. Oldest Some Name", purpose="Home Improvement", amount=40000),
        # adjudicated
        record("APP-D1", "2026-01-02 09:00:00", name="Mr. First Approved Name", status="HUMAN_APPROVED",
               officer_user_id="uw.one", officer="Mr. Some Underwriter"),
        record("APP-D2", "2026-01-03 09:00:00", name="Ms. Second Denied Name", status="HUMAN_DENIED",
               officer_user_id="uw.two", officer="Mr. Some Underwriter"),
        record("APP-D3", "2026-01-04 09:00:00", name="Dr. Auto Some Name", status="AUTO_APPROVED", officer="AI Agent"),
    ]
    for r in rows:
        test_app_db.insert_new_application(r, actor="TEST")
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def listing(api, user_id: str, **params) -> List[str]:
    res = api.get("/api/v1/applications", params=params, headers=bearer_headers(api, user_id, PASSWORD))
    assert res.status_code == 200, res.text
    return [a["application_id"] for a in res.json()]


class TestPendingQueueOrder:
    def test_pending_review_queue_oldest_first(self, api):
        assert listing(api, "uw.one", status="pending_review", sort="oldest") == ["APP-P1", "APP-P2", "APP-P3"]

    def test_default_order_is_unchanged_for_existing_clients(self, api):
        assert listing(api, "uw.one", status="pending_review") == ["APP-P3", "APP-P2", "APP-P1"]

    def test_desk_requests_the_pending_queue_oldest_first(self, api):
        js = api.get("/static/js/desk.js").text
        assert "status: 'pending_review', sort: 'oldest'" in js


class TestMetadataFilters:
    def test_each_filter(self, api):
        base = {"status": "pending_review", "sort": "oldest"}
        assert listing(api, "uw.one", **base, q="middle") == ["APP-P2"]
        assert listing(api, "uw.one", **base, q="app-p3") == ["APP-P3"]
        assert listing(api, "uw.one", **base, loan_purpose="Home Improvement") == ["APP-P1", "APP-P2"]
        assert listing(api, "uw.one", **base, min_amount=30000) == ["APP-P1", "APP-P3"]
        assert listing(api, "uw.one", **base, max_amount=15000) == ["APP-P2"]
        assert listing(api, "uw.one", **base, submitted_from="2026-01-06") == ["APP-P2", "APP-P3"]
        assert listing(api, "uw.one", **base, submitted_to="2026-01-05") == ["APP-P1"]

    def test_filters_compose_and_keep_the_order(self, api):
        ids = listing(api, "uw.one", status="pending_review", sort="oldest",
                      loan_purpose="Home Improvement", min_amount=10000, submitted_to="2026-01-06")
        assert ids == ["APP-P1", "APP-P2"]

    def test_archive_outcome_filter(self, api):
        assert listing(api, "uw.one", status="approved", sort="oldest") == ["APP-D1", "APP-D3"]
        assert listing(api, "uw.one", status="rejected") == ["APP-D2"]

    @pytest.mark.parametrize("params", [
        {"min_amount": 20000, "max_amount": 10000},
        {"submitted_from": "2026-02-01", "submitted_to": "2026-01-01"},
        {"loan_purpose": "Yacht"},
        {"sort": "random"},
        {"submitted_from": "not-a-date"},
        {"min_amount": -1},
    ])
    def test_invalid_filters_are_422(self, api, params):
        res = api.get("/api/v1/applications", params=params, headers=bearer_headers(api, "uw.one", PASSWORD))
        assert res.status_code == 422


class TestDecidedByMe:
    def test_each_underwriter_sees_only_their_own_determinations(self, api):
        """Matched on user ID: both underwriters are displayed as 'Mr. Some Underwriter'."""
        assert listing(api, "uw.one", status="adjudicated", decided_by_me="true") == ["APP-D1"]
        assert listing(api, "uw.two", status="adjudicated", decided_by_me="true") == ["APP-D2"]

    def test_combines_with_other_filters(self, api):
        assert listing(api, "uw.one", status="adjudicated", decided_by_me="true", q="first") == ["APP-D1"]
        assert listing(api, "uw.one", status="adjudicated", decided_by_me="true", q="second") == []

    def test_automated_decisions_belong_to_nobody(self, api):
        everything = listing(api, "uw.one", status="adjudicated")
        assert "APP-D3" in everything
        assert "APP-D3" not in listing(api, "uw.one", status="adjudicated", decided_by_me="true")

    def test_administrator_has_made_no_credit_decisions(self, api):
        assert listing(api, "admin.one", status="adjudicated", decided_by_me="true") == []

    def test_the_determination_really_records_the_user_id(self, api, test_app_db):
        """End to end: a decision made through the API is found by that underwriter's 'decided by me' filter."""
        h = bearer_headers(api, "uw.two", PASSWORD)
        res = api.post("/api/v1/applications/APP-P1/decision", headers=h,
                       json={"decision": "APPROVE", "notes": "Verified W-2 and pay stubs."})
        # the seeded record has no workflow checkpoint; the runner synthesizes the final state, so it still settles
        assert res.status_code == 200, res.text
        assert "APP-P1" in listing(api, "uw.two", status="adjudicated", decided_by_me="true")
        assert "APP-P1" not in listing(api, "uw.one", status="adjudicated", decided_by_me="true")


class TestDeskFilterControls:
    def test_both_queues_have_filter_bars(self, api, test_app_db):
        browser = TestClient(api.app)
        assert browser.post("/api/v1/auth/login", json={"user_id": "uw.one", "password": PASSWORD}).status_code == 200
        html = browser.get("/desk").text
        for form in ('id="pending-filters"', 'id="archive-filters"'):
            assert form in html
        for field in ('name="q"', 'name="loan_purpose"', 'name="min_amount"', 'name="max_amount"',
                      'name="submitted_from"', 'name="submitted_to"'):
            assert html.count(field) == 2, field  # one per queue
        assert html.count('name="decided_by_me"') == 1 and "Only decisions I made" in html  # archive only
        assert html.count('name="status"') == 1  # outcome: archive only
        assert "Oldest First" in html
