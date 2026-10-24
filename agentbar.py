#!/usr/bin/env python3
"""Agent bar prototype: talk to an LLM agent that controls desktop scenes through dscene.

F4 (bin/agentbar-toggle) summons the bar, or hides it if it's already focused; while a
tutorial step waits, F4 answers Done instead.
Keys: Enter = ask / answer, ←/→ = flip scenes, Esc = clear the agent's marks, else back to
my desktop (twice: hide).
On the user's own desktop the bar hides itself when it loses focus; while a
scene is showing it stays, as the scene's control strip.

The bar hosts the dscene MCP server (dscene/server.py). Requests go to the
brain (brain.py), an agent that acts only through dscene tools, the same tools
any other MCP client can use. Commands start with "/" (see /help).
"""
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402

import brain  # noqa: E402
import dscene.server  # noqa: E402
from dscene import spec  # noqa: E402
from dscene.adapters import impress  # noqa: E402
from dscene.desktop import Desktop  # noqa: E402
from dscene.recorder import RECORDER  # noqa: E402
from overlay import AnnotationLayer, Caption  # noqa: E402
from selector import RegionSelector  # noqa: E402

CAPTURES = Path.home() / ".cache" / "agentbar" / "captures"
HELP = ("/show me how to … — step-by-step guidance; the agent won't do it for you · "
        "/model — show the brain · /model gemini|local [MODEL] — switch · /models — list models · "
        "/key gemini KEY — save API key · /ground holo|gemini — who finds elements on screen · "
        "/answers google|duckduckgo|bing|chatgpt|perplexity — where questions open · "
        "/new — new conversation · /shot — screenshot to slide")

CSS = b"""
#bar { background: #1c1d24; color: #e8e8ee; }
#bar entry { font-size: 15px; padding: 4px 10px; background: #2a2c36;
             color: #e8e8ee; caret-color: #e8e8ee; border-color: #3b3e4c; }
#bar button.card { padding: 2px 12px; border-radius: 8px; }
#bar button.card.active { background: #3b5bdb; color: white; }
#bar button.option { padding: 2px 14px; border-radius: 8px; background: #f59f0a; color: #1c1d24; }
#bar .reply { color: #ffffff; font-size: 16px; font-weight: bold; }
#bar .status { color: #e0e2ea; font-size: 14px; }
#stepbuttons { background: #1c1d24; }
#stepbuttons button.option { padding: 4px 14px; border-radius: 8px; background: #f59f0a;
                             color: #1c1d24; font-weight: bold; }
#bar .question { color: #f5c46a; font-size: 14px; font-weight: bold; }
"""


def workarea():
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    r = monitor.get_workarea()
    return r.x, r.y, r.width, r.height


class AgentBar(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Agent bar")
        self.set_name("bar")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()  # on every workspace: focusing it must never switch workspaces

        self.desktop = Desktop()
        self.caption = Caption()
        self.toaster = Caption()
        self.annotations = AnnotationLayer(top=lambda: self.scene_area()[1] + 24)
        self.toast_timer = None
        self.current = None     # active scene index; None = the user's own desktop
        self.cards = []
        self.preparing = False  # scene windows come and go: don't auto-hide meanwhile
        self.thinking = False   # the agent is working on a request
        self.pending_ask = None  # done() callback while ask_user waits for an answer
        self.quick_answer = None  # what F4 answers while that question waits (guide steps)
        self.asked_at = 0.0
        self.agent = brain.Agent(on_event=lambda line: GLib.idle_add(self._progress, line))

        self.set_resizable(False)  # the window then shrinks back to fit whatever is shown
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=6)
        self.entry = Gtk.Entry(placeholder_text=self.PLACEHOLDER)
        self.entry.connect("activate", self.on_enter)
        self.question = Gtk.Label(xalign=0, wrap=True)
        self.question.get_style_context().add_class("question")
        self.options_box = Gtk.Box(spacing=8)
        self.cards_box = Gtk.Box(spacing=8)
        self.reply = Gtk.Label(xalign=0, wrap=True, selectable=True)  # the agent's answer
        self.reply.get_style_context().add_class("reply")
        self.reply.set_no_show_all(True)  # shown only once there is a reply
        self.status = Gtk.Label(xalign=0, wrap=True)  # progress, timing, command output
        self.status.get_style_context().add_class("status")
        self.status.set_text(f"Brain: {brain.describe(brain.load_config())} · /help for commands")
        self.status.set_hexpand(True)
        self.ask_row = Gtk.Box(spacing=10)  # a question and its answer buttons, side by side
        self.ask_row.pack_start(self.question, False, False, 0)
        self.ask_row.pack_start(self.options_box, False, False, 0)
        info_row = Gtk.Box(spacing=12)  # the reply, then progress / timing in the same line
        info_row.pack_start(self.reply, False, False, 0)
        info_row.pack_start(self.status, True, True, 0)
        for w in (self.entry, self.ask_row, self.cards_box, info_row):
            box.pack_start(w, False, False, 0)
        self.add(box)

        self.connect("key-press-event", self.on_key)
        self.connect("focus-out-event", self.on_focus_out)
        self.connect("show", lambda *_: self.dock())

    PLACEHOLDER = "Ask the agent…  (try: hotels near King's Cross under £150 · /help)"

    def dock(self):
        x, y, w, _ = workarea()
        self.set_size_request(w, -1)
        self.move(x, y)
        GLib.idle_add(self._hide_empty)

    def _hide_empty(self):
        if not self.pending_ask:
            self.ask_row.hide()
        return False

    def scene_area(self):
        x, y, w, h = workarea()
        bar_h = self.get_allocated_height() if self.get_visible() else 0
        return x, y + bar_h, w, h - bar_h

    # -- showing / hiding ---------------------------------------------------
    def summon(self):
        self.desktop.remember_focus()
        self.show_all()
        self.present()
        if self.cards and not self.pending_ask:
            self.cards[self.current or 0].grab_focus()
        else:
            self.entry.grab_focus()

    def dismiss(self, refocus):
        """Hide the bar; refocus=True hands focus back to the user's window."""
        self.hide()
        if refocus:
            self.desktop.refocus()

    def on_focus_out(self, *_):
        GLib.timeout_add(200, self._maybe_autohide)  # ignore transient focus moves
        return False

    def _maybe_autohide(self):
        if (self.get_visible() and not self.is_active() and not self.preparing
                and not self.pending_ask and self.current is None):
            self.dismiss(refocus=False)  # focus already went where the user clicked
        return False

    def toast(self, markup, seconds=3):
        """A brief notice near the bottom of the screen, independent of the bar."""
        if self.toast_timer:
            GLib.source_remove(self.toast_timer)
        _, y, _, h = workarea()
        self.toaster.show_centered(markup, y + h - 40)
        self.toast_timer = GLib.timeout_add(seconds * 1000, self._end_toast)

    def _end_toast(self):
        self.toaster.hide()
        self.toast_timer = None
        return False

    # -- talking to the agent -----------------------------------------------
    def on_enter(self, entry):
        text = entry.get_text().strip()
        if self.pending_ask:
            if text:
                self.answer(text)
            return
        if not text:
            return
        teach = text.lower().startswith("/show ")
        if teach:  # "/show me how to …": a request, in teaching mode
            text = text[1:]
        elif text.startswith("/"):
            self.command(text)
            return
        if self.thinking:
            self.status.set_text("Still working on the last request…")
            return
        entry.set_text("")
        entry.set_placeholder_text(f"“{text}”")
        cfg = brain.load_config()
        self.thinking = True
        if not self.pending_ask:
            self.annotations.remove()  # marks from the last request are done with
        self.reply.set_text("")
        self.reply.hide()
        self.status.set_text(f"Thinking… ({brain.describe(cfg)})")
        threading.Thread(target=self._think, args=(text, cfg, teach), daemon=True).start()

    def _think(self, text, cfg, teach=False):
        t0 = time.perf_counter()
        try:
            reply = self.agent.run(text, cfg, teach)
        except Exception as e:  # noqa: BLE001 - show any failure in the bar
            reply = f"Failed: {e}"
        GLib.idle_add(self._replied, reply, time.perf_counter() - t0, cfg["models"][cfg["provider"]])

    def _progress(self, line):
        self.status.set_text(f"{self.status.get_text()} {line}")

    def _replied(self, reply, took, model):
        self.thinking = False
        self.entry.set_placeholder_text(self.PLACEHOLDER)
        self.reply.set_text(reply or "Done.")
        self.reply.show()
        self.status.set_text(f"{took:.1f}s · {model}")
        if not self.get_visible():
            self.toast(GLib.markup_escape_text(reply or "Done."), 5)

    def command(self, text):
        args = text.split()
        cmd = args[0].lower()
        RECORDER.op("user", "command", command=cmd)  # never the arguments: /key carries a secret
        cfg = brain.load_config()
        if cmd == "/model" and len(args) >= 2:
            provider = args[1].lower()
            if provider not in cfg["models"]:
                self.status.set_text(f"Unknown provider {provider!r}: use gemini or local")
                return
            cfg["provider"] = provider
            if len(args) >= 3:
                cfg["models"][provider] = args[2]
            brain.save_config(cfg)
            self.agent.reset()  # histories don't carry across providers
            self.status.set_text(f"Brain: {brain.describe(cfg)}")
        elif cmd == "/model":
            self.status.set_text(f"Brain: {brain.describe(cfg)}")
        elif cmd == "/models":
            self.status.set_text("Listing models…")
            threading.Thread(target=self._list_models, args=(cfg,), daemon=True).start()
        elif cmd == "/key" and len(args) == 3 and args[1].lower() == "gemini":
            cfg["gemini_api_key"] = args[2]
            brain.save_config(cfg)
            self.status.set_text(f"Gemini key saved to {brain.CONFIG} · Brain: {brain.describe(cfg)}")
        elif cmd == "/ground" and len(args) == 2 and args[1].lower() in ("holo", "gemini"):
            cfg["grounding"]["provider"] = args[1].lower()
            brain.save_config(cfg)
            self.status.set_text(f"find_element now uses {args[1].lower()}")
        elif cmd == "/ground":
            self.status.set_text(f"find_element uses {cfg['grounding']['provider']} · /ground holo|gemini")
        elif cmd == "/answers" and len(args) == 2 and args[1].lower() in spec.ENGINES:
            cfg["answer_engine"] = args[1].lower()
            brain.save_config(cfg)
            self.status.set_text(f"Questions now open in {args[1].lower()}")
        elif cmd == "/answers":
            self.status.set_text(f"Questions open in {cfg.get('answer_engine', 'duckduckgo')} · "
                                 f"/answers {'|'.join(spec.ENGINES)}")
        elif cmd == "/new":
            self.agent.reset()
            self.status.set_text("New conversation")
        elif cmd == "/shot":
            self.select_region("Drag to select · Esc to cancel", self._shot_selected)
        else:
            self.status.set_text(HELP)
        self.entry.set_text("")  # never leave a key sitting in the entry

    def _list_models(self, cfg):
        try:
            names = brain.list_models(cfg)
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(self.status.set_text, f"Failed: {e}")
            return
        GLib.idle_add(self.status.set_text,
                      f"{cfg['provider']} models: {', '.join(names)}\nSwitch with /model {cfg['provider']} NAME")

    # -- dscene: called by dscene.server on the GTK thread ------------------------
    def dscene_state(self):
        return {"deck": [{"index": i, "name": s.name, "note": s.note, "layout": s.layout,
                          "panes": s.urls()} for i, s in enumerate(self.desktop.scenes)],
                "showing": self.current,
                "pane_rects": [list(r) for r in self.desktop.rects],
                "annotations": [{"id": i, **{k: v for k, v in it.items()}}
                                for i, it in self.annotations.items.items()],
                "asking": self.question.get_text() if self.pending_ask else None,
                "screen": list(workarea()), "bar_visible": self.get_visible()}

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
            for card in self.cards:
                card.get_style_context().remove_class("active")
            if scene.caption and self.desktop.rects:
                x, y, w, h = self.desktop.rects[-1]
                self.caption.show_at(f"{GLib.markup_escape_text(scene.name)}\n<span size='small'>"
                                     f"{GLib.markup_escape_text(scene.caption)}</span>", x + 24, y + h - 24)
            else:
                self.caption.hide()
            if self.annotations.items:
                self.annotations.refresh()
            done(result)
            return False

        threading.Thread(target=work, daemon=True).start()

    def present_deck(self, scenes, show_first, done):
        """Build the deck's windows in the background, then show its cards (and scene 0)."""
        self.preparing = True
        self.status.set_text(f"{self.status.get_text()} · preparing {len(scenes)} scenes…")
        if not self.get_visible():
            self.summon()

        def work():
            t0 = time.perf_counter()
            try:
                live, swapped = spec.with_live_links(scenes)
                self.desktop.prepare(live)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._prepared_failed, e, done)
                return
            GLib.idle_add(self._prepared, live, swapped, show_first, time.perf_counter() - t0, done)

        threading.Thread(target=work, daemon=True).start()

    def _prepared_failed(self, error, done):
        self.preparing = False
        self.status.set_text(f"Failed to prepare scenes: {error}")
        done({"error": str(error)})

    def _prepared(self, scenes, swapped, show_first, took, done):
        self.preparing = False
        self.show_cards(scenes, took)
        if show_first:
            self.go(0)
        done({"deck": [{"index": i, "name": s.name, "note": s.note} for i, s in enumerate(scenes)],
              "showing": self.current, "links_replaced_by_search": swapped})

    def show_scene(self, index):
        if index is not None and not 0 <= index < len(self.desktop.scenes):
            raise ValueError(f"no scene {index}; the deck has {len(self.desktop.scenes)}")
        self.go(index)
        return {"showing": self.current}

    def ask(self, question, options, quick, done):
        """Show a question in the bar without taking keyboard focus from the user's app.
        quick: the answer F4 gives (None: F4 focuses the bar to type one). A quick question
        (a tutorial step) isn't shown in the bar: its buttons sit beside the step's mark."""
        self.ask_cancel()
        self.pending_ask = done
        self.quick_answer = quick
        self.asked_at = time.time()
        self.desktop.remember_focus()  # where their keys are going now, and go back to
        if quick:
            return
        self.question.set_text(question)
        for option in options:
            button = Gtk.Button(label=option)
            button.get_style_context().add_class("option")
            button.connect("clicked", lambda _b, o=option: self.answer(o))
            self.options_box.pack_start(button, False, False, 0)
        self.entry.set_text("")
        self.entry.set_placeholder_text(f"Press a button or F4 for {quick}, or click here to type…" if quick
                                        else "Press a button, or F4 to type an answer…")
        if not self.get_visible():
            self.set_focus_on_map(False)  # appear, but leave the keyboard where it is
            self.show_all()
            GLib.timeout_add(500, lambda: self.set_focus_on_map(True) or False)
        self.ask_row.show_all()

    def step_mode(self):
        """Get out of the way for a tutorial step: the bar must not cover what gets marked."""
        if self.get_visible():
            self.dismiss(refocus=self.is_active())

    def answer(self, text):
        if not self.pending_ask:
            return  # already answered (e.g. F4 and a click together)
        done, self.pending_ask = self.pending_ask, None
        RECORDER.op("user", "answer", answer=text, waited=round(time.time() - self.asked_at, 1))
        self.ask_cancel()
        if done:
            self.desktop.refocus()  # hand the keyboard back to the user's app
            done(text)

    def ask_cancel(self):
        self.pending_ask = None
        self.quick_answer = None
        for child in self.options_box.get_children():
            child.destroy()
        self.ask_row.hide()
        self.entry.set_text("")
        self.entry.set_placeholder_text(self.PLACEHOLDER)

    def select_region(self, prompt, done):
        """done((png_path, rect)) after the user drags a box, or done((None, None))."""
        self.hide()
        self.toast(GLib.markup_escape_text(prompt), 3)

        def selected(pixbuf, rect):
            if pixbuf is None:
                done((None, None))
                return
            CAPTURES.mkdir(parents=True, exist_ok=True)
            path = CAPTURES / time.strftime("capture-%Y%m%d-%H%M%S.png")
            pixbuf.savev(str(path), "png", [], [])
            done((str(path), rect))

        GLib.timeout_add(250, lambda: RegionSelector(selected) and False)  # let the bar vanish first

    # -- /shot: screenshot -> slide without the agent -----------------------------
    def _shot_selected(self, result):
        path, _ = result
        if not path:
            self.summon()
            self.status.set_text("Screenshot cancelled")
            return
        self.toast("Adding to Impress…", seconds=60)
        threading.Thread(target=self._insert_slide, args=(path,), daemon=True).start()

    def _insert_slide(self, path):
        try:
            info = impress.insert_image(path)
        except Exception as e:  # noqa: BLE001 - show any failure in the notice
            GLib.idle_add(self.toast, f"Couldn't add to Impress: {GLib.markup_escape_text(str(e))}", 6)
            return
        GLib.idle_add(self.toast, f"Added as slide {info['slide']} of {info['total']}"
                                  "  <span size='small'>· Ctrl+Z in Impress to undo</span>")

    # -- cards and switching -------------------------------------------------
    def show_cards(self, scenes, took):
        self.current = None
        self.caption.hide()
        for child in self.cards_box.get_children():
            child.destroy()
        self.cards = []
        for i, s in enumerate(scenes):
            card = Gtk.Button()
            label = Gtk.Label()
            label.set_markup(f"<b>{GLib.markup_escape_text(s.name)}</b>\n"
                             f"<small>{GLib.markup_escape_text(s.note)}</small>")
            card.add(label)
            card.get_style_context().add_class("card")
            card.connect("clicked", lambda _b, i=i: self.user_go(i))
            self.cards_box.pack_start(card, False, False, 0)
            self.cards.append(card)
        home = Gtk.Button(label="↩ My desktop")
        home.connect("clicked", lambda _b: self.user_go(None))
        self.cards_box.pack_end(home, False, False, 0)
        self.cards_box.show_all()
        self.status.set_text(f"{self.status.get_text().split(' · preparing')[0]} · "
                             f"{len(scenes)} scenes ready in {took:.1f}s · ←/→ to flip, Esc to go back")
        self.present()  # hiding the preloaded windows can move focus away from us
        if self.cards and not self.pending_ask:
            self.cards[0].grab_focus()

    def user_go(self, i):
        """The user flipped scenes (card or key): record it, then show."""
        RECORDER.op("user", "swap_scene", index=i, scene=self.desktop.scenes[i].name if i is not None else None)
        self.go(i)

    def go(self, i):
        if i is None:
            self.desktop.restore()
            self.caption.hide()
        else:
            rects = self.desktop.apply(i, self.scene_area())
            s = self.desktop.scenes[i]
            text = s.caption or s.note
            if text:
                x, y, w, h = rects[-1]
                self.caption.show_at(f"{GLib.markup_escape_text(s.name)}\n"
                                     f"<span size='small'>{GLib.markup_escape_text(text)}</span>",
                                     x + 24, y + h - 24)
            else:
                self.caption.hide()
        self.current = i
        for j, card in enumerate(self.cards):
            ctx = card.get_style_context()
            (ctx.add_class if j == i else ctx.remove_class)("active")
        if self.annotations.items:
            self.annotations.refresh()  # keep annotations above the raised scene windows
        self.present()

    def on_key(self, _w, event):
        key = event.keyval
        if key == Gdk.KEY_Escape:
            if self.annotations.items and not self.pending_ask:
                self.annotations.remove()  # first Esc: clear the agent's marks
                return True
            if self.pending_ask:
                self.desktop.refocus()  # Esc never cancels the agent: it hands the keyboard back
            elif self.current is None:
                self.dismiss(refocus=True)
            else:
                self.user_go(None)
            return True
        if self.cards and not self.entry.has_focus() and key in (Gdk.KEY_Left, Gdk.KEY_Right):
            step = 1 if key == Gdk.KEY_Right else -1
            start = -1 if self.current is None and step == 1 else (self.current or 0)
            self.user_go((start + step) % len(self.cards))
            return True
        typing = Gdk.keyval_to_unicode(key) >= 32 and not (
            event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK))
        if typing and not self.entry.has_focus():
            self.entry.grab_focus_without_selecting()  # type-to-ask: the key goes on to the entry
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.agentbar.Prototype")
        self.win = None

    def do_activate(self):
        if self.win is None:
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.win = AgentBar(self)
            print("dscene listening on", dscene.server.start(self.win), flush=True)
            self.win.summon()
        elif self.win.pending_ask and self.win.quick_answer:
            self.win.answer(self.win.quick_answer)  # F4 during a tutorial step = Done
        elif self.win.get_visible() and self.win.is_active():
            self.win.dismiss(refocus=self.win.current is None)
        else:  # hidden, or visible while the user works in another window
            self.win.summon()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
