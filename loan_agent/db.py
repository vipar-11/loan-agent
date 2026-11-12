"""
Application Database Manager for Loan Underwriting.
Manages persistent storage for loan applications, metadata, adjudication outcomes, and audit trails.
Supports both local SQLite and remote PostgreSQL storage.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence, Tuple
import functools
import json
import os
import sqlite3
import threading

from loan_agent.config import DatabaseConfig, load_db_config


# ---------------------------------------------------------------------------
# Canonical administrator statuses. Every persisted status rolls up into exactly one of these five.
# ---------------------------------------------------------------------------
MAIN_STATUSES: List[str] = ["new", "pending processing", "pending review", "approved", "rejected"]

MAIN_STATUS_LABELS: Dict[str, str] = {
    "new": "New",
    "pending processing": "Pending Processing",
    "pending review": "Pending Review",
    "approved": "Approved",
    "rejected": "Rejected",
}


def main_status_for(status: Optional[str]) -> str:
    """Rolls a persisted status (e.g. HUMAN_DENIED) up into its administrator-facing main status (e.g. rejected)."""
    clean = (status or "").strip().upper().replace("-", "_").replace(" ", "_")
    if clean == "NEW":
        return "new"
    if clean == "PENDING_PROCESSING":
        return "pending processing"
    if clean == "PENDING_REVIEW":
        return "pending review"
    if "APPROV" in clean:
        return "approved"
    if "DEN" in clean or "REJECT" in clean:
        return "rejected"
    return "new"


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


@dataclass(frozen=True)
class ApplicationQuery:
    """
    Criteria for `ApplicationDatabase.query_applications()`. Every criterion is translated into the SQL WHERE /
    ORDER BY / LIMIT clauses — nothing is filtered or sorted in memory — and all criteria that are set must match.
    """

    statuses: Optional[Sequence[str]] = None          # status IN (...), case-insensitive
    exclude_statuses: Optional[Sequence[str]] = None  # status NOT IN (...), case-insensitive
    submitted_by: Optional[str] = None                # owning self-service applicant (user ID)
    decided_by: Optional[str] = None                  # user ID of the officer who recorded the determination
    search: Optional[str] = None                      # applicant name or application ID: case-insensitive substring
    loan_purpose: Optional[str] = None                # exact, case-insensitive
    min_amount: Optional[float] = None                # loan_amount_requested >= (inclusive)
    max_amount: Optional[float] = None                # loan_amount_requested <= (inclusive)
    submitted_from: Optional[str] = None              # submitted_at >= this timestamp (inclusive)
    submitted_before: Optional[str] = None            # submitted_at <  this timestamp (exclusive)
    oldest_first: bool = False                        # ORDER BY submitted_at ASC (first in, first out) vs DESC
    limit: Optional[int] = None


def _like_pattern(text: str) -> str:
    """Substring LIKE pattern with the user's own % and _ treated literally (used with ESCAPE '!')."""
    escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


class ApplicationIdConflict(Exception):
    """The generated application ID is already in use (retry with a new one)."""


def _synchronized(method):
    """Serializes access to the shared DB connection (FastAPI executes sync handlers in a threadpool)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


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
        self._lock = threading.RLock()

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
                    graph_state TEXT,
                    submitted_by TEXT,
                    decided_by TEXT
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    found INTEGER DEFAULT 0,
                    selected INTEGER DEFAULT 0,
                    approved INTEGER DEFAULT 0,
                    rejected INTEGER DEFAULT 0,
                    escalated INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    error_message TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    partner_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    stored_path TEXT,
                    results_path TEXT,
                    sha256 TEXT,
                    file_bytes INTEGER DEFAULT 0,
                    received_at TEXT NOT NULL,
                    processed_at TEXT,
                    status TEXT NOT NULL,
                    total_rows INTEGER DEFAULT 0,
                    accepted_rows INTEGER DEFAULT 0,
                    flagged_rows INTEGER DEFAULT 0,
                    error_message TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    row_number INTEGER NOT NULL,
                    partner_id TEXT NOT NULL,
                    partner_reference TEXT,
                    status TEXT NOT NULL,
                    application_id TEXT,
                    errors TEXT
                );
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_intake_rows_batch ON intake_rows(batch_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_intake_batches_sha ON intake_batches(partner_id, sha256);")
            # A partner reference can be ACCEPTED (i.e. submitted as an application) only once per partner.
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_accepted_reference "
                "ON intake_rows(partner_id, partner_reference) WHERE status = 'ACCEPTED';"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS application_checkouts (
                    application_id TEXT PRIMARY KEY,
                    checked_out_by TEXT NOT NULL,
                    holder_key TEXT NOT NULL,
                    checked_out_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ACTIVE'
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                );
                """
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
                    graph_state TEXT,
                    submitted_by TEXT,
                    decided_by TEXT
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id SERIAL PRIMARY KEY,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    found INTEGER DEFAULT 0,
                    selected INTEGER DEFAULT 0,
                    approved INTEGER DEFAULT 0,
                    rejected INTEGER DEFAULT 0,
                    escalated INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    error_message TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_batches (
                    id SERIAL PRIMARY KEY,
                    partner_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    stored_path TEXT,
                    results_path TEXT,
                    sha256 TEXT,
                    file_bytes INTEGER DEFAULT 0,
                    received_at TEXT NOT NULL,
                    processed_at TEXT,
                    status TEXT NOT NULL,
                    total_rows INTEGER DEFAULT 0,
                    accepted_rows INTEGER DEFAULT 0,
                    flagged_rows INTEGER DEFAULT 0,
                    error_message TEXT
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_rows (
                    id SERIAL PRIMARY KEY,
                    batch_id INTEGER NOT NULL,
                    row_number INTEGER NOT NULL,
                    partner_id TEXT NOT NULL,
                    partner_reference TEXT,
                    status TEXT NOT NULL,
                    application_id TEXT,
                    errors TEXT
                );
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_intake_rows_batch ON intake_rows(batch_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_intake_batches_sha ON intake_batches(partner_id, sha256);")
            # A partner reference can be ACCEPTED (i.e. submitted as an application) only once per partner.
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_accepted_reference "
                "ON intake_rows(partner_id, partner_reference) WHERE status = 'ACCEPTED';"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS application_checkouts (
                    application_id TEXT PRIMARY KEY,
                    checked_out_by TEXT NOT NULL,
                    holder_key TEXT NOT NULL,
                    checked_out_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ACTIVE'
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                );
                """
            )
            self.conn.commit()
        self._migrate_schema()
        self._initialize_auth_schema()

    def _migrate_schema(self) -> None:
        """
        Additive, idempotent schema changes for databases created before these columns existed.

        `submitted_by` — the self-service applicant's user ID; NULL for applications with no owning account
        (Applicant Portal submissions before self-signup existed, partner batch / admin-triggered submissions).
        `decided_by` — the user ID of the officer who recorded a human determination (mirrors
        adjudication.officer_user_id, kept in step by save_application), so "decided by me" is an indexed column
        comparison instead of a scan of the adjudication JSON.
        """
        cursor = self.conn.cursor()
        for column in ("submitted_by", "decided_by"):
            if self.backend == "sqlite":
                try:
                    cursor.execute(f"ALTER TABLE loan_applications ADD COLUMN {column} TEXT")
                    self.conn.commit()
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                    self.conn.rollback()
            else:
                cursor.execute(f"ALTER TABLE loan_applications ADD COLUMN IF NOT EXISTS {column} TEXT")
                self.conn.commit()
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_loan_apps_submitted_by ON loan_applications(submitted_by);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_loan_apps_decided_by ON loan_applications(decided_by);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_loan_apps_submitted_at ON loan_applications(submitted_at);")
        self.conn.commit()
        self._backfill_decided_by()

    def _backfill_decided_by(self) -> None:
        """One-off for determinations recorded before the decided_by column existed (no-op once backfilled)."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT application_id, adjudication FROM loan_applications "
            "WHERE decided_by IS NULL AND adjudication LIKE '%officer_user_id%'"
        )
        for row in cursor.fetchall():
            officer = (self._deserialize(dict(row)["adjudication"]) or {})
            user_id = officer.get("officer_user_id") if isinstance(officer, dict) else None
            if user_id:
                cursor.execute(f"UPDATE loan_applications SET decided_by = {p} WHERE application_id = {p}",
                               (user_id, dict(row)["application_id"]))
        self.conn.commit()

    def _initialize_auth_schema(self) -> None:
        """
        Sign-in users and sessions (see loan_agent/auth). The DDL is portable: identical on SQLite and PostgreSQL.

        app_users.password_hash is NULL until the user sets a password (the "empty default password");
        auth_provider / external_subject let SSO-linked accounts (no local password) live in the same table.
        auth_sessions stores only a SHA-256 hash of each session token, never the token itself.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT,
                must_change_password INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                auth_provider TEXT NOT NULL DEFAULT 'local',
                external_subject TEXT,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                last_login_at TEXT,
                password_changed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_external_subject "
            "ON app_users(auth_provider, external_subject) WHERE external_subject IS NOT NULL;"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                auth_method TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);")
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

    @_synchronized
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
        adjudication = app_data.get("adjudication")
        decided_by = adjudication.get("officer_user_id") if isinstance(adjudication, dict) else None
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
                    graph_state = {placeholder},
                    decided_by = {placeholder}
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
                    decided_by,
                    app_id,
                ),
            )
        else:
            query = f"""
                INSERT INTO loan_applications (
                    application_id, applicant_name, ssn_last_4, stated_annual_income,
                    loan_amount_requested, loan_purpose, fcra_consent, status,
                    submitted_at, updated_at, adjudication, graph_state, submitted_by, decided_by
                ) VALUES ({', '.join([placeholder] * 14)})
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
                    app_data.get("submitted_by"),
                    decided_by,
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

    @_synchronized
    def insert_new_application(
        self,
        app_data: Dict[str, Any],
        actor: str = "APPLICANT",
        event_type: str = "APPLICATION_SUBMITTED",
        details: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Creates a brand-new application. Unlike save_application (an upsert), this never overwrites: raises
        ApplicationIdConflict if the ID is already taken — in this process (checked under the DB lock) or by
        another process (primary-key violation) — so the caller can retry with a fresh ID.
        """
        if self.get_application(app_data["application_id"]):
            raise ApplicationIdConflict(app_data["application_id"])
        try:
            return self.save_application(app_data, actor=actor, event_type=event_type, details=details)
        except Exception as exc:  # sqlite3.IntegrityError / psycopg UniqueViolation
            if "unique" in str(exc).lower() or "primary key" in str(exc).lower() or "duplicate" in str(exc).lower():
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise ApplicationIdConflict(app_data["application_id"]) from exc
            raise

    @_synchronized
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

    @_synchronized
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

    def _row_to_application(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        data["fcra_consent"] = bool(data["fcra_consent"])
        data["adjudication"] = self._deserialize(data.get("adjudication"))
        data["graph_state"] = self._deserialize(data.get("graph_state"))
        return data

    def _where(self, query: "ApplicationQuery") -> Tuple[str, List[Any]]:
        """WHERE clause + parameters for an ApplicationQuery (shared by query_applications and count queries)."""
        p = "?" if self.backend == "sqlite" else "%s"
        clauses: List[str] = []
        params: List[Any] = []
        if query.statuses:
            statuses = sorted({s.upper() for s in query.statuses})
            clauses.append(f"UPPER(status) IN ({', '.join([p] * len(statuses))})")
            params.extend(statuses)
        if query.exclude_statuses:
            excluded = sorted({s.upper() for s in query.exclude_statuses})
            clauses.append(f"UPPER(status) NOT IN ({', '.join([p] * len(excluded))})")
            params.extend(excluded)
        if query.submitted_by is not None:
            clauses.append(f"submitted_by = {p}")
            params.append(query.submitted_by)
        if query.decided_by is not None:
            clauses.append(f"decided_by = {p}")
            params.append(query.decided_by)
        if query.search and query.search.strip():
            pattern = _like_pattern(query.search.strip().lower())
            clauses.append(f"(LOWER(applicant_name) LIKE {p} ESCAPE '!' OR LOWER(application_id) LIKE {p} ESCAPE '!')")
            params.extend([pattern, pattern])
        if query.loan_purpose:
            clauses.append(f"LOWER(loan_purpose) = {p}")
            params.append(query.loan_purpose.lower())
        if query.min_amount is not None:
            clauses.append(f"loan_amount_requested >= {p}")
            params.append(float(query.min_amount))
        if query.max_amount is not None:
            clauses.append(f"loan_amount_requested <= {p}")
            params.append(float(query.max_amount))
        if query.submitted_from:
            clauses.append(f"submitted_at >= {p}")  # fixed-width timestamps: string order == time order
            params.append(query.submitted_from)
        if query.submitted_before:
            clauses.append(f"submitted_at < {p}")
            params.append(query.submitted_before)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @_synchronized
    def query_applications(self, query: Optional["ApplicationQuery"] = None) -> List[Dict[str, Any]]:
        """
        Applications matching `query`, filtered, ordered and limited by the database itself.
        Order: submission time (oldest or newest first), ties broken by application ID so it is stable.
        """
        query = query or ApplicationQuery()
        p = "?" if self.backend == "sqlite" else "%s"
        where, params = self._where(query)
        direction = "ASC" if query.oldest_first else "DESC"
        sql = f"SELECT * FROM loan_applications{where} ORDER BY submitted_at {direction}, application_id {direction}"
        if query.limit is not None:
            sql += f" LIMIT {p}"
            params.append(int(query.limit))
        cursor = self.conn.cursor()
        cursor.execute(sql, params)
        return [self._row_to_application(r) for r in cursor.fetchall()]

    def list_applications(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Application records, optionally filtered by normalized status, newest first."""
        return self.query_applications(ApplicationQuery(statuses=normalize_status_filter(status)))

    def list_applications_for_user(self, submitted_by: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Applications owned by one self-service user (`submitted_by`), newest first. Never returns another user's."""
        return self.query_applications(
            ApplicationQuery(submitted_by=submitted_by, statuses=normalize_status_filter(status))
        )

    @_synchronized
    def next_reviewable_application_ids(self, holder_key: str, now_str: str, limit: int = 25) -> List[str]:
        """
        "Get Work" candidates, highest priority first: PENDING_REVIEW applications that nobody else has checked out
        (no checkout, an expired one, or one held by `holder_key` itself — those come first, so a reviewer is handed
        back the file they already hold rather than a second one), then oldest submission first.
        The caller claims a candidate with the atomic acquire_checkout(); losing a race simply means trying the next.
        """
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            SELECT a.application_id
            FROM loan_applications a
            LEFT JOIN application_checkouts c
                   ON c.application_id = a.application_id AND c.expires_at > {p}
            WHERE UPPER(a.status) = 'PENDING_REVIEW'
              AND (c.application_id IS NULL OR c.holder_key = {p})
            ORDER BY CASE WHEN c.holder_key = {p} THEN 0 ELSE 1 END, a.submitted_at ASC, a.application_id ASC
            LIMIT {p}
            """,
            (now_str, holder_key, holder_key, int(limit)),
        )
        return [dict(r)["application_id"] for r in cursor.fetchall()]

    @_synchronized
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

    @_synchronized
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
        returning = "" if self.backend == "sqlite" else " RETURNING id"

        query = f"""
            INSERT INTO loan_audit_trail (
                application_id, timestamp, event_type, actor, from_status, to_status, details
            ) VALUES ({', '.join([placeholder] * 7)}){returning}
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
        event_id = cursor.lastrowid if self.backend == "sqlite" else list(cursor.fetchone().values())[0]
        self.conn.commit()
        return int(event_id or 0)

    @_synchronized
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

    # ------------------------------------------------------------------
    # Status summary (administrator view)
    # ------------------------------------------------------------------
    @_synchronized
    def count_by_status(self) -> Dict[str, int]:
        """Exact record counts grouped by persisted status."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT status, COUNT(*) AS n FROM loan_applications GROUP BY status")
        rows = cursor.fetchall()
        result: Dict[str, int] = {}
        for r in rows:
            d = dict(r)
            key = str(d.get("status") or "UNKNOWN").upper()
            result[key] = result.get(key, 0) + int(d.get("n") or d.get("COUNT(*)") or 0)
        return result

    def status_summary(self) -> List[Dict[str, Any]]:
        """
        Administrator summary across the five main statuses
        (new, pending processing, pending review, approved, rejected), each with its sub-status breakdown.
        """
        by_status = self.count_by_status()
        groups: Dict[str, Dict[str, int]] = {m: {} for m in MAIN_STATUSES}
        for status_name, count in by_status.items():
            groups[main_status_for(status_name)][status_name] = count
        return [
            {
                "status": m,
                "label": MAIN_STATUS_LABELS[m],
                "count": sum(groups[m].values()),
                "sub_statuses": dict(sorted(groups[m].items())),
            }
            for m in MAIN_STATUSES
        ]

    # ------------------------------------------------------------------
    # Workflow run log (initiate_workflow executions)
    # ------------------------------------------------------------------
    def _row_to_run(self, row: Any) -> Dict[str, Any]:
        return dict(row)

    @_synchronized
    def create_workflow_run(self, trigger_source: str, status: str = "REQUESTED") -> Dict[str, Any]:
        """Records that an initiate_workflow run was requested. Returns the new run record."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        if self.backend == "sqlite":
            cursor.execute(
                "INSERT INTO workflow_runs (trigger_source, status, requested_at) VALUES (?, ?, ?)",
                (trigger_source, status, now_str),
            )
            run_id = cursor.lastrowid
        else:
            cursor.execute(
                "INSERT INTO workflow_runs (trigger_source, status, requested_at) VALUES (%s, %s, %s) RETURNING id",
                (trigger_source, status, now_str),
            )
            run_id = list(cursor.fetchone().values())[0]
        self.conn.commit()
        return self.get_workflow_run(int(run_id))  # type: ignore[return-value]

    @_synchronized
    def update_workflow_run(self, run_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Updates progress/outcome columns of a workflow run (append-only semantics; nothing is removed)."""
        allowed = {
            "status", "started_at", "finished_at", "found", "selected",
            "approved", "rejected", "escalated", "errors", "error_message",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return self.get_workflow_run(run_id)
        placeholder = "?" if self.backend == "sqlite" else "%s"
        assignments = ", ".join(f"{k} = {placeholder}" for k in fields)
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE workflow_runs SET {assignments} WHERE id = {placeholder}",
            (*fields.values(), run_id),
        )
        self.conn.commit()
        return self.get_workflow_run(run_id)

    @_synchronized
    def get_workflow_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        placeholder = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM workflow_runs WHERE id = {placeholder}", (run_id,))
        row = cursor.fetchone()
        return self._row_to_run(row) if row else None

    @_synchronized
    def list_workflow_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Most recent workflow runs first."""
        placeholder = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM workflow_runs ORDER BY id DESC LIMIT {placeholder}", (int(limit),))
        return [self._row_to_run(r) for r in cursor.fetchall()]

    def get_last_workflow_run(self) -> Optional[Dict[str, Any]]:
        runs = self.list_workflow_runs(limit=1)
        return runs[0] if runs else None

    # ------------------------------------------------------------------
    # Review checkouts (one holder per PENDING_REVIEW application at a time)
    #
    # holder_key is the caller's opaque identity key (the signed-in user ID combined with the browser tab's
    # session, see api.schemas.UserIdentity.holder_key); checked_out_by is its display label. Every mutation is a
    # single conditional SQL statement, so exclusivity holds across threads, worker processes and hosts sharing
    # the database.
    # Timestamps use the fixed-width "%Y-%m-%d %H:%M:%S" format and are compared as strings.
    # ------------------------------------------------------------------
    @_synchronized
    def acquire_checkout(
        self, application_id: str, holder_key: str, holder_name: str, now_str: str, expires_str: str
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically checks the application out to `holder_key`, or renews that holder's own ACTIVE checkout.
        Takes over a checkout only once it has expired. Returns the row as it stands afterwards:
        the caller owns the checkout iff the returned holder_key is its own and state is ACTIVE.
        """
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            INSERT INTO application_checkouts
                (application_id, checked_out_by, holder_key, checked_out_at, expires_at, state)
            VALUES ({p}, {p}, {p}, {p}, {p}, 'ACTIVE')
            ON CONFLICT (application_id) DO UPDATE SET
                checked_out_by = excluded.checked_out_by,
                checked_out_at = CASE
                    WHEN application_checkouts.holder_key = excluded.holder_key
                         AND application_checkouts.expires_at > {p}
                    THEN application_checkouts.checked_out_at
                    ELSE excluded.checked_out_at END,
                holder_key = excluded.holder_key,
                expires_at = excluded.expires_at,
                state = 'ACTIVE'
            WHERE (application_checkouts.holder_key = excluded.holder_key AND application_checkouts.state = 'ACTIVE')
               OR application_checkouts.expires_at <= {p}
            """,
            (application_id, holder_name, holder_key, now_str, expires_str, now_str, now_str),
        )
        self.conn.commit()
        return self.get_checkout(application_id)

    @_synchronized
    def renew_checkout(self, application_id: str, holder_key: str, expires_str: str) -> bool:
        """
        Extends the lease of the holder's own ACTIVE checkout. Never (re)creates one: if the checkout was
        force-released, or lapsed and was taken by someone else, the row is gone / not ours and this fails.
        """
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE application_checkouts SET expires_at = {p} "
            f"WHERE application_id = {p} AND holder_key = {p} AND state = 'ACTIVE'",
            (expires_str, application_id, holder_key),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def get_checkout(self, application_id: str) -> Optional[Dict[str, Any]]:
        """Raw checkout row (may be expired — expiry is evaluated by the caller)."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM application_checkouts WHERE application_id = {p}", (application_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @_synchronized
    def list_checkouts(self, active_at: Optional[str] = None) -> List[Dict[str, Any]]:
        """All checkout rows, or only those not yet expired at `active_at`."""
        cursor = self.conn.cursor()
        if active_at:
            p = "?" if self.backend == "sqlite" else "%s"
            cursor.execute(f"SELECT * FROM application_checkouts WHERE expires_at > {p}", (active_at,))
        else:
            cursor.execute("SELECT * FROM application_checkouts")
        return [dict(r) for r in cursor.fetchall()]

    @_synchronized
    def claim_checkout_for_decision(
        self, application_id: str, holder_key: str, now_str: str, expires_str: str
    ) -> bool:
        """ACTIVE -> DECIDING for the holder's unexpired checkout. Exactly one concurrent caller can win."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            UPDATE application_checkouts SET state = 'DECIDING', expires_at = {p}
            WHERE application_id = {p} AND holder_key = {p} AND state = 'ACTIVE' AND expires_at > {p}
            """,
            (expires_str, application_id, holder_key, now_str),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def restore_checkout(self, application_id: str, holder_key: str) -> bool:
        """DECIDING -> ACTIVE (a determination failed; the holder keeps the file checked out)."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE application_checkouts SET state = 'ACTIVE' "
            f"WHERE application_id = {p} AND holder_key = {p} AND state = 'DECIDING'",
            (application_id, holder_key),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def release_checkout(self, application_id: str, holder_key: Optional[str] = None) -> bool:
        """Deletes the checkout; only the holder's when `holder_key` is given, unconditionally otherwise."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        if holder_key is None:
            cursor.execute(f"DELETE FROM application_checkouts WHERE application_id = {p}", (application_id,))
        else:
            cursor.execute(
                f"DELETE FROM application_checkouts WHERE application_id = {p} AND holder_key = {p}",
                (application_id, holder_key),
            )
        self.conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Partner batch intake (files received over FTP and the outcome of every row)
    # ------------------------------------------------------------------
    _BATCH_FIELDS = {
        "stored_path", "results_path", "sha256", "file_bytes", "processed_at", "status",
        "total_rows", "accepted_rows", "flagged_rows", "error_message",
    }

    @_synchronized
    def create_intake_batch(self, partner_id: str, file_name: str, received_at: str, status: str = "RECEIVED") -> Dict[str, Any]:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        if self.backend == "sqlite":
            cursor.execute(
                f"INSERT INTO intake_batches (partner_id, file_name, received_at, status) VALUES ({p}, {p}, {p}, {p})",
                (partner_id, file_name, received_at, status),
            )
            batch_id = cursor.lastrowid
        else:
            cursor.execute(
                f"INSERT INTO intake_batches (partner_id, file_name, received_at, status) "
                f"VALUES ({p}, {p}, {p}, {p}) RETURNING id",
                (partner_id, file_name, received_at, status),
            )
            batch_id = list(cursor.fetchone().values())[0]
        self.conn.commit()
        return self.get_intake_batch(int(batch_id))  # type: ignore[return-value]

    @_synchronized
    def update_intake_batch(self, batch_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        fields = {k: v for k, v in updates.items() if k in self._BATCH_FIELDS}
        if fields:
            p = "?" if self.backend == "sqlite" else "%s"
            cursor = self.conn.cursor()
            cursor.execute(
                f"UPDATE intake_batches SET {', '.join(f'{k} = {p}' for k in fields)} WHERE id = {p}",
                (*fields.values(), batch_id),
            )
            self.conn.commit()
        return self.get_intake_batch(batch_id)

    @_synchronized
    def get_intake_batch(self, batch_id: int) -> Optional[Dict[str, Any]]:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM intake_batches WHERE id = {p}", (batch_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @_synchronized
    def list_intake_batches(self, limit: int = 50, partner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        if partner_id:
            cursor.execute(
                f"SELECT * FROM intake_batches WHERE partner_id = {p} ORDER BY id DESC LIMIT {p}", (partner_id, int(limit))
            )
        else:
            cursor.execute(f"SELECT * FROM intake_batches ORDER BY id DESC LIMIT {p}", (int(limit),))
        return [dict(r) for r in cursor.fetchall()]

    @_synchronized
    def find_processed_intake_file(self, partner_id: str, sha256: str) -> Optional[Dict[str, Any]]:
        """
        An earlier batch from the same partner with identical content whose rows were actually evaluated
        (ACCEPTED / PARTIALLY_ACCEPTED / FLAGGED). Rejected, failed or interrupted files do not count, so an identical
        resend after a fix (or after a crash) is processed normally.
        """
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"SELECT * FROM intake_batches WHERE partner_id = {p} AND sha256 = {p} "
            f"AND status IN ('ACCEPTED', 'PARTIALLY_ACCEPTED', 'FLAGGED') ORDER BY id ASC LIMIT 1",
            (partner_id, sha256),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    @_synchronized
    def add_intake_row(
        self,
        batch_id: int,
        row_number: int,
        partner_id: str,
        partner_reference: Optional[str],
        status: str,
        application_id: Optional[str] = None,
        errors: Optional[List[str]] = None,
    ) -> None:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"INSERT INTO intake_rows (batch_id, row_number, partner_id, partner_reference, status, application_id, errors) "
            f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})",
            (batch_id, row_number, partner_id, partner_reference, status, application_id, self._serialize(errors or [])),
        )
        self.conn.commit()

    @_synchronized
    def list_intake_rows(self, batch_id: int) -> List[Dict[str, Any]]:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM intake_rows WHERE batch_id = {p} ORDER BY row_number ASC, id ASC", (batch_id,))
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            d["errors"] = self._deserialize(d.get("errors")) or []
            rows.append(d)
        return rows

    @_synchronized
    def find_accepted_partner_reference(self, partner_id: str, partner_reference: str) -> Optional[Dict[str, Any]]:
        """The earlier ACCEPTED row for this partner reference (references are case-sensitive), if any."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"SELECT * FROM intake_rows WHERE partner_id = {p} AND partner_reference = {p} AND status = 'ACCEPTED'",
            (partner_id, partner_reference),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # System settings (key/value, persisted across restarts)
    # ------------------------------------------------------------------
    @_synchronized
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        placeholder = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT value FROM system_settings WHERE key = {placeholder}", (key,))
        row = cursor.fetchone()
        if not row:
            return default
        return str(dict(row)["value"])

    @_synchronized
    def set_setting(self, key: str, value: Any, updated_by: str = "SYSTEM") -> None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        if self.backend == "sqlite":
            cursor.execute(
                """
                INSERT INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,
                                               updated_by = excluded.updated_by
                """,
                (key, str(value), now_str, updated_by),
            )
        else:
            cursor.execute(
                """
                INSERT INTO system_settings (key, value, updated_at, updated_by) VALUES (%s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at,
                                                updated_by = EXCLUDED.updated_by
                """,
                (key, str(value), now_str, updated_by),
            )
        self.conn.commit()

    @_synchronized
    def get_setting_record(self, key: str) -> Optional[Dict[str, Any]]:
        placeholder = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM system_settings WHERE key = {placeholder}", (key,))
        row = cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Sign-in users (loan_agent.auth). user_id is stored normalized (lowercase) by the auth layer.
    # ------------------------------------------------------------------
    _USER_FIELDS = {
        "display_name", "role", "password_hash", "must_change_password", "is_active", "auth_provider",
        "external_subject", "failed_attempts", "locked_until", "last_login_at", "password_changed_at",
    }
    _USER_BOOL_FIELDS = ("must_change_password", "is_active")

    def _row_to_user(self, row: Any) -> Dict[str, Any]:
        data = dict(row)
        for key in self._USER_BOOL_FIELDS:
            if key in data:
                data[key] = bool(data[key])
        return data

    def _user_values(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        unknown = set(updates) - self._USER_FIELDS
        if unknown:
            raise ValueError(f"Unknown user field(s): {', '.join(sorted(unknown))}")
        return {k: (int(bool(v)) if k in self._USER_BOOL_FIELDS else v) for k, v in updates.items()}

    @_synchronized
    def create_user(self, user_id: str, display_name: str, role: str, **fields: Any) -> Dict[str, Any]:
        """Inserts a user. Raises ValueError if the user ID is already taken."""
        if self.get_user(user_id):
            raise ValueError(f"User '{user_id}' already exists.")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        values = {"display_name": display_name, "role": role, **self._user_values(fields)}
        columns = ["user_id", *values.keys(), "created_at", "updated_at"]
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"INSERT INTO app_users ({', '.join(columns)}) VALUES ({', '.join([p] * len(columns))})",
            (user_id, *values.values(), now_str, now_str),
        )
        self.conn.commit()
        return self.get_user(user_id)  # type: ignore[return-value]

    @_synchronized
    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM app_users WHERE user_id = {p}", (user_id,))
        row = cursor.fetchone()
        return self._row_to_user(row) if row else None

    @_synchronized
    def list_users(self) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM app_users ORDER BY role, user_id")
        return [self._row_to_user(r) for r in cursor.fetchall()]

    @_synchronized
    def update_user(self, user_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        values = self._user_values(updates)
        if values:
            p = "?" if self.backend == "sqlite" else "%s"
            values["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            assignments = ", ".join(f"{k} = {p}" for k in values)
            cursor = self.conn.cursor()
            cursor.execute(f"UPDATE app_users SET {assignments} WHERE user_id = {p}", (*values.values(), user_id))
            self.conn.commit()
        return self.get_user(user_id)

    @_synchronized
    def record_failed_sign_in(self, user_id: str, threshold: int, locked_until_str: str) -> None:
        """
        Counts a failed sign-in atomically; the attempt that reaches `threshold` locks the account until
        `locked_until_str` and restarts the count (so the user gets `threshold` fresh attempts after the lock).
        """
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            UPDATE app_users SET
                locked_until = CASE WHEN failed_attempts + 1 >= {p} THEN {p} ELSE locked_until END,
                failed_attempts = CASE WHEN failed_attempts + 1 >= {p} THEN 0 ELSE failed_attempts + 1 END
            WHERE user_id = {p}
            """,
            (threshold, locked_until_str, threshold, user_id),
        )
        self.conn.commit()

    @_synchronized
    def record_successful_sign_in(self, user_id: str, now_str: str) -> None:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE app_users SET failed_attempts = 0, locked_until = NULL, last_login_at = {p} WHERE user_id = {p}",
            (now_str, user_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Sign-in sessions (browser cookie and API bearer tokens share this table; only token hashes are stored)
    # ------------------------------------------------------------------
    @_synchronized
    def create_auth_session(
        self, token_hash: str, user_id: str, auth_method: str, now_str: str, expires_str: str
    ) -> None:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"INSERT INTO auth_sessions (token_hash, user_id, auth_method, created_at, last_seen_at, expires_at) "
            f"VALUES ({p}, {p}, {p}, {p}, {p}, {p})",
            (token_hash, user_id, auth_method, now_str, now_str, expires_str),
        )
        self.conn.commit()

    @_synchronized
    def get_auth_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM auth_sessions WHERE token_hash = {p}", (token_hash,))
        row = cursor.fetchone()
        return dict(row) if row else None

    @_synchronized
    def touch_auth_session(self, token_hash: str, now_str: str) -> None:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(f"UPDATE auth_sessions SET last_seen_at = {p} WHERE token_hash = {p}", (now_str, token_hash))
        self.conn.commit()

    @_synchronized
    def revoke_auth_session(self, token_hash: str, now_str: str) -> bool:
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE auth_sessions SET revoked_at = {p} WHERE token_hash = {p} AND revoked_at IS NULL",
            (now_str, token_hash),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    @_synchronized
    def revoke_user_auth_sessions(self, user_id: str, now_str: str, except_token_hash: Optional[str] = None) -> int:
        """Signs the user out everywhere (optionally keeping one session, e.g. the one changing the password)."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE auth_sessions SET revoked_at = {p} "
            f"WHERE user_id = {p} AND revoked_at IS NULL AND token_hash <> {p}",
            (now_str, user_id, except_token_hash or ""),
        )
        self.conn.commit()
        return cursor.rowcount

    @_synchronized
    def purge_auth_sessions(self, expired_before_str: str) -> int:
        """Deletes sessions that expired (or were revoked) before the given time."""
        p = "?" if self.backend == "sqlite" else "%s"
        cursor = self.conn.cursor()
        cursor.execute(
            f"DELETE FROM auth_sessions WHERE expires_at < {p} OR revoked_at < {p}",
            (expired_before_str, expired_before_str),
        )
        self.conn.commit()
        return cursor.rowcount

    @_synchronized
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
