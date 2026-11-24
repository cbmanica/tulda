"""CLI management of the catalog: db, ls, group, prune, trash. All writes happen here; the
HTTP server never mutates metadata or deletes files."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import load_registry
from .catalog import (Catalog, CatalogError, Filters, norm_tag, parse_duration, prune_candidates,
                      select_prune, valid_day)


def _catalog(args, sync: bool = True) -> Catalog:
    from .cli import renders_root
    reg = load_registry(None, Path(args.config) if args.config else None)
    cat = Catalog(renders_root(args, reg), log=lambda m: print(m, file=sys.stderr))
    if sync:
        cat.sync()
    return cat


def _flags(g: dict) -> str:
    return ("*" if g["starred"] else "-") + ("T" if g["trashed"] else "-")


def _line(g: dict) -> str:
    title = g["title"] or ", ".join(g["sources"]) or ""
    tags = (" [" + ",".join(g["tags"]) + "]") if g["tags"] else ""
    return f"{g['id']:48} {_flags(g)} {g['n_renders']:3} img  {title}{tags}"


def _day_arg(s: str | None, end: bool = False) -> str | None:
    if not s:
        return None
    try:
        td = parse_duration(s)
        return (datetime.now(timezone.utc) - td).strftime("%Y-%m-%d")
    except CatalogError:
        pass
    if len(s) == 10 and valid_day(s):
        return s
    raise CatalogError(f"bad date {s!r}: use YYYY-MM-DD or a duration like 7d")


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---- db ---------------------------------------------------------------------------

def cmd_db(args) -> int:
    cat = _catalog(args, sync=False)
    if args.db_cmd == "reindex":
        t0 = time.monotonic()
        st = cat.sync(force=True)
        print(f"reindexed {st['indexed']} renders in {time.monotonic() - t0:.2f}s "
              f"({st['missing']} marked missing); management metadata untouched")
    elif args.db_cmd == "stats":
        for k, v in cat.stats().items():
            print(f"{k:16} {v}")
    elif args.db_cmd == "vacuum":
        before = cat.path.stat().st_size
        cat.vacuum()
        print(f"vacuumed: {before} -> {cat.path.stat().st_size} bytes")
    elif args.db_cmd == "check":
        cat.sync()
        probs = cat.check()
        for p in probs:
            print(p)
        print("ok" if not probs else f"{len(probs)} problem(s); `tulda db reindex` fixes index drift")
        return 1 if probs else 0
    return 0


# ---- ls ---------------------------------------------------------------------------

def cmd_ls(args) -> int:
    cat = _catalog(args)
    f = Filters(project=args.project, tags=[norm_tag(t) for t in args.tag or []], starred=args.starred,
                trash=args.trashed, trashed_only=args.trashed, since=_day_arg(args.since),
                until=_day_arg(args.until, True))
    limit = args.limit or 100000
    groups, nxt = cat.list_groups(f, limit, with_items=False)
    for g in groups:
        print(_line(g))
    if nxt:
        print(f"... more; raise --limit (showing {len(groups)})", file=sys.stderr)
    if not groups:
        print("no matching groups", file=sys.stderr)
    return 0


# ---- group --------------------------------------------------------------------------

def cmd_group(args) -> int:
    cat = _catalog(args)
    g = cat.resolve_group(args.id)
    gid = g["id"]
    a = args.group_cmd
    if a == "show":
        print(f"id       {gid}\ntitle    {g['title']}\nauto     {g['auto_title']}\ncreated  {g['created']}"
              f"\nstarred  {g['starred']}\ntrashed  {g['trashed']}\ntags     {', '.join(g['tags'])}"
              f"\nnotes    {g['notes'] or ''}\nsources  {', '.join(g['sources'])}\ncommand  {g['cmd'] or ''}")
        for i in g["items"]:
            print(f"  {i['project']}/{i['file']}  ({i['adapter']}/{i['stage']}{'/' + i['view'] if i['view'] else ''})")
    elif a == "rename":
        cat.set_title(gid, args.title)
        print(f"{gid}: title {'cleared' if not args.title.strip() else 'set'}")
    elif a == "note":
        cat.set_notes(gid, args.text)
        print(f"{gid}: note {'cleared' if not args.text.strip() else 'set'}")
    elif a in ("star", "unstar"):
        cat.set_starred(gid, a == "star")
        print(f"{gid}: {'starred' if a == 'star' else 'unstarred'}")
    elif a == "tag":
        cat.add_tags(gid, args.tags)
        print(f"{gid}: tags {', '.join(cat.get_group(gid)['tags'])}")
    elif a == "untag":
        cat.remove_tags(gid, args.tags)
        print(f"{gid}: tags {', '.join(cat.get_group(gid)['tags'])}")
    elif a in ("trash", "restore"):
        cat.set_trashed(gid, a == "trash")
        print(f"{gid}: {'moved to trash (files kept)' if a == 'trash' else 'restored'}")
    elif a == "rm":
        n = len(g["items"])
        if not _confirm(f"Permanently delete group {gid} ({n} image(s) + sidecars) under {cat.root}?", args.yes):
            print("aborted")
            return 1
        return _report_delete(cat, [gid])
    return 0


def _report_delete(cat: Catalog, ids: list[str]) -> int:
    bad = 0
    total = 0
    for gid in ids:
        res = cat.delete_group(gid)
        total += len(res["deleted"])
        for e in res["errors"]:
            print(f"error: {e}", file=sys.stderr)
        bad += len(res["errors"])
    print(f"deleted {total} file(s) from {len(ids)} group(s)" + (f"; {bad} error(s)" if bad else ""))
    return 1 if bad else 0


# ---- prune / trash -------------------------------------------------------------------

def cmd_prune(args) -> int:
    if args.yes and args.dry_run:
        raise CatalogError("--yes and --dry-run are mutually exclusive")
    cat = _catalog(args)
    older = parse_duration(args.older_than) if args.older_than else None
    sel = select_prune(prune_candidates(cat, args.project), time.time(), older, args.keep_last,
                       args.keep_starred)
    for g in sel:
        print(f"{'delete' if args.yes else 'would delete'}: {g['id']}  {g['n_renders']} img  "
              f"{g['day']}  {g['title'] or ''}")
    print(f"{len(sel)} group(s) selected" + ("" if args.yes else "; dry run, pass --yes to delete"))
    if not args.yes or not sel:
        return 0
    return _report_delete(cat, [g["id"] for g in sel])


def cmd_trash(args) -> int:
    cat = _catalog(args)
    groups, _ = cat.list_groups(Filters(trash=True, trashed_only=True), 100000, with_items=False)
    for g in groups:
        print(_line(g))
    if not groups:
        print("trash is empty")
        return 0
    if not _confirm(f"Permanently delete {len(groups)} trashed group(s)?", args.yes):
        print("aborted")
        return 1
    return _report_delete(cat, [g["id"] for g in groups])


# ---- parsers ----------------------------------------------------------------------------

def add_manage_parsers(sub, common) -> None:
    d = sub.add_parser("db", help="manage the SQLite catalog (an index; sidecars stay the source of truth)")
    ds = d.add_subparsers(dest="db_cmd", required=True)
    for name, h in (("reindex", "re-read every sidecar; keeps stars/tags/titles/trash"),
                    ("stats", "counts and schema version"), ("vacuum", "compact the database"),
                    ("check", "integrity check plus index-vs-disk comparison")):
        common(ds.add_parser(name, help=h))
    d.set_defaults(fn=cmd_db)

    ls = sub.add_parser("ls", help="list request groups (newest first)")
    ls.add_argument("--project")
    ls.add_argument("--since", metavar="DATE|AGE", help="YYYY-MM-DD (UTC) or e.g. 7d")
    ls.add_argument("--until", metavar="DATE|AGE")
    ls.add_argument("--tag", action="append", help="group tag (source-file stem or user tag); repeat to AND")
    ls.add_argument("--starred", action="store_true")
    ls.add_argument("--trashed", action="store_true", help="list only trashed groups")
    ls.add_argument("--limit", type=int, default=50, help="0 = all (default 50)")
    common(ls)
    ls.set_defaults(fn=cmd_ls)

    g = sub.add_parser("group", help="inspect and manage one group (id or unique prefix)")
    gs = g.add_subparsers(dest="group_cmd", required=True)
    for name, h in (("show", "details"), ("star", "star"), ("unstar", "unstar"),
                    ("trash", "soft delete (files kept)"), ("restore", "undo trash"),
                    ("rm", "PERMANENTLY delete files + rows")):
        sp = gs.add_parser(name, help=h)
        sp.add_argument("id")
        if name == "rm":
            sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        common(sp)
    sp = gs.add_parser("rename", help="set the title ('' reverts to the original)")
    sp.add_argument("id")
    sp.add_argument("title")
    common(sp)
    sp = gs.add_parser("note", help="set a note ('' clears)")
    sp.add_argument("id")
    sp.add_argument("text")
    common(sp)
    for name in ("tag", "untag"):
        sp = gs.add_parser(name, help=f"{name} a group with user tags (source-name tags are automatic and cannot be removed)")
        sp.add_argument("id")
        sp.add_argument("tags", nargs="+")
        common(sp)
    g.set_defaults(fn=cmd_group)

    pr = sub.add_parser("prune", help="delete old groups (dry run unless --yes)")
    pr.add_argument("--older-than", metavar="AGE", help="e.g. 30d, 12h, 2w (age of the group's newest render)")
    pr.add_argument("--keep-last", type=int, metavar="N", help="always keep the N newest groups")
    pr.add_argument("--project", help="only groups entirely within this project")
    pr.add_argument("--keep-starred", action=argparse.BooleanOptionalAction, default=True)
    pr.add_argument("--dry-run", action="store_true", help="show what would go (the default)")
    pr.add_argument("--yes", action="store_true", help="really delete")
    common(pr)
    pr.set_defaults(fn=cmd_prune)

    t = sub.add_parser("trash", help="manage the trash")
    ts = t.add_subparsers(dest="trash_cmd", required=True)
    e = ts.add_parser("empty", help="PERMANENTLY delete all trashed groups")
    e.add_argument("--yes", action="store_true")
    common(e)
    t.set_defaults(fn=cmd_trash)
