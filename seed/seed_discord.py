"""Post the demo conversation into a real Discord server, one webhook per channel.

    python -m seed.seed_discord --dry-run
    python -m seed.seed_discord                     # needs DISCORD_WEBHOOKS in .env

Setup: create channels general, backend, frontend, ml-and-data, standup. For each:
Edit Channel → Integrations → Webhooks → New Webhook → Copy URL. Then in .env:
  DISCORD_WEBHOOKS=general=https://discord.com/api/webhooks/...,backend=https://...,...

Each message is posted with the demo member's discord_name as the webhook username, so the
bot maps it to the right person (start the bot with DISCORD_ACCEPT_WEBHOOKS=1 and
TEAMPULSE_TEAM=data/demo/team.toml). Limits: Discord stamps every message "now", and
webhooks can't create reply links, so replies are posted as plain messages. The mapping
from seed keys to real message ids is saved for evaluation.
"""
from __future__ import annotations

import argparse
import time

import requests

from seed import scenario as S
from seed.live_common import parse_map, save_run
from teampulse.config import env


def post(url: str, payload: dict) -> dict:
    while True:
        r = requests.post(url, params={"wait": "true"}, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        r.raise_for_status()
        return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=0.8, help="seconds between messages")
    a = ap.parse_args()
    hooks = parse_map(env("DISCORD_WEBHOOKS"))
    msgs = sorted(S.MESSAGES, key=lambda m: m["ts"])

    missing = sorted({m["channel"] for m in msgs} - set(hooks))
    if a.dry_run or missing:
        for m in msgs:
            print(f"#{m['channel']:12} {S.MEMBERS[m['author']]['discord_name']:12} {m['text']}")
        if missing and not a.dry_run:
            raise SystemExit(f"\nNo webhook for: {', '.join(missing)} (set DISCORD_WEBHOOKS)")
        return

    ids = {}
    for m in msgs:
        member = S.MEMBERS[m["author"]]
        res = post(hooks[m["channel"]], {
            "content": m["text"],
            "username": member["discord_name"],
            "allowed_mentions": {"parse": []},  # don't ping anyone
        })
        ids[m["key"]] = res["id"]
        print(f"#{m['channel']} {member['discord_name']}: {m['text'][:60]}")
        time.sleep(a.delay)
    path = save_run("discord", {"message_ids": ids})
    print(f"Done: {len(ids)} messages. Mapping saved to {path}.")


if __name__ == "__main__":
    main()
