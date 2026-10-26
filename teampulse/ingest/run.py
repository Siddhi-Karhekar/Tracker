"""Ingestion CLI.

    python -m teampulse.ingest.run demo              # rebuild data/demo.db from the seeded demo files
    python -m teampulse.ingest.run github            # poll GitHub once (incremental)
    python -m teampulse.ingest.run notion            # poll Notion once (incremental)
    python -m teampulse.ingest.run all --watch 300   # poll GitHub + Notion every 5 min
    python -m teampulse.ingest.run discord-export path/to/exports/   # load DiscordChatExporter JSON
    python -m teampulse.ingest.run stats [--db data/demo.db]

The live Discord bot runs separately:  python -m teampulse.ingest.discord_bot
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from teampulse import db
from teampulse.config import ROOT, env, load_team
from teampulse.ingest import discord_ingest, github_ingest, notion_ingest

DEMO = ROOT / "data" / "demo"


def run_demo(db_file: Path) -> None:
    if db_file.exists():
        db_file.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{db_file}{suffix}").unlink(missing_ok=True)
    team = load_team(DEMO / "team.toml")
    conn = db.connect(db_file)
    db.sync_members(conn, team)
    print("github :", github_ingest.load_offline(conn, team, DEMO / "github"))
    print("notion :", notion_ingest.load_offline(conn, team, DEMO / "notion" / "tasks.json"))
    print("discord:", discord_ingest.load_offline(conn, team, DEMO / "discord"))
    print("linked :", discord_ingest.link_hash_refs(conn), "Discord messages linked to tasks via #refs")
    stats(conn)


def poll_once(conn, team, which: str, snapshot: bool) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = (ROOT / "data" / "live" / stamp) if snapshot else None
    if which in ("github", "all"):
        token, repo = env("GITHUB_TOKEN"), env("GITHUB_REPO")
        if not token or not repo:
            sys.exit("Set GITHUB_TOKEN and GITHUB_REPO in .env")
        print("github :", github_ingest.poll(conn, team, token, repo, snap / "github" if snap else None))
    if which in ("notion", "all"):
        token, dbid = env("NOTION_TOKEN"), env("NOTION_DATABASE_ID")
        if not token or not dbid:
            sys.exit("Set NOTION_TOKEN and NOTION_DATABASE_ID in .env")
        print("notion :", notion_ingest.poll(conn, team, token, dbid, snap / "notion" if snap else None))
    print("linked :", discord_ingest.link_hash_refs(conn))


def stats(conn) -> None:
    print("\nevents by source/type:")
    for r in conn.execute("SELECT source, type, COUNT(*) n FROM events GROUP BY 1,2 ORDER BY 1,2"):
        print(f"  {r['source']:8} {r['type']:16} {r['n']}")
    print("tasks :", conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    print("claims:", ", ".join(f"{r[0]}/{r[1]}={r[2]}" for r in conn.execute(
        "SELECT source, attribute, COUNT(*) FROM claims GROUP BY 1,2 ORDER BY 1,2")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["demo", "github", "notion", "all", "discord-export", "stats"])
    ap.add_argument("path", nargs="?", help="folder of DiscordChatExporter JSON files (discord-export)")
    ap.add_argument("--db", help="SQLite file (default: data/demo.db for demo, else TEAMPULSE_DB)")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="keep polling every N seconds")
    ap.add_argument("--snapshot", action="store_true", help="also save raw API responses under data/live/")
    a = ap.parse_args()

    if a.command == "demo":
        run_demo(Path(a.db) if a.db else ROOT / "data" / "demo.db")
        return

    conn = db.connect(a.db)
    team = load_team()
    db.sync_members(conn, team)
    if a.command == "stats":
        stats(conn)
    elif a.command == "discord-export":
        if not a.path:
            sys.exit("Give the folder with exported .json files")
        print("discord:", discord_ingest.load_offline(conn, team, a.path))
        print("linked :", discord_ingest.link_hash_refs(conn))
    else:
        while True:
            poll_once(conn, team, a.command, a.snapshot)
            if not a.watch:
                break
            time.sleep(a.watch)


if __name__ == "__main__":
    main()
