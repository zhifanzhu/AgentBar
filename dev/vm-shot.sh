#!/usr/bin/env bash
# Run on vast: save a PNG of the VM's X screen (default /tmp/vm-shot.png).
out="${1:-/tmp/vm-shot.png}"
ssh archvm 'DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority python -c "
import gi; gi.require_version(\"Gdk\", \"3.0\"); from gi.repository import Gdk
w = Gdk.get_default_root_window()
Gdk.pixbuf_get_from_window(w, 0, 0, w.get_width(), w.get_height()).savev(\"/tmp/vm-shot.png\", \"png\", [], [])
"' && scp -q archvm:/tmp/vm-shot.png "$out" && echo "$out"
