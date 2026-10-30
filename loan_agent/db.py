"""
Application Database Manager for Loan Underwriting.
Manages persistent storage for loan applications, metadata, adjudication outcomes, and audit trails.
Supports both local SQLite and remote PostgreSQL storage.
"""
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import os
import sqlite3

from loan_agent.config import DatabaseConfig, load_db_config


def normalize_status_filter(status: Optional[str]) -> Optional[List[str]]:
    """
    Normalizes status query string to matching database statuses.
    Handles hyphenated, underscored, and space-separated variations.
    """
    if not status:
        return None

    clean = status.strip().lower().replace("-", " ").replace("_", " ")

    if clean == "new":
        return ["NEW", "new"]
    elif clean in ["pending processing", "pending"]:
        return ["PENDING_PROCESSING", "pending processing", "pending_processing", "pending-processing"]
    elif clean == "pending review":
        return ["PENDING_REVIEW", "pending review", "pending-review", "pending_review"]
    elif clean == "approved":
        return ["APPROVED", "AUTO_APPROVED", "HUMAN_APPROVED", "approved"]
    elif clean in ["rejected", "denied"]:
        return ["REJECTED", "AUTO_DENIED", "REJECTED_NO_CONSENT", "HUMAN_DENIED", "rejected", "denied"]
    else:
        # Fall back to exact case-insensitive match
        return [status.strip().upper(), status.strip()]


class ApplicationDatabase:
    """Persistent storage manager for loan applications and regulatory audit trails."""

    def __init__(
        self,
        config: Optional[DatabaseConfig] = None,
        in_memory: bool = False,
        db_path: Optional[str | Path] = None,
    ):
        self.config = config or load_db_config()
        self.in_memory = in_memory

        if self.in_memory:
            self.backend = "sqlite"
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        elif db_path:
            self.backend = "sqlite"
            p = Path(db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(p), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        elif self.config.is_remote:
            self.backend = "postgres"
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(self.config.remote_app_db_url, row_factory=dict_row)
        else:
            self.backend = "sqlite"
            p = self.config.local_app_db_path
            p.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(p), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row

        self.initialize_schema()

    def initialize_schema(self) -> None:
        """Initializes tables and indexes for applications and audit trail."""
        cursor = self.conn.cursor()
        if self.backend == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS loan_applications (
                    application_id TEXT PRIMARY KEY,
                    applicant_name TEXT NOT NULL,
                    ssn_last_4 TEXT NOT NULL,
                    stated_annual_income REAL NOT NULL,
                    loan_amount_requested REAL NOT NULL,
                    loan_purpose TEXT NOT NULL,
                    fcra_consent INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    adjudication TEXT,
                    graph_state TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS loan_audit_trail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    details TEXT
                );
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_loan_apps_status ON loan_applications(status);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_loan_audit_app_id ON loan_audit_trail(application_id);"
            )
            self.conn.commit()
        else:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS loan_applications (
                    application_id TEXT PRIMARY KEY,
                    applicant_name TEXT NOT NULL,
                    ssn_last_4 TEXT NOT NULL,
                    stated_annual_income DOUBLE PRECISION NOT NULL,
                    loan_amount_requested DOUBLE PRECISION NOT NULL,
                    loan_purpose TEXT NOT NULL,
                    fcra_consent INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    adjudication TEXT,
                    graph_state TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS loan_audit_trail (
                    id SERIAL PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    details TEXT
                );
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_loan_apps_status ON loan_applications(status);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_loan_audit_app_id ON loan_audit_trail(application_id);"
            )
            self.conn.commit()

    def _serialize(self, obj: Any) -> Optional[str]:
        if obj is None:
            return None
        if isinstance(obj, str):
            return obj
        return json.dumps(obj)

    def _deserialize(self, val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return val
        try:
            return json.loads(val)
        except Exception:
            return val

    def save_application(
        self,
        app_data: Dict[str, Any],
        actor: str = "APPLICANT",
        event_type: str = "APPLICATION_SUBMITTED",
        details: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Saves a loan application and writes an audit trail entry."""
        app_id = app_data["application_id"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        existing = self.get_application(app_id)
        from_status = existing["status"] if existing else None
        to_status = app_data.get("status", "NEW")
        submitted_at = app_data.get("submitted_at") or (
            existing["submitted_at"] if existing else now_str
        )
        updated_at = now_str

        adj_json = self._serialize(app_data.get("adjudication"))
        state_json = self._serialize(app_data.get("graph_state"))
        fcra_int = 1 if app_data.get("fcra_consent") else 0

        cursor = self.conn.cursor()
        placeholder = "?" if self.backend == "sqlite" else "%s"

        if existing:
            query = f"""
                UPDATE loan_applications
                SET applicant_name = {placeholder},
                    ssn_last_4 = {placeholder},
                    stated_annual_income = {placeholder},
                    loan_amount_requested = {placeholder},
                    loan_purpose = {placeholder},
                    fcra_consent = {placeholder},
                    status = {placeholder},
                    updated_at = {placeholder},
                    adjudication = {placeholder},
                    graph_state = {placeholder}
                WHERE UPPER(application_id) = UPPER({placeholder})
            """
            cursor.execute(
                query,
                (
                    app_data["applicant_name"],
                    str(app_data.get("ssn_last_4", "")),
                    float(app_data.get("stated_annual_income", 0.0)),
                    float(app_data.get("loan_amount_requested", 0.0)),
                    app_data.get("loan_purpose", ""),
                    fcra_int,
                    to_status,
                    updated_at,
                    adj_json,
                    state_json,
                    app_id,
                ),
            )
        else:
            query = f"""
                INSERT INTO loan_applications (
                    application_id, applicant_name, ssn_last_4, stated_annual_income,
                    loan_amount_requested, loan_purpose, fcra_consent, status,
                    submitted_at, updated_at, adjudication, graph_state
                ) VALUES ({', '.join([placeholder] * 12)})
            """
            cursor.execute(
                query,
                (
                    app_id,
                    app_data["applicant_name"],
                    str(app_data.get("ssn_last_4", "")),
                    float(app_data.get("stated_annual_income", 0.0)),
                    float(app_data.get("loan_amount_requested", 0.0)),
                    app_data.get("loan_purpose", ""),
                    fcra_int,
                    to_status,
                    submitted_at,
                    updated_at,
                    adj_json,
                    state_json,
                ),
            )

        self.conn.commit()

        # Record audit event
        audit_details = details or (
            "Application created" if not existing else "Application record updated"
        )
        self.add_audit_event(
            application_id=app_id,
            event_type=event_type,
            actor=actor,
            from_status=from_status,
            to_status=to_status,
            details=audit_details,
        )

        return self.get_application(app_id)  # type: ignore

    def get_application(self, application_id: str) -> Optional[Dict[str, Any]]:
        """Case-insensitive retrieval of an application record."""
        cursor = self.conn.cursor()
        placeholder = "?" if self.backend == "sqlite" else "%s"
        query = f"SELECT * FROM loan_applications WHERE UPPER(application_id) = UPPER({placeholder})"
        cursor.execute(query, (application_id.strip(),))
        row = cursor.fetchone()
        if not row:
            return None

        data = dict(row)
        data["fcra_consent"] = bool(data["fcra_consent"])
        data["adjudication"] = self._deserialize(data.get("adjudication"))
        data["graph_state"] = self._deserialize(data.get("graph_state"))
        return data

    def update_application(
        self,
        application_id: str,
        updates: Dict[str, Any],
        actor: str = "SYSTEM",
        event_type: str = "APPLICATION_UPDATED",
        details: Optional[Any] = None,
    ) -> bool:
        """Updates specific fields on an existing application and logs an audit trail event."""
        existing = self.get_application(application_id)
        if not existing:
            return False

        from_status = existing["status"]
        to_status = updates.get("status", from_status)

        merged = {**existing, **updates}
        merged["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.save_application(
            merged,
            actor=actor,
            event_type=event_type,
            details=details or f"Status changed from {from_status} to {to_status}",
        )
        return True

    def list_applications(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Lists application records, optionally filtered by normalized status.
        Returns records ordered by submission time descending.
        """
        cursor = self.conn.cursor()
        norm_statuses = normalize_status_filter(status)
        placeholder = "?" if self.backend == "sqlite" else "%s"

        if norm_statuses:
            placeholders = ", ".join([placeholder] * len(norm_statuses))
            query = f"""
                SELECT * FROM loan_applications
                WHERE UPPER(status) IN ({placeholders})
                ORDER BY submitted_at DESC
            """
            cursor.execute(query, [s.upper() for s in norm_statuses])
        else:
            query = "SELECT * FROM loan_applications ORDER BY submitted_at DESC"
            cursor.execute(query)

        rows = cursor.fetchall()
        results = []
        for r in rows:
            data = dict(r)
            data["fcra_consent"] = bool(data["fcra_consent"])
            data["adjudication"] = self._deserialize(data.get("adjudication"))
            data["graph_state"] = self._deserialize(data.get("graph_state"))
            results.append(data)
        return results

    def count_applications(self, status: Optional[str] = None) -> int:
        """Returns the total number of applications matching the optional status filter."""
        cursor = self.conn.cursor()
        norm_statuses = normalize_status_filter(status)
        placeholder = "?" if self.backend == "sqlite" else "%s"

        if norm_statuses:
            placeholders = ", ".join([placeholder] * len(norm_statuses))
            query = f"SELECT COUNT(*) FROM loan_applications WHERE UPPER(status) IN ({placeholders})"
            cursor.execute(query, [s.upper() for s in norm_statuses])
        else:
            query = "SELECT COUNT(*) FROM loan_applications"
            cursor.execute(query)

        res = cursor.fetchone()
        if self.backend == "sqlite":
            return res[0] if res else 0
        else:
            return list(res.values())[0] if res else 0

    def add_audit_event(
        self,
        application_id: str,
        event_type: str,
        actor: str,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        details: Optional[Any] = None,
    ) -> int:
        """Appends an event to the regulatory audit trail."""
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details_str = self._serialize(details) if not isinstance(details, str) else details

        cursor = self.conn.cursor()
        placeholder = "?" if self.backend == "sqlite" else "%s"

        query = f"""
            INSERT INTO loan_audit_trail (
                application_id, timestamp, event_type, actor, from_status, to_status, details
            ) VALUES ({', '.join([placeholder] * 7)})
        """
        cursor.execute(
            query,
            (
                application_id.strip(),
                timestamp_str,
                event_type,
                actor,
                from_status,
                to_status,
                details_str,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_audit_trail(self, application_id: str) -> List[Dict[str, Any]]:
        """Retrieves chronological audit events for an application (case-insensitive)."""
        cursor = self.conn.cursor()
        placeholder = "?" if self.backend == "sqlite" else "%s"
        query = f"""
            SELECT * FROM loan_audit_trail
            WHERE UPPER(application_id) = UPPER({placeholder})
            ORDER BY id ASC
        """
        cursor.execute(query, (application_id.strip(),))
        rows = cursor.fetchall()
        results = []
        for r in rows:
            data = dict(r)
            data["details"] = self._deserialize(data.get("details"))
            results.append(data)
        return results

    def clear_all(self) -> None:
        """Clears all records from applications and audit trail tables."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM loan_audit_trail;")
        cursor.execute("DELETE FROM loan_applications;")
        self.conn.commit()

    def close(self) -> None:
        """Closes the underlying database connection."""
        if hasattr(self, "conn") and self.conn:
            self.conn.close()


_GLOBAL_APP_DB: Optional[ApplicationDatabase] = None


def get_application_database(
    config: Optional[DatabaseConfig] = None,
    in_memory: bool = False,
    force_new: bool = False,
) -> ApplicationDatabase:
    """
    Singleton / factory providing access to the Application Database.
    Supports in_memory=True for isolated in-memory storage.
    """
    global _GLOBAL_APP_DB
    if in_memory:
        return ApplicationDatabase(config=config, in_memory=True)

    if _GLOBAL_APP_DB is not None and not force_new:
        return _GLOBAL_APP_DB

    _GLOBAL_APP_DB = ApplicationDatabase(config=config, in_memory=False)
    return _GLOBAL_APP_DB
