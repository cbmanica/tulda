"""The interactive viewer page, /_v/<project>/<name>/ (read-only, one static HTML shell).

The page embeds one JSON blob (model/frame URLs, sidecar info) and loads two same-origin
scripts from /_s/: viewer.js (three.js, loaded lazily) and turntable.js (no-WebGL fallback)."""

from __future__ import annotations

import json
import re
import urllib.parse
from html import escape

from .items import STATIC_PREFIX, render_url

THREE_VERSION = "0.187.0"
VENDOR = f"/{STATIC_PREFIX}/vendor/three-{THREE_VERSION}/"

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__ - tulda</title>
<script type="importmap">__IMPORTMAP__</script>
<style>
:root{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--card:#f2f2f5;--acc:#2e8b57;--line:#d8d8de;--stage:#eceef2;--panel:#fffd}
:root[data-theme=dark]{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--card:#1d1d21;--acc:#4fc58a;--line:#33333a;--stage:#17171a;--panel:#1d1d21ee}
[hidden]{display:none!important}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;align-items:center;gap:4px 14px;padding:8px 14px;border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;overflow-wrap:anywhere;flex:1 1 200px}
header a{color:var(--acc);font-size:14px}
.sub{color:var(--mut);font-size:12px;flex-basis:100%}
#stage{position:relative;flex:1;min-height:240px;background:var(--stage);overflow:hidden;touch-action:none}
#cv{position:absolute;inset:0;width:100%;height:100%;display:block;outline:none}
#msg{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);max-width:min(90%,420px);text-align:center;color:var(--mut);
background:var(--panel);padding:10px 16px;border-radius:10px;z-index:3}
#msg.bad{color:var(--fg);border:1px solid var(--line)}
.tools{position:absolute;left:8px;right:8px;bottom:calc(8px + env(safe-area-inset-bottom));display:flex;flex-wrap:wrap;gap:6px;z-index:2;pointer-events:none}
.tools>*{pointer-events:auto}
.grp{display:flex;flex-wrap:wrap;gap:4px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:4px}
.b{min-height:36px;min-width:36px;padding:4px 11px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--fg);
font:inherit;font-size:13px;cursor:pointer}
.b[aria-pressed=true]{background:var(--acc);border-color:var(--acc);color:#fff}
.b:focus-visible,summary:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#parts{position:absolute;top:8px;right:8px;max-width:min(260px,70vw);max-height:60%;overflow:auto;background:var(--panel);
border:1px solid var(--line);border-radius:12px;padding:6px 10px;z-index:2;font-size:13px}
#parts summary{cursor:pointer;color:var(--mut);padding:4px 0}
#parts label{display:flex;align-items:center;gap:7px;padding:4px 0;min-height:30px;overflow-wrap:anywhere}
#parts .sw{width:14px;height:14px;border-radius:3px;border:1px solid #0005;flex:none}
#parts .o{color:var(--mut);font-size:11px;margin-top:6px;overflow-wrap:anywhere}
#tt{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--stage);z-index:4;padding:8px}
#tt img{max-width:100%;max-height:calc(100% - 90px);object-fit:contain;touch-action:pan-y;user-select:none;-webkit-user-drag:none;cursor:grab;background:#fff}
#tt img:active{cursor:grabbing}
#tt .bar{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;justify-content:center;color:var(--mut);font-size:13px}
#tt .note{margin-top:6px;font-size:12px;color:var(--mut);max-width:90%;text-align:center}
#tt input[type=range]{width:min(60vw,320px)}
</style></head><body>
<header><h1>__HEADING__</h1>__LINKS__<div class="sub">__SUB__</div></header>
<div id="stage">
<canvas id="cv" tabindex="0" aria-label="3D model viewer. Drag to rotate, scroll or pinch to zoom, right-drag or two fingers to pan."></canvas>
<div id="msg" role="status" __MSGHIDE__>__NOJS__</div>
<details id="parts" hidden open><summary>Parts</summary><div id="plist"></div></details>
<div class="tools" id="ui" hidden>
<div class="grp" role="group" aria-label="Camera"><button class="b" id="reset" type="button">Reset view</button>
<button class="b" data-preset="iso" type="button">Iso</button><button class="b" data-preset="top" type="button">Top</button>
<button class="b" data-preset="bottom" type="button">Bottom</button><button class="b" data-preset="front" type="button">Front</button>
<button class="b" data-preset="back" type="button">Back</button><button class="b" data-preset="left" type="button">Left</button>
<button class="b" data-preset="right" type="button">Right</button></div>
<div class="grp" role="group" aria-label="Display"><button class="b" id="wire" type="button" aria-pressed="false">Wireframe</button>
<button class="b" id="grid" type="button" aria-pressed="true">Grid</button><button class="b" id="theme" type="button">Dark background</button>
<button class="b" id="full" type="button">Full screen</button></div>
</div>
<div id="tt" hidden></div>
</div>
<script type="application/json" id="vdata">__DATA__</script>
<script src="/_s/turntable.js"></script>
<script type="module" src="/_s/viewer.js"></script>
</body></html>
"""


def _json(obj) -> str:
    return (json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def viewer_data(r: dict) -> dict:
    """The JSON the page script reads, from a catalog render dict (Catalog.get_render)."""
    info = r.get("info") or {}
    q = urllib.parse.quote
    d = {"kind": r["kind"], "project": r["project"], "file": r["file"], "vendor": VENDOR,
         "info": {k: v for k, v in info.items() if k in
                  ("triangles", "bbox", "parts", "bed", "warnings", "frames", "format", "frames_dir")},
         "glb": None, "turntable": None, "source": r.get("source")}
    if r["kind"] == "mesh":
        d["glb"] = r["url"]
    tt = r if r["kind"] == "turntable" else r.get("turntable")
    if tt:
        tinfo = tt.get("info") or {}
        n = tinfo.get("frames")
        fdir = tinfo.get("frames_dir")
        if isinstance(n, int) and 2 <= n <= 720 and isinstance(fdir, str):
            d["turntable"] = {"frames": n, "poster": tt["url"],
                              "base": f"/{q(tt['project'])}/{q(fdir)}/", "pattern": "f{i}.png",
                              "digits": 3, "view_url": tt.get("viewer_url")}
    return d


def render_viewer(r: dict) -> str:
    data = viewer_data(r)
    title = r.get("source") or r["file"]
    heading = escape(title)
    gt = r.get("group_title")
    sub_bits = [r["file"]]
    inf = r.get("info") or {}
    if inf.get("triangles"):
        sub_bits.append(f"{inf['triangles']:,} triangles")
    if inf.get("frames"):
        sub_bits.append(f"{inf['frames']} frames")
    for w in (inf.get("warnings") or [])[:3]:
        if isinstance(w, str):
            sub_bits.append("warning: " + w)
    links = [f'<a href="/_g/{urllib.parse.quote(r["group_id"])}/">&larr; request{" " + escape(gt) if gt else ""}</a>']
    if r["kind"] == "mesh":
        links.append(f'<a href="{escape(r["url"])}" download>Download .glb</a>')
    im = {"imports": {"three": VENDOR + "build/three.module.js"}}
    subs = {"__IMPORTMAP__": _json(im), "__TITLE__": escape(title), "__HEADING__": heading,
            "__LINKS__": "".join(links), "__SUB__": escape("  |  ".join(sub_bits)),
            "__MSGHIDE__": "", "__NOJS__": "This page needs JavaScript.", "__DATA__": _json(data)}
    return re.sub(r"__[A-Z]+__", lambda m: subs.get(m.group(0), m.group(0)), PAGE)
