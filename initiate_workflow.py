"""
initiate_workflow.py
Kick-starts the underwriting approval workflow for new loan applications, via the
underwriting service REST API (POST /api/v1/workflows/run). Requires the service to be
running (`python run_api.py`); target it with --api-url or LOAN_AGENT_API_URL.

Executable via:
  python -m initiate_workflow
  python -m initiate_workflow --dry-run
  python -m initiate_workflow --limit 10
"""
import sys
from loan_agent.api_cli import main_initiate_workflow

if __name__ == "__main__":
    sys.exit(main_initiate_workflow())
