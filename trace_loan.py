"""
trace_loan.py
Admin tool to trace the consolidated history of a loan application, via the underwriting
service REST API (GET /api/v1/applications/{id} and /api/v1/applications/{id}/audit-trail).
Requires the service to be running (`python run_api.py`); target it with --api-url or LOAN_AGENT_API_URL.
Signs in as an Administrator: --user USER_ID (or LOAN_AGENT_USER) with the password from LOAN_AGENT_PASSWORD or a
prompt, or a bearer token in LOAN_AGENT_TOKEN.

Executable via:
  python -m trace_loan --app_id app-2026-001 (case-insensitive)
"""
import sys
from loan_agent.api_cli import main_trace_loan

if __name__ == "__main__":
    sys.exit(main_trace_loan())
