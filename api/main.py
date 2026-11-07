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
from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routers import admin, applications, pages, reference, workflows
from api.services import scheduler as scheduler_service

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
    unless disabled, runs the built-in initiate_workflow scheduler for the lifetime of the process.
    """
    load_dotenv()
    from api.dependencies import get_checkpointer, get_db

    db = get_db()
    checkpointer = get_checkpointer()
    logger.info("Apex Underwriting API ready (storage backend: %s)", db.backend)

    stop = asyncio.Event()
    task = None
    if getattr(app.state, "scheduler_enabled", True):
        task = asyncio.create_task(scheduler_service.scheduler_loop(db, checkpointer, stop))
    try:
        yield
    finally:
        stop.set()
        if task:
            await task
        db.close()


def create_app(scheduler_enabled: bool = True) -> FastAPI:
    """
    Application factory. `scheduler_enabled=False` skips the in-process periodic initiate_workflow
    trigger (tests, or deployments that rely exclusively on an external cron).
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

    app.include_router(reference.router)
    app.include_router(applications.router)
    app.include_router(workflows.router)
    app.include_router(admin.router)
    app.include_router(pages.router)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app(scheduler_enabled=os.environ.get("WORKFLOW_SCHEDULER_ENABLED", "1").lower() not in ("0", "false", "no"))
