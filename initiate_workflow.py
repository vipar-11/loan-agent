"""
initiate_workflow.py
Kick-starts the underwriting graph workflow for new loan applications.
Executable via:
  python -m initiate_workflow
  python -m initiate_workflow --dry-run
  python -m initiate_workflow --limit 10
"""
import sys
from loan_agent.workflow_runner import main

if __name__ == "__main__":
    sys.exit(main())
