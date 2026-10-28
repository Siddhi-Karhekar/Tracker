"""Small admin CLI.

    python -m teampulse.cli stats
    python -m teampulse.cli recent --limit 15
    python -m teampulse.cli members
    python -m teampulse.cli link --name Siddhi --discord 1234567890 --github siddhi-k --notion <user-uuid>
"""
import argparse
import json

from teampulse import db


def stats(conn, _args):
    rows = conn.execute(
        "SELECT source, kind, COUNT(*) AS n, MAX(updated_at) AS latest FROM events GROUP BY source, kind ORDER BY source, kind"
    ).fetchall()
    if not rows:
        print("No events yet. Run the pollers and the Discord bot first.")
    for r in rows:
        print(f"{r['source']:8} {r['kind']:14} {r['n']:6}   latest {r['latest']}")


def recent(conn, args):
    rows = conn.execute(
        "SELECT source, kind, author_name, title, body, status, updated_at FROM events "
        "ORDER BY updated_at DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for r in rows:
        text = (r["title"] or r["body"] or "").replace("\n", " ")[:80]
        status = f" [{r['status']}]" if r["status"] else ""
        print(f"{r['updated_at']}  {r['source']}/{r['kind']}{status}  {r['author_name']}: {text}")


def members(conn, _args):
    for r in conn.execute("SELECT * FROM members ORDER BY id"):
        print(json.dumps(dict(r)))


def link(conn, args):
    """Record that one person uses these identities across tools (needed for contribution reports)."""
    with conn:
        row = conn.execute(
            "SELECT id FROM members WHERE discord_id = ? OR github_login = ? OR notion_user_id = ? LIMIT 1",
            (args.discord, args.github, args.notion),
        ).fetchone()
        if row is None:
            cur = conn.execute("INSERT INTO members (display_name) VALUES (?)", (args.name,))
            member_id = cur.lastrowid
        else:
            member_id = row["id"]
        conn.execute(
            "UPDATE members SET display_name = ?, discord_id = COALESCE(?, discord_id), "
            "github_login = COALESCE(?, github_login), notion_user_id = COALESCE(?, notion_user_id) WHERE id = ?",
            (args.name, args.discord, args.github, args.notion, member_id),
        )
    print(f"Linked member #{member_id} ({args.name}).")


def main():
    parser = argparse.ArgumentParser(prog="teampulse")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats").set_defaults(func=stats)
    p = sub.add_parser("recent")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=recent)
    sub.add_parser("members").set_defaults(func=members)
    p = sub.add_parser("link")
    p.add_argument("--name", required=True)
    p.add_argument("--discord")
    p.add_argument("--github")
    p.add_argument("--notion")
    p.set_defaults(func=link)
    args = parser.parse_args()
    args.func(db.connect(), args)


if __name__ == "__main__":
    main()
