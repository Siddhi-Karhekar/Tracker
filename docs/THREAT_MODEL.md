# TeamPulse threat model

TeamPulse is one install per team: a Streamlit app, a SQLite file and a poll loop that reads the team's GitHub,
Notion and Discord. It shows an AI assistant that data. This file lists what is worth protecting, who might attack
it, where trust changes hands, and for each threat the control that stops it and the test that proves the control
works. A control without a test isn't listed as a control.

## What is worth protecting

| Asset | Why it matters |
|---|---|
| Contribution reports and approvals | Used for grading and peer review. Faking or quietly changing one is the most likely insider goal. |
| Team chat, issues, pages | Private conversation between students; some of it is personal. |
| API keys (GitHub, Notion, Discord, Groq/Gemini) | Read access to the team's real accounts. |
| Accounts, sessions, MCP tokens | Whoever holds them acts as that person. |
| Activity log | The record of who did what; worthless if it can be edited. |
| Tasks, deadlines, progress | Planning data; tampering hides missed work. |
| Backups | A full copy of everything above. |

## Who might attack it

1. **A teammate (insider).** Has an account. Wants to inflate their own contribution, hide someone else's, read
   things they shouldn't, or undo a leader's decision.
2. **Anyone who can write text TeamPulse reads.** On a public repo that's anyone on the internet (an issue or a
   PR comment). They can't sign in, but they can put instructions in front of the AI.
3. **Someone with a stolen password or session.** Phished, reused, or read over a shoulder.
4. **Someone with the laptop or a backup file.** Has the disk, not an account.
5. **A compromised dependency or CI action.** Code running inside the build or the app.
6. **The AI model itself, being wrong.** Not malicious, but it invents links, names and claims.

## Trust boundaries

```
 GitHub / Notion / Discord  ──(untrusted text)──►  poll loop ──► SQLite ◄── Streamlit pages ◄── browser (signed in)
                                                                  │                      ▲
                                                                  ▼                      │
                                                    LLM.chat (redact, isolate) ──► AI provider (Ollama local / cloud)
                                                                  │
                                                         guardrails (output checks) ──► page
 MCP client (AI assistant) ──(token)──► mcp_server ──► same actions + permission checks
```

Every crossing has one choke point: all reads and writes go through `teampulse/actions.py` and `pm.py` (permission
checks); every AI request goes through `LLM.chat`; every AI answer goes through `guardrails.apply`.

## Threats (STRIDE) and controls

| # | Threat | Control | Test |
|---|---|---|---|
| S1 | Password guessing | scrypt hashes, lockout per account, also counts wrong two-factor codes | `test_auth.py`, `test_concurrency.py` (parallel guesses), `test_identity.py` |
| S2 | Stolen password | Optional two-factor sign-in (TOTP), required for leaders with `TEAMPULSE_MFA=leaders` | `test_identity.py` |
| S3 | Replayed code | Last accepted time step stored; a used code is refused | `test_identity.py` (two-step sign-in) |
| S4 | Stolen session cookie used elsewhere | Session bound to the browser's user agent; a change revokes it and is logged | `test_identity.py` (session moving to another browser) |
| S5 | Forgotten open session | Idle timeout, maximum age, "Sign out" for other sessions on My account | `test_auth.py`, `test_identity.py` |
| S6 | Stolen MCP token | Hashed at rest, shown once, scoped to its owner's permissions, revocable, refused if the owner still needs two-factor | `test_mcp.py` |
| T1 | Member edits data they don't own | Deny by default, `auth.authorize` with resource rules, every action covered (AST scan, CI) | `test_security.py`, `test_auth.py` (16 admin actions) |
| T2 | Lost update between two users | Optimistic locking (version column) | `test_concurrency.py`, `test_pm.py` |
| T3 | Evidence changed after a report was approved | Approval stores a digest of the evidence; a change lapses it and tells the member | `test_identity.py` (approval lapses) |
| T4 | Activity log edited or truncated | Hash chain from a stored head; checked on the Security page | `test_security.py` (log chain, truncation) |
| T5 | Backup edited | Fernet authenticated encryption; verified on creation and restore | `test_security.py` |
| T6 | SQL injection | Parameterised queries; column names from allowlists (`TASK_COLUMNS`); Bandit in CI | `test_pm.py`, CI `security` job |
| R1 | "I didn't do that" | Every change and every refusal is in the activity log with the actor | `test_auth.py`, `test_security.py` |
| I1 | Key in the repo or history | `scripts/scan_secrets.py` on all files and full history, in CI | `test_security.py` |
| I2 | Key or personal details sent to a cloud AI | Redaction in `LLM.chat` (keys always; emails and phone numbers by default) | `test_security.py` |
| I3 | AI answer leaks contact details, keys or its instructions | Output guardrails | `test_guardrails.py` |
| I4 | Opted-out person's messages kept | Deletion includes derived tables, the search index and the AI cache | `test_privacy.py` |
| I5 | XSS through team text | Escaped everywhere; attack strings rendered on every page | `test_xss.py` |
| I6 | Plain copy of the data on disk via backups | Backups encrypted in memory; no plaintext temp file | `test_security.py` |
| D1 | AI quota or cost exhausted | Per-person hourly limit, max tokens, response cache | `test_llm_safety.py` |
| D2 | Login flooding | Lockout; pending two-factor sign-ins expire in 5 minutes | `test_identity.py` |
| E1 | Member becomes leader | Roles change only through leader actions with recent password (and code) confirmation; some capabilities can never be granted | `test_security.py` (grants) |
| E2 | Prompt injection makes the AI act | The AI has no write tools; permissions never depend on AI output; injected text is flagged and isolated | `test_guardrails.py`, `test_security.py`, `test_llm_safety.py` |
| E3 | Compromised CI action | Actions pinned to commit SHAs, read-only token, Dependabot updates, CodeQL | `test_supply_chain.py` |

## Accepted risks

- **The machine owner can read everything.** SQLite isn't encrypted at rest; use disk encryption (BitLocker,
  FileVault, LUKS). Backups are encrypted.
- **Injection detection uses patterns.** A new phrasing gets past the detector. That's why it isn't the control
  that matters: the AI can't change anything, and its output is checked.
- **A leader is trusted.** There's one team per install and leaders manage it. Their actions are logged and need a
  recent confirmation, but a leader acting in bad faith isn't prevented.
- **Cloud AI providers see the prompt.** Redaction removes keys and contact details, not the substance of the chat.
  For real team data use Ollama (local).
