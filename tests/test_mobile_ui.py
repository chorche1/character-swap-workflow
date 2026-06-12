"""Mobile / iPhone support (Hugo 2026-06-12): same single codebase, made
fully usable at 375px touch. Pins the load-bearing pieces so a refactor
can't silently regress phone use.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_pwa_head_tags_and_assets():
    assert "viewport-fit=cover" in _HTML
    assert 'rel="manifest"' in _HTML
    assert 'rel="apple-touch-icon"' in _HTML
    assert 'name="theme-color"' in _HTML
    static = _ROOT / "web" / "static"
    assert (static / "manifest.webmanifest").exists()
    assert (static / "apple-touch-icon.png").exists()
    assert (static / "icon-512.png").exists()


def test_ios_input_zoom_guard_and_touch_hover_css():
    # <16px inputs make iOS Safari zoom on focus.
    assert "font-size: 16px !important" in _HTML
    # Hover-revealed controls must be visible on touch devices.
    assert '[class*="hover:opacity-100"] { opacity: 1 !important; }' in _HTML
    # Drag affordances get thumb-sized minimums.
    assert 'cursor-ew-resize"] { min-width: 14px' in _HTML


def test_sidebar_and_library_are_mobile_drawers():
    # Jobs sidebar: slide-over below md (was `hidden md:flex` — unreachable).
    assert "hidden md:flex" not in _HTML
    assert "mobileNav ? 'translate-x-0' : '-translate-x-full" in _HTML
    assert 'title="Jobb & projekt"' in _HTML          # hamburger FAB
    assert "mobileNav: false" in _JS
    # Character library: right slide-over with backdrop on mobile.
    assert "fixed md:static inset-y-0 right-0" in _HTML


def test_timeline_drag_supports_touch():
    # startHandleDrag + seekTimeline were mouse-only (clientX undefined on
    # touch) — the CapCut timeline was un-draggable on iPhone.
    seg = _JS.split("startHandleDrag(event, idx, side)")[1]
    assert "event.touches?.[0]?.clientX" in seg[:900]
    assert "window.addEventListener('touchmove', this._tlOnMove" in seg[:3000]
    seek = _JS.split("seekTimeline(event)")[1]
    assert "event.touches?.[0]?.clientX" in seek[:600]
    assert "@touchstart.self.passive=\"seekTimeline($event)\"" in _HTML
    assert _HTML.count("startHandleDrag($event, i,") == 4   # mouse+touch × 2
