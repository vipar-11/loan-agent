"""
list_loans.py
Admin tool to query loan applications by status, via the underwriting service REST API
(GET /api/v1/applications, GET /api/v1/stats). Requires the service to be running
(`python run_api.py`); target it with --api-url or LOAN_AGENT_API_URL.

Executable via:
  python -m list_loans
  python -m list_loans --count                      # summary of counts grouped by status
  python -m list_loans --status new
  python -m list_loans --status new --count
  python -m list_loans --status "pending review"
  python -m list_loans --status pending-review
  python -m list_loans --status pending_review
"""
import sys
from loan_agent.api_cli import main_list_loans

if __name__ == "__main__":
    sys.exit(main_list_loans())
