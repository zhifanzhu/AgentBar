#!/usr/bin/env python3
"""Insert an image as a slide in LibreOffice Impress, through its UNO API.

Usage: impress.py IMAGE  -> prints JSON {"slide": n, "total": m, "title": ...}

Starts LibreOffice (listening on a local socket) if needed, reuses the current
presentation if there is one, and fills the current slide if it's still empty;
otherwise adds a new slide after it. The insert is one undo step in Impress.
"""
import json
import subprocess
import sys
import time

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


if __name__ == "__main__":
    print(json.dumps(insert(sys.argv[1])))
