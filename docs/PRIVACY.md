# Privacy and data handling

TeamPulse runs on the team's own machine. Nothing is sent anywhere except to the AI provider the team chose
(Ollama keeps it on the machine) and, if switched on, LangSmith for evaluation.

## What is stored, and how sensitive it is

| Class | Examples | Where | Who can see it |
|---|---|---|---|
| **Secret** | API keys, password hashes, two-factor keys, session and MCP token hashes, backup key | `.env` (keys); `users`, `sessions`, `api_tokens` (hashes) | Nobody through the app. Keys are never shown, logged or sent to the AI. |
| **Personal** | Discord messages, names, emails or phone numbers in chat | `events`, derived tables, search index | Signed-in team members |
| **Assessment** | Contribution reports, approvals, check-ins | `contribution_reviews`, `pm_progress_updates` | The member and leaders; the team only after the member approves |
| **Team** | Tasks, deadlines, issues, commits, pages | `pm_*`, `events` | Signed-in team members |
| **Operational** | Activity log, alerts, AI cache | `audit_log`, `pm_alerts`, `llm_cache` | Leaders (activity log); system |

## What the AI receives

Every request passes through `LLM.chat`:

1. API keys and tokens are replaced with `[redacted]`.
2. Email addresses and phone numbers are replaced (`TEAMPULSE_AI_REDACT_PII=on`, the default).
3. Text that tries to instruct the AI is marked as data (see [AI_SECURITY.md](AI_SECURITY.md)).
4. The activity log records how many items were redacted, never the items themselves.

## How long things are kept

| Data | Kept | Setting |
|---|---|---|
| Activity log | 180 days | `TEAMPULSE_AUDIT_DAYS` |
| Cached AI answers | 30 days | `TEAMPULSE_CACHE_DAYS` |
| Ended sessions | a few days after ending | fixed |
| Pending two-factor sign-ins | 5 minutes | fixed |
| Backups | newest 14 | `TEAMPULSE_BACKUP_KEEP` |
| Team data | until deleted at the source or the person opts out | |

`python -m teampulse.maintenance purge` applies these; the poll loop runs it daily.

## Opting out and deletion

- **Discord opt-out** (`!teampulse leave` in Discord): deletes the person's messages and every copy made from them: extracted
  claims, labels, task links, link feedback and the search index entries. Cached AI answers can't be traced to a
  person (they're keyed by a hash), so the whole cache is cleared.
- **A message deleted in Discord** is deleted here with its copies as soon as the bot sees the deletion.
- **Backups** made before the deletion still contain it until they rotate out (14 days by default). Delete older
  backups by hand if someone asks for immediate erasure.
- **Deactivating an account** signs the person out everywhere and revokes their AI assistant connection tokens; their history stays,
  because removing it would change other people's contribution evidence.

## Consent

The team leader connects the sources, so the team should agree before TeamPulse reads their chat. Tell members
that the AI summarises what they write, that contribution reports are generated from their activity, and that
they can opt out of Discord reading at any time.

People's messages are only stored after they type `!teampulse join`, and only from then on. The Security page shows
how many opted-out people still have messages stored (it should always be 0).
