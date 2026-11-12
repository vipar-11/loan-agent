"""
Review checkouts: a PENDING_REVIEW application can be open for review by only one identity at a time.

The holder is the caller's `UserIdentity` as established by `api.dependencies.establish_user_identity()`: the
signed-in user in one browser tab (or the user as a whole, for API clients). Holders are compared on
`UserIdentity.holder_key` only, so another tab of the same user is "another session" too.
Other callers are never told who holds a file: listings, details and conflict messages say "another session"
(OTHER_HOLDER_LABEL). Only the holder sees its own label; the regulatory audit trail records the real holder.

Lifecycle
    checkout()        The file is launched from the Underwriter Desk. Grants a lease of CHECKOUT_TTL_SECONDS, or
                      refuses (CheckoutConflict) while another identity holds an unexpired checkout.
                      Calling it again as the holder re-opens the file (e.g. after a page reload in the same tab).
    renew()           Heartbeat sent every HEARTBEAT_SECONDS while the review is open. Only extends a checkout the
                      caller still holds — it never re-creates one, so a force-release by an administrator (or a
                      lapsed lease taken over by someone else) is final and the open review is told it lost the file.
    release()         The review is closed (or an administrator force-releases a stuck checkout).
    begin_decision()  Approve / Reject: atomically claims the holder's checkout (ACTIVE -> DECIDING) so exactly one
                      determination can run. Callers that never checked out (API clients) get one implicitly,
                      provided nobody else holds the file.
    end_decision() / abort_decision()
                      Drop the checkout after the determination is recorded / hand it back to the holder on failure.

A lease that is not renewed (tab closed without notice, laptop asleep, crash) lapses after CHECKOUT_TTL_SECONDS and
the file becomes available again, so a checkout can never lock a file permanently.

All exclusivity is enforced by single conditional SQL statements in `ApplicationDatabase`, so it holds across
threads, worker processes and hosts that share the application database.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from api.schemas import CheckoutInfo, UserIdentity
from loan_agent.db import ApplicationDatabase

CHECKOUT_TTL_SECONDS = 300  # lease granted by each checkout / heartbeat
HEARTBEAT_SECONDS = 60  # desk renews well before the lease lapses (tolerates a couple of missed beats)

TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
REVIEWABLE_STATUS = "PENDING_REVIEW"
OTHER_HOLDER_LABEL = "another session"  # what every non-holder is told about who has the file


class CheckoutConflict(Exception):
    """The application is checked out by another identity (or a determination is already being recorded)."""

    def __init__(self, message: str, holder: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.holder = holder


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TIMESTAMP_FMT)


def _window(now: Optional[datetime]) -> Tuple[str, str]:
    now = now or _now()
    return _fmt(now), _fmt(now + timedelta(seconds=CHECKOUT_TTL_SECONDS))


def _is_active(row: Optional[Dict[str, Any]], now_str: str) -> bool:
    return bool(row) and str(row["expires_at"]) > now_str


def _holder_label(row: Dict[str, Any], viewer: Optional[UserIdentity]) -> str:
    """The holder's own label for the holder; an anonymous label for everybody else."""
    if viewer is not None and row.get("holder_key") == viewer.holder_key:
        return row["checked_out_by"]
    return OTHER_HOLDER_LABEL


def to_info(row: Dict[str, Any], viewer: Optional[UserIdentity] = None) -> CheckoutInfo:
    """Public projection of a checkout row, from `viewer`'s perspective. Never reveals another holder's identity."""
    return CheckoutInfo(
        application_id=row["application_id"],
        checked_out_by=_holder_label(row, viewer),
        held_by_you=viewer is not None and row["holder_key"] == viewer.holder_key,
        checked_out_at=row["checked_out_at"],
        expires_at=row["expires_at"],
        state=row.get("state") or "ACTIVE",
        ttl_seconds=CHECKOUT_TTL_SECONDS,
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )


def _conflict_message(row: Dict[str, Any], viewer: UserIdentity) -> str:
    app_id = row.get("application_id", "?")
    if row.get("state") == "DECIDING":
        where = "in this session" if row.get("holder_key") == viewer.holder_key else f"by {OTHER_HOLDER_LABEL}"
        return f"A determination for application '{app_id}' is already being recorded {where}."
    return (
        f"Application '{app_id}' is checked out by {OTHER_HOLDER_LABEL}. It can be opened once it is closed "
        f"there, or automatically after {CHECKOUT_TTL_SECONDS // 60} minutes of inactivity."
    )


def _require_application(db: ApplicationDatabase, application_id: str) -> Dict[str, Any]:
    record = db.get_application(application_id)
    if not record:
        raise KeyError(f"Application '{application_id}' not found.")
    return record


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def active_checkouts(
    db: ApplicationDatabase, viewer: Optional[UserIdentity] = None, now: Optional[datetime] = None
) -> Dict[str, CheckoutInfo]:
    """Unexpired checkouts keyed by canonical application ID (one query, for queue listings)."""
    now_str = _fmt(now or _now())
    return {row["application_id"]: to_info(row, viewer) for row in db.list_checkouts(active_at=now_str)}


def active_checkout(
    db: ApplicationDatabase,
    application_id: str,
    viewer: Optional[UserIdentity] = None,
    now: Optional[datetime] = None,
) -> Optional[CheckoutInfo]:
    row = db.get_checkout(application_id)
    return to_info(row, viewer) if _is_active(row, _fmt(now or _now())) else None


# ---------------------------------------------------------------------------
# Launch / heartbeat / close
# ---------------------------------------------------------------------------
def _acquire(
    db: ApplicationDatabase, record: Dict[str, Any], identity: UserIdentity, now: Optional[datetime],
    via: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Returns (checkout row, newly_acquired). Raises ValueError / CheckoutConflict."""
    app_id = record["application_id"]
    if (record.get("status") or "").upper() != REVIEWABLE_STATUS:
        raise ValueError(
            f"Application '{app_id}' is not awaiting human review (status={record.get('status')}); "
            f"only PENDING_REVIEW files can be checked out."
        )
    now_str, expires_str = _window(now)
    before = db.get_checkout(app_id)
    # upsert: a row always exists afterwards
    row = db.acquire_checkout(app_id, identity.holder_key, identity.display_name, now_str, expires_str)
    if row is None or row["holder_key"] != identity.holder_key or row.get("state") != "ACTIVE":
        raise CheckoutConflict(_conflict_message(row or {}, identity), holder=row)

    renewed = before is not None and _is_active(before, now_str) and before["holder_key"] == identity.holder_key
    if not renewed:
        how = f" via {via}" if via else ""
        details = f"Checked out for review by {identity.display_name}{how} (lease {CHECKOUT_TTL_SECONDS}s)."
        if before is not None and before["holder_key"] != identity.holder_key:  # only possible once it had expired
            details += f" Previous checkout by {before['checked_out_by']} had expired."
        db.add_audit_event(app_id, "CHECKOUT_ACQUIRED", actor=identity.audit_actor,
                           from_status=record["status"], to_status=record["status"], details=details)
    return row, not renewed


def checkout(
    db: ApplicationDatabase, application_id: str, identity: UserIdentity, now: Optional[datetime] = None,
    via: Optional[str] = None,
) -> CheckoutInfo:
    """
    Launch (or re-open) a review. Raises KeyError (unknown application), ValueError (not PENDING_REVIEW)
    or CheckoutConflict (held by another identity). `via` is noted in the audit trail (e.g. "Get Work").
    """
    record = _require_application(db, application_id)
    row, _ = _acquire(db, record, identity, now, via=via)
    return to_info(row, identity)


GET_WORK_BATCH = 25  # candidates fetched per round; more than enough to absorb concurrent "Get Work" clicks
GET_WORK_ROUNDS = 3


def checkout_next(
    db: ApplicationDatabase, identity: UserIdentity, now: Optional[datetime] = None
) -> Optional[CheckoutInfo]:
    """
    "Get Work": checks out the highest-priority file for `identity` and returns the checkout, or None when nothing
    is available. Priority (decided by the database query, see next_reviewable_application_ids): a file the caller
    already holds first — a reviewer never accumulates several — then the oldest PENDING_REVIEW submission that
    nobody else has open. Each candidate is claimed with the same atomic acquire as a manual launch, so two
    reviewers clicking at once never receive the same file: the one that loses the race just gets the next file.
    """
    now_str, _ = _window(now)
    tried = set()
    for _ in range(GET_WORK_ROUNDS):
        candidates = [a for a in db.next_reviewable_application_ids(identity.holder_key, now_str, GET_WORK_BATCH)
                      if a not in tried]
        if not candidates:
            return None
        for app_id in candidates:
            tried.add(app_id)
            try:
                return checkout(db, app_id, identity, now, via="Get Work")
            except (CheckoutConflict, ValueError, KeyError):
                continue  # claimed by someone else meanwhile, or no longer awaiting review: next candidate
    return None


def renew(
    db: ApplicationDatabase, application_id: str, identity: UserIdentity, now: Optional[datetime] = None
) -> CheckoutInfo:
    """Heartbeat. Raises KeyError (unknown application) or CheckoutConflict (the caller no longer holds it)."""
    record = _require_application(db, application_id)
    app_id = record["application_id"]
    _, expires_str = _window(now)
    if not db.renew_checkout(app_id, identity.holder_key, expires_str):
        row = db.get_checkout(app_id)
        if row and _is_active(row, _fmt(now or _now())):
            reason = f"it is now checked out by {OTHER_HOLDER_LABEL}"
            if row["holder_key"] == identity.holder_key:
                reason = "a determination is being recorded"
        elif (record.get("status") or "").upper() != REVIEWABLE_STATUS:
            reason = f"it is no longer awaiting review (status={record.get('status')})"
        else:
            reason = "the checkout was released (by an administrator, or it lapsed)"
        raise CheckoutConflict(f"Your checkout of application '{app_id}' is no longer active: {reason}.", holder=row)
    return to_info(db.get_checkout(app_id), identity)  # type: ignore[arg-type]


def release(
    db: ApplicationDatabase,
    application_id: str,
    identity: UserIdentity,
    force: bool = False,
) -> bool:
    """
    Close a review. Only the caller's own checkout is released unless `force` (administrator override — the
    caller's permission to force is checked by the router). Returns True if a checkout was released.
    Raises KeyError (unknown application).
    """
    record = _require_application(db, application_id)
    app_id = record["application_id"]
    row = db.get_checkout(app_id)
    if not row:
        return False
    if force:
        released = db.release_checkout(app_id)
        if released:
            db.add_audit_event(app_id, "CHECKOUT_FORCE_RELEASED", actor=identity.audit_actor,
                               from_status=record["status"], to_status=record["status"],
                               details=f"Checkout held by {row['checked_out_by']} was force-released "
                                       f"by {identity.display_name}.")
        return released
    if row["holder_key"] != identity.holder_key or row.get("state") != "ACTIVE":
        return False  # not the holder (or a determination is in flight): nothing to release
    released = db.release_checkout(app_id, identity.holder_key)
    if released:
        db.add_audit_event(app_id, "CHECKOUT_RELEASED", actor=identity.audit_actor,
                           from_status=record["status"], to_status=record["status"],
                           details=f"Review closed by {identity.display_name}; file returned to the queue.")
    return released


# ---------------------------------------------------------------------------
# Approve / Reject
# ---------------------------------------------------------------------------
def begin_decision(
    db: ApplicationDatabase, application_id: str, identity: UserIdentity, now: Optional[datetime] = None
) -> Tuple[str, bool]:
    """
    Claims the right to record the determination. Returns (canonical application ID, held_before):
    `held_before` is False when the checkout was taken implicitly for this request.
    Raises KeyError, ValueError (not PENDING_REVIEW) or CheckoutConflict.
    """
    record = _require_application(db, application_id)
    row, newly_acquired = _acquire(db, record, identity, now)
    now_str, expires_str = _window(now)
    if not db.claim_checkout_for_decision(row["application_id"], identity.holder_key, now_str, expires_str):
        # Lost the race to a concurrent determination by the same identity (e.g. a double-click).
        current = db.get_checkout(row["application_id"]) or row
        raise CheckoutConflict(_conflict_message({**current, "state": "DECIDING"}, identity), holder=current)
    return row["application_id"], not newly_acquired


def end_decision(db: ApplicationDatabase, application_id: str) -> None:
    """The determination is persisted: the file has left the review queue, drop its checkout."""
    db.release_checkout(application_id)


def abort_decision(db: ApplicationDatabase, application_id: str, identity: UserIdentity, held_before: bool) -> None:
    """The determination failed: give the file back to the holder, or drop an implicit checkout."""
    if held_before:
        db.restore_checkout(application_id, identity.holder_key)
    else:
        db.release_checkout(application_id, identity.holder_key)
