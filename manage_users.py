"""
manage_users.py
Sets up sign-in accounts for the three personas — Loan Applicant, Loan Underwriter, Administrator — with an EMPTY
default password, directly in the application database configured in dbconfig.ini (the service need not be running).
Each user signs in with their user ID and an empty password, and is then required to choose a password.

Executable via:
  python manage_users.py                                   # creates 'applicant', 'underwriter', 'admin'
  python manage_users.py --applicant jane.doe --applicant-name "Jane Doe" --underwriter uw.smith --admin ops.lee
  python manage_users.py --underwriter uw.second           # add another underwriter
  python manage_users.py --reset uw.smith                  # back to the empty default password
  python manage_users.py --disable uw.second               # / --enable
  python manage_users.py --list
"""
import sys

from loan_agent.auth.manage_users_cli import main

if __name__ == "__main__":
    sys.exit(main())
