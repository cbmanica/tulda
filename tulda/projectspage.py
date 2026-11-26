"""/_p/: the Projects tab: every project tulda works with, sortable by name or by date.

Rows come from two read-only sources:
  * the catalog (renders root): one row per project directory with present renders in
    untrashed groups: render and request counts, newest render time, the trigger of that
    render's request and the newest PNG as a thumbnail (`Catalog.project_summary`);
  * project folders (a directory holding `tulda-project.json`) directly inside the user
    config's `[[watch.dir]] destination`s and `[projects] default_destination`. A folder whose
    name matches a renders project adds its source (shown as `<destination name>/<folder>`,
    folder names only, never an absolute path, like the Watcher tab) and creation time; one
    with no renders yet is listed as "no renders yet", without a gallery link.

Sorting is a plain query parameter (`?sort=date`, the default: newest activity first, i.e. the
newest render, else the project's creation time; `?sort=name`: case-insensitive name), so it
works without JavaScript. Names come from file and folder names and are untrusted: every value
is escaped. Rows with a source folder also show a compact file summary (counts by type, 3mf
plates, a post-processing-script flag; projectfiles.folder_summary) and link to the project
page `/_p/<project>/` (projectdetail.py). The only script is timefmt.LOCAL_JS (allowed by
hash), which shows the UTC <time> elements in the browser's zone."""

from __future__ import annotations

import os
import urllib.parse
from html import escape
from pathlib import Path

from .gallery import trigger_badge
from .items import PROJECTS_PREFIX
from .tabs import CSS as TAB_CSS, tabs_html
from .timefmt import LOCAL_SCRIPT, parse_ts, time_html, ts_key, utc_from_ts

SORTS = ("date", "name")
MAX_SOURCE_PROJECTS = 1000


def parse_sort(q: dict) -> str:
    v = (q.get("sort") or [""])[0]
    return v if v in SORTS else "date"


def _e(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def source_destinations(config: Path | None) -> list[Path]:
    """Watch destinations and [projects] default_destination of the user config (resolved,
    existing, de-duplicated). Any config problem gives none."""
    if config is None:
        return []
    from . import webrender
    out = list(webrender.watch_destinations(config))
    try:
        import tomllib
        v = tomllib.loads(Path(config).read_text("utf-8")).get("projects", {}).get("default_destination")
        if isinstance(v, str) and v.strip():
            d = Path(v).expanduser()
            if d.is_absolute():
                d = d.resolve()
                if d.is_dir() and d not in out:
                    out.append(d)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    return out


def source_projects(dests: list[Path]) -> dict[str, dict]:
    """{folder name: {source, created}} for project folders in dests (first destination wins
    on a name clash). Same folder checks as the Watcher tab: real directory, readable manifest."""
    from .webrender import _read_manifest, safe_project
    out: dict[str, dict] = {}
    for dest in dests:
        try:
            names = sorted(e.name for e in os.scandir(dest))
        except OSError:
            continue
        for name in names:
            if len(out) >= MAX_SOURCE_PROJECTS:
                return out
            if name in out:
                continue
            proj = safe_project(dest, name)
            man = _read_manifest(proj) if proj is not None else None
            if man is None:
                continue
            created = man.get("created_utc")
            # "path" is private: used to list the folder, never put in a page or JSON
            out[name] = {"source": f"{dest.name}/{name}",
                         "created": created if parse_ts(created) else None, "path": proj}
    return out


def detail_url(name: str) -> str:
    return f"/{PROJECTS_PREFIX}/{urllib.parse.quote(name, safe='')}/"


def _files(src: dict) -> dict | None:
    """The row's compact file summary (projectfiles.folder_summary of the source folder, put
    in src["summary"] by the caller): type counts, 3mf plates, post-processing flag."""
    s = src.get("summary")
    if not s:
        return None
    from .projectfiles import summary_text
    return {"text": summary_text(s), "counts": s["counts"], "threemf": s["threemf"],
            "plates": s["plates"], "post_process": s["post_process"]}


def project_rows(summary: list[dict], sources: dict[str, dict]) -> list[dict]:
    """Merge catalog rows and source folders into display rows (unsorted)."""
    rows = []
    for s in summary:
        src = sources.get(s["name"]) or {}
        rows.append({"name": s["name"], "url": f"/{urllib.parse.quote(s['name'], safe='')}/",
                     "detail": detail_url(s["name"]),
                     "renders": s["renders"], "requests": s["requests"],
                     "last": utc_from_ts(s["mtime"]) if s.get("mtime") else None,
                     "trigger": s.get("trigger"), "thumb": s.get("thumb"),
                     "source": src.get("source"), "created": src.get("created"), "files": _files(src)})
    seen = {s["name"] for s in summary}
    for name, src in sources.items():
        if name not in seen:
            rows.append({"name": name, "url": None, "detail": detail_url(name), "renders": 0,
                         "requests": 0, "last": None, "trigger": None, "thumb": None,
                         "source": src["source"], "created": src["created"], "files": _files(src)})
    return rows


def activity(row: dict) -> str:
    """Sort key for 'newest activity': newest render, else creation time ('' when neither)."""
    return ts_key(row.get("last")) or ts_key(row.get("created"))


def sort_rows(rows: list[dict], sort: str) -> list[dict]:
    out = sorted(rows, key=lambda r: (r["name"].casefold(), r["name"]))
    if sort == "date":
        # newest first; undated ('') last; reverse=True keeps the name order among equal keys
        out.sort(key=activity, reverse=True)
    return out


def _row_html(r: dict) -> str:
    name = _e(r["name"])
    title = f'<a href="{_e(r["url"])}">{name}</a>' if r["url"] else name
    if r["thumb"]:
        thumb = (f'<a class=th href="{_e(r["url"])}" tabindex=-1 aria-hidden=true>'
                 f'<img src="{_e(r["thumb"])}" alt="" loading=lazy decoding=async></a>')
    else:
        thumb = '<span class="th none" aria-hidden=true></span>'
    if r["renders"]:
        n, g = r["renders"], r["requests"]
        facts = [_e(f"{n} render{'s' * (n != 1)} in {g} request{'s' * (g != 1)}"),
                 f"newest {time_html(r['last'])}"]
        badge = trigger_badge(r.get("trigger"))
        if badge:
            facts.append(f"last trigger {badge}")
    else:
        facts = ["no renders yet"]
    lines = [f'<div class=m>{" · ".join(facts)}</div>']
    extra = []
    if r.get("source"):
        extra.append(f"source <span class=src>{_e(r['source'])}</span>")
    if r.get("created"):
        extra.append(f"created {time_html(r['created'])}")
    if extra:
        lines.append(f'<div class=m>{" · ".join(extra)}</div>')
    f = r.get("files")
    if f is not None:
        lines.append(f'<div class=m>files: {_e(f["text"])}</div>')
        if f["post_process"]:
            lines.append('<div class=pp>post-processing script in a 3mf</div>')
    if r.get("detail"):
        lines.append(f'<div class=m><a href="{_e(r["detail"])}">'
                     f'{"files, plates and print settings" if r.get("source") else "project page"}</a></div>')
    return f'<li class=pr>{thumb}<div class=pi><h3>{title}</h3>{"".join(lines)}</div></li>'


def _sort_link(key: str, label: str, cur: str) -> str:
    on = key == cur
    return (f'<a class="sb{" on" if on else ""}" href="/{PROJECTS_PREFIX}/?sort={key}"'
            f'{" aria-current=true" if on else ""}>{label}</a>')


def render_projects_page(rows: list[dict], sort: str) -> str:
    """rows: already sorted (sort_rows)."""
    sort = sort if sort in SORTS else "date"
    if rows:
        n = len(rows)
        body = (f'<p class=m>{n} project{"s" * (n != 1)}.</p>'
                f'<ul class=pl>{"".join(_row_html(r) for r in rows)}</ul>')
    else:
        body = ("<p class=m>No projects yet. Render something (<code>tulda render FILE</code>) or "
                "create a project (<code>tulda project new NAME</code>).</p>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tulda projects</title><style>
:root{{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#d8d8de;--acc:#2e8b57;--card:#f2f2f5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--line:#33333a;--acc:#4fc58a;--card:#1d1d21}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
main{{padding:12px 16px 16px;max-width:1000px}}{TAB_CSS}
a{{color:inherit}}h1{{font-size:20px;margin:0}}.m{{color:var(--mut);font-size:14px}}
.hd{{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin:0 0 10px}}
.sorts{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.sb{{padding:6px 12px;min-height:36px;display:inline-flex;align-items:center;border:1px solid var(--line);
border-radius:16px;background:var(--card);text-decoration:none;font-size:14px}}
.sb.on{{background:var(--acc);border-color:var(--acc);color:#fff}}
a:focus-visible{{outline:2px solid var(--acc);outline-offset:2px}}
.pl{{list-style:none;margin:0;padding:0}}
.pr{{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line)}}
.th{{flex:none;width:96px;height:72px;border-radius:8px;background:var(--card);overflow:hidden;display:block}}
.th img{{width:100%;height:100%;object-fit:contain;display:block}}
.pi{{min-width:0;flex:1}}.pi h3{{font-size:16px;margin:0 0 2px;overflow-wrap:anywhere}}
.src{{font-family:ui-monospace,monospace;font-size:13px;overflow-wrap:anywhere}}
.pp{{display:inline-block;font-size:13px;font-weight:600;color:#b42318;border:2px solid #b42318;border-radius:6px;padding:0 6px;margin:2px 0}}
.trg{{font-size:11px;padding:0 7px;border-radius:9px;border:1px dashed var(--mut);color:var(--mut);white-space:nowrap}}
.trg-web{{border-style:solid;border-color:#3b82f6;color:#3b82f6}}.trg-watch{{border-style:solid;border-color:#a855f7;color:#a855f7}}
@media (max-width:480px){{main{{padding:10px 12px}}.th{{width:64px;height:48px}}.pr{{gap:10px}}}}
</style></head><body>
{tabs_html("projects")}
<main><div class=hd><h1>Projects</h1>
<nav class=sorts aria-label="Sort"><span class=m>Sort:</span>{_sort_link("date", "Newest activity", sort)}{_sort_link("name", "Name", sort)}</nav></div>
{body}
<p class=m>A project is a folder of the renders root (counts exclude trashed requests) or a project
folder in the configured destinations. Times are shown in your browser's time zone (UTC without
JavaScript). JSON: <a href="/api/projects?sort={sort}">/api/projects</a></p>
</main>{LOCAL_SCRIPT}</body></html>
"""
