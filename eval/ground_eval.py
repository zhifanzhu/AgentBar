#!/usr/bin/env python3
"""Score a Holo server on clicking hand-labelled UI targets in a screenshot.

Usage: ground_eval.py [targets.json] [--url http://127.0.0.1:8000/v1] [--model holo3-1-9b]
A target counts as a hit when the predicted click point lands inside its box.
Writes <image>-result.png with hits (green) and misses (red) drawn on it.
"""
import argparse
import base64
import json
import subprocess
import time
import urllib.request
from pathlib import Path

SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "X coordinate as integer in [0, 1000]"},
        "y": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "Y coordinate as integer in [0, 1000]"},
    },
    "required": ["x", "y"],
}


def image_size(path):
    out = subprocess.run(["identify", "-format", "%w %h", str(path)],
                         capture_output=True, text=True, check=True).stdout
    w, h = out.split()
    return int(w), int(h)


def localize(url, model, data_uri, desc):
    # Prompt and settings follow H Company's element-localization recipe.
    prompt = ("Localize an element on the GUI image according to the provided target "
              "and output a click position.\n"
              f" * You must output a valid JSON following the format: {json.dumps(SCHEMA)}\n"
              f" Your target is:\n{desc}")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt},
        ]}],
        "structured_outputs": {"json": SCHEMA},
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0.0,
        "max_tokens": 64,
    }
    req = urllib.request.Request(f"{url}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    return json.loads(resp["choices"][0]["message"]["content"]), resp["usage"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="?", default=Path(__file__).parent / "blender_targets.json")
    ap.add_argument("--url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--model", default="holo3-1-9b")
    args = ap.parse_args()

    spec_path = Path(args.targets)
    spec = json.loads(spec_path.read_text())
    image = spec_path.parent / spec["image"]
    w, h = image_size(image)
    data_uri = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()

    results = []
    for t in spec["targets"]:
        t0 = time.perf_counter()
        point, usage = localize(args.url, args.model, data_uri, t["desc"])
        ms = (time.perf_counter() - t0) * 1000
        px, py = round(point["x"] / 1000 * w), round(point["y"] / 1000 * h)
        x0, y0, x1, y1 = t["box"]
        hit = x0 <= px <= x1 and y0 <= py <= y1
        dist = max(x0 - px, 0, px - x1) + max(y0 - py, 0, py - y1)  # px outside the box
        results.append({**t, "point": [px, py], "hit": hit, "off_by": dist, "ms": round(ms),
                        "prompt_tokens": usage["prompt_tokens"]})
        print(f"{'HIT ' if hit else 'MISS'} {t['kind']:4s} {t['name']:18s} -> ({px:4d},{py:3d})"
              f"{'' if hit else f'  off by {dist}px'}  {ms:5.0f} ms")

    for kind in ("text", "icon", None):
        rs = [r for r in results if kind is None or r["kind"] == kind]
        print(f"{kind or 'all':4s}: {sum(r['hit'] for r in rs)}/{len(rs)} hits")
    lat = sorted(r["ms"] for r in results)
    print(f"latency: median {lat[len(lat) // 2]} ms, max {lat[-1]} ms; "
          f"prompt tokens per call: {results[0]['prompt_tokens']}")

    draws = []
    for r in results:
        colour = "#22c55e" if r["hit"] else "#ef4444"
        x0, y0, x1, y1 = r["box"]
        px, py = r["point"]
        draws += ["-stroke", colour, "-fill", "none", "-draw", f"rectangle {x0},{y0} {x1},{y1}",
                  "-fill", colour, "-draw", f"circle {px},{py} {px + 4},{py}"]
    out = image.with_name(image.stem + "-result.png")
    subprocess.run(["convert", str(image), "-strokewidth", "2", *draws, str(out)], check=True)
    (spec_path.parent / (image.stem + "-result.json")).write_text(json.dumps(results, indent=1))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
