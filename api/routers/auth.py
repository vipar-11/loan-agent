"""
/api/v1/auth — sign-in, sign-out, password change; /api/v1/session — the caller's identity (per browser tab).

Two ways to sign in with a user ID and password, both backed by the same server-side sessions:
    POST /auth/login   JSON body; sets an HttpOnly session cookie — used by the browser workbench.
    POST /auth/token   OAuth2 password grant (form fields username / password); returns a bearer token — used by
                       API clients, the admin CLI tools, and the "Authorize" button of the interactive docs (/docs).

Accounts start with an EMPTY default password (see manage_users.py). Signing in with it yields a restricted session
that can only set a real password (POST /auth/password); every other endpoint answers 403 until then. That the default
is empty is deliberately not mentioned in anything a user sees (sign-in page, API responses, OpenAPI docs).

Loan Applicants may also create their own account:
    POST /auth/signup  Self-service sign-up. Always the Loan Applicant role — the one role a user may grant
                       themselves; a real password is chosen right away (no empty-default step), and it signs the
                       new user in immediately (same session cookie as /auth/login).
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status

from api.dependencies import (
    establish_user_identity,
    get_auth_config,
    get_db,
    get_session_manager,
    authenticate_request,
)
from api.schemas import (
    LoginRequest,
    LoginResponse,
    Message,
    PasswordChangeRequest,
    SignInOption,
    SignupRequest,
    TokenResponse,
    UserIdentity,
)
from api.security import (
    CSRF_HEADER,
    bearer_token,
    clear_session_cookie,
    default_sign_in_options,
    set_session_cookie,
)
from loan_agent.auth import (
    AuthenticationError,
    LocalPasswordVerifier,
    PasswordChangeError,
    Principal,
    SessionManager,
    SignupError,
)
from loan_agent.auth import hash_token
from loan_agent.auth import users as user_directory
from loan_agent.config import AuthConfig
from loan_agent.db import ApplicationDatabase

logger = logging.getLogger("apex.auth")

router = APIRouter(prefix="/api/v1", tags=["Authentication"])


def _verify_password(request: Request, db: ApplicationDatabase, config: AuthConfig, user_id: str, password: str) -> Principal:
    factory = getattr(request.app.state, "password_verifier_factory", None) or LocalPasswordVerifier
    try:
        principal = factory(db, config).verify(user_id, password)
    except AuthenticationError as exc:
        logger.warning("Sign-in failed (%s)", f"user {exc.user_id}" if exc.user_id else "unknown user ID")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    logger.info("User %s signed in (%s)", principal.user_id, principal.auth_method)
    return principal


class TokenForm:
    """
    OAuth2 password-grant form (as sent by OAuth2 clients and the /docs "Authorize" dialog). Unlike FastAPI's
    OAuth2PasswordRequestForm, the password may be empty — that is how the empty default password is presented.
    """

    def __init__(
        self,
        username: str = Form(..., max_length=64, description="Your user ID."),
        password: str = Form("", max_length=256, description="Your password."),
        grant_type: Optional[str] = Form(None, pattern="^password$"),
    ):
        self.username = username
        self.password = password or ""


def _presented_token(request: Request, config: AuthConfig) -> Optional[str]:
    return bearer_token(request) or request.cookies.get(config.cookie_name)


@router.get("/auth/providers", response_model=List[SignInOption], summary="Available sign-in methods")
def sign_in_options(request: Request) -> List[SignInOption]:
    """What the sign-in page offers: user ID & password today; SSO providers are added here when configured."""
    options = getattr(request.app.state, "sign_in_options", None) or default_sign_in_options()
    return [SignInOption(**o) for o in options]


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    responses={401: {"model": Message}},
    summary="Sign in (browser): sets the session cookie",
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: ApplicationDatabase = Depends(get_db),
    config: AuthConfig = Depends(get_auth_config),
    sessions: SessionManager = Depends(get_session_manager),
) -> LoginResponse:
    """
    Verifies the user ID and password and starts a session held in an HttpOnly cookie. When the user must choose a new
    password (`password_change_required: true`), the session is restricted to doing so.
    """
    principal = _verify_password(request, db, config, payload.user_id, payload.password)
    sessions.revoke(request.cookies.get(config.cookie_name))  # replace any previous session of this browser
    issued = sessions.issue(principal)
    set_session_cookie(response, issued.token, config)
    identity = UserIdentity.from_principal(principal)
    return LoginResponse(
        identity=identity,
        expires_at=issued.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        message=("Welcome! Please set a new password to continue." if principal.password_change_required
                 else f"Signed in as {principal.display_name} ({principal.role_label})."),
    )


@router.post(
    "/auth/signup",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": Message}},
    summary="Self-service sign-up (Loan Applicant) — sets the session cookie",
)
def signup(
    payload: SignupRequest,
    response: Response,
    db: ApplicationDatabase = Depends(get_db),
    config: AuthConfig = Depends(get_auth_config),
    sessions: SessionManager = Depends(get_session_manager),
) -> LoginResponse:
    """
    Creates a Loan Applicant account — the only role a user may grant themselves — with the password given here,
    and signs in immediately, landing straight on the Applicant Portal.
    """
    try:
        user = user_directory.self_register(db, payload.user_id, payload.password, payload.display_name, config)
    except SignupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    principal = user_directory.principal_for(user, auth_method="password")
    issued = sessions.issue(principal)
    set_session_cookie(response, issued.token, config)
    logger.info("User %s signed up", principal.user_id)
    return LoginResponse(
        identity=UserIdentity.from_principal(principal),
        expires_at=issued.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        message=f"Welcome, {principal.display_name}! Your account is ready.",
    )


@router.post(
    "/auth/token",
    response_model=TokenResponse,
    responses={401: {"model": Message}},
    summary="Sign in (API clients): OAuth2 password grant returning a bearer token",
)
def issue_token(
    request: Request,
    form: TokenForm = Depends(),
    db: ApplicationDatabase = Depends(get_db),
    config: AuthConfig = Depends(get_auth_config),
    sessions: SessionManager = Depends(get_session_manager),
) -> TokenResponse:
    """
    Form fields `username` (the user ID) and `password`. Send the returned token as
    `Authorization: Bearer <access_token>`; revoke it with POST /api/v1/auth/logout. The token also ends after the
    configured period of inactivity.
    """
    principal = _verify_password(request, db, config, form.username, form.password)
    issued = sessions.issue(principal)
    return TokenResponse(
        access_token=issued.token,
        expires_in=int((issued.expires_at - datetime.now()).total_seconds()),
        password_change_required=principal.password_change_required,
    )


@router.post("/auth/logout", response_model=Message, summary="Sign out (ends the cookie session or bearer token)")
def logout(
    request: Request,
    response: Response,
    config: AuthConfig = Depends(get_auth_config),
    sessions: SessionManager = Depends(get_session_manager),
) -> Message:
    """Idempotent: ends the presented session, if any, and clears the session cookie."""
    token = bearer_token(request)
    if token is None and request.cookies.get(config.cookie_name):
        if not request.headers.get(CSRF_HEADER):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Request rejected: missing the {CSRF_HEADER} header.")
        token = request.cookies.get(config.cookie_name)
    if sessions.revoke(token):
        logger.info("A session was signed out")
    clear_session_cookie(response, config)
    return Message(detail="Signed out.")


@router.post(
    "/auth/password",
    response_model=UserIdentity,
    responses={400: {"model": Message}, 401: {"model": Message}},
    summary="Change your password",
)
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    db: ApplicationDatabase = Depends(get_db),
    config: AuthConfig = Depends(get_auth_config),
    principal: Principal = Depends(authenticate_request),
) -> UserIdentity:
    """
    Also completes a required password change. On success any restriction is lifted for this session and every
    OTHER session of the user is signed out.
    """
    token = _presented_token(request, config)
    try:
        user = user_directory.change_password(
            db, principal.user_id, payload.current_password, payload.new_password, config,
            keep_session_hash=hash_token(token) if token else None,
        )
    except PasswordChangeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info("User %s changed their password", principal.user_id)
    return UserIdentity.from_principal(user_directory.principal_for(user, principal.auth_method))


@router.get(
    "/session",
    response_model=UserIdentity,
    responses={401: {"model": Message}},
    summary="Who am I? Establishes / confirms this browser tab's session",
)
def session_identity(identity: UserIdentity = Depends(establish_user_identity)) -> UserIdentity:
    """
    Returns the signed-in user (also while a password change is pending, so the sign-in page can detect it).

    The workbench calls this first in every browser tab. Without the `X-Apex-Session-Id` header a new tab token is
    issued in that response header (`is_new_session: true`); the tab stores it in sessionStorage and sends it on
    every later call, which keeps review checkouts exclusive to that tab.
    """
    return identity
