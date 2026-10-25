"""DScene spec v1: a declarative description of what the desktop should show.

A scene is a layout of panes plus an optional caption; a deck is the list of
scenes from one request (the cards in the bar). The same JSON is what agents
send through the protocol and what gets saved to disk:

    {"dscene": 1, "name": "The Savoy", "note": "Strand · from £420",
     "layout": "split-2",
     "panes": [{"kind": "url", "url": "https://www.thesavoylondon.com/", "scroll_y": 900},
               {"kind": "map", "lat": 51.5104, "lon": -0.1206, "label": "The Savoy"}],
     "caption": "5-star, riverside"}

Browser panes (url, map, search) live in the bar's own Chromium windows. App panes
(blender, impress, window) are the user's own app windows. Every pane is a state
descriptor: the fields needed to bring that window back. `fidelity` (set when a
scene is captured) says how much comes back: exact, view, or layout only.
"""
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

VERSION = 1
LAYOUTS = {"full": 1, "split-2": 2, "split-3": 3}  # plus "free": every pane brings its own rect
BROWSER_KINDS = ("url", "map", "route", "search")
APP_KINDS = ("blender", "impress", "window")
DECKS = Path.home() / ".local" / "share" / "dscene" / "decks"
BLOBS = Path.home() / ".local" / "share" / "dscene" / "blobs"  # .blend / .odp checkpoints


class Pane(BaseModel):
    """One window in a scene, with the state to show in it."""
    kind: Literal["url", "map", "route", "search", "blender", "impress", "window"] = Field(
        description="url: a web page; map: Google Maps showing `label` (its place card) around lat/lon; "
                    "route: Google Maps directions from `origin` to `destination` by `travel`; "
                    "search: a web search for `query`; blender / impress: the user's Blender or "
                    "LibreOffice Impress window, optionally restored to a captured state; window: "
                    "any other app window, matched by `app`, placed but not restored")
    # browser panes
    url: str | None = Field(None, description="url: a real URL, e.g. from search results")
    scroll_y: int | None = Field(None, description="url: vertical scroll position in pixels")
    lat: float | None = Field(None, description="map: latitude")
    lon: float | None = Field(None, description="map: longitude")
    label: str | None = Field(None, description="map: the place's name")
    origin: str | None = Field(None, description="route: where from, a place name or address "
                                                 "(e.g. \"Queen Square, Bristol\") or \"lat,lon\"")
    destination: str | None = Field(None, description="route: where to, same forms as origin")
    travel: Literal["transit", "walking", "driving", "bicycling"] | None = Field(
        None, description="route: how; default transit (buses and trains, with times)")
    query: str | None = Field(None, description="search: what to search for")
    engine: Literal["google", "duckduckgo", "bing", "chatgpt", "perplexity"] | None = Field(
        None, description="search: where to search; defaults to the user's chosen answer engine")
    # blender
    blend: str | None = Field(None, description="blender: a .blend checkpoint to open (from "
                                                "capture_scene); omit to keep the current file")
    selected: list[str] | None = Field(None, description="blender: names of objects to select")
    active: str | None = Field(None, description="blender: name of the active object")
    mode: str | None = Field(None, description="blender: OBJECT, EDIT, SCULPT, ...")
    view: list[float] | None = Field(None, description="blender: 3D view as [loc x, y, z, rotation "
                                                       "w, x, y, z, distance], from capture_scene")
    properties_tab: str | None = Field(None, description="blender: properties tab, e.g. MODIFIER")
    # impress
    doc: str | None = Field(None, description="impress: path of the presentation to show")
    slide: int | None = Field(None, description="impress: slide number, 1-based")
    # any app window
    app: str | None = Field(None, description="window: app name or title text to match")
    title: str | None = Field(None, description="The window title when captured (informational)")
    # placement
    rect: list[int] | None = Field(None, description="[x, y, width, height]; required in layout free")
    maximized: bool | None = Field(None, description="Show the window maximized (overrides rect)")
    fidelity: Literal["exact", "view", "layout"] | None = Field(
        None, description="Set by capture_scene: how much of this window's state can come back")

    def resolve(self):
        """The URL a browser pane shows."""
        if self.kind == "url" and self.url:
            return self.url
        if self.kind == "map":  # Google Maps: a name gets its place card (photos, rating, hours)
            here = self.lat is not None and self.lon is not None
            if self.label and here:  # the name, searched around the point: the right "Box Hill"
                return (f"https://www.google.com/maps/search/{urllib.parse.quote(self.label)}"
                        f"/@{self.lat},{self.lon},15z?hl=en")
            return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(
                {"api": 1, "query": f"{self.lat},{self.lon}" if here else self.label or "", "hl": "en"})
        if self.kind == "route":  # Google's documented directions link; no key needed
            return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(
                {"api": 1, "origin": self.origin or "", "destination": self.destination or self.label or "",
                 "travelmode": self.travel or "transit", "hl": "en"})
        return search_url(self.query or self.label or self.url or "", self.engine)


class SceneSpec(BaseModel):
    """One desktop arrangement: what each visible window shows, and where."""
    dscene: int = Field(VERSION, description="Spec version")
    name: str = Field(description="Card title, e.g. the option's name")
    note: str = Field("", description="Key facts for the card, under 60 characters")
    layout: Literal["full", "split-2", "split-3", "free"] | None = Field(
        None, description="Pane arrangement, left to right; free = every pane gives its own rect. "
                          "Defaults to one matching the panes")
    panes: list[Pane] = Field(description="1-3 panes for full/split layouts, up to 8 for free")
    caption: str = Field("", description="Optional text drawn over the last pane")

    @model_validator(mode="after")
    def _check_layout(self):
        if self.layout is None:
            self.layout = ("free" if all(p.rect for p in self.panes) and self.panes
                           else {1: "full", 2: "split-2", 3: "split-3"}.get(len(self.panes)))
        if self.layout == "free":
            if not 1 <= len(self.panes) <= 8 or not all(p.rect and len(p.rect) == 4 for p in self.panes):
                raise ValueError("layout free needs 1-8 panes, each with rect [x, y, w, h]")
        elif self.layout is None or LAYOUTS[self.layout] != len(self.panes):
            raise ValueError(f"layout {self.layout} doesn't fit {len(self.panes)} panes "
                             "(full: 1, split-2: 2, split-3: 3, free: each pane has a rect)")
        return self

    def browser_panes(self):
        return [p for p in self.panes if p.kind in BROWSER_KINDS]

    def urls(self):
        return [p.resolve() for p in self.browser_panes()]


ENGINES = {"google": "https://www.google.com/search?hl=en&q=", "duckduckgo": "https://duckduckgo.com/?kl=uk-en&q=",
           "bing": "https://www.bing.com/search?q=", "chatgpt": "https://chatgpt.com/?q=",
           "perplexity": "https://www.perplexity.ai/search?q="}


def search_url(text, engine=None):
    """A search (or ask) URL for text, on engine or the user's configured answer engine."""
    if engine is None:
        from dscene import config
        engine = config.load().get("answer_engine", "duckduckgo")
    # site: takes a domain only; "site:lu.ma/encode-club x" matches nothing, so keep the
    # path as plain words: "site:lu.ma encode-club x".
    text = re.sub(r"\bsite:([^\s/]+)/(\S*)", lambda m: f"site:{m[1]} {m[2].replace('/', ' ')}", text)
    return ENGINES.get(engine, ENGINES["google"]) + urllib.parse.quote(text.strip())


def layout_rects(scene, area):
    """Where each pane goes: its own rect (free layout) or equal side-by-side splits of area."""
    if scene.layout == "free":
        return [tuple(p.rect) for p in scene.panes]
    n = LAYOUTS[scene.layout]
    x, y, w, h = area
    cuts = [x + w * i // n for i in range(n + 1)]
    return [(cuts[i], y, cuts[i + 1] - cuts[i], h) for i in range(n)]


REDIRECTORS = {"vertexaisearch.cloud.google.com"}  # Gemini's Google Search links: a hop to the real page


def _check_link(url):
    """(dead, where it really goes). Dead only for links that clearly don't exist (sites that
    block bots still count as alive)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return False, r.geturl()
    except urllib.error.HTTPError as e:
        return e.code in (404, 410), e.geturl() or url
    except (urllib.error.URLError, ValueError, TimeoutError):
        return True, url


def _top_results(query):
    """The first web results for query (DuckDuckGo Lite: plain HTML, no consent page)."""
    req = urllib.request.Request("https://lite.duckduckgo.com/lite/", headers={"User-Agent": "Mozilla/5.0"},
                                 data=urllib.parse.urlencode({"q": query, "kl": "uk-en"}).encode())
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            page = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ValueError, TimeoutError):
        return []
    return [html.unescape(u) for u in re.findall(r"href=\"(https?://[^\"]+)\"[^>]*class=.result-link", page)]


def _live_link(url, name):
    """The link to open: url itself (redirect links followed to the real page), else, when it
    doesn't exist (often a link the model half-remembered), the first live web result for the
    scene's name; None when there's neither."""
    dead, final = _check_link(url)
    if not dead:
        return final if urllib.parse.urlsplit(url).netloc in REDIRECTORS else url
    return next((alt for alt in _top_results(name)[:3] if not _check_link(alt)[0]), None)


def with_live_links(scenes):
    """Swap made-up page links (404 / no such host) for the top web result for the scene (or a
    web search), and redirect links for the page they lead to (the card shows the real link;
    /close knows the tab's site). Returns (scenes, names of scenes left showing a search)."""
    checks = [(i, j, p.url) for i, s in enumerate(scenes) for j, p in enumerate(s.panes)
              if p.kind == "url" and p.url]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda c: _live_link(c[2], scenes[c[0]].name), checks))
    scenes = [s.model_copy(deep=True) for s in scenes]
    swapped = []
    for (i, j, _), live in zip(checks, results):
        if live:
            scenes[i].panes[j].url = live
        else:
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
