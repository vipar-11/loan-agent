"""
Sign-in providers: how a user proves who they are when signing in.

`PasswordVerifier` is the contract for user ID / password sign-in. `LocalPasswordVerifier` checks the application's
own user directory. An LDAP / Active Directory bind would be another implementation of the same contract.

Redirect-based SSO (OIDC / SAML) does not take a password, so it is not a PasswordVerifier. It plugs in at the same
level: an SSO callback validates the IdP's assertion, maps it to a `Principal` (e.g. via `app_users.auth_provider` /
`external_subject`, or group claims -> Role) and hands that principal to `SessionManager.issue()`, exactly like
password sign-in does. See api/security.py for the request-side counterpart (how each request is authenticated).
"""
import logging
from datetime import datetime, timedelta
from typing import Protocol

from loan_agent.auth.passwords import MAX_PASSWORD_LENGTH, burn_verification_time, verify_password
from loan_agent.auth.principal import AuthenticationError, Principal
from loan_agent.auth.users import LOCAL_PROVIDER, TIMESTAMP_FMT, normalize_user_id, principal_for
from loan_agent.config import AuthConfig
from loan_agent.db import ApplicationDatabase

logger = logging.getLogger("apex.auth")

# One message for every failure, so the response never reveals whether a user ID exists or is locked.
SIGN_IN_FAILED = (
    "Sign-in failed: invalid user ID or password. Accounts are locked for a while after repeated failed attempts."
)


class PasswordVerifier(Protocol):
    provider_id: str
    label: str

    def verify(self, user_id: str, password: str) -> Principal:
        """Returns the principal for valid credentials; raises AuthenticationError otherwise."""
        ...


class LocalPasswordVerifier:
    """
    Verifies passwords against `app_users`, with account lockout.

    Empty default password: an account that has no password yet accepts the empty password, and the resulting
    principal has `password_change_required=True` — such a session can do nothing but set a real password.
    """

    provider_id = LOCAL_PROVIDER
    label = "User ID & password"

    def __init__(self, db: ApplicationDatabase, config: AuthConfig):
        self.db = db
        self.config = config

    def verify(self, user_id: str, password: str) -> Principal:
        password = password or ""
        try:
            uid = normalize_user_id(user_id)
        except ValueError:
            burn_verification_time(password)
            raise AuthenticationError(SIGN_IN_FAILED)
        if len(password) > MAX_PASSWORD_LENGTH:
            burn_verification_time(password)
            raise AuthenticationError(SIGN_IN_FAILED)

        user = self.db.get_user(uid)
        now = datetime.now().replace(microsecond=0)
        now_str = now.strftime(TIMESTAMP_FMT)
        if not user or not user["is_active"] or user.get("auth_provider") != LOCAL_PROVIDER:
            burn_verification_time(password)
            raise AuthenticationError(SIGN_IN_FAILED)
        if user.get("locked_until") and str(user["locked_until"]) > now_str:
            burn_verification_time(password)  # locked: not even a correct password gets in, and nothing is counted
            raise AuthenticationError(SIGN_IN_FAILED, user_id=uid)

        if user.get("password_hash"):
            valid = verify_password(password, user["password_hash"])
        else:
            burn_verification_time(password)
            valid = password == ""  # empty default password
        if not valid:
            locked_until = (now + timedelta(minutes=self.config.lockout_minutes)).strftime(TIMESTAMP_FMT)
            self.db.record_failed_sign_in(uid, self.config.lockout_threshold, locked_until)
            if (self.db.get_user(uid) or {}).get("locked_until") == locked_until:
                logger.warning("Account %s locked until %s after %d failed sign-ins",
                               uid, locked_until, self.config.lockout_threshold)
            raise AuthenticationError(SIGN_IN_FAILED, user_id=uid)

        self.db.record_successful_sign_in(uid, now_str)
        return principal_for(user, auth_method="password")
