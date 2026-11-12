"""
Request authentication: how each HTTP request proves who is calling.

The service asks an ordered chain of `RequestAuthenticator`s (app.state.authenticators). Each one looks for its own
kind of credential and returns
    * a `Principal`          — the credential is valid;
    * None                   — its credential is absent, ask the next authenticator;
    * raises RequestAuthError — its credential is present but unacceptable (the request is refused outright).
If nobody recognises the request it is anonymous, and everything except sign-in / liveness answers 401.

Built in:
    BearerTokenAuthenticator   `Authorization: Bearer <token>` — API clients, CLI tools, Swagger UI (/docs).
                               Tokens come from POST /api/v1/auth/token (OAuth2 password grant).
    SessionCookieAuthenticator HttpOnly session cookie set by POST /api/v1/auth/login — the browser workbench.
                               State-changing requests must also carry the CSRF_HEADER (see below).

Both resolve server-side sessions (loan_agent.auth.sessions), so sign-out and revocation are immediate.

Extending (SSO / token-based authentication):
    * IdP-issued access tokens (OIDC / OAuth2 JWTs): add an authenticator that validates the JWT (issuer, audience,
      signature via the IdP's JWKS, expiry), maps its claims to a Principal (subject -> user_id, groups -> Role) and
      put it first in the chain via create_app(authenticators=[...]).
    * Trusted SSO gateway / reverse proxy: an authenticator that trusts an identity header set by the gateway
      (only behind a gateway that strips it from client requests, or over mTLS).
    * Browser SSO (OIDC authorization-code / SAML): add start + callback routes that validate the IdP response,
      build the Principal and call SessionManager.issue() + set_session_cookie() — the cookie authenticator then
      handles those sessions unchanged. Advertise the option in app.state.sign_in_options so the sign-in page
      offers it.
Nothing downstream changes: permissions, review checkouts and audit actors only ever see the Principal.
"""
from typing import List, Optional, Protocol

from fastapi import Request, Response

from loan_agent.auth import Principal, SessionManager
from loan_agent.config import AuthConfig

# Cross-site request forgery defence for cookie-authenticated calls: browsers only attach a custom header to
# cross-origin requests after a CORS preflight, which this service never grants. Combined with SameSite=Lax
# cookies, a forged form post or cross-site fetch cannot carry a usable session.
CSRF_HEADER = "X-Requested-With"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class RequestAuthError(Exception):
    """A credential was presented but must be refused (status 401 or 403)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RequestAuthenticator(Protocol):
    name: str

    def authenticate(self, request: Request, sessions: SessionManager) -> Optional[Principal]:
        ...


def bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class BearerTokenAuthenticator:
    name = "bearer"

    def authenticate(self, request: Request, sessions: SessionManager) -> Optional[Principal]:
        token = bearer_token(request)
        if token is None:
            return None
        principal = sessions.resolve(token)
        if principal is None:
            raise RequestAuthError(401, "The access token is invalid or has expired. Sign in again.")
        return principal


class SessionCookieAuthenticator:
    name = "cookie"

    def __init__(self, cookie_name: str):
        self.cookie_name = cookie_name

    def authenticate(self, request: Request, sessions: SessionManager) -> Optional[Principal]:
        principal = sessions.resolve(request.cookies.get(self.cookie_name))
        if principal is None:
            return None  # absent / expired cookie: simply not signed in
        if request.method.upper() not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
            raise RequestAuthError(403, f"Request rejected: missing the {CSRF_HEADER} header required for "
                                        f"browser-session requests that change data.")
        return principal


def default_authenticators(config: AuthConfig) -> List[RequestAuthenticator]:
    return [BearerTokenAuthenticator(), SessionCookieAuthenticator(config.cookie_name)]


def default_sign_in_options() -> List[dict]:
    """What the sign-in page offers. SSO providers add e.g. {"id": "okta", "type": "redirect", "label": ..., "url": ...}."""
    return [{"id": "local", "type": "password", "label": "User ID & password", "url": "/api/v1/auth/login"}]


def set_session_cookie(response: Response, token: str, config: AuthConfig) -> None:
    # No Max-Age: a browser-session cookie. The server enforces idle / absolute expiry itself.
    response.set_cookie(
        config.cookie_name, token, httponly=True, secure=config.cookie_secure, samesite="lax", path="/",
    )


def clear_session_cookie(response: Response, config: AuthConfig) -> None:
    response.delete_cookie(config.cookie_name, path="/", httponly=True, secure=config.cookie_secure, samesite="lax")
