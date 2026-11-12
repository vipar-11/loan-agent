"""
test_agentic_app_redirect.py
The retired Streamlit agentic workbench (agentic_app.py) sends every visitor to the FastAPI workbench's landing page
and renders nothing else — in particular it never opens the application database (no way around sign-in).
"""
from pathlib import Path
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

import loan_agent.db as db_module

APP = str(Path(__file__).resolve().parent.parent / "agentic_app.py")


def run_app(monkeypatch, url=None):
    if url is None:
        monkeypatch.delenv("APEX_WORKBENCH_URL", raising=False)
    else:
        monkeypatch.setenv("APEX_WORKBENCH_URL", url)
    with mock.patch.object(db_module, "get_application_database", side_effect=AssertionError("database opened")):
        at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


@pytest.mark.parametrize("configured,expected", [
    (None, "http://localhost:8000/"),
    ("https://workbench.apex.example", "https://workbench.apex.example/"),
])
def test_redirects_to_the_landing_page(monkeypatch, configured, expected):
    at = run_app(monkeypatch, configured)
    scripts = [h.proto.body for h in at.get("html")]
    assert scripts == [f'<script>window.location.replace("{expected}");</script>']
    assert all(h.proto.unsafe_allow_javascript for h in at.get("html"))
    assert [(b.proto.label, b.proto.url) for b in at.get("link_button")] == [
        ("Continue to the Apex Financial workbench ↗", expected)  # fallback if scripts are blocked
    ]


def test_legacy_workbench_no_longer_renders(monkeypatch):
    at = run_app(monkeypatch)
    assert len(at.tabs) == 0 and len(at.button) == 0 and len(at.text_input) == 0 and len(at.sidebar) == 0
