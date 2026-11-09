"""
FastAPI dependency providers.

Resolved lazily through the module attributes of `loan_agent.db` / `loan_agent.checkpointer`
so that singletons are shared across requests and tests can swap them via
`app.dependency_overrides` (or by monkeypatching the underlying factories).
"""
import hashlib
import threading
import uuid
from typing import Any, Optional

from fastapi import Header, Request, Response

import loan_agent.checkpointer as checkpointer_module
import loan_agent.db as db_module
from api.schemas import UserIdentity
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
# Caller identity
#
# Until an authentication mechanism exists, every browser TAB is its own anonymous identity:
#   1. A request without the session header is a new session: the server issues a random UUID token and
#      returns it in the same header. (Browsers cannot add custom headers to a page navigation, so the
#      workbench establishes its session with its first API call: GET /api/v1/session.)
#   2. The front end keeps the token in sessionStorage — which is scoped to one tab — and sends it on
#      every subsequent call from that tab.
#   3. A second tab has empty sessionStorage, so it receives a different token: a file checked out in
#      one tab cannot be opened from another.
#
# The token is an unguessable identifier, not a credential check: anyone who obtains a tab's token can act
# as that tab. That is why other sessions only ever see a derived label (display_name), never the token.
# ---------------------------------------------------------------------------
SESSION_HEADER = "X-Apex-Session-Id"


def _session_label(token: str) -> str:
    """Stable, non-reversible label for a session token (safe to show to other users)."""
    return f"Session {hashlib.sha256(token.encode()).hexdigest()[:8].upper()}"


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
    session_token: Optional[str] = Header(
        None,
        alias=SESSION_HEADER,
        description="Per-browser-tab session token. Omit it on the first call; the server issues one in the "
                    "response header of the same name. Send it on every later call from that tab.",
    ),
) -> UserIdentity:
    """
    Establishes who is calling. Applied to every /api/v1 route (FastAPI caches it per request, so endpoints that
    need the identity simply declare `identity: UserIdentity = Depends(establish_user_identity)` as well).

    Today: anonymous per-tab sessions (see above). The token is echoed in the `X-Apex-Session-Id` response header.

    Authentication plug-in point: once SSO / OAuth2 / mTLS is in place, resolve the authenticated principal here
    (e.g. validate a bearer token or trusted gateway header) and return it as `UserIdentity(user_id=<stable
    subject>, display_name=<officer name>, auth_method=...)`. Everything downstream — review checkouts, audit
    trail actors — keys off `user_id` / `display_name` and needs no change.
    """
    cached = getattr(request.state, "identity", None)
    if cached is not None:
        return cached

    token = _parse_session_token(session_token)
    is_new = token is None
    if is_new:
        token = str(uuid.uuid4())

    identity = UserIdentity(user_id=token, display_name=_session_label(token), is_new_session=is_new)
    request.state.identity = identity
    response.headers[SESSION_HEADER] = token
    return identity
