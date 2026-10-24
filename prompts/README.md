# Prompt layers

What the agent bar's model is told, assembled for every request (`brain.system_prompt`):

1. `SYSTEM.md`: how the agent bar works and its rules. Versioned with the code.
2. `ABOUT.md` plus the current settings (brain model, grounding, answer engine): what agentBar
   is, so it can answer questions about itself without searching the web.
3. `~/.config/agentbar/USER.md`: the user's own notes about themselves and their preferences;
   `/remember TEXT` in the bar appends a line. Can't loosen the boundaries: those (no clicking,
   no running commands) are enforced in code.
4. `apps/<name>.md`: tips for one app, sent only when a window of that app is open or the
   request names it (e.g. `blender.md` when Blender is on screen), so other calls stay small.

Files are read on every request, so edits apply without restarting the bar. All of it is sent
to the model on every call: keep it short, and keep secrets out.
