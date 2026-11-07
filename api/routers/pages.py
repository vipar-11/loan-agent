"""
Server-rendered workbench pages (Jinja2). All dynamic data is fetched client-side from the JSON API,
so these templates stay thin and the API remains the single integration surface.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api.schemas import ANNUAL_RATE, LOAN_AMOUNT_MAX, LOAN_AMOUNT_MIN, LOAN_PURPOSES, TERM_MONTHS

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["Workbench Pages"], include_in_schema=False)


def _base_context(request: Request, view: str) -> dict:
    return {
        "request": request,
        "active_view": view,
        "loan_purposes": LOAN_PURPOSES,
        "loan_amount_min": LOAN_AMOUNT_MIN,
        "loan_amount_max": LOAN_AMOUNT_MAX,
        "annual_rate_pct": f"{ANNUAL_RATE * 100:.1f}",
        "term_months": TERM_MONTHS,
    }


@router.get("/", response_class=RedirectResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/portal", status_code=307)


@router.get("/portal", response_class=HTMLResponse)
def applicant_portal(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "portal.html", _base_context(request, "portal"))


@router.get("/desk", response_class=HTMLResponse)
def underwriter_desk(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "desk.html", _base_context(request, "desk"))


@router.get("/admin", response_class=HTMLResponse)
def administrator(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "admin.html", _base_context(request, "admin"))
