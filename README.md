
# <img width="443" height="67" alt="SceneBar" src="https://github.com/user-attachments/assets/6e3ccd11-4505-41e9-a852-67001255290b" />

SceneBar is a small AI bar that sits at the top of the Linux desktop. Press F4, type what you need, and it sets up the screen for it, e.g. a few hotels side by side with their maps, ready to flip through like cards, or a Blender window with the next button to press circled. SceneBar works alongside the person at the computer rather than in their place.

[![SceneBar demo video](https://img.youtube.com/vi/C1G2WH4QIcI/maxresdefault.jpg)](https://youtu.be/C1G2WH4QIcI)

▶ [Watch the demo video](https://youtu.be/C1G2WH4QIcI)

Runs on Arch Linux + XFCE (X11).

## The problem

Two slow parts of computer work: comparing options means juggling a dozen tabs and windows, and unfamiliar software means switching back and forth between a tutorial and the app.

Existing computer-use agents (codex, hermes, etc) take over the mouse and keyboard, which is slow, hard to trust, and teaches the user nothing.

## Install

Linux with an X11 session (developed on Arch Linux + XFCE). On Arch, everything comes from
the official repositories:

    sudo pacman -S gtk3 python-gobject python-cairo at-spi2-core \
        wmctrl xdotool xorg-xprop xorg-xwininfo xclip chromium xfce4-terminal \
        python-mcp python-pydantic python-websocket-client python-pillow
    git clone https://github.com/zhifanzhu/SceneBar.git
    SceneBar/bin/scenebar setup

Optional: `blender` (Blender lessons and changes) and `libreoffice-fresh` (screenshots into
Impress slides).

Elsewhere, install the same tools with your package manager, and the Python packages in a
venv inside the checkout (`bin/scenebar` uses `.venv` when it's there):

    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -r requirements.txt

`scenebar setup` checks what's missing, links the Blender bridge, adds SceneBar to the app
menu and binds F4 (on XFCE; elsewhere, bind a key to `bin/scenebar-toggle` yourself). Run it
again after upgrading Blender.

## Run

Press F4 (the first press starts the bar), type `/key gemini YOUR_KEY` once (get a key at
https://aistudio.google.com/apikey), then type what you need. `/help` lists the commands.

Gemini is the brain, and by default it also finds buttons on screen. Optionally, Holo3.1, a
vision model from H Company, does that faster and with no API cost, served by an
OpenAI-compatible server such as vLLM on a GPU machine: `/ground holo http://HOST:8000/v1`.

## Uninstall

    SceneBar/bin/scenebar uninstall

It stops the bar, removes the Blender link, the menu entry and the F4 shortcut, then asks
before deleting each of SceneBar's folders:

| Folder | What's in it |
|---|---|
| `~/.config/scenebar`, `~/.config/dscene` | settings, your notes, the API key |
| `~/.cache/dscene` | the agent's browser profiles (cookies, sign-ins) |
| `~/.local/share/dscene` | saved decks, Blender and Impress checkpoints |
| `~/.local/state/scenebar` | recordings of each request, with screenshots |

Then delete the `SceneBar` folder.


## How it works

SceneBar has firm boundaries: it never clicks, types or runs commands on the user's behalf. It arranges windows, lays out options, marks where to click, and changes apps only through their own interfaces. Running a command or making a booking always stays the user's decision.

  - Gemini is the brain. It plans and calls tools over MCP.
  - Underneath is `dscene`, an MCP server with 20 tools that describes, shows, saves and restores "scenes": which windows are on screen, where, and each app's state (Chromium pages via CDP, Blender via a bridge inside Blender, LibreOffice Impress via UNO, terminals via the accessibility tree).
  - A vision model finds buttons on screen in apps like Blender that offer no other way to locate them: Gemini, or Holo3.1 running locally.
  - `dscene` can also run without the bar, so other agents (Claude Code, Codex, Hermes) can use the same scenes and the same "show, don't take over" approach.


## What's next
  - Use the accessibility tree for exact on-screen marks in GTK, LibreOffice and Chromium, keeping vision for apps without one.
  - Support more desktops: Wayland, GNOME/KDE, then macOS.
  - Package dscene as a standalone MCP server other agents can install.
  - Share scenes: send someone a whole working setup that opens on their screen.


