"""
The authenticated principal — the single output of every authentication mechanism.

Password sign-in produces one today; an SSO callback or a validated IdP-issued bearer token (JWT) will produce one
the same way. Everything downstream (permission checks, review checkouts, audit-trail actors) only ever sees a
Principal, so new mechanisms plug in without touching business code.
"""
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

from loan_agent.auth.roles import ROLE_LABELS, Permission, Role, home_path_for, permissions_for


@dataclass(frozen=True)
class Principal:
    user_id: str                      # stable subject (local user ID today; IdP subject / email for SSO later)
    display_name: str
    role: Role
    auth_method: str = "password"     # how the principal authenticated, e.g. "password", later "oidc:<provider>"
    permissions: FrozenSet[Permission] = field(default=None)  # type: ignore[assignment]  # defaults from the role
    password_change_required: bool = False

    def __post_init__(self):
        if self.permissions is None:
            object.__setattr__(self, "permissions", permissions_for(self.role))

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, str(self.role))

    @property
    def home_path(self) -> Optional[str]:
        return home_path_for(self.permissions)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def audit_actor(self) -> str:
        """How the principal is recorded in the regulatory audit trail: name plus the stable user ID."""
        return f"{self.display_name} ({self.user_id})"


class AuthenticationError(Exception):
    """
    Credentials were presented but are not valid (wrong password, unknown user, locked, expired session...).
    `user_id` is set only when the attempt targeted an existing account — safe to log. Whatever an unknown "user ID"
    was is never kept (people do type their password into the user ID field).
    """

    def __init__(self, message: str, user_id: Optional[str] = None):
        super().__init__(message)
        self.user_id = user_id


class PasswordChangeError(ValueError):
    """A password change was refused (wrong current password, policy violation, non-local account)."""
