"""
User directory: account provisioning and password management for locally managed users.

Accounts are created WITHOUT a password — the "empty default password". The first sign-in with an empty password
only lets the user choose a real password (the session is restricted until then; see providers.py / sessions.py).
An administrator can return an account to that state with `reset_password()`.
"""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from loan_agent.auth.passwords import hash_password, validate_new_password, verify_password
from loan_agent.auth.principal import PasswordChangeError, Principal
from loan_agent.auth.roles import ROLE_LABELS, Role, parse_role
from loan_agent.config import AuthConfig
from loan_agent.db import ApplicationDatabase

LOCAL_PROVIDER = "local"
USER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._@-]{1,63}$")  # also admits e-mail-style IDs (future SSO)
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    return datetime.now().strftime(TIMESTAMP_FMT)


def normalize_user_id(raw: Optional[str]) -> str:
    """User IDs are case-insensitive: stored and compared in lowercase. Raises ValueError when malformed."""
    clean = (raw or "").strip().lower()
    if not USER_ID_PATTERN.match(clean):
        raise ValueError(
            "User IDs are 2-64 characters: letters, digits and . _ @ - (starting with a letter or digit)."
        )
    return clean


def principal_for(user: Dict[str, Any], auth_method: str) -> Principal:
    """Builds the principal for a stored user record (role and flags are always read fresh from the record)."""
    return Principal(
        user_id=user["user_id"],
        display_name=user["display_name"],
        role=Role(user["role"]),
        auth_method=auth_method,
        password_change_required=bool(user.get("must_change_password")) or not user.get("password_hash"),
    )


def get_user(db: ApplicationDatabase, user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return db.get_user(normalize_user_id(user_id))
    except ValueError:
        return None


def list_users(db: ApplicationDatabase) -> List[Dict[str, Any]]:
    return db.list_users()


def create_user(
    db: ApplicationDatabase, user_id: str, role: Role | str, display_name: Optional[str] = None
) -> Dict[str, Any]:
    """Creates an active local account with the empty default password. Raises ValueError if it already exists."""
    uid = normalize_user_id(user_id)
    role = role if isinstance(role, Role) else parse_role(role)
    name = " ".join((display_name or "").split()) or ROLE_LABELS[role]
    return db.create_user(
        uid, name, role.value,
        password_hash=None, must_change_password=True, is_active=True, auth_provider=LOCAL_PROVIDER,
    )


class SignupError(ValueError):
    """Self-registration was refused (user ID already taken, or the password does not meet policy)."""


def self_register(
    db: ApplicationDatabase, user_id: str, password: str, display_name: Optional[str], config: AuthConfig
) -> Dict[str, Any]:
    """
    Self-service sign-up for Loan Applicants ONLY — the one role a user may grant themselves; Loan Underwriter and
    Administrator accounts are always provisioned by an administrator (manage_users.py). Unlike an administrator-
    provisioned account, the password is chosen here and there is no "empty default password" step: nobody else
    ever knows this user ID, so there is no window in which another person could claim it.
    """
    try:
        uid = normalize_user_id(user_id)
    except ValueError as exc:
        raise SignupError(str(exc)) from exc
    if db.get_user(uid):
        raise SignupError("That user ID is already taken. Choose another.")
    try:
        validate_new_password(password, uid, config.password_min_length)
    except ValueError as exc:
        raise SignupError(str(exc)) from exc

    name = " ".join((display_name or "").split()) or uid
    now = _now_str()
    return db.create_user(
        uid, name, Role.APPLICANT.value,
        password_hash=hash_password(password), must_change_password=False, is_active=True,
        auth_provider=LOCAL_PROVIDER, password_changed_at=now,
    )


def update_profile(
    db: ApplicationDatabase, user_id: str, role: Optional[Role | str] = None, display_name: Optional[str] = None
) -> Dict[str, Any]:
    uid = normalize_user_id(user_id)
    updates: Dict[str, Any] = {}
    if role is not None:
        updates["role"] = (role if isinstance(role, Role) else parse_role(role)).value
    if display_name is not None and display_name.strip():
        updates["display_name"] = " ".join(display_name.split())
    user = db.update_user(uid, updates)
    if user is None:
        raise KeyError(f"User '{uid}' does not exist.")
    return user


def reset_password(db: ApplicationDatabase, user_id: str) -> Dict[str, Any]:
    """
    Returns the account to the empty default password (the user must choose a new one at next sign-in),
    clears any lockout and signs the user out of every session.
    """
    uid = normalize_user_id(user_id)
    user = db.update_user(uid, {
        "password_hash": None, "must_change_password": True, "failed_attempts": 0, "locked_until": None,
    })
    if user is None:
        raise KeyError(f"User '{uid}' does not exist.")
    db.revoke_user_auth_sessions(uid, _now_str())
    return user


def set_active(db: ApplicationDatabase, user_id: str, active: bool) -> Dict[str, Any]:
    """Enables / disables an account. Disabling also ends every session of the user immediately."""
    uid = normalize_user_id(user_id)
    user = db.update_user(uid, {"is_active": active})
    if user is None:
        raise KeyError(f"User '{uid}' does not exist.")
    if not active:
        db.revoke_user_auth_sessions(uid, _now_str())
    return user


def change_password(
    db: ApplicationDatabase,
    user_id: str,
    current_password: str,
    new_password: str,
    config: AuthConfig,
    keep_session_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Self-service password change. The current password must be supplied — for an account still on the empty
    default password that is the empty string. Other sessions of the user are signed out; the session identified
    by `keep_session_hash` (the one making the change) stays signed in. Raises PasswordChangeError.
    """
    uid = normalize_user_id(user_id)
    user = db.get_user(uid)
    if not user or not user["is_active"]:
        raise PasswordChangeError("This account cannot change its password.")
    if user.get("auth_provider") != LOCAL_PROVIDER:
        raise PasswordChangeError("This account signs in through an external identity provider; "
                                  "its password is managed there.")
    current_ok = (current_password or "") == "" if not user.get("password_hash") else \
        verify_password(current_password or "", user["password_hash"])
    if not current_ok:
        raise PasswordChangeError("The current password is incorrect.")
    try:
        validate_new_password(new_password, uid, config.password_min_length)
    except ValueError as exc:
        raise PasswordChangeError(str(exc)) from exc
    if user.get("password_hash") and verify_password(new_password, user["password_hash"]):
        raise PasswordChangeError("The new password must be different from the current password.")

    now = _now_str()
    updated = db.update_user(uid, {
        "password_hash": hash_password(new_password), "must_change_password": False, "password_changed_at": now,
        "failed_attempts": 0, "locked_until": None,
    })
    db.revoke_user_auth_sessions(uid, now, except_token_hash=keep_session_hash)
    return updated  # type: ignore[return-value]
