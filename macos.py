"""macOS window chrome: what turns a web view in a window into a Mac app.

- Unified title bar: the page runs up under a transparent, 52px title bar
  (the height of Simplixity's topbar), the window buttons on its centre line.
  Nothing is painted over the page there: no grey band, the topbar *is* the
  title bar.
- Sidebar vibrancy: the web view draws no background of its own and sits on a
  native sidebar material; skin/macos.css lets it through behind the sidebar
  and keeps everything else opaque.
- Dragging and double-clicking empty topbar space moves and zooms the window
  the way a native title bar does (snapping, Spaces and the user's
  double-click preference included), because AppKit does the work.
- Size and position are remembered by AppKit (frame autosave).
- Scroll bars are the overlay kind, whatever the mouse.
- The right-click menu keeps the text actions (Copy, Look Up, Share, ...) and
  drops the browser ones (Reload, Back, Inspect, Open in New Window).

Which parts of the title bar are empty is the page's call
(`simplixityDesktop.isDragArea` in skin/skin.js). A press in the title bar is
held back while the page is asked, then either becomes a window drag or is
handed to WebKit as if nothing had happened, the release queued behind it.
Asked at press time, not tracked from mouse moves: those do not reliably reach
the page under the title bar. If the page has no answer (no skin, an upstream
page without it, an error) the press goes to WebKit: the worst case is a
title bar that does not drag, never a click that is lost.
"""

from __future__ import annotations

import time

import AppKit
import Foundation
import objc
from PyObjCTools import AppHelper
from webview.platforms.cocoa import BrowserView

# Overlay scroll bars (the thin pill that shows while scrolling) even with a
# mouse attached, where macOS would otherwise give WebKit permanent, browser-
# looking bars. Argument domain: this process only, nothing written to disk,
# the system setting untouched. Must be in place before WebKit starts, which
# importing this module (before webview.start) guarantees.
_defaults = Foundation.NSUserDefaults.standardUserDefaults()
_args = dict(_defaults.volatileDomainForName_(Foundation.NSArgumentDomain) or {})
_args["AppleShowScrollBars"] = "WhenScrolling"
_defaults.setVolatileDomain_forName_(_args, Foundation.NSArgumentDomain)

# WKWebView's own menu items, by identifier. Anything not listed stays.
_BROWSER_ITEMS = {
    "WKMenuItemIdentifierReload",
    "WKMenuItemIdentifierGoBack",
    "WKMenuItemIdentifierGoForward",
    "WKMenuItemIdentifierInspectElement",
    "WKMenuItemIdentifierOpenLinkInNewWindow",
    "WKMenuItemIdentifierOpenImageInNewWindow",
    "WKMenuItemIdentifierOpenMediaInNewWindow",
    "WKMenuItemIdentifierOpenFrameInNewWindow",
    "WKMenuItemIdentifierDownloadLinkedFile",
    "WKMenuItemIdentifierDownloadImage",
    "WKMenuItemIdentifierDownloadMedia",
}

# A press in the title bar waiting for the page's answer, and the mouse events
# that arrived meanwhile. Older than this, the answer is presumed lost (web
# content process gone) and events flow again.
_PENDING_TIMEOUT = 0.5
_pending: dict | None = None


def _title_bar_height(window) -> float:
    if window.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
        return 0.0
    return window.frame().size.height - window.contentLayoutRect().size.height


def _double_click(window) -> None:
    action = AppKit.NSUserDefaults.standardUserDefaults().stringForKey_("AppleActionOnDoubleClick")
    if action == "Minimize":
        window.performMiniaturize_(None)
    elif action != "None":
        window.performZoom_(None)


def _waiting() -> bool:
    global _pending
    if _pending is not None and time.monotonic() - _pending["since"] > _PENDING_TIMEOUT:
        _pending = None
    return _pending is not None


class WebKitHost(objc.Category(BrowserView.WebKitHost)):
    # Replaces pywebview's own mouseDown_/mouseDragged_, which only ever do
    # anything for frameless windows; this one is not frameless.
    def mouseDown_(self, event):
        global _pending
        window = self.window()
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        y = p.y if self.isFlipped() else self.bounds().size.height - p.y
        if _waiting() or y >= _title_bar_height(window):
            return objc.super(BrowserView.WebKitHost, self).mouseDown_(event)

        _pending = {"since": time.monotonic(), "queue": []}
        zoom = self.pageZoom()

        def answered(result, error):
            global _pending
            queue = _pending["queue"] if _pending else []
            _pending = None
            if result is True and error is None:
                if event.clickCount() >= 2:
                    _double_click(window)
                elif AppKit.NSEvent.pressedMouseButtons() & 1:
                    window.performWindowDragWithEvent_(event)
                return  # WebKit never saw the press, so it gets no release either
            host = objc.super(BrowserView.WebKitHost, self)
            host.mouseDown_(event)
            for e in queue:
                (host.mouseUp_ if e.type() == AppKit.NSEventTypeLeftMouseUp else host.mouseDragged_)(e)

        self.evaluateJavaScript_completionHandler_(
            f"window.simplixityDesktop?.isDragArea({p.x / zoom}, {y / zoom}) === true", answered
        )

    def mouseDragged_(self, event):
        if _waiting():
            return _pending["queue"].append(event)
        objc.super(BrowserView.WebKitHost, self).mouseDragged_(event)

    def mouseUp_(self, event):
        if _waiting():
            return _pending["queue"].append(event)
        objc.super(BrowserView.WebKitHost, self).mouseUp_(event)

    # Replaces pywebview's, which empties the menu entirely outside debug mode.
    def willOpenMenu_withEvent_(self, menu, event):
        for item in list(menu.itemArray()):
            if item.identifier() in _BROWSER_ITEMS:
                menu.removeItem_(item)
        # A separator left first, last or doubled by the removals.
        items = list(menu.itemArray())
        for i, item in enumerate(items):
            if item.isSeparatorItem() and (i == 0 or i == len(items) - 1 or items[i - 1].isSeparatorItem()):
                menu.removeItem_(item)


def setup_window(window) -> None:
    """Run from `before_show`: the native window exists, it is not on screen yet."""

    def apply() -> None:
        w = window.native
        webview = BrowserView.instances[window.uid].webview

        # Changing the style mask keeps the *content* size, so the frame would
        # lose the old title bar's height (28px) here, and once more on every
        # launch after a restore. Put the frame back as it was.
        frame = w.frame()
        w.setStyleMask_(w.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
        w.setTitlebarAppearsTransparent_(True)
        w.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        # An empty toolbar in the unified style is what makes the bar 52px tall
        # and centres the window buttons in it.
        toolbar = AppKit.NSToolbar.alloc().initWithIdentifier_("simplixity")
        toolbar.setShowsBaselineSeparator_(False)
        w.setToolbar_(toolbar)
        w.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnified)
        w.setTitlebarSeparatorStyle_(AppKit.NSTitlebarSeparatorStyleNone)
        # pywebview paints the title bar container in the window colour, an
        # opaque band over the page. The page's topbar is the title bar here.
        for view in w.contentView().superview().subviews():
            if view.className() == "NSTitlebarContainerView":
                view.setBackgroundColor_(AppKit.NSColor.clearColor())

        # The web view on top of a sidebar material, in a plain container:
        # where the page is transparent (macos.css: the sidebar), it shows.
        container = AppKit.NSView.alloc().initWithFrame_(webview.frame())
        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(container.bounds())
        effect.setMaterial_(AppKit.NSVisualEffectMaterialSidebar)
        effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(AppKit.NSVisualEffectStateFollowsWindowActiveState)
        w.setContentView_(container)
        for view in (effect, webview):
            view.setFrame_(container.bounds())
            view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
            container.addSubview_(view)
        webview.setValue_forKey_(False, "drawsBackground")
        w.makeFirstResponder_(webview)

        w.setFrame_display_(frame, False)
        # AppKit remembers size and position itself (in the app's defaults),
        # across screens and Spaces. Set last: it restores the saved frame now.
        w.setFrameAutosaveName_("SimplixityMain")

    AppHelper.callAfter(apply)
