"""
Unit tests for loan_agent/db.py.
"""
from pathlib import Path
from unittest.mock import MagicMock
import os
import pytest

from loan_agent.config import DatabaseConfig
from loan_agent.db import (
    ApplicationDatabase,
    get_application_database,
    normalize_status_filter,
)


class TestNormalizeStatusFilter:
    def test_normalize_none(self):
        assert normalize_status_filter(None) is None
        assert normalize_status_filter("") is None

    def test_normalize_new(self):
        res = normalize_status_filter("new")
        assert "NEW" in res
        assert "new" in res

    def test_normalize_pending_processing(self):
        for term in ["pending processing", "pending-processing", "pending_processing", "pending"]:
            res = normalize_status_filter(term)
            assert "PENDING_PROCESSING" in res

    def test_normalize_pending_review(self):
        for term in ["pending review", "pending-review", "pending_review"]:
            res = normalize_status_filter(term)
            assert "PENDING_REVIEW" in res

    def test_normalize_approved(self):
        res = normalize_status_filter("approved")
        assert "APPROVED" in res
        assert "AUTO_APPROVED" in res

    def test_normalize_rejected(self):
        for term in ["rejected", "denied"]:
            res = normalize_status_filter(term)
            assert "REJECTED" in res
            assert "AUTO_DENIED" in res

    def test_normalize_custom_string(self):
        res = normalize_status_filter("CUSTOM_STATUS")
        assert "CUSTOM_STATUS" in res


class TestApplicationDatabase:
    def test_save_and_get_application(self, test_app_db):
        app_data = {
            "application_id": "APP-2026-101",
            "applicant_name": "Mrs. Some Name",
            "ssn_last_4": "1111",
            "stated_annual_income": 95000.0,
            "loan_amount_requested": 18000.0,
            "loan_purpose": "Home Improvement",
            "fcra_consent": True,
            "status": "NEW",
        }

        saved = test_app_db.save_application(app_data, actor="APPLICANT")
        assert saved["application_id"] == "APP-2026-101"
        assert saved["applicant_name"] == "Mrs. Some Name"
        assert saved["status"] == "NEW"
        assert saved["fcra_consent"] is True

        # Case-insensitive retrieval
        fetched = test_app_db.get_application("app-2026-101")
        assert fetched is not None
        assert fetched["application_id"] == "APP-2026-101"
        assert fetched["applicant_name"] == "Mrs. Some Name"

        # Not found lookup
        assert test_app_db.get_application("NON_EXISTENT") is None

    def test_update_application_and_audit(self, test_app_db):
        app_data = {
            "application_id": "APP-2026-102",
            "applicant_name": "Mr. Some Name",
            "ssn_last_4": "2222",
            "stated_annual_income": 80000.0,
            "loan_amount_requested": 15000.0,
            "loan_purpose": "Debt Consolidation",
            "fcra_consent": True,
            "status": "NEW",
        }
        test_app_db.save_application(app_data)

        # Update existing
        success = test_app_db.update_application(
            "app-2026-102",
            {"status": "AUTO_APPROVED", "adjudication": {"decision": "AUTO_APPROVED", "officer": "AI Agent"}},
            actor="WORKFLOW_ENGINE",
            event_type="WORKFLOW_APPROVED",
            details="Underwriting complete",
        )
        assert success is True

        updated = test_app_db.get_application("APP-2026-102")
        assert updated["status"] == "AUTO_APPROVED"
        assert updated["adjudication"]["decision"] == "AUTO_APPROVED"

        # Update non-existent
        assert test_app_db.update_application("NON_EXISTENT", {"status": "AUTO_APPROVED"}) is False

        # Audit trail checks
        trail = test_app_db.get_audit_trail("app-2026-102")
        assert len(trail) >= 2
        assert trail[0]["event_type"] == "APPLICATION_SUBMITTED"
        assert trail[1]["event_type"] == "WORKFLOW_APPROVED"
        assert trail[1]["actor"] == "WORKFLOW_ENGINE"

    def test_list_and_count_applications_filtering(self, test_app_db):
        apps = [
            {"application_id": "APP-001", "applicant_name": "User 1", "ssn_last_4": "1111", "stated_annual_income": 50000.0, "loan_amount_requested": 10000.0, "loan_purpose": "Other", "fcra_consent": True, "status": "NEW"},
            {"application_id": "APP-002", "applicant_name": "User 2", "ssn_last_4": "2222", "stated_annual_income": 60000.0, "loan_amount_requested": 12000.0, "loan_purpose": "Other", "fcra_consent": True, "status": "PENDING_REVIEW"},
            {"application_id": "APP-003", "applicant_name": "User 3", "ssn_last_4": "3333", "stated_annual_income": 70000.0, "loan_amount_requested": 15000.0, "loan_purpose": "Other", "fcra_consent": True, "status": "AUTO_APPROVED"},
            {"application_id": "APP-004", "applicant_name": "User 4", "ssn_last_4": "4444", "stated_annual_income": 80000.0, "loan_amount_requested": 20000.0, "loan_purpose": "Other", "fcra_consent": True, "status": "AUTO_DENIED"},
        ]
        for a in apps:
            test_app_db.save_application(a)

        assert test_app_db.count_applications() == 4
        assert len(test_app_db.list_applications()) == 4

        # Filter by NEW
        assert test_app_db.count_applications("new") == 1
        assert len(test_app_db.list_applications("new")) == 1

        # Filter by PENDING_REVIEW variations
        assert test_app_db.count_applications("pending review") == 1
        assert test_app_db.count_applications("pending-review") == 1
        assert test_app_db.count_applications("pending_review") == 1

        # Filter by APPROVED
        assert test_app_db.count_applications("approved") == 1

        # Filter by REJECTED
        assert test_app_db.count_applications("rejected") == 1

    def test_file_based_sqlite_database(self, tmp_path):
        db_file = tmp_path / "sub" / "file_app.db"
        db = ApplicationDatabase(db_path=db_file)
        db.save_application({
            "application_id": "APP-FILE-01",
            "applicant_name": "File Tester",
            "ssn_last_4": "9999",
            "stated_annual_income": 90000.0,
            "loan_amount_requested": 25000.0,
            "loan_purpose": "Purchase",
            "fcra_consent": True,
            "status": "NEW",
        })
        assert db.get_application("APP-FILE-01") is not None
        assert db.count_applications() == 1
        db.close()

    def test_get_application_database_factory(self):
        db1 = get_application_database(in_memory=True)
        assert db1.in_memory is True

        db2 = get_application_database(in_memory=True, force_new=True)
        assert db2.in_memory is True

    def test_postgres_backend_adapter_mock(self, monkeypatch):
        # Test postgres schema creation and query execution paths via mock
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"count": 3}
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        cfg = DatabaseConfig(
            mode="remote",
            local_app_db_path=Path("dummy.db"),
            local_langgraph_db_path=Path("dummy_lg.db"),
            remote_app_db_url="postgresql://localhost:5432/app",
            remote_langgraph_db_url="postgresql://localhost:5432/lg",
        )

        # Patch psycopg
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        monkeypatch.setattr("psycopg.connect", mock_psycopg.connect)

        db = ApplicationDatabase(config=cfg, in_memory=False)
        assert db.backend == "postgres"
        assert db.count_applications() == 3

    def test_add_audit_event_on_postgres_does_not_use_lastrowid(self, monkeypatch):
        """
        Regression test: psycopg3 cursors have no `lastrowid` attribute at all (unlike sqlite3's), so
        add_audit_event() must fetch the id via `RETURNING id`, like create_workflow_run / create_intake_batch
        already do. A plain MagicMock would auto-create `.lastrowid` and hide this bug, so the fake cursor below
        mirrors the real psycopg3 cursor instead: no such attribute.
        """

        class NoLastrowidCursor:
            def __init__(self):
                self.queries = []

            def execute(self, query, params=None):
                self.queries.append(query)

            def fetchone(self):
                return {"id": 42}

            def fetchall(self):  # schema initialization (decided_by backfill) lists rows
                return []

        mock_cursor = NoLastrowidCursor()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        cfg = DatabaseConfig(
            mode="remote", local_app_db_path=Path("dummy.db"), local_langgraph_db_path=Path("dummy_lg.db"),
            remote_app_db_url="postgresql://localhost:5432/app", remote_langgraph_db_url="postgresql://localhost:5432/lg",
        )
        monkeypatch.setattr("psycopg.connect", MagicMock(return_value=mock_conn))
        db = ApplicationDatabase(config=cfg, in_memory=False)

        event_id = db.add_audit_event("APP-1", "TEST_EVENT", actor="tester")
        assert event_id == 42
        assert "RETURNING id" in mock_cursor.queries[-1]

    def test_serialization_and_deserialization_edge_cases(self, test_app_db):
        assert test_app_db._serialize("already_string") == "already_string"
        assert test_app_db._serialize(None) is None
        assert test_app_db._deserialize({"key": "val"}) == {"key": "val"}
        assert test_app_db._deserialize("not-valid-json") == "not-valid-json"

    def test_local_config_file_branch(self, tmp_path):
        db_path = tmp_path / "local_test_file.db"
        cfg = DatabaseConfig(
            mode="local",
            local_app_db_path=db_path,
            local_langgraph_db_path=tmp_path / "lg.db",
            remote_app_db_url="postgresql://localhost/app",
            remote_langgraph_db_url="postgresql://localhost/lg",
        )
        db = ApplicationDatabase(config=cfg, in_memory=False)
        assert db.backend == "sqlite"
        assert db_path.exists()
        db.close()

    def test_global_singleton_initialization(self, tmp_path):
        import loan_agent.db as db_mod
        db_mod._GLOBAL_APP_DB = None

        cfg = DatabaseConfig(
            mode="local",
            local_app_db_path=tmp_path / "global_app.db",
            local_langgraph_db_path=tmp_path / "global_lg.db",
            remote_app_db_url="postgresql://localhost/app",
            remote_langgraph_db_url="postgresql://localhost/lg",
        )
        instance = db_mod.get_application_database(config=cfg, in_memory=False)
        assert instance is not None
        assert db_mod._GLOBAL_APP_DB is instance
        instance.close()


class TestApplicationOwnership:
    """submitted_by (the self-service Loan Applicant's user ID) and GET /api/v1/applications/mine's backing query."""

    def test_submitted_by_round_trips_and_survives_updates(self, test_app_db):
        app_data = {
            "application_id": "APP-OWN-1", "applicant_name": "Mrs. Some Name", "ssn_last_4": "1111",
            "stated_annual_income": 90000.0, "loan_amount_requested": 10000.0, "loan_purpose": "Other",
            "fcra_consent": True, "status": "NEW", "submitted_by": "some.name",
        }
        saved = test_app_db.insert_new_application(app_data, actor="some.name")
        assert saved["submitted_by"] == "some.name"

        # ownership is never part of an update payload, and survives regardless
        test_app_db.update_application("APP-OWN-1", {"status": "PENDING_PROCESSING"}, actor="WORKFLOW_ENGINE")
        assert test_app_db.get_application("APP-OWN-1")["submitted_by"] == "some.name"

    def test_submitted_by_defaults_to_null(self, test_app_db):
        app_data = {
            "application_id": "APP-OWN-2", "applicant_name": "Partner Borrower", "ssn_last_4": "2222",
            "stated_annual_income": 50000.0, "loan_amount_requested": 5000.0, "loan_purpose": "Other",
            "fcra_consent": True, "status": "NEW",
        }
        saved = test_app_db.insert_new_application(app_data, actor="PARTNER:ACME")
        assert saved["submitted_by"] is None

    def test_list_applications_for_user_is_scoped_and_ordered(self, test_app_db):
        for i, (owner, status_) in enumerate([("applicant.one", "NEW"), ("applicant.two", "NEW"), ("applicant.one", "AUTO_APPROVED")]):
            test_app_db.insert_new_application(
                {"application_id": f"APP-U-{i}", "applicant_name": "X", "ssn_last_4": "0000",
                 "stated_annual_income": 1.0, "loan_amount_requested": 1.0, "loan_purpose": "Other",
                 "fcra_consent": True, "status": status_, "submitted_by": owner},
                actor=owner,
            )
        first_apps = test_app_db.list_applications_for_user("applicant.one")
        assert {a["application_id"] for a in first_apps} == {"APP-U-0", "APP-U-2"}
        assert test_app_db.list_applications_for_user("applicant.one", status="approved") == \
            [a for a in first_apps if a["application_id"] == "APP-U-2"]
        assert test_app_db.list_applications_for_user("nobody") == []

    def test_migration_adds_the_column_to_a_pre_existing_database(self, tmp_path):
        """A database created before ownership existed gains the column (and stays usable) on the next open."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE loan_applications (
                application_id TEXT PRIMARY KEY, applicant_name TEXT NOT NULL, ssn_last_4 TEXT NOT NULL,
                stated_annual_income REAL NOT NULL, loan_amount_requested REAL NOT NULL, loan_purpose TEXT NOT NULL,
                fcra_consent INTEGER NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, adjudication TEXT, graph_state TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO loan_applications (application_id, applicant_name, ssn_last_4, stated_annual_income, "
            "loan_amount_requested, loan_purpose, fcra_consent, status, submitted_at, updated_at) "
            "VALUES ('APP-OLD-1','Legacy','0000',1,1,'Other',1,'NEW','2025-01-01 00:00:00','2025-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        db = ApplicationDatabase(db_path=db_path)  # initialize_schema() runs the migration
        try:
            record = db.get_application("APP-OLD-1")
            assert record["submitted_by"] is None
            assert db.list_applications_for_user("applicant.one") == []  # the old row belongs to nobody, not an error
            # re-opening (column already present) is a no-op, not an error
            ApplicationDatabase(db_path=db_path).close()
        finally:
            db.close()

