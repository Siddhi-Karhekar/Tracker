# TeamPulse

A multi-agent tracker that reads a student team's **GitHub**, **Notion** and **Discord**, finds where they
disagree, pulls blockers out of casual Hinglish chat, and builds an evidence-backed contribution report.
Everything runs on free tools.

```
                 ┌──────────────────────┐
User question ─► │  Supervisor Agent    │ ◄─ Memory (SQLite)
                 └──────────┬───────────┘
        ┌──────────┬────────┼──────────┬──────────────┐
        ▼          ▼        ▼          ▼              ▼
   Retrieval   Blocker   Discrepancy  Contribution  Verifier
        └──────────┴────────┬──────────┴──────────────┘
                            ▼
              Claims DB (SQLite) + ChromaDB
                            ▲
       Ingestion pipeline (plain code, not an agent)      ◄── built so far
         GitHub · Notion · Discord bot
```

## Status

| Week | Goal | State |
|---|---|---|
| 1 | Seeded demo with planted discrepancies + Hinglish blockers, answer key | ✅ `seed/`, `data/demo/` |
| 2 | Ingestion: GitHub + Notion pollers, Discord bot (live + backfill) → SQLite | ✅ `teampulse/ingest/` |
| 3 | Retrieval Agent + cited Q&A in Streamlit | next |
| 4–10 | Supervisor, Blocker, Discrepancy, Contribution, Verifier agents; eval; deploy | |

## Quick start (no accounts needed)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m teampulse.ingest.run demo     # loads data/demo/* into data/demo.db
pytest                                  # 24 tests: answer key is true, ingestion is correct
```

## The seeded demo (Week 1)

A fictional 4-person team (Siddhi, Aarav, Priya, Rohan) builds **CampusEats**, a canteen pre-order app,
from 28 Sep to 26 Oct 2026: 12 issues, 7 PRs, 13 Notion tasks, 62 Discord messages in Hinglish across
5 channels (`general`, `backend`, `frontend`, `ml-and-data`, `standup`).

The whole scenario lives in one file, [`seed/scenario.py`](seed/scenario.py). `python -m seed.build`
turns it into files shaped exactly like the real APIs, so the same ingestion code reads both:

| File | Shape |
|---|---|
| `data/demo/github/{issues,pulls,commits}.json` | GitHub REST (issue + comments + events, PR + reviews + commits) |
| `data/demo/notion/tasks.json` | Notion `data_sources.query` result (API 2025-09-03) |
| `data/demo/discord/<channel>.json` | DiscordChatExporter JSON |
| `data/demo/labels/discord_messages.csv` | Gold label per message, the first 62 of your ~300 |
| `data/demo/answer_key.json` | Discrepancies, blockers, entity links, chat claims, eval questions |

### Planted discrepancies

| ID | Type | Task | What's wrong | Visible without reading chat? |
|---|---|---|---|---|
| D1 | status | Login (#1) | Notion "Done"; issue reopened; Discord: "login wala page phir se crash ho raha hai" | yes |
| D2 | owner | Payment (#3) | Assigned to Rohan; Priya wrote PR #17 and said "payment wala kaam maine utha liya" | yes |
| D3 | deadline | Tracking (#4) | Notion 30 Oct, milestone 5 Nov, Discord "2nd Nov tak" | yes |
| D4 | stale plan | Reco model (#5) | Notion "In progress", untouched since 1 Oct; PR merged 17 Oct | yes |
| D5 | status | Notifications (#7) | Rohan says "done" twice; no PR, no commits, issue open | **no** |
| D6 | stale plan | Admin dashboard (#8) | Dropped in Discord on 22 Oct; still live in Notion and GitHub | **no** |
| D7 | owner | Cart UI (#10) | Assigned to Aarav; Siddhi built it (PR #18) | yes |
| D8 | deadline | Report (#12) | Notion 10 Nov; faculty said 8 Nov (Discord) | **no** |

Tasks #2, #6, #9, #11 and "Viva preparation" are consistent everywhere: anything flagged there is a false
alarm. D5, D6 and D8 can only be found by understanding the Hinglish chat, which is what the Blocker Agent
adds over a plain rule engine, and a ready-made argument for your evaluation.

Chat labels use the plan's five classes plus **`solution`** (a reply that unblocks someone), because the
Contribution Agent needs to credit people who help: Siddhi gave 6 solutions, Rohan and Priya 1 each.

## Ingestion (Week 2)

Everything becomes rows in `events` (one per issue, PR, review, commit, comment, Notion page, message).
GitHub and Notion also produce **claims** directly, since their fields are already structured:

| Source | Claims |
|---|---|
| GitHub issue | `status` (open / done / dropped, timed at the last close or reopen), `owner` per assignee, `deadline` from milestone |
| GitHub PR | `owner` = PR author and `status` = done (merged) / in_progress (open), for every issue it `Closes #n` |
| Notion page | `status`, `owner`, `deadline`, all stamped with `last_edited_time` (that's how stale plans show up) |
| Discord | none yet: meaning comes from the Blocker Agent (Week 5). `#17` in a message links it to whatever PR #17 closes. |

```bash
python -m teampulse.ingest.run github              # poll once (incremental, cursor in sync_state)
python -m teampulse.ingest.run notion
python -m teampulse.ingest.run all --watch 300     # keep polling every 5 minutes
python -m teampulse.ingest.discord_bot             # live listener; backfills missed history on start
python -m teampulse.ingest.run discord-export exports/   # or load DiscordChatExporter files
python -m teampulse.ingest.run stats
```

### Setting up the real sources

1. **`.env`**: copy `.env.example` to `.env` and fill it in. **`team.toml`**: one block per teammate;
   `python -m teampulse.ingest.whoami` prints GitHub logins and Notion user ids.
2. **GitHub**: fine-grained token with read access to Issues, Pull requests, Contents.
3. **Notion**: create an internal integration, share the task database with it, and put the database id in
   `NOTION_DATABASE_ID`. Property names default to `Status`, `Assignee`, `Due`, `GitHub Issue`; override with
   `NOTION_PROP_*` if yours differ.
4. **Discord**: Developer Portal → New Application → Bot → turn on **Message Content Intent** → invite with
   *View Channels* + *Read Message History* (+ *Send Messages* for command replies). Set `DISCORD_BOT_TOKEN`,
   `DISCORD_GUILD_ID`, `DISCORD_CHANNELS`.

### Live demo with the seeded scenario

To show the integrations working on the demo data, push it into a throwaway repo, Notion page and server:

```bash
python -m seed.seed_github --repo you/teampulse-demo --as siddhi=Siddhi-Karhekar
python -m seed.seed_notion
python -m seed.seed_discord          # one webhook per channel in DISCORD_WEBHOOKS
DISCORD_ACCEPT_WEBHOOKS=1 TEAMPULSE_TEAM=data/demo/team.toml python -m teampulse.ingest.discord_bot
```

Each script supports `--dry-run`. Real platforms stamp everything "now" and attribute actions to the
token owner (set `GITHUB_TOKEN__<HANDLE>` per teammate to fix that for GitHub), so **evaluate on the offline
files**, and use the live copy to show ingestion working.

## Privacy

- The bot reads only the configured server and channels and stores data locally in SQLite.
- `!teampulse status` explains what's stored; `!teampulse optout` stops storage and **deletes** that
  person's stored messages; deleted Discord messages are removed from the database too.
- Use hosted free LLM tiers (Groq / Gemini) only on the seeded demo data; run Ollama locally for real team chat.
- Everyone in the server should agree before the bot is added; delete `data/` when the project ends.

## Layout

```
seed/            scenario.py (single source of truth), build.py, seed_{github,notion,discord}.py
data/demo/       generated demo data + answer key (committed)
teampulse/
  config.py      .env + team.toml
  db/            schema.sql (members, tasks, events, claims, sync_state, memory, optouts)
  ingest/        github_ingest, notion_ingest, discord_ingest, discord_bot, run, whoami
tests/           answer-key consistency, ingestion, live-path checks
```
