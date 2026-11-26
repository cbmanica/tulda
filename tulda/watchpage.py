"""/_w/: read-only watcher activity page (status from watch.json, events newest first).

Every value shown comes from file names in watched folders or from error text, so it is
attacker-controlled: everything goes through html.escape, nothing is inserted as markup and the
page has no script."""

from __future__ import annotations

from html import escape

OUTCOME_LABEL = {"created": "imported", "exists": "already imported", "skipped": "skipped",
                 "held": "held (needs review)", "error": "error", "backlog": "backlog ignored"}


def _e(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def _status_html(st: dict | None) -> str:
    if not st:
        return "<p class=m>No watcher state found (the watcher has not run on this machine, or " \
               "it uses another state directory).</p>"
    run = "running" if st["running"] else "not running"
    rows = [("Process", f"{run}, started {st.get('started') or '-'}"),
            ("Config", (st.get("mode") or "-")
             + (f": {st['reason']}" if st.get("reason") else "")),
            ("Last poll", st.get("last_poll") or "-")]
    lc = st.get("last_created")
    rows.append(("Last import", f"{lc['time']}  {lc['project']} ← {lc['file']}" if lc else "-"))
    le = st.get("last_error")
    rows.append(("Last error", f"{le['time']}  {le['message']}" if le else "-"))
    for d in st.get("dirs") or []:
        t = d["totals"]
        rows.append((f"Folder {d['dir']}",
                     f"→ {d['destination']}: created {t['created']}, exists {t['exists']}, "
                     f"skipped {t['skipped']}, held {t.get('held', 0)}, errors {t['errors']}"
                     + (f"; problem: {d['problem']}" if d.get("problem") else "")))
    cls = "ok" if st["running"] else "bad"
    return (f'<p class="st {cls}">Watcher {_e(run)}</p><table class=kv>'
            + "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows) + "</table>")


def _events_html(events: list[dict]) -> str:
    if not events:
        return "<p class=m>No watcher activity recorded yet.</p>"
    out = ["<table class=ev><thead><tr><th>Time</th><th>Folder</th><th>File</th><th>Outcome</th>"
           "<th>Project / reason</th></tr></thead><tbody>"]
    for ev in events:
        oc = ev["outcome"]
        detail = _e(ev.get("project") if oc in ("created", "exists") else ev.get("reason"))
        if oc == "held":
            detail += ("<details open><summary>Post-processing script (review before approving; "
                       "tulda never runs it)</summary>"
                       f"<pre>{_e(ev.get('script'))}</pre></details>"
                       f"<div class=m>Approve after review:</div><pre class=cmd>{_e(ev.get('approve'))}</pre>")
        out.append(f'<tr class="o-{_e(oc)}"><td class=t>{_e(ev.get("time"))}</td>'
                   f'<td>{_e(ev.get("dir"))}</td><td class=f>{_e(ev.get("file"))}</td>'
                   f'<td class=oc>{_e(OUTCOME_LABEL.get(oc, oc))}</td><td>{detail}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def render_watch_page(status: dict | None, events: list[dict]) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>tulda watcher</title><style>
:root{{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#d8d8de;--ok:#2e8b57;--bad:#c2410c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--line:#33333a;--ok:#4fc58a;--bad:#fb923c}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
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
</style></head><body>
<h1><a href="/">tulda</a> / watcher</h1>
{_status_html(status)}
<h2>Activity (newest first)</h2>
<div class=wrap>{_events_html(events)}</div>
<p class=m>Read-only. Refreshes every 30 s. JSON: <a href="/api/watch">/api/watch</a></p>
</body></html>
"""
