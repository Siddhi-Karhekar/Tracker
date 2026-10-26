"""Print the platform IDs you need for team.toml.

    python -m teampulse.ingest.whoami

GitHub: collaborators of GITHUB_REPO.  Notion: workspace users visible to the integration.
Discord: authors the bot has already stored (run the bot once first), or right-click a user
in Discord with Developer Mode on → Copy User ID.
"""
from __future__ import annotations

from teampulse import db
from teampulse.config import env


def main() -> None:
    if env("GITHUB_TOKEN") and env("GITHUB_REPO"):
        from github import Auth, Github

        repo = Github(auth=Auth.Token(env("GITHUB_TOKEN"))).get_repo(env("GITHUB_REPO"))
        print("GitHub collaborators:")
        for u in repo.get_collaborators():
            print(f"  github = \"{u.login}\"   ({u.name or ''})")
    if env("NOTION_TOKEN"):
        from notion_client import Client

        print("Notion users:")
        for u in Client(auth=env("NOTION_TOKEN")).users.list()["results"]:
            if u.get("type") == "person":
                print(f"  notion_user_id = \"{u['id']}\"   ({u.get('name')})")
    conn = db.connect()
    rows = conn.execute("SELECT DISTINCT author_raw FROM events WHERE source='discord' ORDER BY 1").fetchall()
    if rows:
        print("Discord authors seen by the bot  (name (id)):")
        for r in rows:
            print("  ", r[0])


if __name__ == "__main__":
    main()
