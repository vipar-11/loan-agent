"""
FastAPI application factory for the Apex Financial Autonomous Underwriting service.

    uvicorn api.main:app --reload
    python run_api.py

Endpoints:
    /login                    Sign-in page
    /portal, /desk, /admin    Server-rendered workbench (Applicant Portal, Underwriter Desk, Administrator console)
    /api/v1/auth/...          Sign-in / sign-out / password change
    /api/v1/...               JSON API (see /docs for the interactive OpenAPI reference)

Authentication: every page and API endpoint requires a signed-in user, except the sign-in endpoints and the liveness
probe. Access is role-based (see loan_agent/auth/roles.py).
"""
import asyncio
import logging
from typing import List, Optional
from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from api.dependencies import SESSION_HEADER, require_signed_in
from api.routers import admin, applications, auth, pages, reference, workflows
from api.security import RequestAuthenticator, default_authenticators, default_sign_in_options
from api.services import batch_intake as intake_service
from api.services import scheduler as scheduler_service
from loan_agent.config import AuthConfig, IntakeConfig, load_auth_config, load_intake_config

STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("apex.api")

API_DESCRIPTION = """
Enterprise-grade, FCRA & GLBA-compliant personal installment loan underwriting service.

* **Applicant Portal** submits applications (`POST /api/v1/applications`).
* The **agentic underwriting workflow** (see `loan_agent/graph_workflow.py`) is dispatched per application
  or in batch.
* **AUTO_APPROVED / AUTO_DENIED** files settle immediately; **ESCALATE** files pause at the
  human-in-the-loop breakpoint and await `POST /api/v1/applications/{id}/decision`.

**Authentication.** Sign in with `POST /api/v1/auth/token` (or the **Authorize** button on this page: enter your user
ID and password) and send `Authorization: Bearer <token>`. The browser workbench uses `POST /api/v1/auth/login`
(session cookie). Access is role-based.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Warms up the persistent application database and workflow checkpointer once per process and,
    unless disabled, runs the built-in initiate_workflow scheduler and the partner batch-intake monitor
    for the lifetime of the process.
    """
    load_dotenv()
    from api.dependencies import get_checkpointer, get_db

    db = get_db()
    checkpointer = get_checkpointer()
    logger.info("Apex Underwriting API ready (storage backend: %s)", db.backend)

    stop = asyncio.Event()
    tasks = []
    if getattr(app.state, "scheduler_enabled", True):
        tasks.append(asyncio.create_task(scheduler_service.scheduler_loop(db, checkpointer, stop)))
    if getattr(app.state, "intake_enabled", False):
        app.state.intake_task = asyncio.create_task(
            intake_service.intake_loop(db, stop, getattr(app.state, "intake_config", None))
        )
        tasks.append(app.state.intake_task)
    try:
        yield
    finally:
        stop.set()
        for task in tasks:
            await task
        db.close()


def create_app(
    scheduler_enabled: bool = True,
    intake_enabled: Optional[bool] = None,
    intake_config: Optional[IntakeConfig] = None,
    auth_config: Optional[AuthConfig] = None,
    authenticators: Optional[List[RequestAuthenticator]] = None,
) -> FastAPI:
    """
    Application factory.

    `scheduler_enabled=False` skips the in-process periodic initiate_workflow trigger (tests, or deployments that
    rely exclusively on an external cron).
    `intake_enabled` controls the partner batch-intake folder monitor; by default it runs when background jobs are
    enabled (`scheduler_enabled`) and batch_intake.ini has `enabled = true`.
    `auth_config` defaults to auth.ini; `authenticators` replaces the request-authenticator chain (default: bearer
    token, then session cookie) — the hook for adding SSO / IdP-token authentication (see api/security.py).
    """
    app = FastAPI(
        title="Apex Financial — Autonomous Underwriting API",
        version=reference.API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.state.scheduler_enabled = scheduler_enabled
    app.state.intake_config = intake_config or load_intake_config()
    app.state.intake_enabled = (
        (scheduler_enabled and app.state.intake_config.enabled) if intake_enabled is None else intake_enabled
    )
    app.state.intake_task = None

    # Authentication wiring (see api/security.py). `authenticators` is the extension point for token-based / SSO
    # mechanisms: each request is offered to them in order. `sign_in_options` is what the sign-in page offers;
    # `password_verifier_factory(db, config)` checks user ID / password sign-ins (local user directory by default).
    app.state.auth_config = auth_config or load_auth_config()
    app.state.authenticators = list(authenticators) if authenticators is not None else default_authenticators(
        app.state.auth_config
    )
    app.state.sign_in_options = default_sign_in_options()
    app.state.password_verifier_factory = None  # None -> loan_agent.auth.LocalPasswordVerifier

    @app.middleware("http")
    async def echo_session_identity(request, call_next):
        """
        Returns the browser-tab session token established by `establish_user_identity()` on EVERY API response,
        including error responses (headers set by a dependency are dropped when the endpoint raises, e.g. 404 / 409).
        """
        response = await call_next(request)
        identity = getattr(request.state, "identity", None)
        if identity is not None and identity.tab_session:
            response.headers[SESSION_HEADER] = identity.tab_session
        return response

    # Public: sign-in endpoints and the liveness probe.
    app.include_router(auth.router)
    app.include_router(reference.public_router)
    # Everything else requires a signed-in user (secure by default); endpoints add their role permissions on top.
    signed_in = [Depends(require_signed_in)]
    app.include_router(reference.router, dependencies=signed_in)
    app.include_router(applications.router, dependencies=signed_in)
    app.include_router(workflows.router, dependencies=signed_in)
    app.include_router(admin.router, dependencies=signed_in)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # Pages check sign-in and roles themselves (redirecting to /login instead of answering 401). Registered LAST:
    # its catch-all route answers every other path exactly like a page the user may not open.
    app.include_router(pages.router)
    return app


app = create_app(scheduler_enabled=os.environ.get("WORKFLOW_SCHEDULER_ENABLED", "1").lower() not in ("0", "false", "no"))
