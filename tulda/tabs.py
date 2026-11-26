"""The section tabs shown at the top of every HTML page (static markup, works without script).

Three tabs: Gallery (`/`, every gallery view: recent, project, date, request and search, whose box
sits in the gallery header), Projects (`/_p/`) and Watcher (`/_w/`). Colours come from the page's own CSS
variables (--fg, --mut, --acc, --line, --card), so each page's light/dark handling applies."""

from __future__ import annotations

from html import escape

TABS = (("gallery", "Gallery", "/"), ("projects", "Projects", "/_p/"), ("watcher", "Watcher", "/_w/"))

CSS = (".tabs{display:flex;align-items:flex-end;gap:2px;padding:0 12px;border-bottom:1px solid var(--line);"
       "overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}"
       ".tabs::-webkit-scrollbar{display:none}"
       ".tabs .brand{font-weight:700;font-size:17px;color:var(--fg);text-decoration:none;padding:10px 12px 9px 4px;"
       "margin-right:6px;white-space:nowrap}"
       ".tabs .tab{flex:none;padding:10px 16px 8px;font-size:15px;font-weight:600;color:var(--mut);"
       "text-decoration:none;border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;"
       "margin-bottom:-1px;white-space:nowrap}"
       ".tabs .tab:hover{color:var(--fg);background:var(--card)}"
       ".tabs .tab.on{color:var(--acc);background:var(--bg);border-color:var(--line);"
       "box-shadow:inset 0 3px 0 var(--acc)}"
       ".tabs .tab:focus-visible,.tabs .brand:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}"
       "@media (max-width:420px){.tabs{padding:0 6px}.tabs .brand{padding-right:6px;margin-right:2px}"
       ".tabs .tab{padding:10px 12px 8px}}")


def tabs_html(current: str | None) -> str:
    out = ['<nav class="tabs" aria-label="Sections"><a class="brand" href="/">tulda</a>']
    for key, label, href in TABS:
        on = key == current
        out.append(f'<a class="tab{" on" if on else ""}" href="{escape(href)}"'
                   f'{" aria-current=page" if on else ""}>{escape(label)}</a>')
    out.append("</nav>")
    return "".join(out)
