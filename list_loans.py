"""
list_loans.py
Admin tool to query loan applications from persistent data store by status.
Executable via:
  python -m list_loans
  python -m list_loans --count
  python -m list_loans --status new
  python -m list_loans --status new --count
  python -m list_loans --status "pending review"
  python -m list_loans --status pending-review
  python -m list_loans --status pending_review
"""
import sys
from loan_agent.admin import main_list_loans

if __name__ == "__main__":
    sys.exit(main_list_loans())
