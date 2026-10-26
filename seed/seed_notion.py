"""Create the demo task board in Notion.

    python -m seed.seed_notion --dry-run
    python -m seed.seed_notion                       # needs NOTION_TOKEN + NOTION_PARENT_PAGE_ID
    python -m seed.seed_notion --people siddhi=<notion-user-uuid>,aarav=<uuid>,...

Creates a database "CampusEats Tasks" under NOTION_PARENT_PAGE_ID (share that page with your
integration first) with Name, Status, Assignee, Due, GitHub Issue, and one page per task.
If every demo member is mapped to a real Notion user with --people, Assignee is a People
property; otherwise it's a Select holding the demo handle (ingestion reads both).

Notion can't backdate edits, so "last edited" will be now. The stale-plan discrepancy (D4)
therefore only shows up in the offline data; in the live board it's still a status conflict.
Prints NOTION_DATABASE_ID for your .env.
"""
from __future__ import annotations

import argparse

from seed import scenario as S
from seed.live_common import parse_map, save_run
from teampulse.config import env

STATUSES = [("Not started", "gray"), ("In progress", "blue"), ("Done", "green"), ("Dropped", "red")]


def page_properties(t: dict, people: dict, issue_numbers: dict) -> dict:
    props = {
        "Name": {"title": [{"text": {"content": t["name"]}}]},
        "Status": {"select": {"name": t["status"]}},
        "Due": {"date": {"start": t["due"]}},
    }
    if t["issue"]:
        props["GitHub Issue"] = {"number": int(issue_numbers.get(str(t["issue"]), t["issue"]))}
    if people:
        props["Assignee"] = {"people": [{"id": people[t["assignee"]]}]}
    else:
        props["Assignee"] = {"select": {"name": t["assignee"]}}
    return props


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", help="demo handle → Notion user id, for all four members")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    people = parse_map(a.people)
    if people and set(people) != set(S.MEMBERS):
        raise SystemExit(f"--people must map all of: {', '.join(S.MEMBERS)}")

    from seed.live_common import load_run
    issue_numbers = {str(k): v for k, v in load_run("github").get("issue_numbers", {}).items()}

    if a.dry_run:
        for t in S.NOTION_TASKS:
            print(t["name"], page_properties(t, people, issue_numbers))
        return

    from notion_client import Client

    token, parent = env("NOTION_TOKEN"), env("NOTION_PARENT_PAGE_ID")
    if not token or not parent:
        raise SystemExit("Set NOTION_TOKEN and NOTION_PARENT_PAGE_ID in .env")
    client = Client(auth=token)

    schema = {
        "Name": {"title": {}},
        "Status": {"select": {"options": [{"name": n, "color": c} for n, c in STATUSES]}},
        "Assignee": {"people": {}} if people else
                    {"select": {"options": [{"name": h} for h in S.MEMBERS]}},
        "Due": {"date": {}},
        "GitHub Issue": {"number": {"format": "number"}},
    }
    title = [{"type": "text", "text": {"content": "CampusEats Tasks"}}]
    parent_obj = {"type": "page_id", "page_id": parent}
    if hasattr(client, "data_sources"):  # API 2025-09-03
        db = client.databases.create(parent=parent_obj, title=title, initial_data_source={"properties": schema})
        ds_id = db["data_sources"][0]["id"]
        page_parent = {"type": "data_source_id", "data_source_id": ds_id}
    else:
        db = client.databases.create(parent=parent_obj, title=title, properties=schema)
        ds_id = None
        page_parent = {"database_id": db["id"]}

    pages = {}
    for t in S.NOTION_TASKS:
        p = client.pages.create(parent=page_parent, properties=page_properties(t, people, issue_numbers))
        pages[t["key"]] = p["id"]
        print("page", t["name"], "→", p["url"])

    path = save_run("notion", {"database_id": db["id"], "data_source_id": ds_id, "pages": pages})
    print(f"\nDone. Add to .env:\nNOTION_DATABASE_ID={db['id']}\n(mapping saved to {path})")


if __name__ == "__main__":
    main()
