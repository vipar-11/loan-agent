"""
Authentication & authorization core (UI- and HTTP-agnostic).

    roles.py      Role, Permission, ROLE_PERMISSIONS — role-based access control
    principal.py  Principal — the authenticated subject every mechanism produces
    passwords.py  scrypt password hashing + password policy
    users.py      user directory: provisioning, password reset / change (empty default password)
    providers.py  sign-in providers: PasswordVerifier contract, LocalPasswordVerifier (with lockout)
    sessions.py   SessionManager: mechanism-independent sign-in sessions (cookie / bearer token)

The HTTP side (how each request is authenticated, permission dependencies, sign-in endpoints) lives in
api/security.py and api/routers/auth.py.
"""
from loan_agent.auth.principal import AuthenticationError, PasswordChangeError, Principal
from loan_agent.auth.providers import SIGN_IN_FAILED, LocalPasswordVerifier, PasswordVerifier
from loan_agent.auth.roles import (
    PAGES,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    home_path_for,
    parse_role,
    permissions_for,
)
from loan_agent.auth.sessions import IssuedSession, SessionManager, hash_token
from loan_agent.auth.users import SignupError, self_register

__all__ = [
    "AuthenticationError",
    "IssuedSession",
    "LocalPasswordVerifier",
    "PAGES",
    "PasswordChangeError",
    "PasswordVerifier",
    "Permission",
    "Principal",
    "ROLE_LABELS",
    "ROLE_PERMISSIONS",
    "Role",
    "SIGN_IN_FAILED",
    "SessionManager",
    "SignupError",
    "hash_token",
    "home_path_for",
    "parse_role",
    "permissions_for",
    "self_register",
]
