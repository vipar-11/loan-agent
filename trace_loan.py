"""
trace_loan.py
Admin tool to trace consolidated history of a loan application across
the Application Database and LangGraph checkpointer.
Executable via:
  python -m trace_loan --app_id app-2026-001 (case-insensitive)
"""
import sys
from loan_agent.admin import main_trace_loan

if __name__ == "__main__":
    sys.exit(main_trace_loan())
