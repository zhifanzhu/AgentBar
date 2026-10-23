#!/usr/bin/env bash
# Run on vast: (re)start the agent bar inside the Arch VM's XFCE session.
# It uses the VM's ~/.venvs/agentbar (system site packages + the MCP SDK).
ssh archvm 'pkill -f "python agentbar.py$"; cd ~/proj/agentbar &&
  DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
  setsid -f ~/.venvs/agentbar/bin/python agentbar.py >/tmp/agentbar.log 2>&1 </dev/null'
