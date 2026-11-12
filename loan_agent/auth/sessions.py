"""
Sign-in sessions — independent of how the user signed in.

`issue()` turns any authenticated `Principal` (password today, SSO later) into an opaque random token. The browser
holds it in an HttpOnly cookie; API clients present it as `Authorization: Bearer <token>`. Only a SHA-256 hash of the
token is stored, so a copy of the database does not yield usable sessions.

`resolve()` re-reads the user on every request: disabling an account, resetting its password or changing its role
takes effect immediately, and sessions can be revoked server-side (sign-out, password change).
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from loan_agent.auth.principal import Principal
from loan_agent.auth.users import TIMESTAMP_FMT, principal_for
from loan_agent.config import AuthConfig
from loan_agent.db import ApplicationDatabase

TOUCH_INTERVAL_SECONDS = 60  # last_seen_at is written at most this often per session (the desk polls every few s)
MAX_TOKEN_LENGTH = 128


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedSession:
    token: str
    expires_at: datetime  # absolute end of the session (the idle timeout may end it earlier)


class SessionManager:
    def __init__(self, db: ApplicationDatabase, config: AuthConfig):
        self.db = db
        self.config = config

    @staticmethod
    def _now() -> datetime:
        return datetime.now().replace(microsecond=0)

    def issue(self, principal: Principal) -> IssuedSession:
        now = self._now()
        expires = now + timedelta(hours=self.config.session_max_hours)
        token = secrets.token_urlsafe(32)
        self.db.purge_auth_sessions((now - timedelta(days=1)).strftime(TIMESTAMP_FMT))
        self.db.create_auth_session(
            hash_token(token), principal.user_id, principal.auth_method,
            now.strftime(TIMESTAMP_FMT), expires.strftime(TIMESTAMP_FMT),
        )
        return IssuedSession(token=token, expires_at=expires)

    def resolve(self, token: Optional[str], now: Optional[datetime] = None) -> Optional[Principal]:
        """The principal behind a live session token, or None (unknown, revoked, expired, idle, user disabled)."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        token_hash = hash_token(token)
        session = self.db.get_auth_session(token_hash)
        if not session or session.get("revoked_at"):
            return None
        now = now or self._now()
        now_str = now.strftime(TIMESTAMP_FMT)
        last_seen = datetime.strptime(str(session["last_seen_at"]), TIMESTAMP_FMT)
        if str(session["expires_at"]) <= now_str or now - last_seen >= timedelta(minutes=self.config.session_idle_minutes):
            return None
        user = self.db.get_user(session["user_id"])
        if not user or not user["is_active"]:
            return None
        if (now - last_seen).total_seconds() >= TOUCH_INTERVAL_SECONDS:
            self.db.touch_auth_session(token_hash, now_str)
        return principal_for(user, auth_method=session["auth_method"])

    def revoke(self, token: Optional[str]) -> bool:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        return self.db.revoke_auth_session(hash_token(token), self._now().strftime(TIMESTAMP_FMT))
