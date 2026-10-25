#!/usr/bin/env bash
# Run on vast: (re)start SceneBar inside the Arch VM's XFCE session.
# It uses the VM's ~/.venvs/scenebar (system site packages + the MCP SDK).
ssh archvm 'pkill -f "python scenebar.py$"; cd ~/proj/scenebar &&
  DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
  setsid -f ~/.venvs/scenebar/bin/python scenebar.py >/tmp/scenebar.log 2>&1 </dev/null'
