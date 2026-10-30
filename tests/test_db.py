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
            "applicant_name": "Alice Morgan",
            "ssn_last_4": "1122",
            "stated_annual_income": 95000.0,
            "loan_amount_requested": 18000.0,
            "loan_purpose": "Home Improvement",
            "fcra_consent": True,
            "status": "NEW",
        }

        saved = test_app_db.save_application(app_data, actor="APPLICANT")
        assert saved["application_id"] == "APP-2026-101"
        assert saved["applicant_name"] == "Alice Morgan"
        assert saved["status"] == "NEW"
        assert saved["fcra_consent"] is True

        # Case-insensitive retrieval
        fetched = test_app_db.get_application("app-2026-101")
        assert fetched is not None
        assert fetched["application_id"] == "APP-2026-101"
        assert fetched["applicant_name"] == "Alice Morgan"

        # Not found lookup
        assert test_app_db.get_application("NON_EXISTENT") is None

    def test_update_application_and_audit(self, test_app_db):
        app_data = {
            "application_id": "APP-2026-102",
            "applicant_name": "Bob Vance",
            "ssn_last_4": "3344",
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
        db.clear_all()
        assert db.count_applications() == 0
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

