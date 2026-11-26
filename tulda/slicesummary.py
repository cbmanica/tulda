"""Slice summary: parse a Bambu Studio plate gcode (+ slice_info.config) into a plain dict
and render it as a small self-contained HTML page. Stdlib only; no knowledge of HTTP or the
catalog. The slicing itself is tulda/adapters/slicing.py.

The gcode is the slicer's own output, so everything here is read from `; FEATURE:` markers,
`; CHANGE_LAYER`, the header comments and the `; key = value` config block."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import escape

# Config keys copied into the summary (the gcode config block has hundreds; keep only these).
SETTING_KEYS = ("layer_height", "initial_layer_print_height", "sparse_infill_density",
                "sparse_infill_pattern", "wall_loops", "enable_support", "support_type",
                "brim_type", "skirt_loops", "enable_prime_tower", "nozzle_diameter",
                "filament_type")
MAX_FEATURES = 40
_NUM = re.compile(r"[-+]?\d*\.?\d+")
_E = re.compile(r"\bE(-?\d*\.?\d+)")
_TOOL = re.compile(r"^T(\d+)\b")


def _f(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_gcode(lines) -> dict:
    """Stream over gcode lines -> {header, settings, layers, features, tool_mm}.
    features: name -> {"segments": n, "length_mm": filament mm extruded (retractions ignored)}.
    Handles M82/M83 (absolute/relative E) and G92 E resets; extrusion before the first
    FEATURE marker is filed under "(start)"."""
    header: dict[str, str] = {}
    settings: dict[str, str] = {}
    features: dict[str, dict] = {}
    tool_mm: dict[int, float] = {}
    layers = 0
    feature = "(start)"
    tool = 0
    relative = True
    last_e = 0.0
    in_cfg = False
    in_hdr = False
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        c = line[0]
        if c == ";":
            if line.startswith("; FEATURE:"):
                feature = line[10:].strip() or "(unnamed)"
                d = features.setdefault(feature, {"segments": 0, "length_mm": 0.0})
                d["segments"] += 1
            elif line.startswith("; CHANGE_LAYER"):
                layers += 1
            elif line.startswith("; CONFIG_BLOCK_START"):
                in_cfg = True
            elif line.startswith("; CONFIG_BLOCK_END"):
                in_cfg = False
            elif line.startswith("; HEADER_BLOCK_START"):
                in_hdr = True
            elif line.startswith("; HEADER_BLOCK_END"):
                in_hdr = False
            elif in_hdr and ":" in line[:40] and line.startswith("; "):
                k, _, v = line[2:].partition(":")
                if k.strip() and k.strip() not in header:
                    header[k.strip()] = v.strip()
            elif in_cfg and line.startswith("; ") and " = " in line:
                k, _, v = line[2:].partition(" = ")
                if k in SETTING_KEYS:
                    settings[k] = v.strip()[:100]
            continue
        if c == "G":
            if line.startswith(("G1 ", "G0 ", "G2 ", "G3 ")):
                m = _E.search(line)
                if m:
                    e = float(m.group(1))
                    d = e if relative else e - last_e
                    if not relative:
                        last_e = e
                    if d > 0:
                        f = features.setdefault(feature, {"segments": 0, "length_mm": 0.0})
                        f["length_mm"] += d
                        tool_mm[tool] = tool_mm.get(tool, 0.0) + d
            elif line.startswith("G92") and "E" in line:
                m = _E.search(line)
                last_e = float(m.group(1)) if m else 0.0
        elif c == "M":
            if line.startswith("M82"):
                relative = False
            elif line.startswith("M83"):
                relative = True
        elif c == "T":
            m = _TOOL.match(line)
            if m and int(m.group(1)) < 255:
                tool = int(m.group(1))
    return {"header": header, "settings": settings, "layers": layers,
            "features": features, "tool_mm": tool_mm}


def parse_slice_info(xml_text: str, plate_no: int | None = None) -> dict:
    """slice_info.config -> {"prediction_s", "weight_g", "support_used", "filaments": [...]} of
    plate `plate_no` (its `index` metadata), else of the first plate. Returns {} if it is
    missing or malformed."""
    if not isinstance(xml_text, str) or "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError):
        return {}
    plates = root.findall("plate")
    if not plates:
        return {}
    plate = plates[0]
    for p in plates:
        if plate_no is not None and any(m.get("key") == "index" and m.get("value") == str(plate_no)
                                        for m in p.findall("metadata")):
            plate = p
            break
    meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
    fils = []
    for f in plate.findall("filament"):
        fils.append({"id": f.get("id"), "type": f.get("type"), "color": f.get("color"),
                     "used_m": _f(f.get("used_m")), "used_g": _f(f.get("used_g")),
                     "used_for_support": f.get("used_for_support") == "true"})
    return {"prediction_s": _f(meta.get("prediction")), "weight_g": _f(meta.get("weight")),
            "support_used": meta.get("support_used") == "true", "filaments": fils}


def fmt_duration(s) -> str:
    if s is None:
        return "?"
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def _used(parsed: dict, name_prefix: str) -> float:
    return sum(v["length_mm"] for k, v in parsed["features"].items() if k.lower().startswith(name_prefix))


def summarize(parsed: dict, info: dict | None = None) -> dict:
    """Combine parse_gcode() and parse_slice_info() output into the stored summary."""
    info = info or {}
    feats = parsed["features"]
    st = parsed["settings"]
    sup_mm = _used(parsed, "support")
    brim_mm = _used(parsed, "brim")
    skirt_mm = _used(parsed, "skirt")
    tower_mm = _used(parsed, "prime tower")
    support_enabled = st.get("enable_support") in ("1", "true")
    supports = sup_mm > 0 or bool(info.get("support_used"))
    # Filaments: prefer slice_info (grams/metres); fall back to per-tool gcode extrusion.
    fils = [dict(f) for f in info.get("filaments", [])]
    if not fils:
        ftypes = (st.get("filament_type") or "").split(";")
        for idx in sorted(parsed["tool_mm"]):
            fils.append({"id": str(idx + 1), "type": ftypes[idx] if idx < len(ftypes) else None,
                         "color": None, "used_m": round(parsed["tool_mm"][idx] / 1000, 2),
                         "used_g": None, "used_for_support": False})
    hdr = parsed["header"]
    model_t = total_t = None
    for k, v in hdr.items():
        if k == "model printing time":
            mt = re.match(r"(.*?);\s*total estimated time\s*(?::\s*)?(.*)", v)
            model_t, total_t = (mt.group(1).strip(), mt.group(2).strip()) if mt else (v, None)
    total_s = info.get("prediction_s")
    rows = sorted(({"name": k, "segments": v["segments"], "length_mm": round(v["length_mm"], 1)}
                   for k, v in feats.items() if v["length_mm"] > 0 or v["segments"]),
                  key=lambda r: -r["length_mm"])[:MAX_FEATURES]
    total_g = info.get("weight_g")
    if total_g is None:
        gs = [f["used_g"] for f in fils if f.get("used_g") is not None]
        total_g = round(sum(gs), 2) if gs else None
    out = {
        "print_time_s": total_s, "print_time": fmt_duration(total_s) if total_s is not None else total_t,
        "model_time": model_t, "layers": parsed["layers"],
        "layer_height": _f(st.get("layer_height")), "max_z": _f(hdr.get("max_z_height")),
        "total_g": total_g, "filaments": fils, "features": rows,
        "supports": {"generated": supports, "length_mm": round(sup_mm, 1),
                     "setting_enabled": support_enabled, "type": st.get("support_type")},
        "adhesion": {"brim": brim_mm > 0, "brim_mm": round(brim_mm, 1), "brim_type": st.get("brim_type"),
                     "skirt": skirt_mm > 0, "skirt_mm": round(skirt_mm, 1),
                     "skirt_loops": st.get("skirt_loops"),
                     "prime_tower": tower_mm > 0, "prime_tower_mm": round(tower_mm, 1)},
        "settings": st,
    }
    out["headline"] = headline(out)
    return out


def yn(b: bool) -> str:
    return "yes" if b else "no"


def headline(s: dict) -> str:
    sup = s["supports"]
    a = s["adhesion"]
    if sup["generated"]:
        sp = f"Supports generated ({sup['length_mm']:g} mm of filament)"
    elif sup["setting_enabled"]:
        sp = "No supports needed (support is enabled but nothing required it)"
    else:
        sp = "No supports needed (support is off)"
    adh = ", ".join(f"{n}: {yn(a[k])}" for n, k in (("brim", "brim"), ("skirt", "skirt"),
                                                    ("prime tower", "prime_tower")))
    return f"{sp}. {adh}."


def card_lines(s: dict) -> list[str]:
    """Short text lines for the gallery card."""
    sup, a = s["supports"], s["adhesion"]
    lines = [f"{s.get('print_time') or '?'} | {s.get('layers', 0)} layers"
             + (f" | {s['total_g']:g} g" if s.get("total_g") is not None else ""),
             "supports: " + ("YES" if sup["generated"] else "no (none needed)"),
             "brim " + yn(a["brim"]) + " | skirt " + yn(a["skirt"]) + " | prime tower " + yn(a["prime_tower"])]
    return lines


def render_html(s: dict, title: str) -> str:
    e = escape
    sup, a = s["supports"], s["adhesion"]
    rows = "".join(f"<tr><td>{e(r['name'])}</td><td>{r['segments']}</td><td>{r['length_mm']:g}</td></tr>"
                   for r in s["features"])
    fil = "".join(f"<tr><td>{e(str(f.get('id') or ''))}</td><td>{e(str(f.get('type') or ''))}</td>"
                  f"<td><span class=sw style=\"background:{e(f['color'])}\"></span> {e(f['color'])}</td>"
                  f"<td>{'' if f.get('used_g') is None else format(f['used_g'], 'g')}</td>"
                  f"<td>{'' if f.get('used_m') is None else format(f['used_m'], 'g')}</td></tr>"
                  if f.get("color") and re.fullmatch(r"#[0-9A-Fa-f]{3,8}", f["color"]) else
                  f"<tr><td>{e(str(f.get('id') or ''))}</td><td>{e(str(f.get('type') or ''))}</td><td></td>"
                  f"<td>{'' if f.get('used_g') is None else format(f['used_g'], 'g')}</td>"
                  f"<td>{'' if f.get('used_m') is None else format(f['used_m'], 'g')}</td></tr>"
                  for f in s["filaments"])
    cls = "bad" if sup["generated"] else "ok"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} - slice summary</title>
<style>
:root{{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#d8d8de;--ok:#2e8b57;--bad:#c2410c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121214;--fg:#eee;--mut:#9a9aa2;--line:#33333a;--ok:#4fc58a;--bad:#fb923c}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;max-width:720px}}
h1{{font-size:18px;margin:0 0 4px}}h2{{font-size:15px;margin:18px 0 6px}}
.v{{padding:8px 12px;border:2px solid var(--line);border-radius:8px;margin:10px 0}}
.v.ok{{border-color:var(--ok)}}.v.bad{{border-color:var(--bad)}}
table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);font-size:14px}}
.sw{{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;border:1px solid var(--line)}}
.m{{color:var(--mut);font-size:13px}}
</style></head><body>
<h1>{e(title)}</h1>
<div class="m">Slice summary (Bambu Studio, plate {int(s.get('plate') or 1)}, as sliced from the model's embedded settings)</div>
<div class="v {cls}"><b>{e(s['headline'])}</b></div>
<table>
<tr><th>Print time</th><td>{e(str(s.get('print_time') or '?'))}</td></tr>
<tr><th>Layers</th><td>{s.get('layers', 0)} (layer height {e(str(s.get('layer_height') or '?'))} mm, max Z {e(str(s.get('max_z') or '?'))} mm)</td></tr>
<tr><th>Total filament</th><td>{'?' if s.get('total_g') is None else format(s['total_g'], 'g') + ' g'}</td></tr>
<tr><th>Supports generated</th><td>{yn(sup['generated'])} (setting {'on' if sup['setting_enabled'] else 'off'}, type {e(str(sup.get('type') or '?'))})</td></tr>
<tr><th>Brim</th><td>{yn(a['brim'])} (type {e(str(a.get('brim_type') or '?'))})</td></tr>
<tr><th>Skirt</th><td>{yn(a['skirt'])} (loops {e(str(a.get('skirt_loops') or '?'))})</td></tr>
<tr><th>Prime tower</th><td>{yn(a['prime_tower'])}</td></tr>
</table>
<h2>Filaments</h2>
<table><tr><th>#</th><th>Type</th><th>Colour</th><th>g</th><th>m</th></tr>{fil}</table>
<h2>Extrusion by feature</h2>
<table><tr><th>Feature</th><th>Segments</th><th>Filament mm</th></tr>{rows}</table>
<p class="m">Lengths are filament (1.75 mm) pushed through the extruder per feature; prime tower and
purge for multi-colour changes are included in the filament totals.</p>
</body></html>
"""
