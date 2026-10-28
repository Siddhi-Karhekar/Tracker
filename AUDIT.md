# TeamPulse production-readiness audit

Date: 28 October 2026. Scope: the whole repository at commit `6f1b45f` (web app, agents, ingestion, auth,
configuration, dependencies). Everything below was checked against the code and, where stated, by running it.
Nothing here claims TeamPulse is "completely secure"; it lists what was found, what was fixed and tested, and
what is still open.

## A. Executive summary

**Starting point.** TeamPulse already had server-side role checks, salted password hashes, escaped output and
113 passing tests (79% line coverage). The audit found five serious problems those tests didn't cover:

1. **Data loss under concurrent use (P0).** All visitors shared one SQLite connection across Streamlit's
   threads. With 8 people saving at once, only 36 of 160 writes survived, and one person's failed change was
   committed by someone else's save.
2. **Sign-in lockout bypass (P1).** The failure counter was read, then written, so guesses sent in parallel
   overwrote each other's count. After 12 simultaneous wrong guesses the account was still not locked.
3. **Hidden pages reachable through Ask (P1).** A page the leader closed to members (e.g. Contributions) could
   still be answered in the Ask chat.
4. **Public demo gave every visitor full admin (P1).** Any visitor could deactivate the demo member, reset
   passwords, close pages for everyone or burn the shared AI quota.
5. **Discord opt-out left copies behind (P1).** "Delete everything stored from you" deleted messages but kept
   AI extractions, labels, task links, conflicts quoting them and cached AI responses.

**Now.** All five are fixed, each with a regression test that failed before the fix and passes after
(verified for 1, 2, 3 and 5; number 4 follows directly from the code and is covered by tests written with the fix). Nineteen more findings (P2–P3) were fixed; all except the index, lint and tooling items have their
own regression test. 138 tests pass, including on clean installs from the new lock file on Python 3.12 and 3.13. Coverage is 80%.

**Readiness.** Ready for a **controlled deployment for one student team** (tens of users, one server), with the
checklist in section E done. It is **not** verified against the live GitHub, Notion and Discord APIs (this
environment can't reach them), and the open risks in section F stay open, notably that free hosted AI tiers may
use prompts for training (use Ollama for real team data) and that Streamlit can't send its own security headers.

## B. Findings register

Severity: P0 critical, P1 high, P2 medium, P3 low. "Confirmed" means reproduced by a test or a run before fixing;
"Potential" means a weakness found by reading the code, fixed without an exploit being shown.

| ID | Sev | Status | Category / component | Where | Evidence and root cause | Impact | Fix | Regression test |
|---|---|---|---|---|---|---|---|---|
| TP-01 | P0 | Confirmed, fixed | Data integrity / database | `db.connect` | One `sqlite3.Connection` (with `check_same_thread=False`) cached for all sessions. Python's transaction state is per connection, so threads shared one transaction. Repro: 8 threads × 20 inserts kept 36 rows; a rollback in thread A was committed by thread B. | Lost or wrongly saved edits (verdicts, accounts, labels, audit entries) whenever two people act at once | `ThreadConnection`: one connection per thread behind the same object; 15 s busy timeout; connections closed when their thread is cleaned up | `test_concurrency.py`: rollback isolation, 160/160 writes, connection closing |
| TP-02 | P1 | Confirmed, fixed | Authentication | `auth.login` | Failure count read in Python, incremented, written back; parallel attempts overwrote each other. Repro: 12 parallel wrong guesses, then the right password still signed in. | Brute force at the speed of parallel requests | Count incremented inside SQLite (`failures + 1`) in one transaction (`_record_failure`) | `test_parallel_password_guesses_still_lock_the_account` |
| TP-03 | P1 | Confirmed, fixed | Authorization / AI | `SupervisorAgent.run_specialists`, `actions.ask` | Page visibility was only checked when opening pages. The Ask chat routed to any specialist. Repro: leader closes Contributions; member asks "what did Kabir do" and gets the report. | Members read what the leader hid | Specialists mapped to pages; refused after routing, so neither the AI router nor the wording can reach them; memory recall gated too | `test_asking_cannot_reach_a_page_the_leader_closed` (fails with the gate removed) |
| TP-04 | P1 | Potential (evident in code), fixed | Authorization / public demo | `auth.can`, `views/team.py` | All demo visitors sign in as the same leader, with full rights over shared state | One visitor locks everyone out of the demo or spends the AI quota | In public demo: account, settings, connection-check and evaluation permissions off; Team page shown read-only with an explanation | `test_public_demo_visitors_cannot_change_accounts_or_settings_for_everyone`, `test_public_demo_team_page_is_look_only` |
| TP-05 | P1 | Confirmed, fixed | Privacy | `db.delete_discord_messages_of`, `db.delete_event` | Only `events` rows were deleted; `extractions`, `labels`, `task_links`, `link_feedback` have no foreign key, conflicts embed event IDs, the AI cache holds responses about messages | Broken promise to the person opting out; DPDP erasure | Delete events with all derived rows in one transaction; clear the AI cache on opt-out (its entries can't be attributed); search index drops vectors on next sync | `test_privacy.py` (2 tests; failed before) |
| TP-06 | P2 | Potential, fixed | Authentication | `views/login.py` setup form | Setup code compared without throttling or minimum length | Brute-forcing the first leader account while none exists | `auth.check_setup_code`: 16-character minimum, same lockout as sign-in | `test_setup_code_is_throttled_and_must_be_long` |
| TP-07 | P2 | Potential, fixed | Authentication | `auth.create_first_leader` | "No leader yet?" check and insert were separate | Two simultaneous setups create two leaders | Conditional insert in one statement | `test_two_people_setting_up_at_once_make_only_one_leader` |
| TP-08 | P2 | Potential, fixed | Authentication | `auth.change_password` | Wrong current password not throttled | A stolen session could guess the password | Same counter and lock as sign-in | `test_wrong_current_password_is_throttled` |
| TP-09 | P2 | Potential, fixed | AI cost / availability | `llm.LLM` | No reply-length cap; 3 retries × 60 s per provider | Slow, quota-draining requests ("denial of wallet") | `max_tokens` (default 1500), 2 retries × 45 s | `test_every_call_is_capped_and_warned_about_injected_instructions` |
| TP-10 | P2 | Potential, fixed | AI / prompt injection | all prompts; `views/ask.py` | Untrusted chat/issue text sent to the AI without any instruction to treat it as data; AI answers could contain disguised links (`[official form](evil)`) or images | Manipulated summaries, phishing links in answers | One security note added to every system prompt centrally; model-written links shown as "label (address)", images dropped; only citation links clickable | `test_llm_safety.py` |
| TP-11 | P2 | Potential, fixed | Error handling | `actions.ask` | Unexpected errors reached Streamlit's generic error and the question was logged as successful; failed attempts weren't rate limited | Confusing failures, misleading audit log, unlimited failing requests | `ServiceError` with a safe message, full details in the server log, audit outcome `failed`, failed attempts count toward the hourly limit | `test_a_crash_while_answering_is_reported_safely_and_logged_as_failed` |
| TP-12 | P2 | Potential, fixed | Reliability / external APIs | GitHub and Notion clients | No retry on 429 or 5xx; one blip failed the whole poll | Gaps in data during rate limits or brief outages | `teampulse/http.py`: 3 retries with backoff, honours `Retry-After` | `test_http_retry.py` (local server returning 503, 429, then 200) |
| TP-13 | P2 | Potential, fixed | Data retention | sessions, `audit_log`, `llm_cache` | Grew forever; the AI cache can quote chat | Unbounded storage; personal data kept longer than needed | `maintenance.purge` at app start and in the poll loop (audit 180 d, cache 30 d, ended sessions 7 d) | `test_purge_removes_only_what_is_past_retention` |
| TP-14 | P2 | Potential, fixed | Operations | none existed | No backup or restore procedure | Unrecoverable loss of accounts, verdicts, labels | Online, integrity-checked backups; restore keeps a copy of what it replaces and revokes restored sessions; daily backups in the poll loop | `test_backup_restores_data_and_old_sessions_cannot_be_reused`, damaged-backup test |
| TP-15 | P2 | Confirmed by reading the page, fixed | Functional / monitoring | `views/sources.py` | "Connected" shown whenever a token was set, even if the poller or bot wasn't running | Stale data presented as live | Heartbeats from poller and bot; page shows "Not running…"; `python -m teampulse.health status` exits 1 for uptime monitors | `test_health_status.py` |
| TP-16 | P2 | Confirmed, fixed | Functional / UI | `webapp.get_agent` | Found in a real browser: public-demo banner shown three times per page | Cluttered, unprofessional demo | Banner shown once per page | `test_public_demo_banner_home_and_limit` (asserts exactly one) |
| TP-17 | P3 | Potential, fixed | Input validation | `discrepancies.set_verdict`, `contributions.set_review` | Validation by `assert`, removed under `python -O` | Invalid values stored if run optimised | `ValueError` checks | `test_invalid_values_raise_even_under_python_O` |
| TP-18 | P3 | Potential, fixed | AI output validation | `memory.remember` | AI-returned fields copied unchecked; errors silently ignored | Odd memory kinds or oversized facts | Whitelist and length checks; failures logged | `test_ai_tidied_memory_is_validated_like_any_input` |
| TP-19 | P3 | Potential, fixed | Cost / concurrency | `actions.first_build` | Several first visitors each started the same AI-heavy build | Duplicate AI spend | Lock plus re-check | `test_first_build_runs_once_when_several_people_open_the_app` |
| TP-20 | P3 | Potential, fixed | Performance | schema | No index for `events.created_at` (blocker queries) or per-person rate-limit lookups | Slower queries as data grows | Two indexes (applied automatically, `IF NOT EXISTS`) | Measured, section D |
| TP-21 | P3 | Confirmed, fixed | Resource handling | per-thread connections | 268 "unclosed database" warnings in the test run | File handles held until garbage collection | Closed explicitly when the thread is cleaned up | `test_a_threads_connection_is_closed_when_the_thread_is_gone`; warnings now 0 |
| TP-22 | P3 | Fixed | Supply chain | repo | No lock file, no CI | Untested versions in production; regressions unnoticed | `requirements.lock` (482 pinned packages, tested on a clean install); CI with tests, lint, `pip-audit`, read-only permissions | Clean-venv run: 138 passed |
| TP-24 | P2 | Confirmed, fixed | Build / supply chain | `requirements.lock` | Found by the first CI run: the lock was generated on Python 3.13 and pinned `audioop-lts`, a backport that only exists for 3.13+, so installation failed on 3.12 (the version CI and many hosts use) | No install on Python 3.12 | Version marker `; python_version >= "3.13"`; CI tests 3.12 and 3.13 | Clean installs on 3.12 and 3.13: 138 passed each; CI matrix |
| TP-23 | P3 | Fixed | Code quality | various | Unused imports, a duplicated set entry, non-security hashes not marked | Noise that hides real warnings | Cleaned; `usedforsecurity=False` on ID/test hashes | Lint passes |

**Reviewed and not vulnerabilities:** Bandit B608 (9): every SQL string built in code only inserts fixed column
names or `?` placeholders; values are always bound. Bandit B105: `tp_session` is a session-state key name.
MD5/SHA-1: used for IDs and the offline test embedder, not security. detect-secrets (5 hits): a README
placeholder, the common-passwords list, Ollama's fixed dummy key, cache-folder tags. No subprocess, `eval`,
pickle or shell use anywhere. No file uploads, webhooks, OAuth or email flows exist, so those checks don't apply.

## C. Implementation report

**New files:** `teampulse/http.py` (retrying sessions), `teampulse/maintenance.py` (purge, backup, verify,
restore), `requirements.lock`, `.github/workflows/ci.yml`, `AUDIT.md`, and tests `test_concurrency.py`,
`test_privacy.py`, `test_llm_safety.py`, `test_http_retry.py`, `test_maintenance.py`, `test_health_status.py`.

**Changed:** `db.py` (per-thread connections, busy timeout, deletion with derived data, heartbeats, two indexes),
`auth.py` (atomic lockout, setup-code check, throttled password change, atomic first leader, demo locks),
`actions.py` (specialist gating, safe errors, first-build lock), `agents/supervisor.py` (refusal after routing,
gated memory recall), `llm.py` (security note, token cap, bounded retries), `memory.py`, `ui.py` (`md_safe`),
`health.py` (process status), `webapp.py` (purge on start, one banner), ingestion (`poll.py` heartbeat and
maintenance, `discord_bot.py` heartbeat, pollers use retrying sessions), views (`ask`, `login`, `sources`,
`team`), `.env.example`, `.gitignore`, `README.md`, `SECURITY.md`.

**Database:** no destructive migration. Two `CREATE INDEX IF NOT EXISTS` statements run automatically on start.
Existing data is untouched.

**Dependencies:** none added or upgraded. The lock file pins what was already installed and tested.

**Compatibility:** `db.connect()` now returns a per-thread wrapper that behaves like `sqlite3.Connection`
(`:memory:` still returns a plain connection). The public demo now refuses account and settings changes.
`TEAMPULSE_SETUP_CODE` must be at least 16 characters. Install with `pip install -r requirements.lock`.

## D. Test and verification report

| What | Command / tool | Result |
|---|---|---|
| Baseline tests | `python -m pytest -q --cov=teampulse` | 113 passed, 79% coverage |
| Final tests | same | **138 passed**, 0 failed, 0 skipped, 80% coverage, 0 unclosed-connection warnings (was 268) |
| Clean install | new venv, `pip install -r requirements.lock`, pytest | 138 passed on Python 3.13 and on 3.12 (after TP-24) |
| Regressions fail without the fix | TP-01, 02, 03, 05: tests run before fixing (03 by switching the gate off) | All failed, then passed |
| GitHub Actions CI | `.github/workflows/ci.yml` on push | First run failed at install (TP-24). After the fix: **passed on Python 3.12 and 3.13** (install, lint, 138 tests, dependency scan) |
| Lint (CI rules) | `ruff check --select F,E9,B,S …` | Passed |
| Static security | `bandit -r teampulse views app.py` | 10 findings, all reviewed false positives (section B) |
| Dependencies | `pip-audit -r requirements.lock` (482 packages incl. transitive) | 5 advisories, all ChromaDB 1.5.9 HTTP-server issues, no fixed version; not reachable (embedded use) |
| Secrets | `detect-secrets scan --all-files`; regex scan of full git history for token formats | 5 false positives; no tokens in history |
| Browser | Playwright + Chromium, 390 px and 1280 px, public demo as member and leader | No horizontal overflow, no JavaScript errors; member sees view-only nav without Team & access; found and fixed TP-16; demo Team page locked |
| Load (10,041 events: demo + 10,000 synthetic chat messages, offline embedder, no AI) | one-off timing script (not committed) | Insert 10k: 0.14 s. Index sync: 12.9 s. Full refresh (rules only): 15.4 s. Blocker query: 21 ms median. Page build, median of warm runs: Home 0.41 s, Blockers 0.29 s, Tasks 0.44 s, Conflicts 0.26 s, Contributions 0.69 s |

**Coverage gaps:** Discord bot 27%, GitHub poller 44%, Notion poller 42% (their network paths need the live
services), `maintenance.py` 70% (CLI wrapper untested).

**Not run, and why:** live GitHub/Notion/Discord APIs (no network access to them here; use Data sources → Run
check); the real multilingual
embedding model and real AI latency at scale (the load test used the offline embedder and no AI); Firefox and
Safari; many simultaneous real browser users; accessibility audit.

## E. Production deployment checklist

**Before deploying**
1. Decide the AI provider. For real team data, use **Ollama** (`LLM_PROVIDERS=ollama`); free hosted tiers may use
   prompts for training.
2. Create `.env` (or Streamlit Secrets) from `.env.example`. Never commit it. Fill only the tokens you use, each
   with the narrowest scope (fine-grained read-only GitHub token on the repos; Notion integration shared only
   with the task database and plan pages; Discord bot with read and Message Content permissions only).
3. Keep `TEAMPULSE_AUTH=on` and `TEAMPULSE_PUBLIC_DEMO=off`. Set `TEAMPULSE_BACKUP_DIR`.
4. Install exact versions: `pip install -r requirements.lock`. Run `python -m pytest -q` on the server; all must pass.
5. Run `pip-audit -r requirements.lock`. Expect only the 5 ChromaDB advisories listed above. Never run a Chroma
   server or expose its port.

**Deploy**
6. Serve over HTTPS only. Streamlit Community Cloud does this. If self-hosting, use a reverse proxy, for example
   Caddy, which also adds the headers Streamlit can't:
   ```
   teampulse.example.com {
     reverse_proxy localhost:8501
     header Strict-Transport-Security "max-age=31536000"
     header X-Content-Type-Options nosniff
     header Referrer-Policy no-referrer
     header X-Frame-Options DENY
   }
   ```
   Don't expose port 8501 directly.
7. Create the first leader: `python -m teampulse.auth create-leader --name "…"` (or a 16+ character
   `TEAMPULSE_SETUP_CODE`, removed right after).
8. Start three processes under a supervisor that restarts them (systemd, Docker restart policy):
   `streamlit run app.py`, `python -m teampulse.ingest.poll --scan-chat`, `python -m teampulse.ingest.discord_bot`.

**Smoke test (about 10 minutes)**
9. Data sources → Run check: every line OK. Cards show "Connected" (heartbeats arriving).
10. Sign in as leader, create a member account, sign in as that member in a private window: view-only nav,
    no Team & access, editing buttons absent. Close a page for members; check it's refused in the nav and in Ask.
11. Ask one question; check citations open the right GitHub/Notion/Discord items.

**Monitoring and recovery**
12. Uptime check every 5 minutes: `python -m teampulse.health status` (exit code 1 means the poller or bot
    stopped), plus Streamlit's `/_stcore/health`.
13. Check the activity log weekly (Team & access → Activity) for repeated denied or failed entries.
14. Backups: daily from the poll loop. Once a month, test a restore on a copy:
    `python -m teampulse.maintenance verify <file>`, then `restore <file> --yes` into a scratch path.
15. Rollback: stop the three processes, `git checkout <previous tag>`, `pip install -r requirements.lock`,
    restore the latest backup if the data changed shape, start again. No schema change in this release needs
    undoing (only added indexes).
16. Incident (suspected account compromise): Team & access → Reset password (signs the person out everywhere),
    or Deactivate; rotate any API token you suspect in its service's dashboard; check the activity log.

## F. Residual-risk register

| # | Sev | Risk | Why it's still open | If left open | Next action |
|---|---|---|---|---|---|
| R1 | P1 | Free hosted AI tiers (Groq, Gemini) may use prompts, which contain team chat, for training | Product/provider decision, not code | Team messages leave your control | Use Ollama for real data, or a paid tier with a no-training agreement |
| R2 | P2 | No Content-Security-Policy or other security headers | Streamlit can't set response headers | Less defence in depth against injected scripts (escaping is the primary defence and is tested on every page) | Self-host behind the proxy in E.6; on Streamlit Cloud, accept or move |
| R3 | P2 | Prompt injection can still bias AI labels and summaries | Can't be fully prevented in language models | A crafted chat message could mislabel a blocker or slant a summary | Already mitigated: no AI tools, outputs validated, answers verified against sources, human verdicts. Review "unsupported" warnings |
| R4 | P2 | The Ask page's general search can quote any stored message, even when a topic page (e.g. Blockers) is hidden | Ask is a search over all team data by design; per-message access rules would be a product change | Hiding Blockers doesn't hide blocker messages from Ask | Leader decision: hide Ask from members too, or request per-source visibility as a feature |
| R5 | P2 | Database not encrypted at rest | SQLite without an encryption extension | Someone with disk access reads chat and account data (passwords are hashed) | Full-disk encryption on the host; restrict file permissions to the service account |
| R6 | P2 | Not verified against live services or the deployed CI | No access from this environment | An API change or permission issue shows up only in production | Do E.9–E.11 on deploy; watch the first CI run |
| R7 | P3 | Lockout is per username, with no per-IP limit; a burst of parallel guesses can land before the lock | Streamlit doesn't reliably expose the client IP | A few extra guesses per lock window | Rate limit at the reverse proxy (E.6) |
| R8 | P3 | Refreshing the tab signs you out | Tokens are deliberately kept out of browser storage and cookies | Mild annoyance | Product decision; a secure HttpOnly cookie needs a custom component or a proxy |
| R9 | P3 | Items deleted in GitHub or Notion stay stored | Pollers fetch changes, not deletions | Stale or removed content can still appear | Add a periodic reconciliation pass |
| R10 | P3 | After a single message is deleted, cached AI responses about it last up to 30 days, and its search vector lasts until the next index sync | Cache entries can't be attributed to one message without storing more data | Short-lived leftover copies | Keep `TEAMPULSE_CACHE_DAYS` low; run the poll loop with `--scan-chat` (syncs the index) |
| R11 | P3 | ChromaDB 1.5.9 server advisories | No fixed release yet; not reachable in embedded use | Becomes serious only if someone runs Chroma as a server | Upgrade when a fix ships; CI's `--ignore-vuln` lines must be removed then |
| R12 | P3 | One server, one SQLite file; no horizontal scaling | Fits one student team; scaling out needs a client-server database | Fine for tens of users; not for many teams | Out of scope unless TeamPulse becomes multi-team |
| R13 | P3 | GitHub Actions are pinned to version tags, not commit SHAs | Readability over maximum supply-chain hardening | A compromised action tag could run in CI (CI has read-only permissions and no secrets) | Pin to SHAs if the repo ever holds deployment secrets |
| R14 | P3 | Accessibility not audited | Out of time | Possible issues for screen-reader or keyboard users | Run an axe or Lighthouse audit |
