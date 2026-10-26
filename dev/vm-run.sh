#!/usr/bin/env bash
# Run on vast: (re)start SceneBar inside the Arch VM's XFCE session (bin/scenebar picks the Python).
ssh archvm 'pkill -f "scenebar\.py$"; DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
  setsid -f ~/proj/scenebar/bin/scenebar >/tmp/scenebar.log 2>&1 </dev/null'
