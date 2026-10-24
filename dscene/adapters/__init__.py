"""State adapters: one module per kind of app window, each able to capture what the
window shows and bring it back (see base.py for the interface).

  browser.py   the agent's Chromium slots, over CDP (url, scroll, background screenshots)
  blender.py   Blender, through blender_bridge.py running inside Blender
  impress.py   LibreOffice Impress, through the UNO worker impress_uno.py
  window.py    any other app: position and size only
"""
from dscene.adapters.blender import BlenderAdapter
from dscene.adapters.impress import ImpressAdapter
from dscene.adapters.window import WindowAdapter

APP_ADAPTERS = {"blender": BlenderAdapter(), "impress": ImpressAdapter(), "window": WindowAdapter()}


def adapter_for(win):
    """The most specific adapter for a live window."""
    for kind in ("blender", "impress"):
        if APP_ADAPTERS[kind].matches(win):
            return APP_ADAPTERS[kind]
    return APP_ADAPTERS["window"]
