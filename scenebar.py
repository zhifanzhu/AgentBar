#!/usr/bin/env python3
"""SceneBar: talk to an LLM agent that controls desktop scenes through dscene.

F4 (bin/scenebar-toggle) summons the bar, or hides it if it's already focused; while a
tutorial step waits, F4 answers Done instead.
Keys: Enter = ask / answer, ←/→ = flip scenes, Esc = clear the agent's marks, else back to
my desktop (twice: hide).
On the user's own desktop the bar hides itself when it loses focus; while a
scene is showing it stays, as the scene's control strip.

The bar hosts dscene: its Stage (dscene/stage.py) does everything on the desktop, and its
MCP server (dscene/server.py) is how the brain (brain.py) acts, the same tools any other
MCP client can use. The bar is dscene's Host (dscene/host.py): it reserves the top strip,
shows the deck as cards and asks questions in its own window. Commands start with "/"
(see /help).
"""
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import brain  # noqa: E402
import dscene.server  # noqa: E402
from dscene import config as dscene_config  # noqa: E402
from dscene import spec  # noqa: E402
from dscene.adapters import impress  # noqa: E402
from dscene.host import Host  # noqa: E402
from dscene.stage import Stage, workarea  # noqa: E402
from recorder import RECORDER  # noqa: E402

HELP = ("? — search the text you selected (any app) · /search [WORDS] — search the selection or WORDS · "
        "/deck … — compare options as cards to flip through · "
        "/show me how to … — step-by-step guidance; the agent won't do it for you · "
        "/model — show the brain · /model gemini|local [MODEL] — switch · /models — list models · "
        "/key gemini KEY — save API key · /ground gemini|holo [URL] — who finds elements on screen · "
        "/answers google|duckduckgo|bing|chatgpt|perplexity — where questions open · "
        "/remember TEXT — note about you, sent with every request · "
        "/close — close the deck · /new — new conversation · /shot — screenshot to slide")

CSS = b"""
#bar { background: #1c1d24; color: #e8e8ee; }
#bar entry { font-size: 15px; padding: 4px 10px; background: #2a2c36;
             color: #e8e8ee; caret-color: #e8e8ee; border-color: #3b3e4c; }
#bar button.card { padding: 2px 12px; border-radius: 8px; }
#bar button.card.active { background: #3b5bdb; color: white; }
#bar button.option { padding: 2px 14px; border-radius: 8px; background: #f59f0a; color: #1c1d24; }
#bar .reply { color: #ffffff; font-size: 16px; font-weight: bold; }
#bar .status { color: #e0e2ea; font-size: 14px; }
#bar .question { color: #f5c46a; font-size: 14px; font-weight: bold; }
"""


def _grounding(scene_cfg):
    """Who finds elements on screen, for the status line: "gemini" or "holo at URL"."""
    g = scene_cfg["grounding"]
    return f"holo at {g['url']}" if g["provider"] == "holo" else g["provider"]


def seed_dscene_config():
    """First run of a dscene with its own config: carry over the settings the bar kept in
    brain.json before (grounding, answer engine, Gemini key)."""
    if dscene_config.exists():
        return
    old = brain.load_config()
    dscene_config.update(**{k: old[k] for k in ("grounding", "answer_engine", "gemini_api_key") if old.get(k)})


class SceneBar(Gtk.ApplicationWindow, Host):
    def __init__(self, app):
        super().__init__(application=app, title="SceneBar")
        self.set_name("bar")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()  # on every workspace: focusing it must never switch workspaces

        self.stage = Stage(host=self)
        self.cards = []
        self.thinking = False   # the agent is working on a request
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

    PLACEHOLDER = "Ask the agent…  (try: /deck hotels near King's Cross under £150 · /help)"

    def dock(self):
        x, y, w, _ = workarea()
        self.set_size_request(w, -1)
        self.move(x, y)
        GLib.idle_add(self._hide_empty)

    def _hide_empty(self):
        if not self.stage.pending_ask:
            self.ask_row.hide()
        return False

    # -- dscene Host hooks (dscene/host.py) -------------------------------------------
    def reserved_top(self):
        return self.get_allocated_height() if self.get_visible() else 0

    def quick_key(self):
        return "F4"

    def hide_chrome(self):
        if not self.get_visible():
            return False
        # No refocus: it would raise the user's last window over what they're looking at
        # (e.g. Blender over the agent's page they want to capture); the top window gets focus.
        self.dismiss(refocus=False)
        return True

    def deck_preparing(self, count):
        self.status.set_text(f"{self.status.get_text()} · preparing {count} scenes…")
        if not self.get_visible():
            self.summon()

    def deck_changed(self, scenes, took=None):
        for child in self.cards_box.get_children():
            child.destroy()
        self.cards = []
        if not scenes:
            return
        for i, s in enumerate(scenes):
            card = Gtk.Button()
            label = Gtk.Label()
            label.set_markup(self._card_markup(s))
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
                             f"{len(scenes)} scenes ready in {took or 0:.1f}s · ←/→ to flip, Esc to go back")
        self.present()  # hiding the preloaded windows can move focus away from us
        if self.cards and not self.stage.pending_ask:
            self.cards[0].grab_focus()

    def deck_updated(self, index, scene):
        RECORDER.op("agent", "dead_links_replaced", scenes=[scene.name])
        if index < len(self.cards):
            self.cards[index].get_child().set_markup(self._card_markup(scene))

    def showing(self, index, raise_host=True):
        for j, card in enumerate(self.cards):
            ctx = card.get_style_context()
            (ctx.add_class if j == index else ctx.remove_class)("active")
        if raise_host:
            self.present()

    def ask(self, question, options):
        """Show a question in the bar without taking keyboard focus from the user's app."""
        self.question.set_text(question)
        for option in options:
            button = Gtk.Button(label=option)
            button.get_style_context().add_class("option")
            button.connect("clicked", lambda _b, o=option: self.stage.answer(o))
            self.options_box.pack_start(button, False, False, 0)
        self.entry.set_text("")
        self.entry.set_placeholder_text("Press a button, or F4 to type an answer…")
        if not self.get_visible():
            self.set_focus_on_map(False)  # appear, but leave the keyboard where it is
            self.show_all()
            GLib.timeout_add(500, lambda: self.set_focus_on_map(True) or False)
        self.ask_row.show_all()
        return True

    def ask_ended(self, answer, waited):
        if answer is not None:
            RECORDER.op("user", "answer", answer=answer, waited=waited)
        for child in self.options_box.get_children():
            child.destroy()
        self.ask_row.hide()
        self.entry.set_text("")
        self.entry.set_placeholder_text(self.PLACEHOLDER)

    # -- showing / hiding ---------------------------------------------------
    def summon(self):
        self.stage.remember_focus()
        self.show_all()
        self.present()
        if self.cards and not self.stage.pending_ask:
            self.cards[self.stage.current or 0].grab_focus()
        else:
            self.entry.grab_focus()

    def dismiss(self, refocus):
        """Hide the bar; refocus=True hands focus back to the user's window."""
        self.hide()
        if refocus:
            self.stage.refocus()

    def on_focus_out(self, *_):
        GLib.timeout_add(200, self._maybe_autohide)  # ignore transient focus moves
        return False

    def _maybe_autohide(self):
        if (self.get_visible() and not self.is_active() and not self.stage.preparing
                and not self.stage.pending_ask and self.stage.current is None):
            self.dismiss(refocus=False)  # focus already went where the user clicked
        return False

    def search(self, words):
        """? and /search: open a web search for words, or for the selected text (short
        selections are looked up as "… meaning"). No model call."""
        RECORDER.op("user", "search", typed=bool(words))

        def go(selection):
            text = words or selection["text"]
            if not text:
                self.status.set_text("Select some text first (or type /search WORDS)")
                return
            query = text[:200]
            if not words and len(query.split()) <= 3:
                query = f'"{query}" meaning'
            scene = spec.SceneSpec(name=query[:60], panes=[{"kind": "search", "query": query}])
            self.status.set_text(f"Searching {query[:80]}…")
            self.stage.apply_scene(scene, lambda result: self.status.set_text(
                result.get("error") and f"Search failed: {result['error']}" or f"Searched: {query[:80]}"))

        if words:
            go({"text": None})
        else:
            self.stage.read_selection(go)

    # -- talking to the agent -----------------------------------------------
    def on_enter(self, entry):
        text = entry.get_text().strip()
        if self.stage.pending_ask:
            if text:
                self.stage.answer(text)
            return
        if not text:
            return
        if text == "?" or text.lower() == "/search" or text.lower().startswith("/search "):
            entry.set_text("")
            self.search(text[len("/search"):].strip() if text.lower().startswith("/search") else "")
            return
        mode = "auto"
        if text.lower().startswith("/show "):  # "/show me how to …": a request, in teaching mode
            mode, text = "teach", text[1:]
        elif text.lower().startswith("/deck "):  # "/deck hotels near …": options, as a deck
            mode, text = "deck", text[6:].strip()
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
        self.stage.close_armed = False  # a new request: /close starts over
        if not self.stage.pending_ask:
            self.stage.annotations.remove()  # marks from the last request are done with
        self.reply.set_text("")
        self.reply.hide()
        self.status.set_text(f"Thinking… ({brain.describe(cfg)})")
        threading.Thread(target=self._think, args=(text, cfg, mode), daemon=True).start()

    def _think(self, text, cfg, mode="auto"):
        t0 = time.perf_counter()
        try:
            reply = self.agent.run(text, cfg, mode)
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
            self.stage.toast(GLib.markup_escape_text(reply or "Done."), 5)

    def command(self, text):
        args = text.split()
        cmd = args[0].lower()
        RECORDER.op("user", "command", command=cmd)  # never the arguments: /key carries a secret
        cfg = brain.load_config()
        scene_cfg = dscene_config.load()
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
            dscene_config.update(gemini_api_key=args[2])  # dscene's Gemini grounding uses it too
            self.status.set_text(f"Gemini key saved · Brain: {brain.describe(cfg)}")
        elif cmd == "/ground" and len(args) in (2, 3) and args[1].lower() in ("holo", "gemini"):
            grounding = {"provider": args[1].lower()}
            if len(args) == 3:  # the Holo server; also the local brain's, unless brain.json sets one
                grounding["url"] = args[2].rstrip("/")
            if grounding["provider"] == "holo" and not grounding.get("url", scene_cfg["grounding"]["url"]):
                self.status.set_text("Holo needs its server: /ground holo URL, e.g. http://localhost:8000/v1")
                return
            scene_cfg = dscene_config.update(grounding=grounding)
            self.status.set_text(f"find_element now uses {_grounding(scene_cfg)}")
        elif cmd == "/ground":
            self.status.set_text(f"find_element uses {_grounding(scene_cfg)} · /ground gemini|holo [URL]")
        elif cmd == "/answers" and len(args) == 2 and args[1].lower() in spec.ENGINES:
            dscene_config.update(answer_engine=args[1].lower())
            self.status.set_text(f"Questions now open in {args[1].lower()}")
        elif cmd == "/answers":
            self.status.set_text(f"Questions open in {scene_cfg['answer_engine']} · "
                                 f"/answers {'|'.join(spec.ENGINES)}")
        elif cmd == "/remember" and len(args) >= 2:
            brain.remember(text.split(None, 1)[1])
            self.status.set_text(f"Remembered · {brain.USER_NOTES}")
        elif cmd == "/remember":
            self.status.set_text(f"/remember TEXT adds a line to {brain.USER_NOTES}")
        elif cmd == "/close":
            self.close_deck()
        elif cmd == "/new":
            self.agent.reset()
            self.status.set_text("New conversation")
        elif cmd == "/shot":
            self.stage.select_region("Drag to select · Esc to cancel", self._shot_selected)
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

    # -- /shot: screenshot -> slide without the agent -----------------------------
    def _shot_selected(self, result):
        path, _ = result
        if not path:
            self.summon()
            self.status.set_text("Screenshot cancelled")
            return
        self.stage.toast("Adding to Impress…", seconds=60)
        threading.Thread(target=self._insert_slide, args=(path,), daemon=True).start()

    def _insert_slide(self, path):
        try:
            info = impress.insert_image(path)
        except Exception as e:  # noqa: BLE001 - show any failure in the notice
            GLib.idle_add(self.stage.toast, f"Couldn't add to Impress: {GLib.markup_escape_text(str(e))}", 6)
            return
        GLib.idle_add(self.stage.toast, f"Added as slide {info['slide']} of {info['total']}"
                                        "  <span size='small'>· Ctrl+Z in Impress to undo</span>")

    # -- cards and switching -------------------------------------------------
    def close_deck(self):
        """/close: dscene closes the deck, in two steps when deck pages are on screen."""
        RECORDER.op("user", "close_deck", scenes=len(self.cards), final=self.stage.close_armed)
        self.status.set_text("Closing…")

        def closed(result):
            kept = result.get("kept_on_screen") or []
            self.status.set_text(
                f"Closing failed: {result['error']}" if "error" in result
                else f"Kept “{kept[0][:40]}”{f' and {len(kept) - 1} more' if len(kept) > 1 else ''} "
                     "on screen · /close again to close it" if kept
                else "Deck closed · your tabs and pages you asked for stay open" if result.get("closed")
                else "No deck open · hid the agent's windows, their pages are still there")

        self.stage.close_deck(closed)

    @staticmethod
    def _card_markup(scene):
        return (f"<b>{GLib.markup_escape_text(scene.name)}</b>\n"
                f"<small>{GLib.markup_escape_text(scene.note)}</small>")

    def user_go(self, i):
        """The user flipped scenes (card or key): record it, then show."""
        scenes = self.stage.desktop.scenes
        RECORDER.op("user", "swap_scene", index=i, scene=scenes[i].name if i is not None else None)
        self.stage.show(i)

    def on_key(self, _w, event):
        key = event.keyval
        if key == Gdk.KEY_Escape:
            if self.stage.annotations.items and not self.stage.pending_ask:
                self.stage.annotations.remove()  # first Esc: clear the agent's marks
                return True
            if self.stage.pending_ask:
                self.stage.refocus()  # Esc never cancels the agent: it hands the keyboard back
            elif self.stage.current is None:
                self.dismiss(refocus=True)
            else:
                self.user_go(None)
            return True
        if self.cards and not self.entry.has_focus() and key in (Gdk.KEY_Left, Gdk.KEY_Right):
            step = 1 if key == Gdk.KEY_Right else -1
            current = self.stage.current
            start = -1 if current is None and step == 1 else (current or 0)
            self.user_go((start + step) % len(self.cards))
            return True
        typing = Gdk.keyval_to_unicode(key) >= 32 and not (
            event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK))
        if typing and not self.entry.has_focus():
            self.entry.grab_focus_without_selecting()  # type-to-ask: the key goes on to the entry
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.scenebar.SceneBar")
        self.win = None

    def do_activate(self):
        if self.win is None:
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            seed_dscene_config()
            self.win = SceneBar(self)
            print("dscene listening on", dscene.server.start(self.win.stage), flush=True)
            self.win.summon()
            return
        stage = self.win.stage
        if stage.pending_ask and stage.quick_answer:
            stage.answer(stage.quick_answer)  # F4 during a tutorial step = Done
        elif self.win.get_visible() and self.win.is_active():
            self.win.dismiss(refocus=stage.current is None)
        else:  # hidden, or visible while the user works in another window
            self.win.summon()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
