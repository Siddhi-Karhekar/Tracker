# TeamPulse

An agent that reads a student team's **GitHub**, **Notion** and **Discord**, answers questions with a
citation for every claim, and (coming weeks) finds where those sources disagree, pulls blockers out of
Hinglish chat, and builds evidence-based contribution reports. Everything is free to run.

**Done so far:** Week 1 (demo data), Week 2 (ingestion), Week 3 (Retrieval Agent + web app),
Week 4 (Supervisor Agent + tracing), Week 5 (Blocker Agent + chat classifier evaluation),
Week 6 (task registry, AI-confirmed linking, claims), Week 7 (Discrepancy Agent),
Week 8 (Contribution Agent + team memory), Week 9 (Verifier + full evaluation),
Week 10 (free deployment, public demo mode, overview dashboard), then sign-in with team leader and
team member roles, and a security pass (see [SECURITY.md](SECURITY.md)).

```
app.py                     the web app's navigation (streamlit run app.py); shows only pages your role allows
views/login.py             sign in (and first team leader setup with a setup code)
views/account.py           choose your own password, sign out of other devices
views/team.py              Team & access (leaders): accounts, one-time passwords, what members see, activity log
views/home.py              Home: today's most important issue, needs-attention list, sync strip of every task
views/ask.py               chat with cited answers, sources, verification and the agent trace
views/blockers.py          what people are stuck on or waiting for, linked to tasks
views/discrepancies.py     Conflicts: where sources disagree, both sides, real / false alarm
views/tasks.py             every task's status / owner / deadline per source; fix chat links
views/contributions.py     evidence-based reports; each member reviews and approves their own first
views/sources.py           Data sources: freshness, connection check with fixes, AI test, members and consent
views/memory.py            facts the team asked to remember, re-checked against live data
views/label.py             label chat messages by hand and measure accuracy
eval_data/hinglish_dev.jsonl  60 synthetic labelled Hinglish messages (development set)
eval_data/demo_questions.jsonl  18 demo questions with expected facts, citations and specialists
teampulse/
  models.py                Event: the one format every source is converted into
  db.py                    SQLite schema (events, sync_state, members, claims, memory, indexed, llm_cache)
  config.py                reads .env
  auth.py                  accounts, salted password hashes, sessions, roles and permissions; CLI
  actions.py               the only way pages change data: checks the role on the server first
  maintenance.py           retention purge, verified backups and restore
  guardrails.py            checks every agent's answer before it's shown (see Testing that each agent is correct)
  http.py                  retrying HTTP sessions for the GitHub and Notion pollers
  cli.py                   stats / recent / members / link
  signals.py               chat scanner: conversation windows → classify → link to tasks
  webapp.py                shared Streamlit helpers + public demo mode
  ui.py                    design system: colours, type, pills, rows, sync strip
  health.py                connection checks for GitHub, Notion and Discord tokens and permissions
  tasks.py                 task registry (GitHub + Notion merged) and chat→task linking
  claims.py                status / owner / deadline / worker claims from every source + task cards
  discrepancies.py         conflict rules, severity, open / resolved / dismissed tracking
  contributions.py         per-member evidence, help given, chat claims vs evidence, opt-in + self-review
  memory.py                explicit long-term memory: remember / forget / recall, with live verification
  verifier.py              checks each cited statement against the evidence it cites
  seed_demo.py             demo team "CampusEats" with planted problems + answer key
  index.py                 ChromaDB vector index with a multilingual embedding model
  llm.py                   Groq / Gemini / Ollama with fallback + response cache
  tracing.py               optional Arize Phoenix tracing
  agents/retrieval.py      Retrieval Agent: plan → retrieve → answer → check citations
  agents/supervisor.py     Supervisor: route → specialists → merge/synthesize
  agents/blockers.py       Blocker Agent: blockers, help requests, decisions from chat
  agents/discrepancy.py    Discrepancy Agent: explains conflicts with citations to both sides
  agents/contributions.py  Contribution Agent: neutral, cited summaries; respects consent and review
  agents/baseline.py       plain RAG baseline for the architecture comparison
  eval/blockers.py         precision / recall / F1 + confusion matrix, LLM vs keyword baseline
  eval/linking.py          chat→task linking accuracy: refs vs similarity vs LLM
  eval/discrepancies.py    recall/precision vs planted conflicts, or precision from your verdicts
  eval/qa.py               plain RAG vs single agent vs multi-agent on the demo questions
  eval/agents.py           per-agent correctness + guardrail evaluation with thresholds
  eval/ragas_score.py      RAGAS scores with a free AI judge (own environment: requirements-eval.txt)
  eval/langsmith_eval.py   golden questions as a LangSmith dataset and experiments
  eval/run_all.py          every evaluation in one report
  ingest/
    discord_bot.py         live capture + backfill + consent commands
    github_poller.py       issues, PRs, commits, comments, reviews
    notion_poller.py       task databases + plan pages (incl. page text and to-do checkboxes)
    poll.py                runs GitHub + Notion pollers on a loop
tests/                     offline tests (no API keys or internet needed)
SECURITY.md                how accounts and roles work, and both pre-launch security checklists
AUDIT.md                   production-readiness audit: findings, fixes, tests, deployment checklist, open risks
requirements.lock          exact versions of every package, for reproducible installs
.github/workflows/ci.yml   tests, lint and dependency scan on every push
```

## 1. Quick start with demo data (10 minutes)

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                   # Windows: copy .env.example .env
python -m pytest -q                    # all should pass
python -m teampulse.seed_demo          # creates data/demo.db + data/demo_answer_key.json
                                       # and prints sign-in details for Neha (leader) and Kabir (member)
```

Get **one** free LLM key (or install Ollama, see below) and put it in `.env`:

- **Groq:** https://console.groq.com/keys → `GROQ_API_KEY`
- **Gemini:** https://aistudio.google.com/apikey → `GEMINI_API_KEY`

Then:

```bash
streamlit run app.py
```

Sign in with the details `seed_demo` printed. Try both: Neha (team leader) sees everything, including
**Team & access**; Kabir (team member) gets a read-only view. See [Sign-in and roles](#sign-in-and-roles).

The first start downloads the search model once (~0.2 GB). Try the example questions in the sidebar,
including the Hinglish one. Then open **Blockers** in the sidebar and click **Scan now**, and
open **Tasks** to see what each source says about each task, and **Discrepancies** to see where
they disagree. Ask "kya sab sync me hai?" in the chat. Try **Contributions** and **Memory** too, and ask
"remember that our demo day is on 2 Dec".

### The demo team

"CampusEats", a canteen pre-order app by Riya (frontend), Aman (backend), Kabir (payments + ML) and Neha
(PM). The data has these problems planted on purpose, listed in `data/demo_answer_key.json`:

| Planted problem | Where it shows |
|---|---|
| Login marked **Done** in Notion | GitHub #12 still open; Discord: "OTP abhi bhi nahi aa raha" |
| Menu API **In progress** in Notion (stale) | PR #15 merged, #14 closed |
| Payments assigned to Kabir | Aman is doing it (commits + Discord) |
| Payments deadline differs | Notion due in 2 days vs GitHub milestone in 8 |
| Plan says Firebase | Discord decision: Postgres |
| Hidden blockers in chat | OTP in spam; no canteen data for the ML model |

Week 3's agent can already surface most of these when asked. Weeks 5–7 detect them automatically.

## 2. Language models (all free)

`LLM_PROVIDERS=groq,gemini,ollama` sets the order. If one is rate-limited or offline, the next is used.
Every response is cached in SQLite, so repeating a question costs nothing and demos are repeatable.

**Ollama (local, unlimited, private):** install from https://ollama.com, then `ollama pull qwen2.5:7b`.
Needs about 8 GB RAM. **Use only Ollama for your real team's data** (`LLM_PROVIDERS=ollama`), because
free hosted tiers may use prompts to train their models.

Free-tier model names change occasionally. If you see "model not found", update `GROQ_MODEL` or
`GEMINI_MODEL` to a current free model from the provider's docs.

## 3. Connect your real team

Set `TEAMPULSE_DB=data/teampulse.db` in `.env`, then set up each source.

### Discord bot

1. https://discord.com/developers/applications → **New Application** → `TeamPulse`.
2. **Bot** tab → **Reset Token** → copy into `DISCORD_BOT_TOKEN`.
3. Same tab → turn on **Message Content Intent** → Save.
4. **OAuth2 → URL Generator**: scope `bot`; permissions **View Channels**, **Read Message History**,
   **Send Messages**. Open the URL and add the bot to your team server.
5. Discord settings → Advanced → **Developer Mode** on. Right-click your server → **Copy Server ID** →
   `DISCORD_GUILD_ID`.
6. Run `python -m teampulse.ingest.discord_bot`. Each teammate types `!teampulse join`.

**Consent built in:** nothing is stored for anyone who hasn't joined; storage starts when they join;
`!teampulse leave` deletes everything stored for that person; edits and deletes in Discord are mirrored.
Pin the output of `!teampulse help`. If the bot was offline, it catches up from channel history on restart.

### GitHub

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate.
2. Only your project repo; read-only **Contents**, **Issues**, **Pull requests**, **Metadata**.
3. `GITHUB_TOKEN` = token, `GITHUB_REPOS` = `owner/repo`.

### Notion

1. https://www.notion.so/profile/integrations → **New integration** (internal) → secret into
   `NOTION_TOKEN`. Enable **Read content** and **Read user information**.
2. Task database → **•••** → **Connections** → add the integration. Same for plan pages.
3. Copy the 32-character ID from each URL into `NOTION_DATABASE_IDS` / `NOTION_PAGE_IDS`.

### Run

```bash
python -m teampulse.ingest.discord_bot     # terminal 1, stays running
python -m teampulse.ingest.poll --scan-chat   # terminal 2: GitHub + Notion + chat → tasks → discrepancies, every 5 min
streamlit run app.py                       # terminal 3; use "Refresh index" after new data arrives
```

Useful commands:

```bash
python -m teampulse.cli stats
python -m teampulse.cli recent --limit 20
python -m teampulse.index                                  # update the search index from the terminal
python -m teampulse.agents.supervisor "what changed this week and who owns payments?"
python -m teampulse.cli link --name Riya --discord 1234567890 --github riya-dev --notion <user-id>
```

`link` records which accounts belong to the same person (needed for contribution reports in Week 8).

## The app

`streamlit run app.py` opens **Home**, which leads with the single most important issue in plain words (for
example, "Login with college email OTP: marked done in Notion, but the GitHub issue is still open and chat says
it's blocked"), then a needs-attention list mixing conflicts and blockers, recent activity, and the **sync strip**:
every task with what GitHub, Notion and chat each say, its edge coloured by the most serious conflict.

The sidebar groups the rest: **Today** (Home, Ask), **Team health** (Blockers, Conflicts, Tasks), **People**
(Contributions) and **Setup** (Data sources, Team memory, Label chat).

**Before connecting a real team,** open **Data sources → Run check** (or `python -m teampulse.health`). It signs
in to each service read-only and says exactly what to fix: a wrong token, a repo the token can't see, a Notion
database not shared with the integration, the Discord Message Content intent switched off, or the bot missing
from the server.

Design: sage-white paper, green-black ink and a ballpoint-blue accent; green, amber and red appear only for
meaning (agree, stale, conflict). Headings in Bricolage Grotesque, body in Atkinson Hyperlegible. Colours and
components live in `teampulse/ui.py` and `.streamlit/config.toml`.

## Sign-in and roles

Everyone signs in. There is no sign-up page: the **team leader** creates each account.

1. Create the first leader on the computer running TeamPulse:
   `python -m teampulse.auth create-leader --name "Your Name"`. It prints a username and a one-time password.
   (On Streamlit Cloud, set `TEAMPULSE_SETUP_CODE` in Secrets and create the leader from the sign-in page
   instead; then delete the code.)
2. Sign in and choose your own password.
3. Open **Admin → Team & access → Add a teammate**. Link each account to the person in the team data so
   they can review their own contribution report. TeamPulse shows a one-time password **once**; give it to
   them privately, not in the group chat. They choose their own password when they first sign in.
4. **Accounts** tab: reset a password (signs them out everywhere) or deactivate someone (immediately).
   **What members can see**: choose which pages members can open. **Activity**: who did what.

| | Team leader | Team member |
|---|---|---|
| View pages, ask questions | All pages | Pages the leader allows, read-only |
| Accounts, data refresh, verdicts, labels, memory, settings | ✅ | ❌ |
| Approve or dispute own contribution report | ✅ | ✅ only their own (`TEAMPULSE_CONTRIB_REVIEW=off` to remove) |

Roles are checked on the server for every change, not just by hiding buttons. Other commands:
`python -m teampulse.auth list` and `python -m teampulse.auth reset <username>` (if a leader is locked out).
`TEAMPULSE_AUTH=off` turns sign-in off for development on your own laptop only. Details and the security
checklists: [SECURITY.md](SECURITY.md).

## Running it for real

Install the exact tested versions with `pip install -r requirements.lock` (use `requirements.txt` only when you
mean to upgrade). Then follow the deployment checklist in [AUDIT.md](AUDIT.md#e-production-deployment-checklist).
Day-to-day commands:

```bash
python -m teampulse.health status        # are the poller and bot alive? exit code 1 if not (for uptime checks)
python -m teampulse.maintenance backup   # verified copy in data/backups (the poll loop does this daily if
                                         # TEAMPULSE_BACKUP_DIR is set)
python -m teampulse.maintenance restore data/backups/teampulse-<time>.db --yes   # stop everything first
python -m teampulse.maintenance purge    # retention clean-up (also runs automatically)
```

## Deploy a free public demo (Week 10)

**Recommended: Streamlit Community Cloud** (free; up to 2.7 GB memory; apps sleep after 12 hours without
visitors, so open yours a few minutes before you present).

1. Push this folder to a GitHub repo (public or private). `.gitignore` already keeps `.env` and `data/` out.
2. Go to https://share.streamlit.io → **Create app** → pick the repo, branch, and main file `app.py`.
3. **Advanced settings → Secrets**, paste:

   ```toml
   TEAMPULSE_PUBLIC_DEMO = "on"
   TEAMPULSE_DB = "data/demo.db"
   LLM_PROVIDERS = "groq,gemini"
   GROQ_API_KEY = "your key"
   GEMINI_API_KEY = "your key"          # optional second provider
   TEAMPULSE_DEMO_QUESTION_LIMIT = "15"
   ```
4. Deploy. The first start downloads the search model and builds the demo (a minute or two).

**Public demo mode** uses only the fictional CampusEats team, re-seeds it every 20 hours so dates like "due in
2 days" stay true, shows a banner on every page, and limits questions per visitor so strangers can't use up
your free AI quota (answers are cached, so repeated example questions are free). **Never deploy your real
team's database**; real data stays on your laptop with Ollama.

In public demo mode the sign-in page has two one-click buttons, **Explore as Neha, team leader** and
**Explore as Kabir, team member**, so visitors can see both roles. They only work on the fictional team, and
the one-time setup form is switched off.

**Why not Hugging Face Spaces?** It used to be the usual free option, but since July 2026 community reports say
new free accounts can no longer host Docker or Gradio apps on free CPU (a PRO subscription is required).
Check their current terms if you want to try.

**Showing your laptop's copy instead** (e.g. with real team data in a supervised demo): run
`streamlit run app.py` and open it on the projector. If someone must open it remotely, Cloudflare's free
quick tunnel (`cloudflared tunnel --url http://localhost:8501`) gives a temporary public link; stop it after.

**Lighter install everywhere:** the default search model now runs through FastEmbed (ONNX, ~0.2 GB, no PyTorch),
so `pip install -r requirements.txt` is a few hundred MB instead of ~2 GB. The heavier e5 model is still
available (see `.env.example`). Switching models rebuilds the search index automatically.

## Testing that each agent is correct, and guardrails

**Guardrails** (`teampulse/guardrails.py`) check every agent's answer on the server before anyone sees it, and
again after the Supervisor combines answers. They never ask the AI to judge itself.

| Guardrail | Agents | What happens |
|---|---|---|
| secret_leak, invented_link, invented_contact, too_long | all | removed from the answer |
| prompt_leak (answer repeats TeamPulse's instructions) | all | answer replaced |
| ungrounded_reference (#issue, %, date or amount not in any source) | retrieval, blockers, conflicts, contributions, combined | warning shown |
| uncited_blocker | blockers | warning shown |
| one_sided_conflict (fewer than two sources cited) | conflicts | warning shown |
| judgemental_language (ranking, blaming, "lied", "kaamchor"…) | contributions, combined | answer replaced by the rule-based report |
| withheld_member (work of someone who hasn't opted in or approved) | contributions, combined | answer replaced |

"Replaced" means the answer built directly from the data (the same one shown when the AI is unavailable).

**Per-agent evaluation**: each agent tested on its own job against the demo team's answer key:

```bash
python -m teampulse.eval.agents --offline --check   # no AI; CI runs this on every push
python -m teampulse.eval.agents                     # with your AI key: the real system
```

It reports router accuracy, retrieval context recall, chat-label F1 and blocker detection, linking accuracy,
conflict recall/precision, contribution checks (claim verdicts, withheld reports, neutral wording), memory,
Verifier accuracy, guardrail catch rate and false alarms, and the end-to-end pass rate, each against a threshold
in `eval_data/agent_thresholds.json` (offline: regression floors; online: targets). The guardrail part runs a
set of attacks, from invented links to judgemental summaries, through the real agents.

**RAGAS** (faithfulness, context precision, context recall, factual correctness, with a free AI as the judge).
RAGAS needs older packages than TeamPulse, so it gets its own environment and scores the file the agent
evaluation writes:

```bash
python -m venv .venv-eval && .venv-eval/bin/pip install -r requirements-eval.txt
.venv-eval/bin/python -m teampulse.eval.ragas_score data/eval/agents-<time>-ragas.jsonl --judge groq
```

**LangSmith** (free Developer plan: 1 seat, 5,000 traces a month, 14-day retention). Add
`LANGSMITH_API_KEY` to `.env`, then:

```bash
python -m teampulse.eval.langsmith_eval          # golden questions as a dataset, this run as an experiment
python -m teampulse.eval.agents --langsmith      # same, with the per-agent metrics attached
```

To see live traces of every question (Supervisor graph, specialists, AI calls), also set
`LANGSMITH_TRACING=true`. Traces contain the team's chat and issue text, so use this with the demo team or only
if your team agrees. With no key set, nothing is sent; local test runs are checked to make no network calls.

## Evaluation (Week 9)

```bash
python -m teampulse.eval.run_all --no-llm     # offline sanity run (~1 minute, free)
# add TEAMPULSE_LLM_MIN_INTERVAL=2 to .env (free tiers: ~30 requests/minute), then:
python -m teampulse.eval.run_all              # the real report (≈ 200–300 AI calls; Groq's free daily quota is enough)
```

This writes `data/eval/report-<time>.md` with four sections ready for your report, plus a JSON file with every
answer so you can spot-check them:

1. **Chat classifier**: keyword baseline vs AI on the Hinglish dev set (+ your own labels, if any)
2. **Chat → task linking**: exact refs vs similarity vs AI (+ your corrections)
3. **Discrepancy detection**: rules only vs rules + AI on the planted conflicts (+ your verdicts)
4. **Architecture comparison** on 18 demo questions, all using the same index, evidence format and answer
   prompt, so differences come from the architecture:

   | System | What it does |
   |---|---|
   | plain RAG | search the raw question once → answer |
   | single agent | Retrieval Agent: plan queries (incl. Hinglish) + time windows → answer → citation check |
   | multi-agent | Supervisor → specialists (blockers, discrepancies, contributions, memory) → Verifier |

   Columns: pass rate, correctness (expected facts present), citation recall (the right sources cited),
   faithfulness (Verifier: cited statements actually supported), median latency, AI calls and tokens per
   question, routing accuracy, and pass rate per question type. Multi-agent costs more calls; the table shows
   whether it pays for them, which is the question examiners ask.

**What I could check without AI keys:** offline, plain RAG and the single agent can't answer at all, while the
multi-agent system still passes about half the questions because its specialists answer from structured data
(signals, tasks, claims) and fall back to cited lists. That's a robustness point worth making. The real
comparison needs your run with AI keys; I haven't seen real-model numbers.

### Verifier

After every answer the Supervisor splits it into statements and an AI judge checks each cited statement against
the evidence it cites (supported / partial / unsupported, one call). Unsupported statements become a warning
under the answer, and the app shows "🔎 4/5 statements supported". Without AI, a rough word-overlap check is
used and labelled as such. Turn it off with `TEAMPULSE_VERIFY=off`.

### Robustness when free quotas run out

- Every AI step has a fallback (keyword classifier, ref/similarity links, rules, templates).
- If the AI router fails, questions are routed by keywords ("atka" → blockers, "sync" → discrepancies,
  "what did X do" → contributions), so specialists still answer.
- `TEAMPULSE_LLM_MIN_INTERVAL` spaces out calls; rate-limited calls are retried with backoff before trying the
  next provider; discrepancies are only rebuilt when the data is over 10 minutes old.

## How the agents work

### Contribution Agent and team memory (Week 8)

**Contribution reports** (`contributions.py`) list, per member and period, linked evidence in five groups:
Code (PRs, commits), Reviews & discussion, Planning & docs (issues opened, Notion pages last edited),
Team chat (progress updates, decisions, blockers raised, requests), and **Helping others** (replies to a
teammate's blocker or request). Plus the tasks they're assigned to and the ones they actually worked on.

**Claims vs evidence.** Chat messages where someone says they did something ("kar diya", "done", "80%") are
linked to their task and checked against that member's own commits/PRs/Notion edits on it:

- **supported**: matching evidence within 3 days
- **older evidence only**: e.g. "80% done" with one commit 4 days earlier
- **no linked evidence found (may be offline work)**: never "false"
- **not linked to a task**

**Ethics built in** (worth a slide):

1. *Evidence, not scores*: no ranking or single number; commit counts reward tiny commits and miss design,
   docs, testing and helping others.
2. *Opt-in*: only members who consented (`!teampulse join`) get a report.
3. *Self-review first*: on the Contributions page each member sees their own report, can add context the data
   can't show ("my work is in a Colab notebook"), and approves it before the team view or the chat agent will
   share it (`TEAMPULSE_CONTRIB_REVIEW=off` disables this). There are no logins, so this relies on trust:
   say so in your report.
4. *Careful wording*, enforced in the AI prompt: neutral, no comparisons between people.
5. *Known gap*: Notion's API only reports the last editor of a page, so earlier editors aren't credited.

**Team memory** (`memory.py`) stores only facts someone explicitly asks for: "remember that…", "note that…",
"yaad rakhna ki…" in the chat, or the Memory page. "forget …" removes one. A newer fact on the same topic
replaces the older one. These commands skip the AI router entirely. After every answer, the Supervisor
attaches any relevant memory, **re-checked against live data**: a remembered date is compared with dates in
Notion pages and GitHub milestones, and shown as ✅ matches or ⚠️ "live data now says …". Memory never
overrides live data.


### Discrepancy Agent (Week 7)

`discrepancies.py` runs plain rules over the claims (reliable, testable, free):

| Type | Fires when | Severity |
|---|---|---|
| status | Notion says done, but the GitHub issue is open and/or chat reports a problem (also: closed on GitHub but chat reports a problem afterwards; chat says done but trackers don't) | high if chat says blocked, else medium (low for chat-says-done) |
| stale_plan | GitHub shows it done (issue closed / PR merged) but Notion doesn't | low |
| owner | the assigned people aren't the ones doing the work (chat + commit/PR authors, last 30 days); or GitHub and Notion assign different people | medium; low |
| deadline | sources give dates ≥ 2 days apart, task not finished | high if the earliest date is ≤ 3 days away or past, else medium |
| plan_outdated | a chat decision made after the plan page was last edited contradicts it (AI check; one call per plan page) | medium; low and "unverified" without AI |

Each finding lists both sides (with links), a suggested fix, and gets a stable id from its type, task and
the conflicting values. On every check, findings that disappeared are marked **resolved** (so you can
show "fixed 3 discrepancies this week"), and findings marked **false alarm** stay dismissed until the
values change. People are compared by name or login prefix (`kabir-ml` = `Kabir`) until you link accounts
with `cli link`, so unlinked accounts don't cause false owner conflicts.

The **Discrepancy Agent** answers questions like "where do our sources disagree?", "is our Notion up to
date?", "who is really doing payments?" or "kya sab sync me hai?". It narrows to the tasks and conflict types
the question mentions, and the AI explains each conflict citing both sides. Without AI it returns the
detected list with citations.

**Evaluation**

```bash
python -m teampulse.eval.discrepancies --key data/demo_answer_key.json   # demo: rules only vs rules + AI
python -m teampulse.eval.discrepancies                                   # your team: precision from verdicts
```

On the demo, rules alone find all 5 planted conflicts with no false alarms. **Treat that as a sanity check,
not a result**: I wrote the demo data and the rules together. For your report, run TeamPulse on your real
team for 2–3 weeks, mark each finding as real or false alarm on the Discrepancies page, and report precision
by type. To estimate recall, ask teammates to list mismatches they know about and check how many were found.


### Tasks, linking and claims (Week 6)

Run with `python -m teampulse.tasks`, the **Rebuild tasks** button on the Tasks page, or automatically by the
poller. Steps:

1. **Task registry** (`tasks.py`): every GitHub issue is a task. A Notion task page joins an issue when it
   mentions `#12`, else when the titles are near-identical, else (for close calls) when the AI confirms they
   are the same work, all in one AI call. Otherwise it becomes a Notion-only task. Pages without a status
   (plan pages) aren't tasks. PRs join via "fixes #14", commits via `#16`, comments and reviews via their
   parent issue or PR.
2. **Chat linking**: messages with an exact `#12` keep it. For the rest, the AI picks the right task from a
   shortlist of the 5 most similar tasks, or says "none" (one call per 12 messages). This is what links
   "payment wala kaam" to *Payment integration*. **Human corrections on the Tasks page always win.**
3. **Claims** (`claims.py`): each source's view of each task in one format:

   | | GitHub | Notion | Chat |
   |---|---|---|---|
   | status | issue open/closed, PR merged → done, open PR → in review | status column, normalised ("Not started" → todo) | AI: "OTP nahi aa raha" → blocked |
   | owner | assignees | assignee | AI: "Aman dekh raha hai" → Aman; "main kar raha hu" → the author |
   | deadline | milestone due date | due date | AI: "kal tak" → date; "M2 tak" → M2's due date |
   | worker | commit and PR authors | | |

   Names are unified through the `members` table (GitHub login, Discord id, Notion id → one name).
   Extractions are cached per message, so re-running costs nothing. Without AI, keyword rules fill in
   status/owner/milestone.

On the demo, the Tasks page already shows the planted problems side by side: login (Notion *done*, GitHub
*open*, chat *blocked*), menu API (Notion *in progress*, GitHub *done*), payments (owner Kabir in Notion and
GitHub, Aman in chat and commits; Notion due date differs from milestone M2). Week 7 turns these into
detected, explained, severity-ranked discrepancies (see below).

**Measuring linking:** `python -m teampulse.eval.linking --key data/demo_answer_key.json` (demo) or, after
correcting ~100 links on the Tasks page, just `python -m teampulse.eval.linking`. It compares exact refs
vs similarity vs AI on the same messages. On the demo, refs alone link only 1 of 8 task-related messages,
which is the case for AI linking in your report.


### Blocker Agent and chat scanner (Week 5)

**Scanning** (`teampulse/signals.py`, runs from the poller, the Blockers page, or `python -m teampulse.signals`):

1. Discord messages are grouped into **conversations**: same channel, no gap over 45 minutes, at most 12
   messages. Replies and earlier messages give the model context.
2. Conversations are packed into batches of about 30 messages, and **one LLM call per batch** labels
   every message as `blocker`, `help_request`, `decision`, `progress` or `noise`, with a short English
   summary, the task it's about, and whether a blocker was later fixed. The prompt has Hinglish
   definitions and a worked example. Messages get short ids (m1, m2…) so the model can't garble them.
3. Anything the model skips or labels with a made-up type falls back to the **keyword classifier**,
   which also runs everything when no AI is available (`--keyword`, or "Rules only" in the app).
4. Each non-noise message is **linked to a task**: an exact `#12` in the message, else a single `#12`
   elsewhere in the conversation, else a semantic match against GitHub issues and Notion tasks above a
   similarity threshold (`TEAMPULSE_LINK_THRESHOLD`). Week 6 adds LLM-confirmed linking.

Only new or edited messages are classified; deleted messages lose their labels automatically. If you scan
before setting up an AI key, messages get keyword labels: use **Re-scan everything** afterwards.

**Answering** ("what is blocking us?", "kya atka hua hai?", "what did we decide?"): the Supervisor routes
these to the `blockers` specialist, which picks the message types and time window from the question,
adds each linked task's current GitHub/Notion state, and marks each item:

- **open**: nothing says it's fixed
- **resolved in chat**: a later message says it was fixed
- **possibly resolved**: the linked task was closed or marked done *after* the blocker was reported

It flags contradictions such as "chat says blocked, task says done". With no AI available it still
answers with a cited list of what it found.

### Measuring accuracy

```bash
python -m teampulse.eval.blockers                       # dev set: keyword baseline vs LLM
python -m teampulse.eval.blockers --data db             # your own labels from the Label chat page
```

Reports (Markdown tables ready for your report, plus JSON) are saved to `data/eval/`. Each report has
accuracy, macro F1, precision/recall/F1 per type, a confusion matrix and the misclassified messages.

**Use your own labels for the final numbers.** `eval_data/hinglish_dev.jsonl` is 60 synthetic messages I
wrote for building and testing. The keyword rules were adjusted once against it, so scores on it are
optimistic. For your report, label ~300 real messages from your team's server on the **Label chat** page
(about 45 minutes; a teammate labelling a shared subset lets you report agreement too), then run
`--data db`. The keyword baseline gets about 50% accuracy on the dev set; that's the bar the LLM has to beat.


### Supervisor Agent (Week 4)

```
question + chat history → route → run_specialists ─┬─ one part ─→ answer as-is
                                                   └─ many parts → synthesize → check citations
```

1. **route** (LLM, JSON): rewrites follow-ups into a standalone question ("and who owns it?" →
   "Who owns the payments task?"), and splits the question into at most 3 parts only when it asks about
   clearly separate things. Each part is sent to the best-fitting specialist.
2. **run_specialists**: runs each part in turn (one at a time, which is kinder to free-tier limits).
3. **one part** → the specialist's answer is returned unchanged, so simple questions cost no extra LLM call.
4. **many parts → synthesize**: evidence from all parts is merged and de-duplicated, every partial answer's
   citations are renumbered to the merged list, and the LLM writes one combined answer. Citations are
   checked again afterwards.

Specialists are plug-ins. Today there is one (`retrieval`); Weeks 5–8 add `blockers`, `discrepancies` and
`contributions` with `supervisor.register(Specialist(name, description, run))`, and the router starts
offering them automatically from their descriptions.

If the router fails (rate limit, bad JSON), the whole question goes to the Retrieval Agent.

### Tracing with Arize Phoenix (optional, free, local)

```bash
pip install arize-phoenix
phoenix serve                     # terminal 1 → open http://localhost:6006
# in .env: TEAMPULSE_TRACING=phoenix
streamlit run app.py              # terminal 2
```

Each question then shows as a nested trace: supervisor → specialist → each LangGraph node → each LLM call,
with prompts, responses, tokens and latency. Great for debugging and for a demo slide. The app's own
"Agent trace" panel shows the same structure without Phoenix.

### Retrieval Agent (Week 3)

```
question → plan → retrieve → answer → check_citations → answer + sources + trace
```

1. **plan** (LLM, JSON): 1–4 search queries (adds a Hinglish phrasing when chat is likely relevant),
   which sources to search, and a time window if the question has one ("this week").
2. **retrieve** (no LLM): semantic search for each query, filtered by source and time, plus all activity
   in the time window for "what happened" questions. Results are merged, deduplicated, capped at 24.
3. **answer** (LLM): answers only from numbered evidence, citing `[n]` after each statement, and
   says so when sources disagree or the evidence is missing.
4. **check_citations** (no LLM): removes citations to evidence that doesn't exist and warns when an
   answer cites nothing. The Verifier (Week 9) then checks each cited statement's meaning.

If the planner fails, the agent falls back to searching the raw question. If no LLM is reachable, the app
still shows the evidence it found.

## Design notes for your report

- **One event format for all sources**, each with a link back to the original, so every answer can cite.
- **Multilingual embeddings** (`multilingual-e5-small`) so "OTP nahi aa raha" matches "OTP not arriving".
- **Incremental indexing:** only new or edited events are embedded; deleted ones are removed, which also
  honours `!teampulse leave`.
- **LLM only where judgment is needed.** Ingestion, filtering, merging and citation checks are plain code,
  which keeps them reliable, testable and cheap on free quotas.
- **`#123` references are extracted** from all sources (`extra.issue_refs`) as an exact first pass at
  linking chat to tasks, before AI linking (Week 6).
- **Known limits:** Notion reads top-level blocks only; archived Discord threads aren't backfilled;
  GitHub commits come from the default branch.

## Project status

All 10 weeks are built. What's left is yours to do with real data:

1. Run TeamPulse on your own team for 2–3 weeks (Discord bot + pollers, Ollama for privacy).
2. Label ~300 chat messages, correct ~100 chat links, and give verdicts on discrepancies in the app.
3. Run `python -m teampulse.eval.run_all` with AI keys and put the tables in your report.
4. Deploy the public demo and record a backup video (see the report draft for a script).
