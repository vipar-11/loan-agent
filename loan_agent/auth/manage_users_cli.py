"""
User management CLI (powers the top-level `manage_users.py`).

Provisions sign-in accounts for the three personas — Loan Applicant, Loan Underwriter, Administrator — directly in
the application database configured in dbconfig.ini (the same one the service uses). It works without the service
running, which is how the very first administrator gets created.

Accounts are created with the EMPTY default password: the user signs in with their user ID and an empty password
and must choose a real password before doing anything else. Until they do, anybody who knows the user ID can claim
the account — hand user IDs out only to their owners and ask them to sign in promptly.

Idempotent: existing accounts are left unchanged (use --reset to return one to the empty default password).
"""
import argparse
import sys
from typing import Dict, List, Optional, Tuple

import loan_agent.db as db_module
from loan_agent.auth import users as user_directory
from loan_agent.auth.roles import ROLE_LABELS, Role
from loan_agent.db import ApplicationDatabase

DEFAULT_USER_IDS: Dict[Role, str] = {
    Role.APPLICANT: "applicant",
    Role.UNDERWRITER: "underwriter",
    Role.ADMINISTRATOR: "admin",
}
PERSONA_FLAGS: List[Tuple[Role, str]] = [
    (Role.APPLICANT, "applicant"),
    (Role.UNDERWRITER, "underwriter"),
    (Role.ADMINISTRATOR, "admin"),
]


def _password_status(user: dict, now_str: str) -> str:
    if not user.get("is_active"):
        return "DISABLED"
    if user.get("locked_until") and str(user["locked_until"]) > now_str:
        return f"LOCKED until {user['locked_until']}"
    if not user.get("password_hash"):
        return "EMPTY (set at first sign-in)"
    if user.get("must_change_password"):
        return "MUST CHANGE at next sign-in"
    return "SET"


def print_users(db: ApplicationDatabase) -> None:
    from datetime import datetime

    now_str = datetime.now().strftime(user_directory.TIMESTAMP_FMT)
    rows = user_directory.list_users(db)
    if not rows:
        print("No users are set up yet. Run `python manage_users.py` to create the three personas.")
        return
    header = f"{'User ID':<24} | {'Role':<17} | {'Display Name':<26} | {'Password':<30} | {'Last Sign-in':<19}"
    print("\n" + header)
    print("-" * len(header))
    for u in rows:
        role_label = ROLE_LABELS.get(Role(u["role"]), u["role"]) if u["role"] in Role.__members__ else u["role"]
        print(f"{u['user_id']:<24} | {role_label:<17} | {u['display_name'][:26]:<26} | "
              f"{_password_status(u, now_str):<30} | {str(u.get('last_login_at') or '—'):<19}")
    print()


def ensure_persona(db: ApplicationDatabase, role: Role, user_id: str, display_name: Optional[str]) -> Tuple[str, str]:
    """Creates the account if missing. Returns (outcome, message); outcome is CREATED, EXISTS or ERROR."""
    try:
        uid = user_directory.normalize_user_id(user_id)
    except ValueError as exc:
        return "ERROR", f"'{user_id}': {exc}"
    existing = db.get_user(uid)
    if existing:
        if existing["role"] != role.value:
            current = ROLE_LABELS.get(Role(existing["role"]), existing["role"])
            return "ERROR", (f"'{uid}' already exists with the {current} role; not changed. "
                             f"Choose another user ID for the {ROLE_LABELS[role]}.")
        return "EXISTS", f"'{uid}' ({ROLE_LABELS[role]}) already exists; left unchanged."
    user_directory.create_user(db, uid, role, display_name)
    return "CREATED", f"Created '{uid}' ({ROLE_LABELS[role]}) with an empty default password."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_users.py",
        description=(
            "Set up sign-in accounts for the three personas (Loan Applicant, Loan Underwriter, Administrator) with "
            "an EMPTY default password. Each user signs in with their user ID and an empty password, and is then "
            "asked to choose a password. Without persona options, creates the default accounts "
            f"{', '.join(repr(v) for v in DEFAULT_USER_IDS.values())}."
        ),
        epilog=(
            "Examples:\n"
            "  python manage_users.py\n"
            "  python manage_users.py --applicant applicant.one --applicant-name \"Mrs. Some Name\" "
            "--underwriter uw.one --admin admin.one\n"
            "  python manage_users.py --underwriter uw.second          # add another underwriter\n"
            "  python manage_users.py --reset uw.one                    # back to the empty default password\n"
            "  python manage_users.py --list\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    personas = parser.add_argument_group("personas (create if missing)")
    for role, flag in PERSONA_FLAGS:
        personas.add_argument(f"--{flag}", metavar="USER_ID", help=f"User ID for the {ROLE_LABELS[role]}.")
        personas.add_argument(f"--{flag}-name", metavar="NAME",
                              help=f"Display name for the {ROLE_LABELS[role]} (default: '{ROLE_LABELS[role]}').")
    manage = parser.add_argument_group("account maintenance")
    manage.add_argument("--reset", metavar="USER_ID", action="append", default=[],
                        help="Reset to the empty default password (signs the user out everywhere, clears lockout). "
                             "Repeatable.")
    manage.add_argument("--disable", metavar="USER_ID", action="append", default=[],
                        help="Disable an account (signs the user out everywhere). Repeatable.")
    manage.add_argument("--enable", metavar="USER_ID", action="append", default=[],
                        help="Re-enable a disabled account. Repeatable.")
    manage.add_argument("--list", action="store_true", help="List the configured users and exit.")
    return parser


def main(argv: Optional[List[str]] = None, db: Optional[ApplicationDatabase] = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    db = db or db_module.get_application_database()

    if args.list:
        print_users(db)
        return 0

    failures = 0
    requested = {role: getattr(args, flag) for role, flag in PERSONA_FLAGS}
    maintenance = args.reset or args.disable or args.enable
    if not any(requested.values()) and not maintenance:
        requested = dict(DEFAULT_USER_IDS)  # no options: the three default personas

    created = 0
    for role, flag in PERSONA_FLAGS:
        if not requested.get(role):
            continue
        outcome, message = ensure_persona(db, role, requested[role], getattr(args, f"{flag}_name"))
        print(f"[{outcome}] {message}")
        failures += outcome == "ERROR"
        created += outcome == "CREATED"

    for action, ids in (("reset", args.reset), ("disable", args.disable), ("enable", args.enable)):
        for raw in ids:
            try:
                if action == "reset":
                    user_directory.reset_password(db, raw)
                    print(f"[RESET] '{raw.strip().lower()}' now has the empty default password "
                          f"(signed out everywhere; must choose a new password at next sign-in).")
                else:
                    user_directory.set_active(db, raw, action == "enable")
                    print(f"[{action.upper()}D] '{raw.strip().lower()}' is now {action}d.")
            except (KeyError, ValueError) as exc:
                print(f"[ERROR] {action} '{raw}': {str(exc).strip(chr(39) + chr(34))}")
                failures += 1

    print_users(db)
    if created or args.reset:
        print("Next: each user signs in at http://127.0.0.1:8000/login with their user ID and an EMPTY password,\n"
              "      then chooses a password. Until then anybody who knows the user ID can claim the account.\n")
    return 1 if failures else 0
