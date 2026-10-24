"""Transparent, click-through windows drawn on top of everything: captions and annotations."""
import math

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
from gi.repository import GLib, Gtk, Pango, PangoCairo  # noqa: E402

PAD = 16
ACCENT = (0.23, 0.36, 0.86)   # boxes, labels, arrows
STEP = (0.96, 0.62, 0.04)     # numbered tutorial steps
KINDS = ("box", "step", "label", "arrow", "spotlight")


class AnnotationLayer(Gtk.Window):
    """One full-screen, click-through layer that draws every annotation.

    Each annotation has a screen rect and optional text. Rects are resolved when
    the annotation is made; they don't follow windows that move afterwards.
    """

    def __init__(self, top=lambda: 140):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.top = top  # () -> y where free text goes, e.g. just under the agent bar
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.move(0, 0)
        self.resize(screen.get_width(), screen.get_height())
        self.items = {}  # id -> {"kind", "rect", "text", "step"}; a step's rect may be None
        self.buttons = {}  # id -> StepButtons beside that item's bubble
        self.next_id = 1
        self.connect("draw", self._draw)
        self.connect("realize", lambda w: w.input_shape_combine_region(cairo.Region()))

    def add(self, kind, rect, text="", step=None, seconds=None):
        """Draw an annotation; with seconds, it goes away by itself after that long."""
        item_id = self.next_id
        self.next_id += 1
        self.items[item_id] = {"kind": kind, "rect": rect, "text": text, "step": step}
        self.refresh()
        if seconds:
            GLib.timeout_add(int(seconds * 1000), lambda: self.remove([item_id]) and False)
        return item_id

    def add_buttons(self, item_id, options, on_click):
        """Clickable option buttons beside an item's text bubble; on_click(option)."""
        if item_id not in self.items:
            return
        buttons = StepButtons(options, on_click)
        bx, by, bw, bh = self._bubble_rect(self.items[item_id])
        w, h = buttons.get_preferred_size()[1].width, buttons.get_preferred_size()[1].height
        sw, sh = self.get_screen().get_width(), self.get_screen().get_height()
        x, y = bx + bw + 8, by + (bh - h) // 2
        if x + w > sw - 4:  # no room on the right: go under the bubble
            x, y = min(bx, sw - w - 4), by + bh + 6
        buttons.move(max(4, x), max(4, min(y, sh - h - 4)))
        self.buttons[item_id] = buttons
        buttons.show_all()
        self.refresh()

    def remove(self, ids=None):
        """Remove the given annotation ids, or all of them. Returns the ids removed."""
        gone = list(self.items) if ids is None else [i for i in ids if i in self.items]
        for i in gone:
            del self.items[i]
            if i in self.buttons:
                self.buttons.pop(i).destroy()
        self.refresh()
        return gone

    def refresh(self):
        if not self.items:
            self.hide()
            return
        self.show()
        self.queue_draw()
        self.get_window().raise_()  # stay above windows that scene switches raise
        for buttons in self.buttons.values():
            buttons.get_window().raise_()

    def _text_layout(self, text, cr=None):
        layout = PangoCairo.create_layout(cr) if cr else self.create_pango_layout("")
        layout.set_font_description(Pango.FontDescription("Sans Bold 15"))
        layout.set_text(text, -1)
        return layout

    def _bubble_at(self, text, x, y):
        """The rect of a text bubble whose top-left corner is near (x, y), kept on screen."""
        tw, th = self._text_layout(text).get_pixel_size()
        w, h = tw + 20, th + 12
        x = max(4, min(x, self.get_screen().get_width() - w - 4))
        y = max(4, min(y, self.get_screen().get_height() - h - 4))
        return x, y, w, h

    def _bubble_rect(self, it):
        """Where an item's text bubble goes (not for arrows): under its rect, or above when
        there's no room below; a step without a rect gets a bubble at the top middle."""
        if it["rect"] is None:
            w = self._bubble_at(it["text"], 0, 0)[2]
            return self._bubble_at(it["text"], (self.get_screen().get_width() - w) // 2, self.top())
        x, y, w, h = it["rect"]
        below = y + h + 10
        return self._bubble_at(it["text"], x, below if below + 40 < self.get_screen().get_height() else y - 50)

    def _bubble(self, cr, text, x, y, colour, rect=None):
        """A text bubble whose top-left corner is near (x, y), kept on screen."""
        layout = self._text_layout(text, cr)
        x, y, w, h = rect or self._bubble_at(text, x, y)
        _rounded(cr, x, y, w, h, 8)
        cr.set_source_rgba(*colour, 0.95)
        cr.fill()
        cr.move_to(x + 10, y + 6)
        cr.set_source_rgb(1, 1, 1)
        PangoCairo.show_layout(cr, layout)
        return x, y, w, h

    def _draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        W, H = self.get_allocated_width(), self.get_allocated_height()
        spots = [it["rect"] for it in self.items.values() if it["kind"] == "spotlight"]
        if spots:  # dim everything except the spotlighted rects
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.rectangle(0, 0, W, H)
            for x, y, w, h in spots:
                cr.rectangle(x - 6, y - 6, w + 12, h + 12)
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.fill()
            cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        for it in self.items.values():
            colour = STEP if it["kind"] == "step" else ACCENT
            if it["rect"] is None:  # a step with nothing to point at: just its text
                self._bubble(cr, it["text"], 0, 0, colour, self._bubble_rect(it))
                continue
            x, y, w, h = it["rect"]
            if it["kind"] in ("box", "step", "spotlight"):
                _rounded(cr, x - 4, y - 4, w + 8, h + 8, 6)
                cr.set_source_rgba(*colour, 1)
                cr.set_line_width(3)
                cr.stroke()
            if it["kind"] == "step":
                n = str(it["step"] or "")
                r = 16
                cx, cy = x - 4, y - 4
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.set_source_rgba(*STEP, 1)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(17)
                ext = cr.text_extents(n)
                cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + ext.height / 2)
                cr.show_text(n)
            if it["kind"] == "arrow":
                bx, by, bw, bh = self._bubble(cr, it["text"] or "here", x + w + 60, y - 70, colour)
                tx, ty = x + w / 2, y + h / 2
                sx, sy = bx, by + bh
                ang = math.atan2(ty - sy, tx - sx)
                cr.move_to(sx, sy)
                cr.line_to(tx - 10 * math.cos(ang), ty - 10 * math.sin(ang))
                cr.set_source_rgba(*colour, 1)
                cr.set_line_width(4)
                cr.stroke()
                for side in (-0.5, 0.5):  # arrowhead
                    cr.move_to(tx, ty)
                    cr.line_to(tx - 18 * math.cos(ang + side), ty - 18 * math.sin(ang + side))
                cr.stroke()
            elif it["text"]:
                self._bubble(cr, it["text"], 0, 0, colour, self._bubble_rect(it))


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class StepButtons(Gtk.Window):
    """A row of option buttons that floats over the screen. It takes clicks but never keyboard
    focus (an override-redirect popup), so the user's keys keep going to their app."""

    def __init__(self, options, on_click):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_name("stepbuttons")
        box = Gtk.Box(spacing=6, margin=5)
        for i, option in enumerate(options):
            button = Gtk.Button(label=f"{option}  ·  F4" if i == 0 else option)
            button.set_can_focus(False)
            button.get_style_context().add_class("option")
            button.connect("clicked", lambda _b, o=option: on_click(o))
            box.pack_start(button, False, False, 0)
        self.add(box)
        box.show_all()


class Caption(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.markup = ""
        self.max_width = None  # wrap lines longer than this many pixels
        self.connect("draw", self._draw)
        # Empty input shape: clicks fall through to the windows underneath.
        self.connect("realize", lambda w: w.input_shape_combine_region(cairo.Region()))

    def _layout(self, cr=None):
        layout = PangoCairo.create_layout(cr) if cr else self.create_pango_layout("")
        layout.set_font_description(Pango.FontDescription("Sans Bold 17"))
        layout.set_markup(self.markup, -1)
        if self.max_width:
            layout.set_width(self.max_width * Pango.SCALE)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        return layout

    def show_at(self, markup, x, bottom, max_width=None):
        """Show markup with its bottom-left corner at (x, bottom), wrapped to fit max_width
        (default: the rest of the screen to the right of x)."""
        self.markup = markup
        self.max_width = max(120, (max_width or self.get_screen().get_width() - x - 24) - 2 * PAD)
        w, h = self._layout().get_pixel_size()
        self.resize(w + 2 * PAD, h + 2 * PAD)
        self.move(x, bottom - h - 2 * PAD)
        self.show()
        self.queue_draw()
        if self.get_window():
            self.get_window().raise_()

    def show_centered(self, markup, bottom):
        """Show markup centred horizontally on the screen, bottom edge at bottom; lines wrap
        at 80% of the screen width."""
        self.markup = markup
        self.max_width = int(self.get_screen().get_width() * 0.8) - 2 * PAD
        w, _ = self._layout().get_pixel_size()
        self.show_at(markup, (self.get_screen().get_width() - w) // 2 - PAD, bottom, w + 2 * PAD)

    def _draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        w, h = self.get_allocated_width(), self.get_allocated_height()
        r = 12
        cr.new_sub_path()
        cr.arc(w - r, r, r, -1.5708, 0)
        cr.arc(w - r, h - r, r, 0, 1.5708)
        cr.arc(r, h - r, r, 1.5708, 3.1416)
        cr.arc(r, r, r, 3.1416, 4.7124)
        cr.close_path()
        cr.set_source_rgba(0.08, 0.08, 0.12, 0.78)
        cr.fill()
        cr.move_to(PAD, PAD)
        cr.set_source_rgb(1, 1, 1)
        PangoCairo.show_layout(cr, self._layout(cr))
