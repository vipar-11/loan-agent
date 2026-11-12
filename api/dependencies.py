"""
FastAPI dependency providers.

Resolved lazily through the module attributes of `loan_agent.db` / `loan_agent.checkpointer`
so that singletons are shared across requests and tests can swap them via
`app.dependency_overrides` (or by monkeypatching the underlying factories).
"""
import threading
import uuid
from typing import Any, Callable, Optional

from fastapi import Depends, Header, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer

import loan_agent.checkpointer as checkpointer_module
import loan_agent.db as db_module
from api.schemas import UserIdentity
from api.security import RequestAuthError
from loan_agent.auth import Permission, Principal, SessionManager
from loan_agent.config import AuthConfig, load_auth_config
from loan_agent.db import ApplicationDatabase

# ---------------------------------------------------------------------------
# Workflow concurrency control
#
# Storage is already thread-safe on its own: every ApplicationDatabase method holds the DB's
# internal lock, and the LangGraph Sqlite/Postgres savers serialize access to their connection.
#
# PIPELINE_LOCK serializes agentic pipeline runs (initiate_workflow batches, single-application
# dispatches) so two concurrent runs can never pick up and double-process the same NEW application.
# It is held for the full duration of a run (N applications x several LLM calls).
#
# Human decisions must NOT take PIPELINE_LOCK — waiting on the batch is what made Approve/Reject
# "hang" whenever the periodic initiate_workflow job was in progress. Pipeline runs only touch NEW /
# PENDING_PROCESSING files, decisions only PENDING_REVIEW ones. Reviewer exclusivity on a
# PENDING_REVIEW file is provided by the persisted review checkout (api/services/checkout.py).
# ---------------------------------------------------------------------------
PIPELINE_LOCK = threading.Lock()


def get_db() -> ApplicationDatabase:
    """Singleton application database (applications + audit trail)."""
    return db_module.get_application_database()


def get_checkpointer() -> Any:
    """Singleton workflow checkpointer (distinct database from the application DB)."""
    return checkpointer_module.get_langgraph_checkpointer()


# ---------------------------------------------------------------------------
# Authentication (who is calling) — see api/security.py for the mechanisms
#
#   resolve_principal()        runs the app's authenticator chain; None when the request is anonymous.
#                              THE seam for authentication: tests (and future mechanisms) override this one provider.
#   authenticate_request()     401 unless a principal was resolved.
#   establish_user_identity()  the principal + this browser tab's session (below) as a UserIdentity.
#   require_signed_in()        ... and refuses restricted sessions that must set a password first (403).
#   require_permission(p)      ... and requires permission p (403) — role-based access control.
# ---------------------------------------------------------------------------
def get_auth_config(request: Request) -> AuthConfig:
    config = getattr(request.app.state, "auth_config", None)
    if config is None:
        config = request.app.state.auth_config = load_auth_config()
    return config


def get_session_manager(
    db: ApplicationDatabase = Depends(get_db), config: AuthConfig = Depends(get_auth_config)
) -> SessionManager:
    return SessionManager(db, config)


# Documents bearer sign-in in the OpenAPI schema, so the interactive docs (/docs) offer an "Authorize" button that
# signs in through POST /api/v1/auth/token. The header itself is read by BearerTokenAuthenticator.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def resolve_principal(
    request: Request,
    sessions: SessionManager = Depends(get_session_manager),
    _documented_bearer: Optional[str] = Depends(oauth2_scheme),
) -> Optional[Principal]:
    for authenticator in getattr(request.app.state, "authenticators", None) or []:
        try:
            principal = authenticator.authenticate(request, sessions)
        except RequestAuthError as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
            raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers) from exc
        if principal is not None:
            return principal
    return None


NOT_SIGNED_IN = "Not signed in, or your session has expired. Sign in to continue."
PASSWORD_CHANGE_REQUIRED = "You must set a new password before continuing (POST /api/v1/auth/password)."
PASSWORD_CHANGE_HEADER = "X-Apex-Password-Change-Required"


def authenticate_request(principal: Optional[Principal] = Depends(resolve_principal)) -> Principal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=NOT_SIGNED_IN, headers={"WWW-Authenticate": "Bearer"}
        )
    return principal


# ---------------------------------------------------------------------------
# Browser-tab sessions (review-checkout exclusivity)
#
# A signed-in user may have the workbench open in several tabs. Reviews stay exclusive PER TAB, as before sign-in
# existed: a file checked out in one tab cannot be opened from another tab — of another user or of the same user.
#   1. The workbench's first call in a tab, GET /api/v1/session, is issued a random tab token in the
#      X-Apex-Session-Id response header.
#   2. The tab keeps it in sessionStorage (scoped to that tab) and sends it on every later call.
#   3. Review checkouts are keyed on user + tab (UserIdentity.holder_key). API clients that send no tab header act
#      as the user as a whole.
# The tab token is not a credential (sign-in is): it only distinguishes one signed-in user's tabs from each other.
# ---------------------------------------------------------------------------
SESSION_HEADER = "X-Apex-Session-Id"
SESSION_ENDPOINT = "/api/v1/session"


def _parse_session_token(raw: Optional[str]) -> Optional[str]:
    """Accepts only well-formed UUID tokens (canonical lowercase form); anything else is treated as absent."""
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw.strip()))
    except (ValueError, AttributeError):
        return None


def establish_user_identity(
    request: Request,
    response: Response,
    principal: Principal = Depends(authenticate_request),
    session_token: Optional[str] = Header(
        None,
        alias=SESSION_HEADER,
        description="Per-browser-tab session token, issued by GET /api/v1/session. Distinguishes one signed-in "
                    "user's browser tabs for review checkouts. API clients may omit it.",
    ),
) -> UserIdentity:
    """
    The signed-in caller (applied to every protected /api/v1 route; FastAPI caches it per request, so endpoints that
    need the identity simply declare `identity: UserIdentity = Depends(establish_user_identity)` as well).
    """
    cached = getattr(request.state, "identity", None)
    if cached is not None:
        return cached

    token = _parse_session_token(session_token)
    is_new = token is None and request.url.path == SESSION_ENDPOINT
    if is_new:
        token = str(uuid.uuid4())

    identity = UserIdentity.from_principal(principal, tab_session=token, is_new_session=is_new)
    request.state.identity = identity
    if token:
        response.headers[SESSION_HEADER] = token
    return identity


def require_signed_in(identity: UserIdentity = Depends(establish_user_identity)) -> UserIdentity:
    """A fully signed-in user: sessions that must first replace the empty default password are refused."""
    if identity.password_change_required:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PASSWORD_CHANGE_REQUIRED,
                            headers={PASSWORD_CHANGE_HEADER: "true"})
    return identity


ACCESS_DENIED = "You do not have access to this resource."


def _forbidden(identity: UserIdentity) -> HTTPException:
    """Deliberately uninformative: says nothing about the caller's role or the access the resource would need."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ACCESS_DENIED)


def require_permission(*permissions: Permission) -> Callable[..., UserIdentity]:
    """Dependency factory: the caller must hold ALL of `permissions`."""
    needed = {p.value for p in permissions}

    def _check(identity: UserIdentity = Depends(require_signed_in)) -> UserIdentity:
        if not needed <= set(identity.permissions):
            raise _forbidden(identity)
        return identity

    _check.__name__ = f"require_{'_and_'.join(sorted(needed))}".replace(":", "_")
    return _check


def require_any_permission(*permissions: Permission) -> Callable[..., UserIdentity]:
    """Dependency factory: the caller must hold AT LEAST ONE of `permissions`."""
    allowed = {p.value for p in permissions}

    def _check(identity: UserIdentity = Depends(require_signed_in)) -> UserIdentity:
        if not allowed & set(identity.permissions):
            raise _forbidden(identity)
        return identity

    _check.__name__ = f"require_any_{'_or_'.join(sorted(allowed))}".replace(":", "_")
    return _check
