"""Fill the database with a realistic demo capstone team, with planted problems and a known answer key.

    python -m teampulse.seed_demo            # writes data/demo.db (set TEAMPULSE_DB=data/demo.db to use it)
    python -m teampulse.seed_demo --db data/teampulse.db

Scenario: "CampusEats", a canteen pre-order app built by Riya (frontend), Aman (backend),
Kabir (payments + ML) and Neha (PM/docs). Times are relative to now, so "this week" always works.
The answer key (data/demo_answer_key.json) is used for evaluation in later weeks.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teampulse import db
from teampulse.models import Event, issue_refs

REPO = "campuseats/app"
NOW = datetime.now(timezone.utc).replace(microsecond=0)

PEOPLE = {
    # name: (github, discord, notion)
    "Riya": ("riya-dev", "1001", "n-riya"),
    "Aman": ("aman-codes", "1002", "n-aman"),
    "Kabir": ("kabir-ml", "1003", "n-kabir"),
    "Neha": ("neha-pm", "1004", "n-neha"),
}
GH = {name: ids[0] for name, ids in PEOPLE.items()}
DC = {name: ids[1] for name, ids in PEOPLE.items()}
NT = {name: ids[2] for name, ids in PEOPLE.items()}


def ago(days: float, hours: float = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).isoformat()


def ahead(days: int) -> str:
    return (NOW + timedelta(days=days)).date().isoformat()


def gh_item(number, title, body, author, kind="issue", status="open", assignees=(), created=10, updated=5,
            labels=(), milestone=None, milestone_due=None):
    return Event(
        source="github", kind=kind, native_id=f"{REPO}#{number}", author=GH[author], author_name=GH[author],
        title=title, body=body, status=status, assignees=[GH[a] for a in assignees],
        url=f"https://github.com/{REPO}/{'pull' if kind == 'pr' else 'issues'}/{number}", channel=REPO,
        created_at=ago(created), updated_at=ago(updated),
        extra={"number": number, "labels": list(labels), "milestone": milestone, "milestone_due": milestone_due,
               "merged_at": ago(updated) if status == "merged" else None, "issue_refs": issue_refs(title, body)},
    )


def gh_comment(cid, number, author, body, days):
    return Event(
        source="github", kind="issue_comment", native_id=f"comment:{cid}", author=GH[author],
        author_name=GH[author], body=body, url=f"https://github.com/{REPO}/issues/{number}#issuecomment-{cid}",
        channel=REPO, parent_id=f"github:{REPO}#{number}", created_at=ago(days), updated_at=ago(days),
        extra={"number": number, "issue_refs": issue_refs(body)},
    )


def gh_commit(sha, author, message, days):
    title, _, rest = message.partition("\n")
    return Event(
        source="github", kind="commit", native_id=f"commit:{sha}", author=GH[author], author_name=GH[author],
        title=title, body=rest.strip(), url=f"https://github.com/{REPO}/commit/{sha}", channel=REPO,
        created_at=ago(days), updated_at=ago(days), extra={"sha": sha, "issue_refs": issue_refs(message)},
    )


def gh_review(rid, number, author, state, body, days):
    return Event(
        source="github", kind="review", native_id=f"review:{rid}", author=GH[author], author_name=GH[author],
        body=body, status=state, url=f"https://github.com/{REPO}/pull/{number}#pullrequestreview-{rid}",
        channel=REPO, parent_id=f"github:{REPO}#{number}", created_at=ago(days), updated_at=ago(days),
        extra={"number": number},
    )


def notion_page(pid, title, status, owners, due, editor, edited, body, channel="Tasks"):
    return Event(
        source="notion", kind="notion_page", native_id=pid, author=NT[editor], author_name=editor,
        title=title, body=body, status=status, assignees=list(owners),
        url=f"https://www.notion.so/{pid}", channel=channel, created_at=ago(20), updated_at=ago(edited),
        extra={"due": due, "last_edited_by": NT[editor], "issue_refs": issue_refs(title, body),
               "properties": {"Status": status, "Owner": list(owners), "Due": due}},
    )


_msg_counter = [900000]


def dc(author, text, days, hours=0, channel="general", reply_to=None):
    _msg_counter[0] += 1
    mid = str(_msg_counter[0])
    return Event(
        source="discord", kind="message", native_id=mid, author=DC[author], author_name=author, body=text,
        url=f"https://discord.com/channels/demo/{channel}/{mid}", channel=channel,
        parent_id=f"discord:{reply_to}" if reply_to else None, created_at=ago(days, hours),
        updated_at=ago(days, hours), extra={"issue_refs": issue_refs(text)},
    )


def build() -> tuple[list[Event], dict]:
    _msg_counter[0] = 900000   # same message ids on every build
    ev: list[Event] = []

    # ---------- Notion: plan page + task database ----------
    ev.append(notion_page(
        "plan-campuseats", "Project Plan — CampusEats", None, [], None, "Neha", 18,
        "# Goal\nStudents pre-order canteen food and skip the queue.\n"
        "# Milestones\n- M1 (" + ahead(-3) + "): college-email login, menu API\n"
        "- M2 (" + ahead(8) + "): payments (Razorpay test mode), order history\n"
        "- Final review: " + ahead(18) + "\n"
        "# Decisions\n- Database: Firebase (to be confirmed)\n"
        "# Roles\n- Riya: frontend\n- Aman: backend\n- Kabir: payments + recommendations\n- Neha: PM, report",
        channel="plan pages"))
    ev.append(notion_page("task-login", "Login with college email OTP (#12)", "Done", ["Aman"], ahead(-3),
                          "Neha", 2, "[x] UI\n[x] OTP email\n[x] Session handling"))
    ev.append(notion_page("task-menu", "Menu API (#14)", "In progress", ["Aman"], ahead(-3), "Neha", 13,
                          "[x] Schema\n[ ] Endpoints\n[ ] Tests"))
    ev.append(notion_page("task-payment", "Payment integration (#16)", "In progress", ["Kabir"], ahead(2),
                          "Neha", 9, "Razorpay test mode.\n[ ] Checkout\n[ ] Webhook"))
    ev.append(notion_page("task-recs", "Food recommendations (#18)", "In progress", ["Kabir"], ahead(8),
                          "Kabir", 6, "Need past order data from canteen."))
    ev.append(notion_page("task-history", "Order history screen (#20)", "In review", ["Riya"], ahead(8),
                          "Riya", 1, "[x] List view\n[ ] Filters"))
    ev.append(notion_page("task-deploy", "Deploy on Render (#22)", "Not started", ["Aman"], ahead(15),
                          "Neha", 14, ""))
    ev.append(notion_page("doc-report", "Project Report draft", "In progress", ["Neha"], ahead(16), "Neha", 1,
                          "# Introduction\nCanteen queues waste 15–20 minutes of a student's lunch break...\n"
                          "# Literature survey\n(todo)", channel="docs"))

    # ---------- GitHub ----------
    ev += [
        gh_item(12, "Login with college email OTP", "Use college email domain + OTP.", "Neha",
                status="open", assignees=["Aman"], created=20, updated=1, labels=["backend"], milestone="M1",
                milestone_due=ago(3)),
        gh_comment(5001, 12, "Aman", "OTP mails are landing in spam for the college domain. Trying SPF/DKIM.", 3),
        gh_comment(5002, 12, "Riya", "Still not getting the OTP on my college ID, tested again today.", 1),
        gh_item(14, "Menu API", "CRUD endpoints for canteen menu.", "Neha", status="closed", assignees=["Aman"],
                created=20, updated=3, labels=["backend"], milestone="M1", milestone_due=ago(3)),
        gh_item(15, "Menu API endpoints + tests (fixes #14)", "Adds /menu endpoints and pytest tests.", "Aman",
                kind="pr", status="merged", assignees=["Aman"], created=5, updated=3),
        gh_review(7001, 15, "Riya", "approved", "Works with the frontend, looks good.", 3),
        gh_item(16, "Payment integration (Razorpay test mode)", "Checkout + webhook.", "Neha", status="open",
                assignees=["Kabir"], created=18, updated=2, labels=["backend", "payments"], milestone="M2",
                milestone_due=ahead(8)),
        gh_commit("a1f9c2e", "Aman", "Add Razorpay order creation (#16)", 2),
        gh_commit("b7d0e41", "Aman", "Razorpay webhook signature check (#16)", 1),
        gh_item(18, "Food recommendation model", "Recommend items from past orders.", "Kabir", status="open",
                assignees=["Kabir"], created=15, updated=6, labels=["ml"], milestone="M2", milestone_due=ahead(8)),
        gh_commit("c3e88aa", "Kabir", "Notebook: baseline popularity recommender (#18)", 6),
        gh_item(20, "Order history screen", "Show past orders with filters.", "Riya", status="open",
                assignees=["Riya"], created=10, updated=1, labels=["frontend"], milestone="M2",
                milestone_due=ahead(8)),
        gh_item(21, "Order history list view (#20)", "List view; filters next.", "Riya", kind="pr", status="open",
                assignees=["Riya"], created=4, updated=1),
        gh_review(7002, 21, "Aman", "changes_requested", "Please paginate, API returns max 50 orders.", 2),
        gh_commit("d41bb07", "Riya", "Paginate order history (#20)", 1),
        gh_item(22, "Deploy on Render", "Backend + frontend deploy.", "Neha", status="open", assignees=["Aman"],
                created=14, updated=14, labels=["devops"]),
        gh_commit("e90f1d3", "Riya", "Login page UI (#12)", 9),
        gh_commit("f12a7c8", "Aman", "OTP email sender (#12)", 8),
    ]

    # ---------- Discord (Hinglish, informal) ----------
    m_login = dc("Riya", "bhai login OTP abhi bhi nahi aa raha, spam me ja raha hai shayad. #12", 1, 3, "backend")
    m_pay1 = dc("Kabir", "guys payment wala kaam actually Aman dekh raha hai, mujhe recommendation pe focus karna hai",
                4, 0, "backend")
    m_pay2 = dc("Aman", "haan payment main kar raha hu, Razorpay test mode me order create ho raha hai. "
                "Webhook baaki hai, M2 tak ho jayega", 4, -1, "backend", reply_to=m_pay1.native_id)
    m_data = dc("Kabir", "canteen se past orders ka data abhi tak nahi mila 😕 ma'am se baat karni padegi, "
                "uske bina model train nahi ho sakta", 3, 2, "ml")
    m_review = dc("Riya", "order history PR me pagination add kar diya, @Aman please re-review kar de", 1, 1,
                  "frontend")
    m_db = dc("Aman", "final decision: Firebase nahi, Postgres use karenge. Render pe free Postgres mil raha hai",
              5, 0, "general")
    m_db_ok = dc("Neha", "ok done 👍 plan update kar dungi", 5, -1, "general", reply_to=m_db.native_id)
    m_deploy = dc("Neha", "deploy kab karenge? final review 18 din me hai, kuch toh live chahiye", 2, 0, "general")
    m_report = dc("Neha", "maine report ka introduction likh diya hai, sab log apna section add karo", 1, 2,
                  "general")
    m_kabir_claim = dc("Kabir", "recommendation model almost 80% done hai", 2, 5, "ml")
    noise = [
        dc("Riya", "lunch?", 2, 6, "general"),
        dc("Aman", "😂😂", 2, 5.9, "general"),
        dc("Kabir", "kal class hai kya 9 baje wali?", 3, 8, "general"),
        dc("Neha", "haan hai", 3, 7.8, "general"),
    ]
    # Week 8: a teammate helping with someone else's blocker (shows up under "Helping others")
    m_help = dc("Neha", "main ma'am se baat kar rahi hu data ke liye, kal tak update deti hu", 3, 1, "ml",
                reply_to=m_data.native_id)
    ev += [m_login, m_pay1, m_pay2, m_data, m_review, m_db, m_db_ok, m_deploy, m_report, m_kabir_claim, *noise, m_help]

    key = {
        "discrepancies": [
            {"task": "#12 Login OTP", "type": "status",
             "detail": "Notion says Done; GitHub issue #12 open; Discord says OTP still not arriving",
             "events": ["notion:task-login", f"github:{REPO}#12", f"discord:{m_login.native_id}"]},
            {"task": "#14 Menu API", "type": "stale_plan",
             "detail": "Notion 'In progress' (edited 13 days ago); PR #15 merged and issue #14 closed",
             "events": ["notion:task-menu", f"github:{REPO}#15", f"github:{REPO}#14"]},
            {"task": "#16 Payments", "type": "owner",
             "detail": "Notion and GitHub assign Kabir; Aman is doing the work (commits + Discord)",
             "events": ["notion:task-payment", f"github:{REPO}#16", f"discord:{m_pay1.native_id}",
                        "github:commit:a1f9c2e"]},
            {"task": "#16 Payments", "type": "deadline",
             "detail": f"Notion due {ahead(2)}; GitHub milestone M2 due {ahead(8)}; Aman says 'M2 tak'",
             "events": ["notion:task-payment", f"github:{REPO}#16", f"discord:{m_pay2.native_id}"]},
            {"task": "Database decision", "type": "stale_plan",
             "detail": "Plan page says Firebase; Discord decision says Postgres",
             "events": ["notion:plan-campuseats", f"discord:{m_db.native_id}"]},
        ],
        "blockers": [
            {"event": f"discord:{m_login.native_id}", "task": "#12", "reason": "OTP emails go to spam"},
            {"event": f"discord:{m_data.native_id}", "task": "#18", "reason": "No past order data from canteen"},
        ],
        "help_requests": [{"event": f"discord:{m_review.native_id}", "task": "#20"}],
        "decisions": [{"event": f"discord:{m_db.native_id}", "detail": "Postgres instead of Firebase"}],
        "noise": [f"discord:{m.native_id}" for m in noise],
        # Which task each non-noise chat message is about (None = not about a task). For linking evaluation.
        "chat_links": {
            f"discord:{m.native_id}": task for m, task in [
                (m_login, f"task:{REPO}#12"), (m_pay1, f"task:{REPO}#16"), (m_pay2, f"task:{REPO}#16"),
                (m_data, f"task:{REPO}#18"), (m_kabir_claim, f"task:{REPO}#18"), (m_help, f"task:{REPO}#18"), (m_review, f"task:{REPO}#20"),
                (m_deploy, f"task:{REPO}#22"), (m_report, "task:notion:doc-report"), (m_db, None)]
        },
        # Gold label for every demo chat message (used by the classifier evaluation).
        "message_labels": {
            f"discord:{m.native_id}": label for m, label in [
                (m_login, "blocker"), (m_pay1, "progress"), (m_pay2, "progress"), (m_data, "blocker"),
                (m_review, "help_request"), (m_db, "decision"), (m_db_ok, "noise"), (m_deploy, "help_request"),
                (m_report, "progress"), (m_kabir_claim, "progress"), (m_help, "progress"), *[(m, "noise") for m in noise]]
        },
        "contribution_claims": [
            {"member": "Neha", "claim": "wrote report introduction", "event": f"discord:{m_report.native_id}",
             "verdict": "verified", "evidence": ["notion:doc-report"]},
            {"member": "Kabir", "claim": "recommendation model 80% done", "event": f"discord:{m_kabir_claim.native_id}",
             "verdict": "weak_evidence", "evidence": ["github:commit:c3e88aa"],
             "note": "One notebook commit 6 days ago; blocked on data"},
        ],
    }
    return ev, key


def seed(path: str) -> tuple[int, Path]:
    if Path(path).exists():
        db.wipe(path)   # demo data is always rebuilt fresh (emptied in place: works with the file open)
    conn = db.connect(path)
    events, key = build()
    count = db.upsert_events(conn, events)
    with conn:
        for name, (gh, dc_id, nt) in PEOPLE.items():
            conn.execute(
                "INSERT INTO members (display_name, discord_id, github_login, notion_user_id, consent, consented_at) "
                "VALUES (?, ?, ?, ?, 1, ?)", (name, dc_id, gh, nt, ago(21)))
        conn.executemany("INSERT INTO labels (event_id, label, labeller, labelled_at) VALUES (?, ?, 'answer-key', ?)",
                         [(event_id, label, ago(0)) for event_id, label in key["message_labels"].items()])
        # Week 8: every demo member has reviewed their contribution report; Kabir added context.
        notes = {"Kabir": "Most of my recommendation work is in a Colab notebook that isn't in the repo yet; "
                          "I'll push it once we get the canteen data."}
        conn.executemany("INSERT INTO contribution_reviews VALUES (?, 'approved', ?, ?)",
                         [(name, notes.get(name), ago(0.5)) for name in PEOPLE])
        conn.execute("INSERT INTO memory (fact, source, created_at, kind, subject, fact_date, active, created_by) "
                     "VALUES (?, 'user', ?, 'deadline', 'final review', ?, 1, 'Neha')",
                     (f"Our final review is on {ahead(18)}", ago(3), ahead(18)))
    seed_projects(conn)
    key_path = Path(path).with_name("demo_answer_key.json")
    key_path.write_text(json.dumps(key, indent=2, ensure_ascii=False))
    return count, key_path


def seed_projects(conn) -> None:
    """TeamPulse 2.0 demo plan: one project the team runs inside TeamPulse, matching the story in the chat data
    (OTP mails in spam, Kabir waiting for canteen data, Riya's order history in review)."""
    from datetime import timedelta

    from teampulse import pm

    neha = auth_actor("Neha")
    on = NOW.date()   # the same clock as the GitHub/Notion demo data, so dates agree
    d = lambda days: (on + timedelta(days=days)).isoformat()   # noqa: E731
    pid = pm.create_project(conn, neha, "CampusEats MVP", "Canteen ordering app for our final-year project.",
                            d(-21), d(18))
    m_beta = pm.add_milestone(conn, neha, pid, "Beta for hostel 3", d(6))
    m_final = pm.add_milestone(conn, neha, pid, "Final review", d(18))
    t = lambda title, who, **kw: pm.create_task(conn, neha, pid, title, assign_to=who, **kw)   # noqa: E731
    login = t("Login with college email OTP", ["Aman"], priority="high", due=d(-3), start=d(-16), milestone_id=m_beta,
              description="College email domain + 6-digit OTP.", estimate_hours=10, weight=3)
    menu = t("Menu API", ["Aman"], priority="high", due=d(-3), start=d(-14), milestone_id=m_beta, estimate_hours=12, weight=3)
    payment = t("Payment integration (Razorpay test mode)", ["Kabir", "Aman"], priority="urgent", due=d(2), start=d(-6),
                milestone_id=m_beta, depends_on=[menu], estimate_hours=16, weight=5,
                description="Checkout page + webhook. Test mode only.")
    recs = t("Food recommendations", ["Kabir"], priority="medium", due=d(8), start=d(-10), milestone_id=m_final, estimate_hours=20,
             weight=5)
    history = t("Order history screen", ["Riya"], priority="medium", due=d(8), milestone_id=m_final,
                depends_on=[menu], estimate_hours=8, weight=2)
    deploy = t("Deploy on Render", ["Aman"], priority="high", due=d(15), milestone_id=m_final,
               depends_on=[payment], estimate_hours=6, weight=2)
    report = t("Project report draft", ["Neha"], priority="medium", due=d(16), start=d(-4), milestone_id=m_final, estimate_hours=14,
               weight=3)
    t("Usability test with 5 hostel students", ["Riya", "Neha"], priority="low", due=d(12), status="backlog",
      milestone_id=m_final)
    t("Demo video", [], priority="low", due=d(17), status="backlog", milestone_id=m_final)
    t("Share the API docs with Riya", ["Aman"], priority="medium", due=d(-1), milestone_id=m_beta)

    kabir, aman, riya = auth_actor("Kabir"), auth_actor("Aman"), auth_actor("Riya")
    for title in ("College email check", "OTP mail", "Rate limit resend"):
        pm.set_status(conn, aman, pm.create_task(conn, aman, pid, title, parent_id=login), "done")
    pm.set_status(conn, aman, login, "in_progress")
    pm.set_progress(conn, aman, login, 90)
    pm.set_status(conn, aman, login, "review", note="OTP mails land in spam on Outlook; SPF/DKIM added, please check.")
    pm.set_status(conn, neha, login, "done")
    for title, done in (("GET /menu", True), ("POST/PUT/DELETE /menu", True), ("pytest tests", True)):
        sub = pm.create_task(conn, aman, pid, title, parent_id=menu)
        if done:
            pm.set_status(conn, aman, sub, "done")
    pm.set_status(conn, aman, menu, "in_progress")
    pm.set_progress(conn, aman, menu, 100)
    pm.set_status(conn, aman, menu, "review")
    pm.set_status(conn, neha, menu, "done")

    for title, done in (("Checkout page", True), ("Webhook handler", False), ("Refund flow", False)):
        sub = pm.create_task(conn, kabir, pid, title, parent_id=payment)
        if done:
            pm.set_status(conn, kabir, sub, "done")
    pm.set_status(conn, kabir, payment, "in_progress")
    pm.set_progress(conn, kabir, payment, 80, note="Checkout page works in test mode.",
                    evidence_url="https://github.com/campuseats/app/pull/17")   # reported 80%, 1 of 3 subtasks done
    pm.verify_progress(conn, neha, payment, 40, "Checkout works; webhook and refunds not started.")
    pm.add_comment(conn, aman, payment, "@Kabir I can take the refund flow once the webhook is in.")

    pm.set_status(conn, kabir, recs, "blocked", note="Still no past order data from the canteen.")
    pm.set_progress(conn, kabir, recs, 30)
    pm.request_deadline(conn, kabir, recs, d(13), "The canteen data comes on Monday; training needs 3 more days.")

    pm.set_status(conn, riya, history, "in_progress")
    for title, done in (("List view", True), ("Filters", False)):
        sub = pm.create_task(conn, riya, pid, title, parent_id=history)
        if done:
            pm.set_status(conn, riya, sub, "done")
    pm.set_progress(conn, riya, history, 60, note="List view merged.",
                    evidence_url="https://github.com/campuseats/app/pull/21")
    pm.set_status(conn, riya, history, "review", note="List view done; filters next week.")
    pm.set_status(conn, neha, report, "in_progress")
    pm.set_progress(conn, neha, report, 35)
    # Phase 3: planned tasks linked to their GitHub issue / Notion page. Stored directly because the source task
    # registry is built after seeding (webapp / Rebuild tasks); ids are stable. "Usability test", "Demo video" and
    # "Share the API docs" exist only in TeamPulse.
    links = {login: ("task:campuseats/app#12", "Login with college email OTP"),
             menu: ("task:campuseats/app#14", "Menu API"),
             payment: ("task:campuseats/app#16", "Payment integration (Razorpay test mode)"),
             recs: ("task:campuseats/app#18", "Food recommendation model"),
             history: ("task:campuseats/app#20", "Order history screen"),
             deploy: ("task:campuseats/app#22", "Deploy on Render")}
    with conn:
        conn.executemany("INSERT INTO pm_source_links (task_id, source_task, source_title, linked_by, linked_at) "
                         "VALUES (?, ?, ?, 'Neha', ?)", [(tid, sid, title, ago(1)) for tid, (sid, title) in links.items()])
    del report

    # Phase 5: real teams have history, so the daily check has something to notice. Kabir has been blocked for four
    # days, Riya's work has waited three days for review, his deadline request three days for an answer, and payment
    # has crept from 30% to 80% over nine days while it's due in two.
    with conn:
        for table, col in (("pm_history", "at"), ("pm_comments", "created_at"), ("pm_progress_updates", "created_at")):
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE task_id = ?", (ago(4), recs))
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE task_id = ?", (ago(3), history))
        conn.execute("UPDATE pm_requests SET created_at = ? WHERE task_id = ?", (ago(3), recs))
        conn.execute("UPDATE pm_progress_updates SET created_at = ? WHERE task_id = ? AND kind = 'reported'",
                     (ago(1), payment))
        conn.execute("INSERT INTO pm_progress_updates (task_id, kind, author_id, author, progress, note, created_at) "
                     "VALUES (?, 'reported', 'seed-kabir', 'Kabir', 30, 'Started the checkout page.', ?)",
                     (payment, ago(10)))

    # Phase 4: a proposal from yesterday's meeting notes, waiting for Neha to confirm (made by the rules, so the
    # demo needs no AI key). Nothing in it is a task until she ticks items and presses Create.
    from teampulse import pm_ai
    pm_ai.propose_plan(conn, neha, pid, "Meeting notes, hostel 3 beta\n"
                       "- Riya: design the feedback form by " + d(8) + "\n"
                       "- Aman will set up error logging (urgent)\n"
                       "- Kabir — canteen data cleaning, Friday tak\n"
                       "- Write the demo script", on=on)


def auth_actor(name: str):
    """A stand-in signed-in person for seeding (leaders: Neha; everyone else a member)."""
    from teampulse.auth import Actor
    role = "leader" if name == "Neha" else "member"
    return Actor(f"seed-{name.lower()}", name.lower(), name, role, name, False)


DEMO_ACCOUNTS = [("Neha (demo leader)", "neha", "leader", "Neha"), ("Kabir (demo member)", "kabir", "member", "Kabir"),
                 ("Aman (demo member)", "aman", "member", "Aman"), ("Riya (demo member)", "riya", "member", "Riya")]


def create_demo_accounts(path: str) -> dict[str, str]:
    """A leader (Neha) and a member (Kabir) for the demo team, with fresh random passwords.

    Returns {username: password} so the caller can print them once; only salted hashes are stored."""
    from teampulse import auth

    conn = db.connect(path)
    out = {}
    for display, username, role, member in DEMO_ACCOUNTS:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            continue
        password = auth.generate_password()
        auth._insert_user(conn, display, username, role, member, password, False, None)
        out[username] = password
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/demo.db")
    args = parser.parse_args()
    count, key_path = seed(args.db)
    print(f"Seeded {count} events into {args.db}. Answer key: {key_path}")
    accounts = create_demo_accounts(args.db)
    print("\nDemo sign-ins (shown once, only hashes are stored):")
    for username, password in accounts.items():
        role = next(r for _, u, r, _m in DEMO_ACCOUNTS if u == username)
        print(f"  {role:7} username: {username:6} password: {password}")


if __name__ == "__main__":
    main()
