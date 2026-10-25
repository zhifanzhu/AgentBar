
# <img width="443" height="67" alt="SceneBar" src="https://github.com/user-attachments/assets/6e3ccd11-4505-41e9-a852-67001255290b" />

Demo video: https://www.youtube.com/watch?v=qjR1piz74VA

## Dependencies

Linux with an X11 session (developed on Arch Linux + XFCE).

System packages (Arch names):

    sudo pacman -S gtk3 python-gobject python-cairo at-spi2-core \
        wmctrl xdotool xorg-xprop xorg-xwininfo xclip chromium xfce4-terminal

Optional, for their adapters: `blender`, `libreoffice-fresh` (Impress, via its Python `uno`), `evince`.

Python packages (`requirements.txt`), in a venv that can see the system's GTK bindings:

    python -m venv --system-site-packages ~/.venvs/agentbar
    ~/.venvs/agentbar/bin/pip install -r requirements.txt

Models: a Gemini API key (`/key gemini KEY` in the bar), and optionally an OpenAI-compatible
server with Holo3.1 for finding elements on screen (default `http://10.0.2.2:8000/v1`, set
`grounding.url` in `~/.config/agentbar/brain.json`).
