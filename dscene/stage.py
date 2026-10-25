"""The stage: everything dscene shows on, or reads from, the user's desktop.

It owns the window reconciliation (Desktop), the deck and which card is showing, the
overlays (marks, captions, toasts, question popups), screenshots, the text selection
and the clipboard. dscene's MCP server drives it; the app hosting dscene (SceneBar)
plugs in through a Host (dscene/host.py) for its own UI: the strip it occupies, cards
for the deck, questions in its own window.

Methods run on the GTK main loop; slow work goes to worker threads.
"""
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402

from dscene import spec  # noqa: E402
from dscene.desktop import Desktop  # noqa: E402
from dscene.host import Host  # noqa: E402
from dscene.overlay import AnnotationLayer, Caption, StepButtons  # noqa: E402
from dscene.selector import RegionSelector  # noqa: E402

CAPTURES = Path.home() / ".cache" / "dscene" / "captures"
CSS = b"""
#stepbuttons { background: #1c1d24; }
#stepbuttons label { color: #f5c46a; font-weight: bold; padding: 0 6px; }
#stepbuttons button.option { padding: 4px 14px; border-radius: 8px; background: #f59f0a;
                             color: #1c1d24; font-weight: bold; }
"""


def workarea():
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    r = monitor.get_workarea()
    return r.x, r.y, r.width, r.height


class Stage:
    SELECTION_FRESH_S = 120  # older selections are probably not what the user means now

    def __init__(self, host=None):
        self.host = host or Host()
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.desktop = Desktop()
        self.caption = Caption()
        self.toaster = Caption()
        self.toast_timer = None
        self.annotations = AnnotationLayer(top=lambda: self.scene_area()[1] + 24)
        self.current = None      # deck scene on screen; None = the user's own windows
        self.preparing = False   # scene windows come and go
        self.pending_ask = None  # done() while a question waits for its answer
        self.quick_answer = None  # the answer the host's quick key gives (tutorial steps: Done)
        self.question = None
        self.asked_at = 0.0
        self.ask_popup = None    # dscene's own question popup, when the host doesn't show it
        self.close_armed = False  # a close kept pages on screen: the next one closes them
        self.selected_at = 0.0   # when the user last selected text anywhere (X primary selection)
        Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY).connect(
            "owner-change", lambda *_: setattr(self, "selected_at", time.time()))

    def scene_area(self):
        x, y, w, h = workarea()
        top = self.host.reserved_top()
        return x, y + top, w, h - top

    # -- notices, clipboard, selection --------------------------------------------
    def toast(self, markup, seconds=3):
        """A brief notice near the bottom of the screen."""
        if self.toast_timer:
            GLib.source_remove(self.toast_timer)
        _, y, _, h = workarea()
        bottom = y + h - 40
        if self.caption.get_visible():  # a scene caption is down there: go just above it
            bottom = min(bottom, self.caption.get_position()[1] - 10)
        self.toaster.show_centered(markup, bottom)
        self.toast_timer = GLib.timeout_add(seconds * 1000, self._end_toast)

    def _end_toast(self):
        self.toaster.hide()
        self.toast_timer = None
        return False

    def copy_text(self, text):
        """The clipboard (Ctrl+Shift+V). Not the primary selection: that stays what the user
        selected."""
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()
        short = text if len(text) <= 28 else text[:27] + "…"
        self.toast(f"Copied “{GLib.markup_escape_text(short)}” · Ctrl+Shift+V to paste", 4)

    def read_selection(self, done):
        """done({"text", "age_s"}) with the text the user selected last (in any app), or
        {"text": None} when nothing was selected in the last couple of minutes."""
        age = time.time() - self.selected_at
        if not self.selected_at or age > self.SELECTION_FRESH_S:
            done({"text": None, "age_s": None})
            return
        Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY).request_text(
            lambda _c, text: done({"text": " ".join((text or "").split()) or None, "age_s": round(age)}))

    # -- looking -------------------------------------------------------------------
    def state(self):
        return {"deck": [{"index": i, "name": s.name, "note": s.note, "layout": s.layout,
                          "panes": s.urls()} for i, s in enumerate(self.desktop.scenes)],
                "showing": self.current,
                "pane_rects": [list(r) for r in self.desktop.rects],
                "annotations": [{"id": i, **dict(it)} for i, it in self.annotations.items.items()],
                "asking": self.question if self.pending_ask else None,
                "screen": list(workarea()), "scene_area": list(self.scene_area())}

    def screenshot_png(self, rect=None, fmt="png"):
        """Screen pixels (all, or rect) as PNG, or JPEG for smaller model inputs; plus (w, h)."""
        root = Gdk.get_default_root_window()
        x, y, w, h = rect or (0, 0, root.get_width(), root.get_height())
        pixbuf = Gdk.pixbuf_get_from_window(root, x, y, w, h)
        keys, values = (["quality"], ["85"]) if fmt == "jpeg" else ([], [])
        ok, data = pixbuf.save_to_bufferv(fmt, keys, values)
        return bytes(data), (w, h)

    def screenshot_window(self, win_id, fmt="jpeg"):
        """One window's own pixels by X id: right even when other windows cover it."""
        win = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), int(win_id, 16))
        pixbuf = Gdk.pixbuf_get_from_window(win, 0, 0, win.get_width(), win.get_height()) if win else None
        if pixbuf is None:
            raise ValueError(f"window {win_id} has no pixels (minimized or gone?)")
        keys, values = (["quality"], ["85"]) if fmt == "jpeg" else ([], [])
        return bytes(pixbuf.save_to_bufferv(fmt, keys, values)[1])

    # -- scenes ----------------------------------------------------------------------
    def _caption_for(self, scene, rect, text):
        if text and rect:
            x, y, w, h = rect
            self.caption.show_at(f"{GLib.markup_escape_text(scene.name)}\n<span size='small'>"
                                 f"{GLib.markup_escape_text(text)}</span>", x + 24, y + h - 24, w - 48)
        else:
            self.caption.hide()

    def apply_scene(self, scene, done):
        """set_scene: reconcile the desktop towards scene in a worker thread, then report."""
        self.preparing = True
        area = self.scene_area()

        def work():
            try:
                result = {"scene": scene.name, "panes": self.desktop.set_scene(scene, area)}
            except Exception as e:  # noqa: BLE001
                result = {"scene": scene.name, "error": f"{type(e).__name__}: {e}"}
            GLib.idle_add(finish, result)

        def finish(result):
            self.preparing = False
            self.current = None  # a set scene isn't one of the deck's cards
            self.host.showing(None, raise_host=False)
            self._caption_for(scene, self.desktop.rects[-1] if self.desktop.rects else None, scene.caption)
            if self.annotations.items:
                self.annotations.refresh()
            done(result)
            return False

        threading.Thread(target=work, daemon=True).start()

    def show(self, i):
        """Show deck scene i, or the user's own windows (None)."""
        if i is None:
            self.desktop.restore()
            self.caption.hide()
        else:
            rects = self.desktop.apply(i, self.scene_area())
            s = self.desktop.scenes[i]
            self._caption_for(s, rects[-1] if rects else None, s.caption or s.note)
        self.current = i
        if self.annotations.items:
            self.annotations.refresh()  # keep annotations above the raised scene windows
        self.host.showing(i)

    def show_scene(self, index):
        if index is not None and not 0 <= index < len(self.desktop.scenes):
            raise ValueError(f"no scene {index}; the deck has {len(self.desktop.scenes)}")
        self.show(index)
        return {"showing": self.current}

    # -- decks -----------------------------------------------------------------------
    def present_deck(self, scenes, show_first, done):
        """Build the deck's windows in the background, then announce it (and show scene 0)."""
        self.preparing = True
        self.host.deck_preparing(len(scenes))

        def work():
            t0 = time.perf_counter()
            try:
                live, swapped = spec.with_live_links(scenes)
                self.desktop.prepare(live)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(failed, e)
                return
            GLib.idle_add(ready, live, swapped, time.perf_counter() - t0)

        def failed(error):
            self.preparing = False
            done({"error": f"couldn't prepare the scenes: {error}"})

        def ready(live, swapped, took):
            self.preparing = False
            self.current = None
            self.caption.hide()
            self.close_armed = False
            self.host.deck_changed(live, took)
            if show_first:
                self.show(0)
            self._check_links()
            done({"deck": [{"index": i, "name": s.name, "note": s.note} for i, s in enumerate(live)],
                  "showing": self.current, "links_replaced_by_search": swapped})

        threading.Thread(target=work, daemon=True).start()

    def _check_links(self):
        """In the background, once the deck's pages have loaded: swap pages that don't exist
        for a site search, and tell the host which scenes changed."""
        scenes = self.desktop.scenes

        def work():
            try:
                changed = self.desktop.fix_dead_links()
            except Exception as e:  # noqa: BLE001 - a failed check just leaves the pages as they are
                print("link check failed:", e, flush=True)
                return
            for i in changed:
                GLib.idle_add(lambda i=i: self.desktop.scenes is scenes and self.host.deck_updated(i, scenes[i])
                              and False)

        threading.Thread(target=work, daemon=True).start()

    def close_deck(self, done):
        """Close the deck, in two steps when deck pages are on screen: the first closes the
        deck's pages out of sight but keeps what the user is looking at; the next closes that
        too. Only deck pages: never the user's tabs, nor pages from other requests.
        done(titles kept on screen)."""
        final, self.close_armed = self.close_armed, False
        self.caption.hide()
        self.current = None
        self.host.deck_changed([])

        def work():
            try:
                kept = self.desktop.close_deck(final)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(lambda: done({"error": str(e)}) and False)
                return
            GLib.idle_add(finish, kept)

        def finish(kept):
            self.close_armed = bool(kept)  # the next close closes what was kept
            if not kept:
                self.desktop.refocus()
            done({"kept_on_screen": kept})
            return False

        threading.Thread(target=work, daemon=True).start()

    # -- the user ------------------------------------------------------------------------
    def remember_focus(self):
        self.desktop.remember_focus()

    def refocus(self):
        self.desktop.refocus()

    def ask(self, question, options, quick, done):
        """Ask without taking keyboard focus from the user's app. quick: the answer a host's
        quick key gives (tutorial steps: their buttons sit beside the step's mark instead).
        Otherwise the host shows the question, or dscene's own popup does."""
        self.ask_cancel()
        self.pending_ask, self.quick_answer, self.question = done, quick, question
        self.asked_at = time.time()
        self.desktop.remember_focus()  # where their keys are going now, and go back to
        if quick or self.host.ask(question, options):
            return
        self.ask_popup = StepButtons(options or ["OK"], self.answer, question=question)
        x, y, w, _ = self.scene_area()
        width = self.ask_popup.get_preferred_size()[1].width
        self.ask_popup.move(x + (w - width) // 2, y + 12)
        self.ask_popup.show_all()

    def answer(self, text):
        if not self.pending_ask:
            return  # already answered (e.g. a key and a click together)
        done, self.pending_ask = self.pending_ask, None
        self.host.ask_ended(text, round(time.time() - self.asked_at, 1))
        self.ask_cancel()
        self.desktop.refocus()  # hand the keyboard back to the user's app
        done(text)

    def ask_cancel(self):
        if self.pending_ask:
            self.host.ask_ended(None, None)
        self.pending_ask = self.quick_answer = self.question = None
        if self.ask_popup:
            self.ask_popup.destroy()
            self.ask_popup = None

    def select_region(self, prompt, done):
        """done((png_path, rect)) after the user drags a box, or done((None, None))."""
        self.host.hide_chrome()
        self.toast(GLib.markup_escape_text(prompt), 3)

        def selected(pixbuf, rect):
            if pixbuf is None:
                done((None, None))
                return
            CAPTURES.mkdir(parents=True, exist_ok=True)
            path = CAPTURES / time.strftime("capture-%Y%m%d-%H%M%S.png")
            pixbuf.savev(str(path), "png", [], [])
            done((str(path), rect))

        GLib.timeout_add(250, lambda: RegionSelector(selected) and False)  # let the host vanish first
