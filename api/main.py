"""
FastAPI application factory for the Apex Financial Autonomous Underwriting service.

    uvicorn api.main:app --reload
    python run_api.py

Endpoints:
    /portal, /desk            Server-rendered workbench (Applicant Portal, Underwriter Desk)
    /api/v1/...               JSON API (see /docs for the interactive OpenAPI reference)
"""
import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from api.dependencies import SESSION_HEADER, establish_user_identity
from api.routers import admin, applications, pages, reference, workflows
from api.services import batch_intake as intake_service
from api.services import scheduler as scheduler_service
from loan_agent.config import IntakeConfig, load_intake_config

STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("apex.api")

API_DESCRIPTION = """
Enterprise-grade, FCRA & GLBA-compliant personal installment loan underwriting service.

* **Applicant Portal** submits applications (`POST /api/v1/applications`).
* The **agentic underwriting workflow** (see `loan_agent/graph_workflow.py`) is dispatched per application
  or in batch.
* **AUTO_APPROVED / AUTO_DENIED** files settle immediately; **ESCALATE** files pause at the
  human-in-the-loop breakpoint and await `POST /api/v1/applications/{id}/decision`.
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
) -> FastAPI:
    """
    Application factory.

    `scheduler_enabled=False` skips the in-process periodic initiate_workflow trigger (tests, or deployments that
    rely exclusively on an external cron).
    `intake_enabled` controls the partner batch-intake folder monitor; by default it runs when background jobs are
    enabled (`scheduler_enabled`) and batch_intake.ini has `enabled = true`.
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

    @app.middleware("http")
    async def echo_session_identity(request, call_next):
        """
        Returns the session token established by `establish_user_identity()` on EVERY API response, including
        error responses (headers set by a dependency are dropped when the endpoint raises, e.g. 404 / 409).
        """
        response = await call_next(request)
        identity = getattr(request.state, "identity", None)
        if identity is not None and identity.auth_method == "anonymous-session":
            response.headers[SESSION_HEADER] = identity.user_id
        return response

    # Every JSON API call establishes the caller's identity (and echoes the session header back).
    # Page routes are excluded: a browser navigation cannot carry the header; the page's scripts establish
    # the session with their first API call (GET /api/v1/session).
    identity = [Depends(establish_user_identity)]
    app.include_router(reference.router, dependencies=identity)
    app.include_router(applications.router, dependencies=identity)
    app.include_router(workflows.router, dependencies=identity)
    app.include_router(admin.router, dependencies=identity)
    app.include_router(pages.router)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app(scheduler_enabled=os.environ.get("WORKFLOW_SCHEDULER_ENABLED", "1").lower() not in ("0", "false", "no"))
