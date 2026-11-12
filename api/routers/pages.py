"""
Server-rendered workbench pages (Jinja2). All dynamic data is fetched client-side from the JSON API,
so these templates stay thin and the API remains the single integration surface.

Every page except /login requires a signed-in user whose role grants the page's permission
(loan_agent.auth.roles.PAGES). Browser navigations are answered with redirects rather than 401s:
    not signed in / session expired      -> 303 /login?next=<page>
    password change pending               -> 303 /login?next=<page>   (the sign-in page shows the set-password step)
    signed in, role lacks the permission  -> the "page not found" page (404)

Pages must not reveal which other pages exist: a page the user may not open and a page that does not exist are
answered IDENTICALLY (same status, same bytes) — for signed-in users the 404 page, for everybody else the redirect to
sign-in (see `catch_all`). The 404 page says nothing about the user's role or about what access a page would need.
"""
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from api.dependencies import get_auth_config, resolve_principal
from api.services.checkout import CHECKOUT_TTL_SECONDS
from api.schemas import ANNUAL_RATE, LOAN_AMOUNT_MAX, LOAN_AMOUNT_MIN, LOAN_PURPOSES, TERM_MONTHS
from loan_agent.auth import PAGES, Permission, Principal
from loan_agent.sla import DECISION_SLA_BUSINESS_DAYS

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["Workbench Pages"], include_in_schema=False)


def _safe_next(target: Optional[str]) -> Optional[str]:
    """Only same-site paths are valid post-sign-in destinations (no open redirects)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _base_context(request: Request, view: str, principal: Optional[Principal] = None) -> dict:
    return {
        "request": request,
        "active_view": view,
        "current_path": request.url.path,  # where "Change password" returns to
        "show_role": True,  # profile menu shows the user's role (not on the 404 page)
        "user": principal,
        "nav_pages": [(path, label) for path, label, perm in PAGES if principal and principal.has(perm)],
        "loan_purposes": LOAN_PURPOSES,
        "loan_amount_min": LOAN_AMOUNT_MIN,
        "loan_amount_max": LOAN_AMOUNT_MAX,
        "annual_rate_pct": f"{ANNUAL_RATE * 100:.1f}",
        "term_months": TERM_MONTHS,
        "checkout_ttl_minutes": CHECKOUT_TTL_SECONDS // 60,
        "sla_business_days": DECISION_SLA_BUSINESS_DAYS,
    }


def _to_login(path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(path, safe='/')}", status_code=303)


def _not_found(request: Request, principal: Principal) -> Response:
    """
    The one "page not found" response for signed-in users. Its content depends only on the user (navigation,
    profile menu) — never on the requested path — so a forbidden page and a non-existent page are indistinguishable.
    """
    context = _base_context(request, "", principal)
    context.update(current_path=principal.home_path or "/", show_role=False)
    return templates.TemplateResponse(request, "not_found.html", context, status_code=404)


def _render_page(
    request: Request, principal: Optional[Principal], template: str, view: str, permission: Permission
) -> Response:
    if principal is None or principal.password_change_required:
        return _to_login(request.url.path)
    if not principal.has(permission):
        return _not_found(request, principal)
    return templates.TemplateResponse(request, template, _base_context(request, view, principal))


@router.get("/", response_class=RedirectResponse)
def root(principal: Optional[Principal] = Depends(resolve_principal)) -> RedirectResponse:
    """Signed-in users land on their role's home page; everybody else on the sign-in page."""
    if principal is None or principal.password_change_required or not principal.home_path:
        return RedirectResponse(url="/login", status_code=307)
    return RedirectResponse(url=principal.home_path, status_code=307)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next_path: Optional[str] = Query(None, alias="next")) -> HTMLResponse:
    context = _base_context(request, "login")
    context["next_path"] = _safe_next(next_path) or ""
    context["password_min_length"] = get_auth_config(request).password_min_length
    return templates.TemplateResponse(request, "login.html", context)


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request) -> HTMLResponse:
    """Self-service sign-up for Loan Applicants. Already-signed-in users are sent home by signup.js (GET /session)."""
    context = _base_context(request, "signup")
    context["password_min_length"] = get_auth_config(request).password_min_length
    return templates.TemplateResponse(request, "signup.html", context)


@router.get("/portal", response_class=HTMLResponse)
def applicant_portal(request: Request, principal: Optional[Principal] = Depends(resolve_principal)) -> Response:
    return _render_page(request, principal, "portal.html", "portal", Permission.APPLICATION_SUBMIT)


@router.get("/status", response_class=HTMLResponse)
def my_applications(request: Request, principal: Optional[Principal] = Depends(resolve_principal)) -> Response:
    return _render_page(request, principal, "status.html", "status", Permission.APPLICATION_READ_OWN)


@router.get("/desk", response_class=HTMLResponse)
def underwriter_desk(request: Request, principal: Optional[Principal] = Depends(resolve_principal)) -> Response:
    return _render_page(request, principal, "desk.html", "desk", Permission.APPLICATION_UNDERWRITE)


@router.get("/admin", response_class=HTMLResponse)
def administrator(request: Request, principal: Optional[Principal] = Depends(resolve_principal)) -> Response:
    return _render_page(request, principal, "admin.html", "admin", Permission.SYSTEM_ADMINISTER)


@router.get("/{path:path}", response_class=HTMLResponse)
def catch_all(
    path: str, request: Request, principal: Optional[Principal] = Depends(resolve_principal)
) -> Response:
    """
    Any other browser path. Must be registered after every real route and mount (see api/main.py). Answers exactly
    like a real page the caller may not open: sign-in redirect when signed out, the 404 page when signed in.
    API paths keep their plain JSON 404.
    """
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    if principal is None or principal.password_change_required:
        return _to_login(request.url.path)
    return _not_found(request, principal)
