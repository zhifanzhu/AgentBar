"""DScene spec v1: a declarative description of what the desktop should show.

A scene is a layout of panes plus an optional caption; a deck is the list of
scenes from one request (the cards in the bar). The same JSON is what agents
send through the protocol and what gets saved to disk:

    {"dscene": 1, "name": "The Savoy", "note": "Strand · from £420",
     "layout": "split-2",
     "panes": [{"kind": "url", "url": "https://www.thesavoylondon.com/"},
               {"kind": "map", "lat": 51.5104, "lon": -0.1206, "label": "The Savoy"}],
     "caption": "5-star, riverside"}
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

VERSION = 1
LAYOUTS = {"full": 1, "split-2": 2, "split-3": 3}
DECKS = Path.home() / ".local" / "share" / "dscene" / "decks"


class Pane(BaseModel):
    """One browser pane. kind=url shows a page, map shows OpenStreetMap, search a web search."""
    kind: Literal["url", "map", "search"] = Field(
        description="url: the page at `url`; map: OpenStreetMap at lat/lon (or a search for "
                    "`label`); search: a web search for `query`")
    url: str | None = Field(None, description="For kind=url: a real URL, e.g. from search results")
    lat: float | None = Field(None, description="For kind=map: latitude")
    lon: float | None = Field(None, description="For kind=map: longitude")
    label: str | None = Field(None, description="For kind=map: the place's name")
    query: str | None = Field(None, description="For kind=search: what to search for")

    def resolve(self):
        if self.kind == "url" and self.url:
            return self.url
        if self.kind == "map":
            if self.lat is not None and self.lon is not None:
                return (f"https://www.openstreetmap.org/?mlat={self.lat}&mlon={self.lon}"
                        f"#map=17/{self.lat}/{self.lon}")
            return "https://www.openstreetmap.org/search?query=" + urllib.parse.quote(self.label or "")
        return search_url(self.query or self.label or self.url or "")


class SceneSpec(BaseModel):
    """One desktop arrangement the user can flip to from the bar."""
    dscene: int = Field(VERSION, description="Spec version")
    name: str = Field(description="Card title, e.g. the option's name")
    note: str = Field("", description="Key facts for the card, under 60 characters")
    layout: Literal["full", "split-2", "split-3"] | None = Field(
        None, description="Pane arrangement; defaults to one matching the number of panes")
    panes: list[Pane] = Field(description="1-3 panes, left to right")
    caption: str = Field("", description="Optional text drawn over the last pane")

    @model_validator(mode="after")
    def _check_layout(self):
        if not 1 <= len(self.panes) <= 3:
            raise ValueError("a scene has 1 to 3 panes")
        default = {1: "full", 2: "split-2", 3: "split-3"}[len(self.panes)]
        if self.layout is None:
            self.layout = default
        elif LAYOUTS[self.layout] != len(self.panes):
            raise ValueError(f"layout {self.layout} needs {LAYOUTS[self.layout]} panes, got {len(self.panes)}")
        return self

    def urls(self):
        return [p.resolve() for p in self.panes]


def search_url(text):
    return "https://duckduckgo.com/?q=" + urllib.parse.quote(text)


def layout_rects(layout, area):
    """Split area = (x, y, w, h) into equal side-by-side rects for the layout."""
    n = LAYOUTS[layout]
    x, y, w, h = area
    cuts = [x + w * i // n for i in range(n + 1)]
    return [(cuts[i], y, cuts[i + 1] - cuts[i], h) for i in range(n)]


def _link_is_dead(url):
    """True only for links that clearly don't exist (sites that block bots still count as alive)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=6):
            return False
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except (urllib.error.URLError, ValueError, TimeoutError):
        return True


def with_live_links(scenes):
    """Swap made-up page links (404 / no such host) for a web search. Returns (scenes, swapped names)."""
    checks = [(i, j, p.url) for i, s in enumerate(scenes) for j, p in enumerate(s.panes)
              if p.kind == "url" and p.url]
    with ThreadPoolExecutor(8) as pool:
        dead = list(pool.map(lambda c: _link_is_dead(c[2]), checks))
    scenes = [s.model_copy(deep=True) for s in scenes]
    swapped = []
    for (i, j, _), is_dead in zip(checks, dead):
        if is_dead:
            scenes[i].panes[j] = Pane(kind="search", query=scenes[i].name)
            scenes[i].note = (scenes[i].note + " · link not found, showing search").lstrip(" ·")
            swapped.append(scenes[i].name)
    return scenes, swapped


def _deck_path(name):
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "deck"
    return DECKS / f"{safe}.json"


def save_deck(name, scenes):
    DECKS.mkdir(parents=True, exist_ok=True)
    path = _deck_path(name)
    path.write_text(json.dumps({"dscene": VERSION, "name": name,
                                "scenes": [s.model_dump(exclude_none=True) for s in scenes]}, indent=1))
    return path


def load_deck(name_or_path):
    path = Path(name_or_path)
    if not path.exists():
        path = _deck_path(str(name_or_path))
    data = json.loads(path.read_text())
    return data.get("name", path.stem), [SceneSpec.model_validate(s) for s in data["scenes"]]


def list_decks():
    return sorted(p.stem for p in DECKS.glob("*.json")) if DECKS.exists() else []
