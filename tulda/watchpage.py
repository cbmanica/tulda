"""/_w/: watcher page: status (watch.json), imported projects with Render buttons, events.

Every value shown comes from file names in watched folders or from error text, so it is
attacker-controlled: everything goes through html.escape and nothing is inserted as markup. The
only script is timefmt.LOCAL_JS (allowed by hash), which turns the UTC <time> elements into the
browser's local time; with JavaScript off they read as UTC. The Render buttons are plain HTML forms (POST /api/render with an opaque
file id and the per-process token; the server answers 303 back to this page), so they work
without JavaScript. See webrender.py for what may be rendered."""

from __future__ import annotations

import urllib.parse
from html import escape

from .gallery import trigger_badge
from .tabs import CSS as TAB_CSS, tabs_html
from .timefmt import LOCAL_SCRIPT, time_html

# ?r=<code> after a POST: a fixed message per code (never text from the request).
RESULT_CODES = {"queued": "Render queued.", "duplicate": "Already queued or rendering.",
                "full": "The render queue is full; try again when it has drained.",
                "held": "Not rendered: this file is on hold (reason shown below).",
                "rendered": "Already rendered: the link below opens it."}
JOB_LABEL = {"queued": "queued", "rendering": "rendering\u2026", "done": "done", "failed": "failed"}

OUTCOME_LABEL = {"created": "imported", "exists": "already imported", "skipped": "skipped",
                 "held": "held (needs review)", "error": "error", "backlog": "backlog ignored"}


def _e(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def _status_html(st: dict | None) -> str:
    if not st:
        return "<p class=m>No watcher state found (the watcher has not run on this machine, or " \
               "it uses another state directory).</p>"
    run = "running" if st["running"] else "not running"
    # (label, cell HTML): every piece escaped here; times become <time> elements
    rows = [("Process", f"{_e(run)}, started {time_html(st.get('started'))}"),
            ("Config", _e((st.get("mode") or "-") + (f": {st['reason']}" if st.get("reason") else ""))),
            ("Last poll", time_html(st.get("last_poll")))]
    lc = st.get("last_created")
    rows.append(("Last import", f"{time_html(lc['time'])}  {_e(lc['project'])} ← {_e(lc['file'])}"
                 if lc else "-"))
    le = st.get("last_error")
    rows.append(("Last error", f"{time_html(le['time'])}  {_e(le['message'])}" if le else "-"))
    for d in st.get("dirs") or []:
        t = d["totals"]
        rows.append((f"Folder {d['dir']}",
                     _e(f"→ {d['destination']}: created {t['created']}, exists {t['exists']}, "
                        f"skipped {t['skipped']}, held {t.get('held', 0)}, errors {t['errors']}"
                        + (f"; problem: {d['problem']}" if d.get("problem") else ""))))
    cls = "ok" if st["running"] else "bad"
    return (f'<p class="st {cls}">Watcher {_e(run)}</p><table class=kv>'
            + "".join(f"<tr><th>{_e(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>")


def _events_html(events: list[dict], anchors: dict | None = None) -> str:
    if not events:
        return "<p class=m>No watcher activity recorded yet.</p>"
    out = ["<table class=ev><thead><tr><th>Time</th><th>Folder</th><th>File</th><th>Outcome</th>"
           "<th>Project / reason</th></tr></thead><tbody>"]
    for ev in events:
        oc = ev["outcome"]
        detail = _e(ev.get("project") if oc in ("created", "exists") else ev.get("reason"))
        if oc in ("created", "exists") and (anchors or {}).get(ev.get("project")):
            detail = f'<a href="#{_e(anchors[ev["project"]])}">{detail}</a>'
        if oc == "held":
            detail += ("<details open><summary>Post-processing script (review before approving; "
                       "tulda never runs it)</summary>"
                       f"<pre>{_e(ev.get('script'))}</pre></details>"
                       f"<div class=m>Approve after review:</div><pre class=cmd>{_e(ev.get('approve'))}</pre>")
        out.append(f'<tr class="o-{_e(oc)}"><td class=t>{time_html(ev.get("time"))}</td>'
                   f'<td>{_e(ev.get("dir"))}</td><td class=f>{_e(ev.get("file"))}</td>'
                   f'<td class=oc>{_e(OUTCOME_LABEL.get(oc, oc))}</td><td>{detail}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def _form(f: dict, token: str, label: str) -> str:
    return (f'<form method=post action="/api/render"><input type=hidden name=token value="{_e(token)}">'
            f'<input type=hidden name=id value="{_e(f["id"])}">'
            f'<button class=rb type=submit>{_e(label)}</button></form>')


def _file_row(f: dict, token: str) -> str:
    job = f.get("job") or {}
    st = job.get("state")
    cells = [f'<td class=f>{_e(f["file"])}</td>']
    jobline = ""
    if st in ("done", "failed"):
        when = f" ({time_html(job['finished'])})" if job.get("finished") else ""
        jobline = (f'<div class={"ok" if st == "done" else "bad"}>last web render {_e(JOB_LABEL[st])}'
                   f'{": " + _e(job.get("message")) if job.get("message") else ""}{when}</div>')
    missing = f.get("missing_views") or []
    if st in ("queued", "rendering"):
        state = f'<span class=busy>{_e(JOB_LABEL[st])}</span>'
        action = ""
    elif f.get("hold"):
        state = f'<span class=bad>held: {_e(f["hold"])}</span>'
        action = ""
    elif f["rendered"]:
        action = _form(f, token, "Render all views") if missing else ""
        state = ""
    else:
        state = "not rendered"
        action = _form(f, token, "Render again" if st == "failed" else "Render")
    if f["rendered"]:
        links = []
        for g in f["rendered"][:3]:
            label = g.get("title") or g["id"]
            links.append(f'<div><a class=ok href="/_g/{_e(urllib.parse.quote(g["id"], safe=""))}/">rendered: '
                         f'{_e(label)}</a> {trigger_badge(g.get("trigger"))} <span class=m>({_e(", ".join(g.get("views") or []) or "no views")}'
                         f'{", trashed" if g.get("trashed") else ""})</span></div>')
        if len(f["rendered"]) > 3:
            links.append(f'<div class=m>+{len(f["rendered"]) - 3} more</div>')
        size = next((g["dims_text"] for g in f["rendered"] if g.get("dims_text")), None)
        if size:
            links.append(f'<div class=dim>size {_e(size)}</div>')
        if missing:
            links.append(f'<div class=m>missing views: {_e(", ".join(missing))}</div>')
        state = "".join(links) + (f"<div>{state}</div>" if state else "")
    cells.append(f"<td>{state}{jobline}</td><td class=a>{action}</td>")
    return f'<tr id="f-{_e(f["id"])}">' + "".join(cells) + "</tr>"


def _projects_html(projects: list[dict] | None, token: str) -> str:
    if projects is None:
        return ""
    if not projects:
        return ("<p class=m>No imported projects found in the watch destinations of the user config "
                "(<code>[[watch.dir]] destination</code>).</p>")
    out = []
    for p in projects:
        sub = [_e(f"in {p['destination']}")]
        if p["title"] != p["folder"]:
            sub.insert(0, _e(f"folder {p['folder']}"))
        if p.get("created"):
            sub.append(f"imported {time_html(p['created'])}")
        out.append(f'<section class=proj id="{_e(p["anchor"])}"><h3>{_e(p["title"])}</h3>'
                   f'<div class=m>{" | ".join(sub)}</div>')
        if not p["files"]:
            out.append("<p class=m>No .3mf/.stl/.scad files to render.</p></section>")
            continue
        out.append("<div class=wrap><table class=pf><thead><tr><th>Model file</th><th>State</th>"
                   "<th></th></tr></thead><tbody>")
        out += [_file_row(f, token) for f in p["files"]]
        out.append("</tbody></table></div></section>")
    return "".join(out)


def render_watch_page(status: dict | None, events: list[dict], projects: list[dict] | None = None,
                      token: str = "", notice: str | None = None, busy: bool = False) -> str:
    anchors = {p["folder"]: p["anchor"] for p in projects or []}
    note = f'<p class=note role=status>{_e(RESULT_CODES[notice])}</p>' if notice in RESULT_CODES else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="same-origin">
<meta http-equiv="refresh" content="{5 if busy else 30}"><title>tulda watcher</title><style>
:root{{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#d8d8de;--ok:#2e8b57;--bad:#c2410c;--acc:#2e8b57;--card:#f2f2f5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--line:#33333a;--ok:#4fc58a;--bad:#fb923c;--acc:#4fc58a;--card:#1d1d21}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
main{{padding:12px 16px 16px}}{TAB_CSS}
a{{color:inherit}}h1{{font-size:20px;margin:0 0 12px}}h2{{font-size:16px;margin:20px 0 8px}}
.m{{color:var(--mut)}}.st{{font-weight:600;margin:0 0 8px}}.st.ok{{color:var(--ok)}}.st.bad{{color:var(--bad)}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}
td,th{{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}}
.kv th{{white-space:nowrap;color:var(--mut);font-weight:500;width:1%}}
.ev td.t{{white-space:nowrap;color:var(--mut)}}.ev td.f{{word-break:break-all}}
.o-created .oc{{color:var(--ok);font-weight:600}}.o-error .oc,.o-held .oc{{color:var(--bad);font-weight:600}}
pre{{white-space:pre-wrap;word-break:break-all;margin:4px 0;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px}}
.o-held td{{background:color-mix(in srgb,var(--bad) 8%,transparent)}}
.o-skipped .oc,.o-backlog .oc,.o-exists .oc{{color:var(--mut)}}
.proj{{margin:0 0 14px;padding:8px 12px;border:1px solid var(--line);border-radius:10px}}
.proj h3{{font-size:15px;margin:0;overflow-wrap:anywhere}}.proj:target{{border-color:var(--acc)}}
.pf td.f{{word-break:break-all}}.pf td.a{{width:1%;white-space:nowrap}}.pf form{{margin:0}}
.dim{{font-variant-numeric:tabular-nums}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.busy{{color:var(--acc);font-weight:600}}
.rb{{padding:6px 14px;min-height:36px;border:1px solid var(--acc);border-radius:16px;background:var(--acc);color:#fff;font:inherit;font-size:14px;cursor:pointer}}
.rb:focus-visible{{outline:2px solid var(--acc);outline-offset:2px}}
.note{{padding:8px 12px;border:1px solid var(--acc);border-radius:8px;margin:0 0 12px}}
.trg{{font-size:11px;padding:0 7px;border-radius:9px;border:1px dashed var(--mut);color:var(--mut);white-space:nowrap}}
.trg-web{{border-style:solid;border-color:#3b82f6;color:#3b82f6}}.trg-watch{{border-style:solid;border-color:#a855f7;color:#a855f7}}
</style></head><body>
{tabs_html("watcher")}
<main>{note}
{_status_html(status)}
<h2>Imported projects</h2>
{_projects_html(projects, token)}
<h2>Activity (newest first)</h2>
<div class=wrap>{_events_html(events, anchors)}</div>
<p class=m>Refreshes every {5 if busy else 30} s. Render buttons render one model file of an imported project
(the only thing this server changes); everything else is read-only. Times are shown in your browser's
time zone (UTC without JavaScript). JSON: <a href="/api/watch">/api/watch</a></p>
</main>{LOCAL_SCRIPT}</body></html>
"""
