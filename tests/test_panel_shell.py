"""
tests/test_panel_shell.py
=========================
Stage 13 structural guards for the single-file panel shell.

NOTE (2026-06-17): the panel shell was reverted as the live UI; chat.html is back to
the v1 chat page. The panel shell is preserved on disk at chat_panel_shell.html.bak,
so these structural guards now point there. They keep protecting the shell against
truncation/stubbing in case the UI direction is revisited.

These enforce the full-output-enforcement contract at test time: every required JS
function is defined, every design token exists (with a dark-mode override), there
is exactly one inline <script>, and no CDN / framework / TODO leaked in. A future
truncation or stubbed function fails the suite instead of shipping a half-built UI.

Run: pytest tests/test_panel_shell.py -v
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Panel shell reverted; UI returned to v1 chat.html. The shell lives at the .bak path.
CHAT_HTML = ROOT / "backend" / "static" / "chat_panel_shell.html.bak"
HTML = CHAT_HTML.read_text(encoding="utf-8")

REQUIRED_FUNCTIONS = [
    "init", "restoreFromUrl", "syncUrl", "setSubject", "setPanel", "setObjective",
    "loadPanelData", "renderPanel", "renderLearnPanel", "renderPracticePanel",
    "renderReviewPanel", "renderProgressPanel", "renderLibraryPanel", "renderExamPanel",
    "sendMessage", "receiveResponse", "renderChat", "renderMessage", "renderGradingResult",
    "renderRecallPills", "showToast", "showLoading", "hideLoading", "handleFeedback",
    "apiGet", "apiPost", "startExam", "submitExam", "tickExamTimer",
]

COLOR_TOKENS = [
    "--color-bg", "--color-surface", "--color-surface-elevated", "--color-border",
    "--color-border-strong", "--color-text-primary", "--color-text-secondary",
    "--color-text-muted", "--color-accent", "--color-accent-hover", "--color-success",
    "--color-warn", "--color-danger",
]

OTHER_TOKENS = [
    "--space-1", "--space-2", "--space-3", "--space-4", "--space-5", "--space-6", "--space-8",
    "--radius-sm", "--radius", "--radius-lg", "--font-sans", "--font-mono",
    "--shadow-sm", "--shadow", "--tap",
]


def test_every_required_function_is_defined():
    missing = [f for f in REQUIRED_FUNCTIONS if not re.search(r"\bfunction\s+" + re.escape(f) + r"\s*\(", HTML)]
    assert not missing, "panel shell missing function definitions: " + ", ".join(missing)


def test_all_design_tokens_defined():
    missing = [t for t in (COLOR_TOKENS + OTHER_TOKENS) if (t + ":") not in HTML]
    assert not missing, "design tokens not defined in :root: " + ", ".join(missing)


def test_dark_mode_overrides_every_colour_token():
    assert "@media (prefers-color-scheme: dark)" in HTML
    dark = HTML.split("prefers-color-scheme: dark", 1)[1][:1600]  # the dark :root block
    missing = [t for t in COLOR_TOKENS if (t + ":") not in dark]
    assert not missing, "dark mode missing overrides for: " + ", ".join(missing)


def test_reduced_motion_block_present():
    assert "@media (prefers-reduced-motion: reduce)" in HTML


def test_single_inline_script_no_cdn_no_framework():
    # exactly one <script> ... </script>, with no external src / module / CDN.
    scripts = re.findall(r"<script\b([^>]*)>", HTML)
    assert len(scripts) == 1, "expected exactly one <script> tag, found %d" % len(scripts)
    assert "src=" not in scripts[0], "the script must be inline (file:// safe), not external"
    assert 'type="module"' not in scripts[0], "ES modules are not allowed (file:// safety)"
    for bad in ["cdn.", "unpkg", "jsdelivr", "react", "vue", "https://"]:
        assert bad not in HTML.lower(), "panel shell must have no external/CDN/framework dependency (%s)" % bad


def test_no_todo_or_truncation_markers():
    for marker in ["TODO", "add remaining", "...rest", "stubbed", "FIXME"]:
        assert marker.lower() not in HTML.lower(), "found a placeholder/truncation marker: " + marker


def test_all_six_panels_and_endpoints_referenced():
    for panel in ["learn", "practice", "review", "progress", "library", "exam"]:
        assert ('"' + panel + '"') in HTML, "panel id not referenced: " + panel
    for path in ["/api/syllabus/", "/api/progress/", "/api/past-papers/",
                 "/api/practice-question/", "/api/subjects", "/api/chat", "/api/feedback", "/api/due/"]:
        assert path in HTML, "endpoint not wired in the UI: " + path
