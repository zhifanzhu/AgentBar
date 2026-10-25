"""Full-screen region selector drawn over a frozen screenshot.

Drag to select; Esc or right-click cancels. Freezing the screen first means the
capture never contains the selector itself and the user picks what they saw.
"""
import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

ACCENT = (0.23, 0.36, 0.86)


class RegionSelector(Gtk.Window):
    def __init__(self, on_done):
        """on_done(pixbuf, rect) gets the selected pixels and (x, y, w, h), or (None, None)."""
        super().__init__(type=Gtk.WindowType.POPUP)
        self.on_done = on_done
        root = Gdk.get_default_root_window()
        self.shot = Gdk.pixbuf_get_from_window(root, 0, 0, root.get_width(), root.get_height())
        self.move(0, 0)
        self.resize(root.get_width(), root.get_height())
        self.set_app_paintable(True)
        self.anchor = self.pointer = None
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("draw", self._draw)
        self.connect("map-event", self._grab)
        self.connect("button-press-event", self._press)
        self.connect("motion-notify-event", self._motion)
        self.connect("button-release-event", self._release)
        self.connect("key-press-event", self._key)
        self.show()

    def _grab(self, *_):
        win = self.get_window()
        cursor = Gdk.Cursor.new_from_name(win.get_display(), "crosshair")
        win.get_display().get_default_seat().grab(
            win, Gdk.SeatCapabilities.ALL, True, cursor, None, None, None)

    def _finish(self, pixbuf, rect=None):
        self.get_window().get_display().get_default_seat().ungrab()
        self.destroy()
        self.on_done(pixbuf, rect)

    def _rect(self):
        if not (self.anchor and self.pointer):
            return None
        (ax, ay), (px, py) = self.anchor, self.pointer
        return int(min(ax, px)), int(min(ay, py)), int(abs(px - ax)), int(abs(py - ay))

    def _press(self, _w, event):
        if event.button == 3:
            self._finish(None)
        else:
            self.anchor = self.pointer = (event.x, event.y)
        return True

    def _motion(self, _w, event):
        if self.anchor:
            self.pointer = (event.x, event.y)
            self.queue_draw()
        return True

    def _release(self, _w, event):
        if event.button != 1 or not self.anchor:
            return True
        self.pointer = (event.x, event.y)
        x, y, w, h = self._rect()
        if w < 4 or h < 4:  # a click, not a drag: treat as cancel
            self._finish(None)
        else:
            self._finish(self.shot.new_subpixbuf(x, y, w, h).copy(), (x, y, w, h))
        return True

    def _key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self._finish(None)
        return True

    def _draw(self, _w, cr):
        Gdk.cairo_set_source_pixbuf(cr, self.shot, 0, 0)
        cr.paint()
        W, H = self.shot.get_width(), self.shot.get_height()
        rect = self._rect()
        # Dim everything except the selection (even-odd fill leaves a hole).
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(0, 0, W, H)
        if rect:
            cr.rectangle(*rect)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.fill()

        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        if rect:
            x, y, w, h = rect
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(2)
            cr.rectangle(x - 1, y - 1, w + 2, h + 2)
            cr.stroke()
            self._label(cr, f"{w} × {h}", x, max(y - 30, 4), 13)
        else:
            text = "Drag to select · Esc to cancel"
            cr.set_font_size(22)
            tw = cr.text_extents(text).width
            self._label(cr, text, (W - tw) / 2 - 14, H / 2 - 20, 22)

    @staticmethod
    def _label(cr, text, x, y, size):
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        cr.set_source_rgba(0.08, 0.08, 0.12, 0.85)
        cr.rectangle(x, y, ext.width + 20, size + 14)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(x + 10, y + size + 5)
        cr.show_text(text)
