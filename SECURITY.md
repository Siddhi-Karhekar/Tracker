# Security in TeamPulse

TeamPulse reads a student team's GitHub, Notion and Discord. Anyone on the team, and anyone who can open an
issue on a public repo, controls some of the text it shows, so that text is treated as hostile everywhere.

This file covers how accounts and roles work, then goes through two common pre-launch checklists item by item:
what was fixed, what was already handled, and what doesn't apply (and why). The full audit, with every finding,
its test and the risks that remain, is in [AUDIT.md](AUDIT.md).

## Accounts and roles

| | Team leader | Team member |
|---|---|---|
| Sign in / sign out, change own password | ✅ | ✅ |
| Open pages | All | Only the pages the leader allows (default: everything except Data sources) |
| Ask questions | ✅ | ✅ (rate limited) |
| Create accounts, reset passwords, activate/deactivate | ✅ | ❌ |
| Refresh data, rebuild tasks, read chat, compare sources | ✅ | ❌ |
| Give verdicts, fix chat links, label chat | ✅ | ❌ |
| Add or remove team memory | ✅ | ❌ (even when asked through the chat box) |
| Data sources page, connection check, evaluation | ✅ | ❌ |
| Approve or dispute **their own** contribution report | ✅ | ✅ (`TEAMPULSE_CONTRIB_REVIEW=off` removes it) |
| Project management: create projects and tasks, assign, edit deadlines, approve requests, mark Completed, archive | ✅ | ❌ |
| Project management: **tasks assigned to them**: status up to Under review, progress, subtasks, blockers, deadline requests | ✅ | ✅ |
| Comment on any task | ✅ | ✅ (rate limited) |
| My work (own workspace), Project board | ✅ (any workspace) | ✅ (own workspace only) |
| Team dashboard | ✅ | ❌ |

Members may change only two things: their own contribution report and the tasks assigned to them. A
contribution report about a person should not be visible to the team until that person has seen it. And the
person doing the work is the one who knows its status. Both rules check *whose* item it is on the server, and
both are logged. Project-management checks live in `teampulse/pm.py` (full matrix in
[docs/TEAMPULSE_2.md](docs/TEAMPULSE_2.md#4-permission-matrix)). Each person is limited to 120 project changes
per 10 minutes, and two people saving the same task at once can't overwrite each other: the later save is
refused and asked to reload.

**Where it's enforced.** Streamlit runs every page on the server; there is no separate API a browser could call.
Every change goes through `teampulse/actions.py` (or `teampulse/pm.py` for projects and tasks), and each function there checks the signed-in account with
`auth.require()` *before* touching the database. Hidden buttons are a convenience, not the protection: the tests
call every admin action directly as a member and check that each one is refused, logged, and changes nothing.

**Accounts.** Only a leader creates accounts. TeamPulse generates the username (or the leader picks one) and a
one-time password: 14 random characters, shown once, never stored. The person must choose their own password
at first sign-in before any page opens. A leader who resets a password gets a new one-time password and the
person is signed out everywhere. There is no public sign-up page.

**The first leader** is created from the server: `python -m teampulse.auth create-leader --name "Your Name"`, or
on Streamlit Cloud from the sign-in page with `TEAMPULSE_SETUP_CODE` (remove the code afterwards). Either only
works while no leader exists.

**Passwords** are stored as scrypt hashes with a random 16-byte salt per password (Python's standard library;
n=2¹⁴, r=8, p=1) and compared in constant time. Rules: 10–128 characters, not a common password, at least 5
different characters, and not containing the person's name or username.

**Sessions.** Signing in creates a random 256-bit token. Only its SHA-256 hash is stored in the database; the
token itself lives in the server-side Streamlit session, never in the browser's local storage or a readable
cookie. Sessions end after 60 idle minutes or 12 hours, and immediately on sign-out, deactivation, password reset
or password change. Every page load re-checks the session against the database, so a deactivated person loses
access on their next click. (Trade-off: refreshing the browser tab signs you out.)

**Sign-in errors** are the same whether the username exists or not ("That username and password don't match"),
and the same hashing time is spent on unknown usernames, so accounts can't be discovered. After 5 failed attempts
a username is locked for 15 minutes, including usernames that don't exist.

**Activity log** (Team & access → Activity): sign-ins, failed and refused attempts, account changes and data
edits, with who and when. Passwords and message text are never logged.

## Checklist 1: "20 ways to get your vibe-coded app hacked"

| # | Risk | Status in TeamPulse |
|---|---|---|
| 1 | `.env` in GitHub | **Handled.** `.env`, `.streamlit/secrets.toml`, databases (`*.db`) and the vector index are in `.gitignore`. Only `.env.example` with empty values is committed. |
| 2 | API keys in the frontend | **Handled.** All keys are read on the server (`teampulse/config.py`). Streamlit sends rendered pages, not code or keys, to the browser. Keys are never displayed; Data sources only shows "set / not set". |
| 3 | No row-level security | **Not applicable as such.** The browser never talks to the database; there's no Supabase/Firebase client. The equivalent, server-side permission checks per action and per record, is in `actions.py`. |
| 4 | Frontend-only permissions | **Fixed.** Every write checks the role on the server (`auth.require`). Pages check access on the server (`require_page`). Tested by calling admin actions directly as a member. |
| 5 | No rate limiting | **Fixed.** Sign-in, password changes and the setup code: 5 failures then a 15-minute lock, counted atomically so parallel guesses can't dodge it. AI questions: 60 per person per hour (`TEAMPULSE_ASK_PER_HOUR`), and every AI reply is capped (`TEAMPULSE_LLM_MAX_TOKENS`), which also protects your free quota. |
| 6 | SQL string concatenation | **Handled.** Every query uses `?` placeholders. The few f-strings in SQL only insert fixed column lists or placeholder counts written in the code, never user input. |
| 7 | No input validation | **Fixed.** Names, usernames (`^[a-z][a-z0-9._-]{2,31}$`), roles, passwords, labels, memory facts, review notes and page lists are validated and length-limited on the server; form fields also have `max_chars`. |
| 8 | User content as raw HTML | **Fixed.** Every value from GitHub, Notion, Discord or a person goes through `ui.e()`, which escapes HTML *and* Markdown (so hostile text can't become a link, image, or Streamlit colour/LaTeX directive). Links only allow `http(s)://` (`ui.url`), so `javascript:` links become dead links. In AI answers, embedded images are dropped and links the AI writes are shown as plain "label (address)", so a chat message can't turn into a disguised phishing link; only TeamPulse's own citation links are clickable. `tests/test_xss.py` fills every table with attack strings and renders all 10 pages. |
| 9 | Plain-text passwords | **Fixed.** Salted scrypt hashes; one-time passwords are shown once and never stored. |
| 10 | Auth token in local storage | **Fixed.** Tokens stay in the server-side session; the database stores only their hash. |
| 11 | Open admin panel / OAuth admin | **Fixed.** Team & access and Label chat are leader-only on the server. There's no OAuth sign-in to misconfigure. |
| 12 | CORS set to `*` | **Handled.** Streamlit's same-origin protection (`enableCORS = true`) and XSRF protection are explicitly on in `.streamlit/config.toml`. |
| 13 | No email verification | **Not applicable.** Nobody signs up with an email; the leader creates every account and hands the password over in person. |
| 14 | Predictable IDs | **Fixed.** Account IDs are random 96-bit values; session tokens are 256-bit. Data IDs (e.g. GitHub issue numbers) are public anyway, and access is checked by role, not by guessing IDs. |
| 15 | Saving the whole request body | **Handled.** Each action takes named, validated arguments. A member's "own report" is taken from their account, not from a form field, so it can't be swapped. |
| 16 | Webhook signature checks | **Not applicable.** TeamPulse polls GitHub and Notion and uses a Discord bot; it exposes no webhooks. |
| 17 | Production stack traces | **Fixed.** `showErrorDetails = "none"`: visitors see a generic error; details go to the server log only. |
| 18 | Outdated dependencies | **Checked.** Exact versions of all 482 packages are pinned in `requirements.lock`, and CI (`.github/workflows/ci.yml`) runs `pip-audit` on it. It flags 5 advisories in ChromaDB 1.5.9 (no fixed version yet). All five are in Chroma's **HTTP server** (`/api/v2/...` endpoints and its server RBAC). TeamPulse uses Chroma embedded (`PersistentClient`), so no Chroma server or port exists. Re-run `pip-audit` before each release and upgrade when a fix ships. |
| 19 | Weak passwords | **Fixed.** Length, common-password and variety rules (see above). |
| 20 | File upload validation | **Not applicable.** There are no uploads; `maxUploadSize` is also set to 1 MB as a backstop. |

## Checklist 2: "20 things to do before launching"

| # | Task | Status |
|---|---|---|
| 1 | Hide API keys | Done (server-side only; see 1.2). |
| 2 | Purge Git secrets | Nothing to purge in this code. **If you ever committed a token**, revoke it first (GitHub, Notion, Discord, Groq/Gemini dashboards), then remove it from history with `git filter-repo --replace-text` and force-push. Deleting the file in a new commit isn't enough. |
| 3 | Use the public DB key | Not applicable (no client-side database). |
| 4 | Row-level security | Server-side checks per action instead (see 1.3). |
| 5 | Encrypt sensitive data | Passwords and session tokens are hashed, not stored. The SQLite file isn't encrypted; keep it on the server, and it's excluded from Git. Message text stays on your own server (or your own laptop with Ollama). |
| 6 | Server-side auth | Done. |
| 7 | Lock record access | Done: members can review only their own report; pages the leader hides are refused on the server. |
| 8 | Block field tampering | Done: roles are whitelisted, page lists filtered to known pages, identity comes from the session. |
| 9 | Secure session cookies | No auth cookie is used; tokens stay server-side. Streamlit's own XSRF cookie is on. |
| 10 | Hash passwords | Done (scrypt + salt). |
| 11 | Rate limit login | Done. |
| 12 | Bot protection | No public sign-up exists for bots to abuse; sign-in lockout stops guessing. A CAPTCHA wasn't added (it would need a third-party service). |
| 13 | Parameterised queries | Done. |
| 14 | Validate all input | Done (server-side). |
| 15 | Escape user content | Done, HTML and Markdown, tested on every page. |
| 16 | Restrict file uploads | No uploads. |
| 17 | Trim API responses | No public API. Members don't see the identity table (who is who on GitHub/Discord) or the connection details. |
| 18 | Security headers | Streamlit doesn't let an app set its own headers (e.g. Content-Security-Policy). Streamlit Community Cloud serves over HTTPS. Escaping (1.8) is the main defence against injected scripts. |
| 19 | Force HTTPS | Streamlit Community Cloud forces HTTPS. If you self-host, put it behind a reverse proxy with HTTPS (e.g. Caddy). |
| 20 | Scan dependencies | Done with `pip-audit` on the full lock file, in CI on every push (see 1.18). |

## AI assistant connections (MCP)

People can connect Claude Desktop (or another MCP client) to TeamPulse with a personal token (My account →
Connect an AI assistant).

- **The token acts as its owner and never has more than their role.** Each tool calls the same permission
  checks as the pages, on every call, so a member's assistant sees only their own workspace and changes only
  their own tasks.
- **Read-only by default.** "Read and update" adds only own-task actions (comment, progress, blocker, send for
  review) and, for leaders, proposals that are confirmed in the app. Assigning, deadlines, approval, completion
  and deletion are not exposed at all.
- **Stored like sessions.** Only a SHA-256 hash is stored, and the token is shown once. Tokens expire after 90
  days; each person can have at most 5.
- **Cut off immediately** when removed (by the owner, or by a leader under Team & access), on a password reset,
  password change or "sign out of other devices", and when the account is deactivated.
- **Logged and limited.** Calls appear in the activity log as `mcp.<tool>`; refusals are logged too. There's a
  limit of 120 calls per 10 minutes per person.
- **Local only.** The server runs on the person's computer over stdio and opens no port. It's off in the public
  demo.
- **Prompt injection.** Task text, comments and evidence are written by team members, so the server tells the
  assistant to treat them as data. The real protection is that no tool can do more than the person could.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `TEAMPULSE_AUTH` | `on` | `off` turns accounts off. Only for local development on your own laptop. |
| `TEAMPULSE_SETUP_CODE` | empty | Lets the first leader be created from the sign-in page. Remove after use. |
| `TEAMPULSE_SESSION_HOURS` | `12` | Longest a session lasts. |
| `TEAMPULSE_IDLE_MINUTES` | `60` | Sign out after this long without activity. |
| `TEAMPULSE_ASK_PER_HOUR` | `60` | AI questions per person per hour. |
| `TEAMPULSE_CONTRIB_REVIEW` | `on` | `off` makes members strictly read-only (no report approval). |
| `TEAMPULSE_PUBLIC_DEMO` | `off` | `on` adds one-click demo sign-ins for the fictional team, and switches off account, settings, connection-check and evaluation changes so one visitor can't change them for everyone. Never use with real data. |
| `TEAMPULSE_LLM_MAX_TOKENS` | `1500` | Longest AI reply. |
| `TEAMPULSE_AUDIT_DAYS` / `TEAMPULSE_CACHE_DAYS` | `180` / `30` | Retention for the activity log and cached AI responses. |
| `TEAMPULSE_BACKUP_DIR` / `TEAMPULSE_BACKUP_KEEP` | empty / `14` | Daily verified backups from the poll loop. |

## Tests

`python -m pytest -q` runs the offline test suite, including:

- `tests/test_auth.py`: hashing, password rules, lockout, sessions and expiry, sign-out, deactivation,
  one-time passwords, and a member calling all 16 admin actions directly (each refused, logged, no change).
- `tests/test_roles_ui.py`: both roles through the real pages: what each sees, hidden pages, account management.
- `tests/test_xss.py`: attack strings in every table, rendered on every page.
- `tests/test_concurrency.py`: simultaneous users can't corrupt each other's changes, parallel password guesses
  still lock the account, and only one first leader can be created.
- `tests/test_privacy.py`: opting out of Discord deletes the person's messages and every copy derived from them.
- `tests/test_llm_safety.py`: AI calls are capped and warned about injected instructions; AI text can't carry
  disguised links or images.
- `tests/test_guardrails.py`: a scripted AI breaks one rule at a time (invented links, contact details, keys,
  leaked instructions, made-up references, judgemental or withheld contribution reports, a prompt-injected chat
  message) through the real agents; each is caught, and good answers pass untouched.
- `tests/test_langsmith_eval.py`: evaluation runs without a LangSmith key make no network calls (LangSmith
  otherwise uploads traces, which contain team chat, even with uploads switched off).

## Reporting a problem

If you find a security issue, message the team leader privately rather than posting it in the group chat.
