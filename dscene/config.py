"""dscene's own settings, in ~/.config/dscene/config.json (no dependency on any agent).

  grounding      who finds a described UI element on screen (find_element, guide_step):
                 holo = an OpenAI-compatible server running Holo3.1; gemini = a Gemini call
  answer_engine  where search panes open: duckduckgo, google, bing, chatgpt, perplexity
  gemini_*       only for grounding provider gemini; GEMINI_API_KEY overrides the key
"""
import json
import os
from pathlib import Path

CONFIG = Path.home() / ".config" / "dscene" / "config.json"
DEFAULTS = {
    "grounding": {"provider": "holo", "url": "http://10.0.2.2:8000/v1", "model": "holo3-1-9b"},
    "answer_engine": "duckduckgo",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.6-flash",
    "thinking_level": "minimal",
}


def exists():
    return CONFIG.exists()


def load():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if CONFIG.exists():
        for key, value in json.loads(CONFIG.read_text()).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def save(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    CONFIG.chmod(0o600)  # it may hold an API key


def update(**changes):
    """Change top-level settings (a dict value is merged into the existing one) and save."""
    cfg = load()
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    save(cfg)
    return cfg


def gemini_key(cfg=None):
    return os.environ.get("GEMINI_API_KEY") or (cfg or load()).get("gemini_api_key", "")
