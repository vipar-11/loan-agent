"""
REST-backed administrative CLI for the loan underwriting service.

Powers the top-level `list_loans`, `trace_loan`, and `initiate_workflow` scripts by invoking the
running FastAPI service (`/api/v1/...`) instead of executing database/workflow code in-process.
This keeps a single operational code path (the service) regardless of how an action is triggered.

Target service resolution (first match wins):
    1. --api-url CLI flag
    2. LOAN_AGENT_API_URL environment variable
    3. http://127.0.0.1:8000

Authentication (the service requires a signed-in Administrator for these tools; first match wins):
    1. LOAN_AGENT_TOKEN        a bearer token from POST /api/v1/auth/token (used as-is, never revoked here)
    2. --user / LOAN_AGENT_USER the user ID; the password comes from LOAN_AGENT_PASSWORD or an interactive prompt.
                               The tool signs in, runs, and signs out again (revoking its token).
For unattended runs (cron), set LOAN_AGENT_USER and LOAN_AGENT_PASSWORD in the job's environment.
"""
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional
import argparse
import getpass
import json
import os
import sys

import requests

from loan_agent.admin import normalize_cli_args
from loan_agent.db import MAIN_STATUSES, MAIN_STATUS_LABELS, main_status_for

DEFAULT_API_URL = "http://127.0.0.1:8000"
API_PREFIX = "/api/v1"


class ApiError(Exception):
    """Raised when the service returns an HTTP error response."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"[HTTP {status_code}] {detail}")
        self.status_code = status_code
        self.detail = detail


class ApiClient:
    """
    Minimal JSON client for the underwriting service.

    `session` accepts any object exposing requests-style `get`/`post` (e.g. `requests.Session`
    or `fastapi.testclient.TestClient`), which keeps this module testable without a live server.
    """

    def __init__(self, base_url: Optional[str] = None, session: Optional[Any] = None, token: Optional[str] = None):
        self.base_url = (base_url or os.environ.get("LOAN_AGENT_API_URL") or DEFAULT_API_URL).rstrip("/")
        self._http = session or requests.Session()
        self.token = token or os.environ.get("LOAN_AGENT_TOKEN") or None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def sign_in(self, user_id: str, password: str) -> None:
        """OAuth2 password grant (POST /auth/token); subsequent calls carry the bearer token."""
        body = self._parse(self._http.post(self._url("/auth/token"), data={"username": user_id, "password": password}))
        self.token = body["access_token"]
        if body.get("password_change_required"):
            self.sign_out()
            raise ApiError(403, f"User '{user_id}' must choose a password first: sign in to the workbench "
                                f"(/login) with the empty default password, then re-run this command.")

    def sign_out(self) -> None:
        """Revokes the current token (best effort)."""
        if self.token:
            try:
                self._http.post(self._url("/auth/logout"), headers=self._headers())
            except Exception:
                pass
            self.token = None

    @staticmethod
    def _parse(response: Any) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
                detail = body.get("detail", body) if isinstance(body, dict) else body
            except Exception:
                detail = response.text
            raise ApiError(response.status_code, json.dumps(detail) if not isinstance(detail, str) else detail)
        return response.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._parse(self._http.get(self._url(path), params=params, headers=self._headers()))

    def post(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return self._parse(self._http.post(
            self._url(path), json=payload if payload is not None else {}, headers=self._headers()
        ))


def add_auth_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=str, default=None,
                        help="User ID to sign in with (default: LOAN_AGENT_USER). The password is read from "
                             "LOAN_AGENT_PASSWORD or prompted for. Not needed when LOAN_AGENT_TOKEN is set.")


@contextmanager
def signed_in(client: ApiClient, user: Optional[str]) -> Iterator[ApiClient]:
    """
    Ensures `client` is authenticated for the duration of a command. A pre-supplied token is used as-is; otherwise
    the tool signs in with the user ID and password and signs out (revoking its token) afterwards.
    """
    if client.token:
        yield client
        return
    user_id = user or os.environ.get("LOAN_AGENT_USER")
    if not user_id:
        raise ApiError(401, "Sign-in required: pass --user USER_ID (or set LOAN_AGENT_USER), "
                            "or provide a bearer token in LOAN_AGENT_TOKEN.")
    password = os.environ.get("LOAN_AGENT_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise ApiError(401, "No password: set LOAN_AGENT_PASSWORD for non-interactive use.")
        password = getpass.getpass(f"Password for {user_id}: ")
    client.sign_in(user_id, password)
    try:
        yield client
    finally:
        client.sign_out()


def _connection_help(client: ApiClient, exc: Exception) -> str:
    return (
        f"ERROR: Could not reach the underwriting service at {client.base_url} ({exc}).\n"
        f"       Start it with `python run_api.py` (or `uvicorn api.main:app`), or point this tool at a "
        f"running instance via --api-url / LOAN_AGENT_API_URL."
    )


def _fmt_money(val: Any) -> str:
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return "—"


# ===========================================================================
# 1. LIST LOANS  (GET /api/v1/applications, GET /api/v1/stats)
# ===========================================================================
def summarize_by_main_status(apps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rolls a list of application records up into the five main statuses (with sub-status detail)."""
    groups: Dict[str, Dict[str, int]] = {m: {} for m in MAIN_STATUSES}
    for app in apps:
        raw = str(app.get("status") or "UNKNOWN").upper()
        bucket = groups[main_status_for(raw)]
        bucket[raw] = bucket.get(raw, 0) + 1
    return [
        {"status": m, "label": MAIN_STATUS_LABELS[m], "count": sum(groups[m].values()),
         "sub_statuses": dict(sorted(groups[m].items()))}
        for m in MAIN_STATUSES
    ]


def print_status_summary(groups: List[Dict[str, Any]], title: str = "Loan Application Status Summary") -> None:
    """
    Prints counts for the five main statuses (new, pending processing, pending review, approved, rejected).
    Sub-statuses (e.g. AUTO_DENIED / HUMAN_DENIED under Rejected) are shown indented as supporting detail.
    """
    width = 48
    divider = "-" * width
    print(f"\n{title}")
    print(divider)
    total = 0
    for g in groups:
        total += int(g.get("count", 0))
        print(f"  {g['label']:<32} {g['count']:>6}")
        for sub, n in (g.get("sub_statuses") or {}).items():
            print(f"      └ {sub:<28} {n:>6}")
    print(divider)
    print(f"  {'TOTAL':<32} {total:>6}\n")


def list_loans_via_api(
    client: ApiClient,
    status: Optional[str] = None,
    count_only: bool = False,
) -> int:
    """
    Lists applications from the service, or — with count_only — prints a summary of
    record counts grouped by status (optionally restricted to a status filter).
    Returns the number of matching applications.
    """
    if count_only:
        if status:
            apps = client.get("/applications", params={"status": status})
            groups = [g for g in summarize_by_main_status(apps) if g["count"] > 0]
            print_status_summary(groups, title=f"Loan Application Status Summary (filter: '{status}')")
            return len(apps)
        stats = client.get("/stats")
        print_status_summary(stats.get("main_statuses", []))
        return int(stats.get("total", 0))

    apps = client.get("/applications", params={"status": status} if status else None)
    if not apps:
        if status:
            print(f"No loan applications found matching status: '{status}'.")
        else:
            print("No loan applications found.")
        return 0

    header = (
        f"{'Application ID':<16} | {'Applicant Name':<20} | {'Amount':<12} | "
        f"{'Stated Income':<14} | {'Status':<18} | {'Submitted At':<19}"
    )
    divider = "-" * len(header)
    print(f"\nFound {len(apps)} loan application(s):\n")
    print(divider)
    print(header)
    print(divider)
    for app in apps:
        print(
            f"{app.get('application_id', ''):<16} | {app.get('applicant_name', ''):<20} | "
            f"{_fmt_money(app.get('loan_amount_requested')):<12} | "
            f"{_fmt_money(app.get('stated_annual_income')):<14} | "
            f"{app.get('status', ''):<18} | {str(app.get('submitted_at', '')):<19}"
        )
    print(divider + "\n")
    return len(apps)


def main_list_loans(argv: Optional[List[str]] = None, client: Optional[ApiClient] = None) -> int:
    args = normalize_cli_args(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(
        description="Query loan applications from the underwriting service by status."
    )
    parser.add_argument("--status", type=str, default=None,
                        help="Filter (new, processing, pending review, approved, rejected, adjudicated, all).")
    parser.add_argument("--count", action="store_true",
                        help="Print a summary of application counts grouped by status.")
    parser.add_argument("--api-url", type=str, default=None,
                        help=f"Base URL of the underwriting service (default: {DEFAULT_API_URL}).")
    add_auth_arguments(parser)
    parsed = parser.parse_args(args)

    api = client or ApiClient(base_url=parsed.api_url)
    try:
        with signed_in(api, parsed.user):
            list_loans_via_api(api, status=parsed.status, count_only=parsed.count)
        return 0
    except ApiError as exc:
        print(f"ERROR: {exc}")
        return 1
    except requests.exceptions.RequestException as exc:
        print(_connection_help(api, exc))
        return 2


# ===========================================================================
# 2. TRACE LOAN  (GET /api/v1/applications/{id}, GET /api/v1/applications/{id}/audit-trail)
# ===========================================================================
def trace_loan_via_api(client: ApiClient, app_id: str) -> bool:
    """Prints a consolidated audit view of one application sourced entirely from the service."""
    clean_id = app_id.strip()
    try:
        app = client.get(f"/applications/{clean_id}")
    except ApiError as exc:
        if exc.status_code == 404:
            print(f"\n[ERROR] Loan application '{clean_id}' was not found by the underwriting service.\n")
            return False
        raise

    trail = client.get(f"/applications/{app['application_id']}/audit-trail")
    graph_state = app.get("graph_state") or {}
    adjudication = app.get("adjudication") or {}

    sep_major = "=" * 80
    sep_minor = "-" * 80
    print(f"\n{sep_major}")
    print(f"CONSOLIDATED AUDIT & EXECUTION TRACE: {app['application_id']}")
    print(sep_major)

    print("\n[1. APPLICATION METADATA & PROFILE]")
    print(sep_minor)
    print(f"  • Application ID:       {app.get('application_id')}")
    print(f"  • Applicant Legal Name: {app.get('applicant_name')}")
    print(f"  • SSN:                  {app.get('ssn_masked')}")
    print(f"  • Loan Purpose:         {app.get('loan_purpose')}")
    print(f"  • Loan Amount:          {_fmt_money(app.get('loan_amount_requested'))}")
    print(f"  • Stated Annual Income: {_fmt_money(app.get('stated_annual_income'))}")
    print(f"  • FCRA/GLBA Consent:    {'Granted' if app.get('fcra_consent') else 'Denied'}")
    print(f"  • Current Status:       {app.get('status')} (stage: {app.get('stage')})")
    print(f"  • Initial Submitted At: {app.get('submitted_at')}")
    print(f"  • Last Updated At:      {app.get('updated_at')}")

    print("\n[2. ADJUDICATION OUTCOME]")
    print(sep_minor)
    if adjudication:
        print(f"  • Final Decision:       {adjudication.get('decision')}")
        print(f"  • Adjudicated By:       {adjudication.get('officer')}")
        print(f"  • Adjudicated At:       {adjudication.get('adjudicated_at')}")
        print(f"  • Confidence Score:     {adjudication.get('confidence_score', 'N/A')}")
        print(f"  • Policy Citations:     {', '.join(adjudication.get('policy_citations') or ['None'])}")
        adverse = adjudication.get("adverse_action_reasons") or []
        if adverse:
            print(f"  • Adverse Action:       {'; '.join(adverse)}")
        memo = adjudication.get("reason") or adjudication.get("agent_notes") or "N/A"
        print(f"  • Justification Memo:   {memo}")
    else:
        print("  (No final adjudication recorded yet)")

    print("\n[3. WORKFLOW STATE & FINANCIAL METRICS]")
    print(sep_minor)
    if graph_state:
        metrics = [
            ("Underwriting Decision", graph_state.get("underwriting_decision")),
            ("Final Status", graph_state.get("final_status")),
            ("FICO Score", graph_state.get("fico_score")),
            ("Verified Monthly Income", graph_state.get("verified_monthly_income")),
            ("Income Variance %", graph_state.get("income_variance_pct")),
            ("Post-Loan DTI %", graph_state.get("post_loan_dti")),
            ("Payment Cushion Ratio", graph_state.get("payment_cushion_ratio")),
            ("Error Message", graph_state.get("error_message")),
        ]
        for label, value in metrics:
            if value is not None:
                print(f"  • {label + ':':<26}{value}")
        logs = graph_state.get("execution_log") or []
        if logs:
            print("\n  Workflow Execution Log:")
            for line in logs:
                print(f"    {line}")
    else:
        print("  (Workflow has not been executed for this application yet)")

    print("\n[4. APPLICATION DATABASE AUDIT TRAIL]")
    print(sep_minor)
    if trail:
        for event in trail:
            details = event.get("details")
            details_str = details if isinstance(details, str) else json.dumps(details)
            print(
                f"  {event.get('timestamp')} | {event.get('event_type'):<24} | {event.get('actor'):<28} | "
                f"{event.get('from_status') or '∅':<18} -> {event.get('to_status') or '∅':<18} | {details_str}"
            )
    else:
        print("  (No audit events recorded)")
    print(f"\n{sep_major}\n")
    return True


def main_trace_loan(argv: Optional[List[str]] = None, client: Optional[ApiClient] = None) -> int:
    args = normalize_cli_args(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(
        description="Trace the consolidated history of a loan application via the underwriting service."
    )
    parser.add_argument("--app_id", "--app-id", dest="app_id", type=str, required=True,
                        help="Application reference ID (case-insensitive), e.g. APP-2026-001.")
    parser.add_argument("--api-url", type=str, default=None,
                        help=f"Base URL of the underwriting service (default: {DEFAULT_API_URL}).")
    add_auth_arguments(parser)
    parsed = parser.parse_args(args)

    api = client or ApiClient(base_url=parsed.api_url)
    try:
        with signed_in(api, parsed.user):
            return 0 if trace_loan_via_api(api, parsed.app_id) else 1
    except ApiError as exc:
        print(f"ERROR: {exc}")
        return 1
    except requests.exceptions.RequestException as exc:
        print(_connection_help(api, exc))
        return 2


# ===========================================================================
# 3. INITIATE WORKFLOW  (POST /api/v1/workflows/run — fire-and-forget)
# ===========================================================================
def initiate_workflow_via_api(
    client: ApiClient,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Asks the service to initiate the approval workflow for NEW applications and returns immediately.
    The service logs the run and executes it in the background; progress is visible on the
    Administrator screen (or `list_loans --count`). Dry runs are synchronous previews.
    """
    if dry_run:
        summary = client.post("/workflows/run", {"dry_run": True, "limit": limit})
        print(f"\n[DRY RUN] Found {summary['found']} new loan application(s); "
              f"{summary['selected']} selected for processing.")
        for idx, item in enumerate(summary.get("items", []), 1):
            print(f"  {idx}. {item['application_id']}: {item['applicant_name']} [{item['status']}]")
        print("Dry run completed. No workflow steps executed and no state modified.\n")
        return summary

    accepted = client.post("/workflows/run?trigger=CLI", {"dry_run": False, "limit": limit})
    print(f"\nApproval workflow run #{accepted['run_id']} accepted ({accepted['status']}).")
    print(f"  New applications queued for underwriting: {accepted['found']}"
          + (f" (limit {limit})" if limit else ""))
    print("  The run continues in the background; track it on the Administrator screen "
          "or with `python -m list_loans --count`.\n")
    return accepted


def main_initiate_workflow(argv: Optional[List[str]] = None, client: Optional[ApiClient] = None) -> int:
    args = normalize_cli_args(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(
        description="Kick-starts the approval workflow for newly submitted loans via the underwriting service."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview eligible loans without executing the workflow.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of new loan applications to process.")
    parser.add_argument("--api-url", type=str, default=None,
                        help=f"Base URL of the underwriting service (default: {DEFAULT_API_URL}).")
    add_auth_arguments(parser)
    parsed = parser.parse_args(args)

    api = client or ApiClient(base_url=parsed.api_url)
    try:
        with signed_in(api, parsed.user):
            initiate_workflow_via_api(api, dry_run=parsed.dry_run, limit=parsed.limit)
        return 0
    except ApiError as exc:
        print(f"ERROR: {exc}")
        return 1
    except requests.exceptions.RequestException as exc:
        print(_connection_help(api, exc))
        return 2
