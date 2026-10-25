
# <img width="443" height="67" alt="SceneBar" src="https://github.com/user-attachments/assets/6e3ccd11-4505-41e9-a852-67001255290b" />

SceneBar is a small AI bar that sits at the top of the Linux desktop. Press F4, type what you need, and it sets up the screen for it, e.g. a few hotels side by side with their maps, ready to flip through like cards, or a Blender window with the next button to press circled. SceneBar works alongside the person at the computer rather than in their place.

[![SceneBar demo video](https://img.youtube.com/vi/C1G2WH4QIcI/maxresdefault.jpg)](https://youtu.be/C1G2WH4QIcI)

▶ [Watch the demo video](https://youtu.be/C1G2WH4QIcI)

Runs on Arch Linux + XFCE (X11).

## The problem

Two slow parts of computer work: comparing options means juggling a dozen tabs and windows, and unfamiliar software means switching back and forth between a tutorial and the app.

Existing computer-use agents (codex, hermes, etc) take over the mouse and keyboard, which is slow, hard to trust, and teaches the user nothing.

## Dependencies

Linux with an X11 session (developed on Arch Linux + XFCE).

System packages (Arch names):

    sudo pacman -S gtk3 python-gobject python-cairo at-spi2-core \
        wmctrl xdotool xorg-xprop xorg-xwininfo xclip chromium xfce4-terminal

Optional, for their adapters: `blender`, `libreoffice-fresh` (Impress, via its Python `uno`), `evince`.

Python packages (`requirements.txt`), in a venv that can see the system's GTK bindings:

    python -m venv --system-site-packages ~/.venvs/scenebar
    ~/.venvs/scenebar/bin/pip install -r requirements.txt

Models: a Gemini API key (`/key gemini KEY` in the bar), and optionally an OpenAI-compatible
server with Holo3.1 for finding elements on screen (default `http://10.0.2.2:8000/v1`, set
`grounding.url` in `~/.config/dscene/config.json`).


## How it works

SceneBar has firm boundaries: it never clicks, types or runs commands on the user's behalf. It arranges windows, lays out options, marks where to click, and changes apps only through their own interfaces. Running a command or making a booking always stays the user's decision.

  - Gemini is the brain. It plans and calls tools over MCP.
  - Underneath is `dscene`, an MCP server with 20 tools that describes, shows, saves and restores "scenes": which windows are on screen, where, and each app's state (Chromium pages via CDP, Blender via a bridge inside Blender, LibreOffice Impress via UNO, terminals via the accessibility tree).
  - Holo3.1, a local vision model, finds buttons on screen in apps like Blender that offer no other way to locate them.
  - `dscene` can also run without the bar, so other agents (Claude Code, Codex, Hermes) can use the same scenes and the same "show, don't take over" approach.


## What's next
  - Use the accessibility tree for exact on-screen marks in GTK, LibreOffice and Chromium, keeping vision for apps without one.
  - Support more desktops: Wayland, GNOME/KDE, then macOS.
  - Package dscene as a standalone MCP server other agents can install.
  - Share scenes: send someone a whole working setup that opens on their screen.


