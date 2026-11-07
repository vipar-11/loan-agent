"""
Underwriting policy document loader (policy_v1.md) with Markdown -> HTML rendering.
"""
import html
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_FALLBACK = "# APEX FINANCIAL UNDERWRITING POLICY (US-2026)\nPolicy document text unavailable."


def _policy_path() -> Path | None:
    for candidate in (Path.cwd() / "policy_v1.md", PROJECT_ROOT / "policy_v1.md"):
        if candidate.exists():
            return candidate
    return None


def load_policy_markdown() -> str:
    path = _policy_path()
    return path.read_text(encoding="utf-8") if path else POLICY_FALLBACK


@lru_cache(maxsize=4)
def load_policy_html(markdown_text: str) -> str:
    """Renders Markdown to HTML; degrades gracefully to escaped preformatted text if `markdown` is unavailable."""
    try:
        import markdown  # type: ignore

        return markdown.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:
        return f"<pre>{html.escape(markdown_text)}</pre>"
