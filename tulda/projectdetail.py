"""/_p/<project>/ and /api/projects/<project>: one project's files, and for each .3mf its plates
(with each plate's render and Bambu Studio estimate), filaments, process settings, overrides,
colour changes and post-processing scripts. Read-only: nothing is rendered or sliced on a GET.

Plate renders and estimates come from the catalog, matched by the 3mf's sha256 (so renders made
under any renders-root project or group name count, and renders of an older version of the file
do not): a plate's picture is its newest image render, its 3D view its newest per-plate mesh
(else a whole-build mesh), and its estimate is the one stored in the 3mf's
`Metadata/slice_info.config` when the file was saved after slicing, else the newest `slice` render
of that plate (Bambu Studio CLI, run by `tulda render FILE --plates all`). Without either the page
says how to get one.

Names come from file and folder names and from the 3mf itself and are untrusted: every value is
escaped, colours are used only after matching #RRGGBB, and paths are shown relative to the
project folder (the folder itself as `<destination name>/<folder>`). The only script is
timefmt.LOCAL_JS (allowed by hash)."""

from __future__ import annotations

import shlex
import urllib.parse
from html import escape
from pathlib import Path

from . import projectfiles as pf
from .items import GROUP_PREFIX, PROJECTS_PREFIX
from .slicesummary import fmt_duration
from .tabs import CSS as TAB_CSS, tabs_html
from .timefmt import LOCAL_SCRIPT, time_html, utc_from_ts


def _e(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _ref(r: dict | None) -> dict | None:
    """The public part of a catalog render (no paths): URLs, group, time."""
    if not r:
        return None
    return {"url": r["url"], "viewer_url": r.get("viewer_url"), "view": r.get("view"),
            "kind": r.get("kind"), "plate": r.get("plate"), "group_id": r["group_id"],
            "group_url": f"/{GROUP_PREFIX}/{_q(r['group_id'])}/",
            "created": r.get("created_utc") or utc_from_ts(r["mtime"])}


def _pick(renders: list[dict], kind: str, plate) -> dict | None:
    """Newest render of this kind for the plate (renders are newest first); images prefer iso."""
    hits = [r for r in renders if r.get("kind") == kind and r.get("plate") == plate]
    if kind == "image":
        hits.sort(key=lambda r: r.get("view") != "iso")      # stable: newest iso first
    return hits[0] if hits else None


def _slice_estimate(info: dict | None) -> dict | None:
    if not isinstance(info, dict) or info.get("format") != "slice-summary":
        return None
    fils = []
    for f in info.get("filaments") or []:
        if isinstance(f, dict):
            fils.append({"id": pf._pos_int(f.get("id")), "type": pf.clean(f.get("type"), 40),
                         "colour": pf.colour(f.get("color")), "used_g": pf._num(f.get("used_g")),
                         "used_m": pf._num(f.get("used_m"))})
    return {"prediction_s": pf._num(info.get("print_time_s")), "weight_g": pf._num(info.get("total_g")),
            "filaments": fils}


def plate_views(d: dict, renders: list[dict]) -> list[dict]:
    """The plates of one 3mf's details with their renders and estimate attached."""
    out = []
    whole = _pick(renders, "mesh", None)
    for pl in d["plates"]:
        n = pl["n"]
        img = _pick(renders, "image", n) or _pick(renders, "turntable", n)
        mesh = _pick(renders, "mesh", n)
        sl = _pick(renders, "slice", n)
        est = None
        if pl.get("stored"):
            est = {"source": "file", **pl["stored"]}
        elif sl and _slice_estimate(sl.get("info")):
            est = {"source": "slice", **_slice_estimate(sl["info"]), "created": _ref(sl)["created"]}
        out.append({**{k: v for k, v in pl.items() if k != "stored"},
                    "image": _ref(img), "viewer": _ref(mesh or whole),
                    "viewer_whole_build": mesh is None and whole is not None and len(d["plates"]) > 1,
                    "slice": _ref(sl), "estimate": est})
    return out


def build(name: str, src: dict | None, catalog, in_catalog: bool) -> dict:
    """The page data (JSON-safe, no absolute paths). src: projectspage source entry (with its
    private `path`) or None for a renders-only project."""
    from .webrender import file_facts
    data = {"name": name, "source": (src or {}).get("source"), "created": (src or {}).get("created"),
            "gallery_url": f"/{_q(name)}/" if in_catalog else None, "files": [], "truncated": False,
            "threemf": [], "threemf_skipped": 0}
    folder: Path | None = (src or {}).get("path")
    if folder is None:
        return data
    files, data["truncated"] = pf.list_files(folder)
    data["files"] = [{"name": f["rel"], "type": f["type"], "size": f["size"],
                      "modified": utc_from_ts(f["mtime"])} for f in files]
    tmfs = [f for f in files if f["type"] == "3mf"]
    data["threemf_skipped"] = max(0, len(tmfs) - pf.MAX_3MF)
    paths = []
    for f in tmfs[:pf.MAX_3MF]:
        p = pf.safe_member_path(folder, f["rel"])
        if p is not None:
            paths.append((f, p, file_facts(p).get("sha256")))
    try:
        by_sha = catalog.renders_by_source_sha([s for _, _, s in paths if s])
    except Exception:  # noqa: BLE001 - a catalog problem only hides the renders
        by_sha = {}
    for f, p, sha in paths:
        d = pf.details(p)
        renders = by_sha.get(sha or "", [])
        data["threemf"].append({
            "name": f["rel"], "size": f["size"], "modified": utc_from_ts(f["mtime"]),
            "sha256": sha, "error": d["error"], "application": d["application"],
            "bambu": d["bambu"], "post_process": d["post_process"],
            "plates": plate_views(d, renders), "filaments": d["filaments"],
            "settings": d["settings"], "changed": d["changed"], "overrides": d["overrides"],
            "changes": d["changes"], "variable_layers": d["variable_layers"],
            "render_cmd": shlex.join(["tulda", "render", f["name"], "--plates", "all"])})
    return data


# ---- HTML ----------------------------------------------------------------------------------

def _size(n) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _sw(c) -> str:
    c = pf.colour(c)
    return f'<span class=sw style="background:{c}" title="{c}"></span>' if c else '<span class="sw none"></span>'


def _fil_label(n, fils: list[dict]) -> str:
    f = next((x for x in fils if x["n"] == n), None)
    if not f:
        return f"<span class=fl>{_sw(None)}{_e(n)}</span>"
    return f"<span class=fl>{_sw(f['colour'])}{_e(n)} {_e(f['type'] or '')}</span>"


def _g(v) -> str:
    return "" if v is None else f"{v:g}"


def _estimate_html(est: dict | None, t: dict, fils: list[dict]) -> str:
    if not est:
        return (f'<div class="est none">Not sliced yet: run <code>{_e(t["render_cmd"])}</code> '
                "to render and slice every plate with Bambu Studio.</div>")
    src = ("Bambu Studio estimate stored in the file" if est["source"] == "file"
           else f"Bambu Studio estimate (sliced by tulda {time_html(est.get('created'))})")
    rows = "".join(
        f"<tr><td>{_fil_label(f['id'], fils) if f.get('id') else _sw(f.get('colour')) + _e(f.get('type') or '')}"
        f"</td><td>{_g(f.get('used_g'))} g</td><td>{_g(f.get('used_m'))} m</td></tr>"
        for f in est.get("filaments") or [])
    tot = []
    if est.get("prediction_s") is not None:
        tot.append(f"<b>{_e(fmt_duration(est['prediction_s']))}</b>")
    if est.get("weight_g") is not None:
        tot.append(f"<b>{_g(est['weight_g'])} g</b>")
    return (f'<div class=est><div>{" · ".join(tot) or "?"}</div><div class=m>{src}</div>'
            + (f"<table class=ft><tr><th>Filament</th><th>Used</th><th></th></tr>{rows}</table>"
               if rows else "") + "</div>")


def _plate_html(pl: dict, t: dict) -> str:
    img = pl["image"]
    if img:
        thumb = (f'<a class=pth href="{_e(img["group_url"])}"><img src="{_e(img["url"])}" '
                 f'alt="Plate {_e(pl["n"])} render" loading=lazy decoding=async></a>')
    else:
        thumb = '<div class="pth none">no render yet</div>'
    links = []
    if img:
        links.append(f'<a href="{_e(img["group_url"])}">request</a>')
    if pl["viewer"] and pl["viewer"].get("viewer_url"):
        links.append(f'<a href="{_e(pl["viewer"]["viewer_url"])}">3D viewer'
                     + (" (whole build)" if pl["viewer_whole_build"] else "") + "</a>")
    if pl["slice"]:
        links.append(f'<a href="{_e(pl["slice"]["url"])}">slice summary</a>')
    if not img and pl["estimate"]:
        links.append(f'render it: <code>{_e(t["render_cmd"])}</code>')
    objs = ", ".join(_e(o["name"]) for o in pl["objects"]) or "no objects"
    fil = " ".join(_fil_label(n, t["filaments"]) for n in pl["filaments"])
    ov = ""
    if pl.get("overrides"):
        ov = '<div class=m>Plate settings: ' + ", ".join(
            f"{_e(k)} = {_e(v)}" for k, v in pl["overrides"].items()) + "</div>"
    title = f"Plate {_e(pl['n'])}" + (f": {_e(pl['name'])}" if pl.get("name") else "")
    return (f'<li class=pl>{thumb}<div class=pi><h4>{title}</h4>'
            f'<div>Objects: {objs}</div><div class=fls>{fil}</div>{ov}'
            f'{_estimate_html(pl["estimate"], t, t["filaments"])}'
            f'<div class=lk>{" · ".join(links)}</div></div></li>')


def _threemf_html(i: int, t: dict) -> str:
    head = (f'<h3 id="f{i}">{_e(t["name"])}</h3><div class=m>{_e(_size(t["size"]))} · modified '
            f'{time_html(t["modified"])}' + (f' · {_e(t["application"])}' if t.get("application") else "")
            + "</div>")
    if t["error"]:
        return f'<section class=tf>{head}<p class=bad>Cannot read this 3mf: {_e(t["error"])}</p></section>'
    out = [head]
    if t["post_process"]:
        cmds = "".join(f'<div class=m>in {_e(s.get("config") or "?")} ({_e(s.get("key"))}):</div>'
                       f'<pre>{_e(s.get("command"))}</pre>' for s in t["post_process"])
        out.append('<div class=warn role=alert><b>Post-processing script embedded.</b> A slicer GUI '
                   "runs it as a shell command after exporting G-code. tulda never runs it (slicing "
                   "blanks it in a copy) and holds imports that carry one. Read it before opening "
                   f"this file in a slicer.{cmds}</div>")
    n = len(t["plates"])
    out.append(f'<h4 class=sh>{n} plate{"s" * (n != 1)}</h4><ul class=pls>'
               + "".join(_plate_html(pl, t) for pl in t["plates"]) + "</ul>")
    if not t["bambu"]:
        out.append("<p class=m>No Bambu Studio / OrcaSlicer project settings in this file (a plain "
                   "3mf): it slices with the slicer's current profiles.</p>")
    if t["filaments"]:
        rows = "".join(f"<tr><td>{_sw(f['colour'])}{_e(f['n'])}</td><td>{_e(f['type'] or '')}</td>"
                       f"<td>{_e(f['colour'] or '')}</td><td>{_e(f['profile'] or f['vendor'] or '')}</td>"
                       f"<td>{_e('; '.join(f['used_by']) or 'unused')}</td></tr>" for f in t["filaments"])
        out.append("<h4 class=sh>Filaments</h4><div class=tw><table><tr><th>#</th><th>Type</th>"
                   f"<th>Colour</th><th>Profile</th><th>Used by</th></tr>{rows}</table></div>")
    if t["changes"]:
        rows = "".join(f"<tr><td>{_e(c['plate'])}</td><td>{'' if c['z'] is None else _e(format(c['z'], 'g'))}"
                       f"</td><td>{_e(c['type'])}</td><td>{_fil_label(c['extruder'], t['filaments']) if c['extruder'] and c.get('filament') else ''}"
                       f"{_sw(c['colour']) if c.get('filament') and not c['extruder'] else ''}</td><td>{_e(c['gcode'] or '')}</td></tr>"
                       for c in t["changes"])
        out.append("<h4 class=sh>Colour changes and layer G-code</h4><div class=tw><table><tr><th>Plate</th>"
                   f"<th>Z (mm)</th><th>Type</th><th>Filament</th><th>G-code</th></tr>{rows}</table></div>")
    if t["settings"]:
        rows = "".join(f"<tr><th>{_e(s['label'])}</th><td>{_e(s['value'])}</td></tr>" for s in t["settings"])
        out.append(f"<h4 class=sh>Process settings</h4><div class=tw><table class=kv>{rows}</table></div>")
        if t["changed"]:
            out.append("<p class=m>Changed from the process profile: "
                       + ", ".join(_e(k) for k in t["changed"]) + "</p>")
        if t["variable_layers"]:
            out.append("<p class=m>Has a variable layer height profile.</p>")
    if t["overrides"]:
        rows = "".join(f"<tr><td>{_e(o['object'])}{' / ' + _e(o['part']) if o['part'] else ''}</td><td>"
                       + ", ".join(f"{_e(k)} = {_e(v)}" for k, v in o["settings"].items()) + "</td></tr>"
                       for o in t["overrides"])
        out.append("<h4 class=sh>Per-object overrides</h4><div class=tw><table><tr><th>Object / part</th>"
                   f"<th>Settings</th></tr>{rows}</table></div>")
    return f'<section class=tf>{"".join(out)}</section>'


def render_project_detail(data: dict) -> str:
    name = _e(data["name"])
    facts = []
    if data["source"]:
        facts.append(f"source <span class=src>{_e(data['source'])}</span>")
    if data["created"]:
        facts.append(f"created {time_html(data['created'])}")
    if data["gallery_url"]:
        facts.append(f'<a href="{_e(data["gallery_url"])}">renders in the gallery</a>')
    body = []
    if data["source"] is None:
        body.append("<p class=m>No project folder of this name in the configured destinations, so "
                    "there are no source files to show.</p>")
    else:
        pp = [t for t in data["threemf"] if t["post_process"]]
        if pp:
            body.append('<div class=warn role=alert><b>Post-processing script</b> in '
                        + ", ".join(f'<a href="#f{data["threemf"].index(t)}">{_e(t["name"])}</a>' for t in pp)
                        + " (see below).</div>")
        if data["threemf"]:
            body.append("<h2>3mf files</h2>" + "".join(_threemf_html(i, t) for i, t in enumerate(data["threemf"])))
            if data["threemf_skipped"]:
                body.append(f"<p class=m>{data['threemf_skipped']} more 3mf files not shown.</p>")
        rows = "".join(
            f"<tr><td class=src>{_e(f['name'])}</td><td>{_e(f['type'])}</td><td class=n>{_e(_size(f['size']))}</td>"
            f"<td>{time_html(f['modified'])}</td></tr>" for f in data["files"])
        body.append(f"<h2>Files</h2><div class=tw><table><tr><th>Name</th><th>Type</th><th>Size</th>"
                    f"<th>Modified</th></tr>{rows}</table></div>" if rows else "<h2>Files</h2><p class=m>No files.</p>")
        if data["truncated"]:
            body.append("<p class=m>Listing truncated (too many files or folders too deep).</p>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} - tulda project</title><style>
:root{{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#d8d8de;--acc:#2e8b57;--card:#f2f2f5;--bad:#b42318;--warnbg:#fff4e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--line:#33333a;--acc:#4fc58a;--card:#1d1d21;--bad:#ff8a7a;--warnbg:#3a2410}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
main{{padding:12px 16px 16px;max-width:1000px}}{TAB_CSS}
a{{color:inherit}}a:focus-visible{{outline:2px solid var(--acc);outline-offset:2px}}
h1{{font-size:20px;margin:0 0 4px;overflow-wrap:anywhere}}h2{{font-size:17px;margin:20px 0 6px}}
h3{{font-size:16px;margin:0 0 2px;overflow-wrap:anywhere}}h4{{font-size:15px;margin:0 0 4px;overflow-wrap:anywhere}}
.sh{{margin:14px 0 6px}}.m{{color:var(--mut);font-size:14px}}.bad{{color:var(--bad)}}
.src{{font-family:ui-monospace,monospace;font-size:13px;overflow-wrap:anywhere}}
.tf{{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}}
.warn{{border:2px solid var(--bad);background:var(--warnbg);border-radius:8px;padding:8px 12px;margin:8px 0}}
.warn pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;margin:4px 0}}
.pls{{list-style:none;margin:0;padding:0}}
.pl{{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}}
.pth{{flex:none;width:160px;height:160px;border-radius:8px;background:var(--card);overflow:hidden;display:flex;
align-items:center;justify-content:center;color:var(--mut);font-size:13px;text-align:center}}
.pth img{{width:100%;height:100%;object-fit:contain;display:block}}
.pi{{min-width:0;flex:1}}.fls{{display:flex;flex-wrap:wrap;gap:4px 10px;margin:4px 0}}
.fl{{white-space:nowrap;font-size:14px}}
.sw{{display:inline-block;width:13px;height:13px;border-radius:3px;border:1px solid #0006;vertical-align:-2px;margin-right:4px}}
.sw.none{{background:repeating-linear-gradient(45deg,var(--card),var(--card) 3px,var(--line) 3px,var(--line) 6px)}}
.est{{background:var(--card);border-radius:8px;padding:6px 10px;margin:6px 0}}
.est.none{{color:var(--mut);font-size:14px}}.lk{{font-size:14px}}
code{{font-family:ui-monospace,monospace;font-size:13px;overflow-wrap:anywhere}}
.tw{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}
td,th{{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}}
.kv th{{font-weight:500;color:var(--mut);width:45%}}.ft td,.ft th{{border:none;padding:1px 8px 1px 0}}
td.n{{white-space:nowrap}}
@media (max-width:520px){{main{{padding:10px 12px}}.pl{{flex-direction:column}}.pth{{width:100%;height:220px}}}}
</style></head><body>
{tabs_html("projects")}
<main><p class=m><a href="/{PROJECTS_PREFIX}/">Projects</a> /</p><h1>{name}</h1>
<p class=m>{" · ".join(facts)}</p>
{"".join(body)}
<p class=m>Read-only: this page never renders or slices. Times are shown in your browser's time zone
(UTC without JavaScript). JSON: <a href="/api/projects/{_e(_q(data["name"]))}">/api/projects/{name}</a></p>
</main>{LOCAL_SCRIPT}</body></html>
"""
