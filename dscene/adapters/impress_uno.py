#!/usr/bin/env python3
"""LibreOffice Impress through its UNO API: insert images, capture and restore state.

Runs as a separate process (see impress.py), so the bar never imports uno and a hung
LibreOffice connection can't freeze it:
  python -m dscene.adapters.impress_uno IMAGE           -> {"slide": n, "total": m, "title": ...}
  python -m dscene.adapters.impress_uno --capture       -> {"doc": path, "slide": n, "title", "copy"}
  python -m dscene.adapters.impress_uno --restore JSON  -> {"ok": true, "title": ...}

Starts LibreOffice (listening on a local socket) if needed, reuses the current
presentation if there is one, and fills the current slide if it's still empty;
otherwise adds a new slide after it. The insert is one undo step in Impress.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import uno
from com.sun.star.awt import Point, Size
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException

PORT = 2002
ACCEPT = f"socket,host=127.0.0.1,port={PORT};urp;"
BLANK_LAYOUT = 20     # AUTOLAYOUT_NONE
MARGIN = 1000         # 1 cm; UNO lengths are in 1/100 mm
PX = 26.46            # 1/100 mm per pixel at 96 dpi
MAX_UPSCALE = 2.0     # don't blow small captures up past 2x their pixel size
BLOBS = Path.home() / ".local" / "share" / "dscene" / "blobs"


def prop(name, value):
    return PropertyValue(name, 0, value, 0)


def connect():
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)
    for attempt in range(150):
        try:
            return resolver.resolve(f"uno:{ACCEPT}StarOffice.ComponentContext")
        except NoConnectException:
            if attempt == 0:  # also asks an already-running LibreOffice to listen
                running = subprocess.run(["pgrep", "-x", "soffice.bin"],
                                         capture_output=True).returncode == 0
                subprocess.Popen(["soffice", f"--accept={ACCEPT}", "--norestore", "--nologo"]
                                 + ([] if running else ["--impress"]),  # skip the Start Center
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            time.sleep(0.2)
    raise RuntimeError("LibreOffice did not start listening")


def quiet_startup(smgr, ctx):
    """Turn off Impress's template picker and the tip-of-the-day dialog (persistent)."""
    provider = smgr.createInstanceWithContext(
        "com.sun.star.configuration.ConfigurationProvider", ctx)
    for path, key in (("/org.openoffice.Office.Impress/Misc/NewDoc", "AutoPilot"),
                      ("/org.openoffice.Office.Common/Misc", "ShowTipOfTheDay")):
        node = provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationUpdateAccess", (prop("nodepath", path),))
        if node.getPropertyValue(key):
            node.setPropertyValue(key, False)
            node.commitChanges()
            provider.flush()  # LibreOffice otherwise only writes settings on clean exit


def is_presentation(doc):
    try:
        return doc.supportsService("com.sun.star.presentation.PresentationDocument")
    except AttributeError:  # e.g. the Start Center
        return False


def presentation(desktop):
    if is_presentation(desktop.CurrentComponent):
        return desktop.CurrentComponent
    docs = desktop.Components.createEnumeration()
    while docs.hasMoreElements():
        doc = docs.nextElement()
        if is_presentation(doc):
            return doc
    return desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, ())


def is_empty(page):
    for i in range(page.Count):
        try:
            if not page.getByIndex(i).IsEmptyPresentationObject:
                return False
        except AttributeError:  # a plain shape, not a placeholder
            return False
    return True


def insert(path):
    ctx = connect()
    smgr = ctx.ServiceManager
    quiet_startup(smgr, ctx)
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = presentation(desktop)
    controller = doc.CurrentController
    graphic = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx) \
        .queryGraphic((prop("URL", uno.systemPathToFileUrl(path)),))

    undo = doc.UndoManager
    undo.enterUndoContext("Insert screenshot")
    try:
        page = controller.CurrentPage
        if not is_empty(page):
            # The UI command, not DrawPages.insertNewByIndex: only it is recorded for undo.
            smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx) \
                .executeDispatch(controller.Frame, ".uno:InsertPage", "", 0, ())
            page = controller.CurrentPage  # the new slide, after the old current one
        page.Layout = BLANK_LAYOUT

        px = graphic.SizePixel
        scale = min((page.Width - 2 * MARGIN) / px.Width,
                    (page.Height - 2 * MARGIN) / px.Height,
                    MAX_UPSCALE * PX)
        w, h = int(px.Width * scale), int(px.Height * scale)
        shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
        page.add(shape)
        shape.Graphic = graphic
        shape.Size = Size(w, h)
        shape.Position = Point((page.Width - w) // 2, (page.Height - h) // 2)
    finally:
        undo.leaveUndoContext()

    controller.CurrentPage = page
    return {"slide": page.Number, "total": doc.DrawPages.Count, "title": doc.Title}


def _open_presentations(desktop):
    docs = desktop.Components.createEnumeration()
    while docs.hasMoreElements():
        doc = docs.nextElement()
        if is_presentation(doc):
            yield doc


def capture_state():
    """The current presentation and slide. Unsaved or modified documents are saved as a copy."""
    if subprocess.run(["pgrep", "-x", "soffice.bin"], capture_output=True).returncode != 0:
        return {"ok": False, "error": "LibreOffice is not running"}
    ctx = connect()
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    current = desktop.CurrentComponent
    doc = current if is_presentation(current) else next(_open_presentations(desktop), None)
    if doc is None:
        return {"ok": False, "error": "no presentation open"}
    copy = not doc.hasLocation() or doc.isModified()
    if copy:  # keep the unsaved state without touching the user's document
        BLOBS.mkdir(parents=True, exist_ok=True)
        path = BLOBS / f"impress-{time.strftime('%Y%m%d-%H%M%S')}.odp"
        doc.storeToURL(uno.systemPathToFileUrl(str(path)), (prop("FilterName", "impress8"),))
    else:
        path = Path(uno.fileUrlToSystemPath(doc.Location))
    return {"ok": True, "doc": str(path), "slide": doc.CurrentController.CurrentPage.Number,
            "title": doc.Title, "copy": copy}


def restore_state(state):
    """Show state["doc"] (reusing it if it's already open) at state["slide"]."""
    ctx = connect()
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    url = uno.systemPathToFileUrl(state["doc"])
    open_docs = list(_open_presentations(desktop))
    doc = next((d for d in open_docs if d.hasLocation() and d.Location == url), None)
    if doc is None and state.get("title"):  # the captured copy's original is still open: use it
        doc = next((d for d in open_docs if d.Title == state["title"]), None)
    if doc is None:
        doc = desktop.loadComponentFromURL(url, "_blank", 0, ())
    slide = state.get("slide")
    if slide and 1 <= slide <= doc.DrawPages.Count:
        doc.CurrentController.CurrentPage = doc.DrawPages.getByIndex(slide - 1)
    return {"ok": True, "title": doc.Title}


if __name__ == "__main__":
    if sys.argv[1] == "--capture":
        print(json.dumps(capture_state()))
    elif sys.argv[1] == "--restore":
        print(json.dumps(restore_state(json.loads(sys.argv[2]))))
    else:
        print(json.dumps(insert(sys.argv[1])))
