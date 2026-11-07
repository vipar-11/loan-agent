"""
Apex Financial — Autonomous Underwriting API (FastAPI).

Layered architecture:
    api.routers   -> HTTP contracts only (validation, status codes, serialization)
    api.services  -> orchestration of domain operations (no HTTP, no UI)
    loan_agent    -> core domain package (underwriting workflow, calculator, persistence)

Run with:  uvicorn api.main:app --reload   (or `python run_api.py`)
"""
from api.main import app, create_app

__all__ = ["app", "create_app"]
