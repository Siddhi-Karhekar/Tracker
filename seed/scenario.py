"""The seeded demo: one fictional student team, one month of work, with known answers.

Team "CampusEats" builds a canteen pre-order app. Everything below is fictional.
All times are IST (UTC+05:30). seed/build.py turns this file into platform-shaped JSON
(GitHub REST, Notion API, DiscordChatExporter) plus data/demo/answer_key.json.

Planted on purpose (see DISCREPANCIES):
  D1 status    Login: Notion "Done", GitHub issue reopened, Discord says it crashes
  D2 owner     Payment: assigned to Rohan everywhere, but Priya wrote the PR and said so
  D3 deadline  Tracking screen: Notion 30 Oct, GitHub milestone 5 Nov, Discord 2 Nov
  D4 stale     Reco model: Notion "In progress" untouched since 1 Oct; PR merged 17 Oct
  D5 status    Notifications: Rohan says "done" twice; no PR, no commits, issue open
  D6 stale     Admin dashboard: dropped in Discord on 22 Oct; still live in Notion & GitHub
  D7 owner     Cart UI: assigned to Aarav, built and merged by Siddhi
  D8 deadline  Report: Notion 10 Nov, Discord (confirmed by faculty) 8 Nov
Every other task is consistent across sources, so anything flagged there is a false alarm.
"""
from __future__ import annotations

REPO = "campuseats/campuseats-app"
GUILD = "CampusEats Team"
PROJECT_START = "2026-09-28 18:00"
SNAPSHOT_AT = "2026-10-26 09:00"  # "now" for the demo

# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------
MEMBERS = {
    "siddhi": {"display_name": "Siddhi K", "github": "siddhi-k", "discord_name": "siddhi.k"},
    "aarav": {"display_name": "Aarav M", "github": "aarav-m", "discord_name": "aarav_m"},
    "priya": {"display_name": "Priya S", "github": "priya-s", "discord_name": "priya.codes"},
    "rohan": {"display_name": "Rohan D", "github": "rohan-d", "discord_name": "rohan_d"},
}

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
MILESTONES = {
    "Sprint 1": "2026-10-03",
    "Sprint 2": "2026-10-10",
    "Sprint 3": "2026-10-24",
    "Sprint 4": "2026-11-05",
}

# state: open/closed. events: list of (time, actor, "closed"|"reopened"), closing PR noted on PR side.
ISSUES = [
    dict(number=1, title="Login & signup with OTP", assignee="aarav", milestone="Sprint 2",
         labels=["frontend", "auth"], created="2026-09-28 18:40", author="siddhi",
         body="Phone-number login with OTP, signup form, session handling.",
         events=[("2026-10-04 11:50", "aarav", "closed"), ("2026-10-16 22:18", "aarav", "reopened")],
         comments=[("2026-10-16 22:10", "siddhi",
                    "Crashes after OTP entry: white screen on Android Chrome. Reopening?"),
                   ("2026-10-23 17:50", "siddhi",
                    "Token is returned by /auth/verify; frontend reads localStorage 'authToken' but we store 'token'.")]),
    dict(number=2, title="Menu API (list, item detail, availability)", assignee="siddhi", milestone="Sprint 1",
         labels=["backend"], created="2026-09-28 18:42", author="siddhi",
         body="REST endpoints for the canteen menu, with daily availability.",
         events=[("2026-10-02 11:00", "siddhi", "closed")], comments=[]),
    dict(number=3, title="Payment gateway (Razorpay test mode)", assignee="rohan", milestone="Sprint 3",
         labels=["backend", "payments"], created="2026-09-28 18:44", author="siddhi",
         body="Integrate Razorpay checkout in test mode, verify webhook signature.",
         events=[("2026-10-16 15:30", "siddhi", "closed")], comments=[]),
    dict(number=4, title="Order tracking screen", assignee="aarav", milestone="Sprint 4",
         labels=["frontend"], created="2026-09-28 18:46", author="siddhi",
         body="Live order status + pickup counter location on a map.", events=[], comments=[]),
    dict(number=5, title="Recommendation model (what to order)", assignee="priya", milestone="Sprint 3",
         labels=["ml"], created="2026-09-28 18:48", author="siddhi",
         body="Suggest items from order history; handle new users.",
         events=[("2026-10-17 18:30", "siddhi", "closed")], comments=[]),
    dict(number=6, title="Database schema migration (orders, users)", assignee="siddhi", milestone="Sprint 4",
         labels=["backend", "db"], created="2026-09-28 18:50", author="siddhi",
         body="Move to Postgres schema v2 with proper foreign keys.", events=[], comments=[]),
    dict(number=7, title="Push notifications for order ready", assignee="rohan", milestone="Sprint 3",
         labels=["backend", "mobile"], created="2026-09-28 18:52", author="siddhi",
         body="Notify the user (FCM) when their order is ready for pickup.", events=[], comments=[]),
    dict(number=8, title="Admin dashboard for canteen staff", assignee="aarav", milestone="Sprint 3",
         labels=["frontend"], created="2026-09-28 18:54", author="siddhi",
         body="Staff view to mark items unavailable and orders ready.", events=[], comments=[]),
    dict(number=9, title="CI pipeline (GitHub Actions)", assignee="rohan", milestone="Sprint 2",
         labels=["devops"], created="2026-09-28 18:56", author="siddhi",
         body="Lint + tests on every PR.", events=[("2026-10-11 20:15", "rohan", "closed")], comments=[]),
    dict(number=10, title="Cart & checkout UI", assignee="aarav", milestone="Sprint 4",
         labels=["frontend"], created="2026-09-28 18:58", author="siddhi",
         body="Cart page, quantity edit, checkout button wired to payments.",
         events=[("2026-10-18 13:00", "siddhi", "closed")], comments=[]),
    dict(number=11, title="Load testing", assignee="rohan", milestone=None,
         labels=["devops"], created="2026-09-28 19:00", author="siddhi",
         body="Simulate lunch-hour rush: 300 concurrent orders.", events=[], comments=[]),
    dict(number=12, title="Project report & documentation", assignee="priya", milestone=None,
         labels=["docs"], created="2026-09-28 19:02", author="siddhi",
         body="Final report, architecture diagrams, user manual.", events=[], comments=[]),
]

# PR numbers continue after the issues (GitHub shares one counter).
PULLS = [
    dict(number=13, title="Menu API endpoints", author="siddhi", closes=[2], branch="feat/menu-api",
         created="2026-09-30 22:10", merged="2026-10-02 11:00", merged_by="siddhi",
         reviews=[("2026-09-30 22:40", "rohan", "APPROVED", "LGTM, add pagination later")],
         commits=[("2026-09-29 21:30", "feat(api): menu list + item detail"),
                  ("2026-09-30 21:55", "feat(api): availability flag per day")]),
    dict(number=14, title="Login & signup with OTP", author="aarav", closes=[1], branch="feat/login-otp",
         created="2026-10-03 18:00", merged="2026-10-04 11:50", merged_by="aarav",
         reviews=[("2026-10-04 10:30", "siddhi", "APPROVED", "")],
         commits=[("2026-10-02 23:10", "feat(ui): login + signup screens"),
                  ("2026-10-03 17:40", "feat(ui): OTP flow")]),
    dict(number=15, title="CI: lint and test workflow", author="rohan", closes=[9], branch="ci/actions",
         created="2026-10-10 19:00", merged="2026-10-11 20:15", merged_by="rohan",
         reviews=[("2026-10-11 19:50", "siddhi", "APPROVED", "")],
         commits=[("2026-10-10 18:40", "ci: add lint + pytest workflow"),
                  ("2026-10-11 19:30", "ci: cache pip deps")]),
    dict(number=16, title="Recommendation model v1 + popular-items fallback", author="priya", closes=[5],
         branch="ml/reco-v1", created="2026-10-17 12:00", merged="2026-10-17 18:30", merged_by="siddhi",
         reviews=[("2026-10-17 17:45", "siddhi", "APPROVED", "")],
         commits=[("2026-10-08 13:00", "ml: item-item collaborative filtering"),
                  ("2026-10-13 15:10", "ml: augment with public food dataset"),
                  ("2026-10-16 22:30", "ml: popular-items fallback for cold start")]),
    dict(number=17, title="Razorpay checkout (test mode)", author="priya", closes=[3], branch="feat/razorpay",
         created="2026-10-15 11:50", merged="2026-10-16 15:30", merged_by="siddhi",
         reviews=[("2026-10-16 14:00", "siddhi", "APPROVED", "")],
         commits=[("2026-10-09 15:40", "feat(pay): razorpay order + checkout"),
                  ("2026-10-14 22:00", "fix(pay): verify webhook signature with regenerated secret")]),
    dict(number=18, title="Cart & checkout UI", author="siddhi", closes=[10], branch="feat/cart-ui",
         created="2026-10-18 01:10", merged="2026-10-18 13:00", merged_by="siddhi",
         reviews=[("2026-10-18 10:00", "aarav", "COMMENTED", "Two small comments on qty stepper"),
                  ("2026-10-18 12:30", "aarav", "APPROVED", "")],
         commits=[("2026-10-17 23:20", "feat(ui): cart page"),
                  ("2026-10-18 00:55", "feat(ui): checkout button → payments")]),
    dict(number=19, title="[WIP] Order tracking screen with Leaflet map", author="aarav", closes=[4],
         branch="feat/tracking", created="2026-10-25 22:25", merged=None, merged_by=None, draft=True,
         reviews=[],
         commits=[("2026-10-25 22:10", "feat(ui): tracking screen skeleton + Leaflet map")]),
]

# Commits pushed straight to main (not via a PR)
DIRECT_COMMITS = [
    ("2026-10-07 10:20", "siddhi", "fix(db): orders.user_id as UUID to match users.id (#6)"),
    ("2026-10-25 23:40", "siddhi", "feat(db): seed data script WIP (#6)"),
]

# ---------------------------------------------------------------------------
# Notion task database
# ---------------------------------------------------------------------------
# status: "Not started" | "In progress" | "Done". edited = (time, who) of the last edit.
NOTION_TASKS = [
    dict(key="n1", name="Login & signup", issue=1, assignee="aarav", status="Done", due="2026-10-10",
         edited=("2026-10-04 11:55", "aarav")),
    dict(key="n2", name="Menu API", issue=2, assignee="siddhi", status="Done", due="2026-10-03",
         edited=("2026-10-02 11:05", "siddhi")),
    dict(key="n3", name="Payment gateway", issue=3, assignee="rohan", status="Done", due="2026-10-24",
         edited=("2026-10-16 15:40", "priya")),
    dict(key="n4", name="Order tracking screen", issue=4, assignee="aarav", status="In progress",
         due="2026-10-30", edited=("2026-10-12 21:12", "aarav")),
    dict(key="n5", name="Recommendation model", issue=5, assignee="priya", status="In progress",
         due="2026-10-24", edited=("2026-10-01 15:05", "priya")),
    dict(key="n6", name="DB schema migration", issue=6, assignee="siddhi", status="In progress",
         due="2026-11-05", edited=("2026-10-07 10:30", "siddhi")),
    dict(key="n7", name="Push notifications", issue=7, assignee="rohan", status="In progress",
         due="2026-10-24", edited=("2026-10-09 16:10", "rohan")),
    dict(key="n8", name="Admin dashboard", issue=8, assignee="aarav", status="Not started",
         due="2026-10-24", edited=("2026-09-28 19:20", "siddhi")),
    dict(key="n9", name="CI pipeline", issue=9, assignee="rohan", status="Done", due="2026-10-10",
         edited=("2026-10-11 20:20", "rohan")),
    dict(key="n10", name="Cart & checkout UI", issue=10, assignee="aarav", status="Done", due="2026-11-05",
         edited=("2026-10-18 13:05", "siddhi")),
    dict(key="n11", name="Load testing", issue=11, assignee="rohan", status="In progress", due="2026-10-28",
         edited=("2026-10-22 11:05", "rohan")),
    dict(key="n12", name="Project report", issue=12, assignee="priya", status="In progress", due="2026-11-10",
         edited=("2026-10-19 21:15", "priya")),
    dict(key="n13", name="Viva preparation", issue=None, assignee="siddhi", status="Not started",
         due="2026-11-12", edited=("2026-09-28 19:25", "siddhi")),
]

# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
# label: blocker | help_request | decision | progress_update | solution | noise
#   (the plan's five classes + "solution": a reply that unblocks someone, which the
#    Contribution Agent credits)
# tasks: task ids this message is about; mention: the words that refer to the task
# resolves: key of the blocker/help request this message answers
CHANNELS = ["general", "backend", "frontend", "ml-and-data", "standup"]


def M(key, channel, ts, author, text, label, tasks=(), mention=None, reply_to=None, resolves=None):
    return dict(key=key, channel=channel, ts=ts, author=author, text=text, label=label,
                tasks=list(tasks), mention=mention, reply_to=reply_to, resolves=resolves)


MESSAGES = [
    # ---- kickoff
    M("m001", "general", "2026-09-28 19:30", "siddhi",
      "Hi all! Kickoff ho gaya 🎉 Notion board pe saare tasks daal diye hai, GitHub issues bhi bana diye. Apna apna check kar lo",
      "progress_update"),
    M("m002", "general", "2026-09-28 19:33", "aarav", "done 👍 login aur cart UI mera hai, admin dashboard bhi",
      "decision", ["T-1", "T-10", "T-8"], "login aur cart UI"),
    M("m003", "general", "2026-09-28 19:35", "rohan", "payment gateway aur notifications main dekh lunga. CI bhi",
      "decision", ["T-3", "T-7", "T-9"], "payment gateway aur notifications"),
    M("m004", "general", "2026-09-28 19:38", "priya",
      "reco model + report mera. Canteen ka menu data kisi ke paas hai?",
      "help_request", ["T-5", "T-12"], "reco model"),
    M("m005", "general", "2026-09-28 19:41", "siddhi",
      "haan canteen wale bhaiya se excel liya tha, drive pe daal deti hu", "solution",
      reply_to="m004", resolves="m004"),
    M("m006", "general", "2026-09-28 20:02", "aarav", "bhai koi canteen chal raha hai kya? samosa khatam hone wale hai 😭",
      "noise"),

    # ---- backend
    M("m007", "backend", "2026-09-30 22:15", "siddhi", "Menu API ka PR daal diya (#13), koi review kar do",
      "help_request", ["T-2"], "Menu API"),
    M("m008", "backend", "2026-09-30 22:42", "rohan", "review kar diya, LGTM. bas pagination add kar dena baad me",
      "progress_update", ["T-2"], reply_to="m007"),
    M("m009", "backend", "2026-10-02 11:20", "siddhi", "Menu API merge ho gaya ✅", "progress_update", ["T-2"],
      "Menu API"),
    M("m010", "backend", "2026-10-06 23:48", "siddhi",
      "yaar migration pe atak gayi hu 😩 orders table me foreign key error aa raha hai — users ka id UUID hai aur orders me INT",
      "blocker", ["T-6"], "migration"),
    M("m011", "backend", "2026-10-07 00:05", "rohan", "dono ko UUID kar de, purana data dump karke reload kar",
      "solution", reply_to="m010", resolves="m010"),
    M("m012", "backend", "2026-10-07 10:30", "siddhi",
      "thanks Rohan, UUID wala fix chal gaya. migration ab chal rahi hai, bas seed data baaki", "progress_update",
      ["T-6"], "migration"),
    M("m013", "backend", "2026-10-09 16:00", "priya",
      "payment wala kaam maine utha liya hai, Rohan ke exams chal rahe the. Razorpay test keys laga di hai",
      "decision", ["T-3"], "payment wala kaam"),
    M("m014", "backend", "2026-10-09 16:06", "rohan", "haan thanks yaar 🙏 main notifications pe focus karta hu phir",
      "decision", ["T-3", "T-7"], "notifications", reply_to="m013"),
    M("m015", "backend", "2026-10-14 21:10", "priya",
      "payment callback me signature verify fail ho raha hai, webhook secret galat hai shayad. Razorpay dashboard me kaha milta hai?",
      "help_request", ["T-3"], "payment callback"),
    M("m016", "backend", "2026-10-14 21:30", "siddhi", "Settings > Webhooks me jaake secret regenerate kar, .env me update karna",
      "solution", reply_to="m015", resolves="m015"),
    M("m017", "backend", "2026-10-15 12:00", "priya", "payment PR #17 ready hai, test mode me order place ho raha hai",
      "progress_update", ["T-3"], "payment PR"),
    M("m018", "backend", "2026-10-19 20:45", "rohan", "notifications done kar diya bhai, FCM se aa rahe hai ab",
      "progress_update", ["T-7"], "notifications"),
    M("m019", "backend", "2026-10-21 18:20", "rohan",
      "load testing ke liye JMeter install nahi ho raha mac pe, java version ka issue. koi help?",
      "help_request", ["T-11"], "load testing"),
    M("m020", "backend", "2026-10-21 18:50", "siddhi", "JMeter chhod, locust use kar. pip install locust bas",
      "solution", reply_to="m019", resolves="m019"),
    M("m021", "backend", "2026-10-22 11:00", "rohan", "ok locust se kar raha hu, 28 tak ho jayega",
      "progress_update", ["T-11"], "locust"),
    M("m022", "backend", "2026-10-24 23:30", "siddhi", "seed data script likh rahi hu migration ke liye, kal tak push",
      "progress_update", ["T-6"], "seed data script"),

    # ---- frontend
    M("m023", "frontend", "2026-10-03 18:00", "aarav", "login page ka PR #14 daala, OTP flow bhi hai",
      "progress_update", ["T-1"], "login page"),
    M("m024", "frontend", "2026-10-04 12:10", "aarav", "login merge ho gaya, Notion pe Done kar diya ✅",
      "progress_update", ["T-1"], "login"),
    M("m025", "frontend", "2026-10-16 22:05", "siddhi",
      "@aarav_m login wala page phir se crash ho raha hai OTP daalne ke baad, white screen aa rahi hai",
      "blocker", ["T-1"], "login wala page"),
    M("m026", "frontend", "2026-10-16 22:20", "aarav", "arre yaar 😑 dekhta hu. issue reopen kar diya",
      "progress_update", ["T-1"], reply_to="m025"),
    M("m027", "frontend", "2026-10-18 01:15", "siddhi",
      "cart UI maine bana diya kal raat, aarav ko time nahi mil raha tha. PR #18", "progress_update", ["T-10"],
      "cart UI"),
    M("m028", "frontend", "2026-10-18 01:20", "siddhi", "@aarav_m review kar dena jab time mile", "help_request",
      ["T-10"], reply_to="m027"),
    M("m029", "frontend", "2026-10-18 10:02", "aarav", "thanks yaar! review kar diya, ek do comments hai",
      "progress_update", ["T-10"], reply_to="m028"),
    M("m030", "frontend", "2026-10-20 19:40", "aarav", "tracking screen 2nd Nov tak ho jayega", "progress_update",
      ["T-4"], "tracking screen"),
    M("m031", "frontend", "2026-10-20 19:42", "aarav",
      "Google Maps API key ke liye billing account chahiye, card kisi ke paas nahi hai. isliye atka hua hai",
      "blocker", ["T-4"], "Google Maps API key"),
    M("m032", "frontend", "2026-10-20 19:55", "priya",
      "Maps ki jagah Leaflet + OpenStreetMap use kar, free hai, key nahi chahiye", "solution",
      reply_to="m031", resolves="m031"),
    M("m033", "frontend", "2026-10-20 20:10", "aarav", "sahi hai, Leaflet hi lagate hai", "decision", ["T-4"],
      "Leaflet", reply_to="m032"),
    M("m034", "frontend", "2026-10-23 17:30", "aarav",
      "login crash abhi bhi fix nahi hua, OTP verify ke baad session null aa raha hai. backend se token aa raha hai kya?",
      "blocker", ["T-1"], "login crash"),
    M("m035", "frontend", "2026-10-23 17:45", "siddhi",
      "token aa raha hai, tu localStorage me galat key se padh raha hai shayad — 'authToken' vs 'token'", "solution",
      reply_to="m034", resolves="m034"),
    M("m036", "frontend", "2026-10-25 22:30", "aarav", "PR #19 tracking screen ka draft daala hai, Leaflet map aa raha hai",
      "progress_update", ["T-4"], "tracking screen"),

    # ---- ml-and-data
    M("m037", "ml-and-data", "2026-10-01 15:00", "priya",
      "reco model ke liye collaborative filtering try kar rahi hu, data thoda kam hai", "progress_update", ["T-5"],
      "reco model"),
    M("m038", "ml-and-data", "2026-10-08 14:20", "priya", "cold start problem aa raha hai naye users ke liye 😕 kuch idea?",
      "help_request", ["T-5"], "cold start"),
    M("m039", "ml-and-data", "2026-10-08 14:40", "siddhi", "naye users ko popular items dikha de, simple fallback",
      "solution", reply_to="m038", resolves="m038"),
    M("m040", "ml-and-data", "2026-10-13 15:30", "priya",
      "kaggle pe ek food dataset mila, augmentation ke liye use kar rahi hu", "progress_update", ["T-5"],
      "augmentation"),
    M("m041", "ml-and-data", "2026-10-17 19:00", "priya",
      "reco model done ✅ PR #16 merge ho gaya, popular-items fallback bhi hai", "progress_update", ["T-5"],
      "reco model"),
    M("m042", "ml-and-data", "2026-10-17 19:05", "aarav", "🔥🔥", "noise", reply_to="m041"),
    M("m043", "ml-and-data", "2026-10-23 22:00", "priya",
      "report ke graphs me hindi labels नहीं दिख रहे, matplotlib boxes dikha raha hai ☹️", "blocker", ["T-12"],
      "report ke graphs"),
    M("m044", "ml-and-data", "2026-10-23 22:15", "siddhi",
      "Noto Sans Devanagari install karke rcParams['font.family'] me set kar", "solution",
      reply_to="m043", resolves="m043"),

    # ---- standups
    M("m045", "standup", "2026-10-12 21:00", "siddhi", "Weekly standup 🧵 — sab apna update daalo: kya kiya, kya atka hai",
      "noise"),
    M("m046", "standup", "2026-10-12 21:10", "aarav", "login done, ab cart UI start karunga. tracking screen 30 Oct tak",
      "progress_update", ["T-1", "T-10", "T-4"], "tracking screen"),
    M("m047", "standup", "2026-10-12 21:15", "priya", "reco model 70% ho gaya. payment bhi le liya hai maine",
      "progress_update", ["T-5", "T-3"], "payment"),
    M("m048", "standup", "2026-10-12 21:20", "rohan", "CI pipeline done (GitHub Actions). notifications start kiya",
      "progress_update", ["T-9", "T-7"], "CI pipeline"),
    M("m049", "standup", "2026-10-12 21:25", "siddhi", "menu API done, migration pe kaam chal raha hai",
      "progress_update", ["T-2", "T-6"], "migration"),
    M("m050", "standup", "2026-10-19 21:00", "siddhi", "Standup #2 — updates please 🙏", "noise"),
    M("m051", "standup", "2026-10-19 21:08", "rohan", "notifications done. load testing next", "progress_update",
      ["T-7", "T-11"], "notifications"),
    M("m052", "standup", "2026-10-19 21:12", "priya", "payment done, reco done. report shuru karti hu",
      "progress_update", ["T-3", "T-5", "T-12"], "report"),
    M("m053", "standup", "2026-10-19 21:20", "aarav", "cart UI siddhi ne kar diya, main tracking pe hu",
      "progress_update", ["T-10", "T-4"], "cart UI"),

    # ---- decisions + noise in #general
    M("m054", "general", "2026-10-10 23:59", "rohan", "kal ka lecture cancel hai kya", "noise"),
    M("m055", "general", "2026-10-11 00:03", "aarav", "pata nahi bro so ja", "noise", reply_to="m054"),
    M("m056", "general", "2026-10-22 13:00", "siddhi",
      "Sir se baat hui — admin dashboard optional hai, drop kar rahe hai. Focus core features pe", "decision",
      ["T-8"], "admin dashboard"),
    M("m057", "general", "2026-10-22 13:05", "aarav", "thank god 😂 ek kaam kam", "noise", reply_to="m056"),
    M("m058", "general", "2026-10-22 13:10", "siddhi", "aur report submission 8 Nov hai, sir ne confirm kiya. viva uske baad",
      "decision", ["T-12"], "report submission"),
    M("m059", "general", "2026-10-22 13:15", "priya", "notion pe 10 Nov likha hai, update kar dungi", "progress_update",
      ["T-12"], reply_to="m058"),
    M("m060", "general", "2026-10-24 18:00", "aarav", "who's coming for the hackathon on Saturday?", "noise"),
    M("m061", "general", "2026-10-25 20:00", "rohan", "kal canteen band hai kya? kisi ko pata?", "noise"),
    M("m062", "general", "2026-10-25 20:02", "priya", "haan maintenance ke liye band hai", "noise", reply_to="m061"),
]

# ---------------------------------------------------------------------------
# Answer key
# ---------------------------------------------------------------------------
# evidence refs: "notion:n4", "github:issue:4", "github:pr:17", "discord:m030"
DISCREPANCIES = [
    dict(id="D1", type="status_conflict", task="T-1", severity="high",
         summary="Notion says Login is Done, but the GitHub issue was reopened and Discord reports it still crashes after OTP.",
         sides=[("notion:n1", "status", "done"), ("github:issue:1", "status", "open"),
                ("discord:m025", "status", "broken"), ("discord:m034", "status", "broken")]),
    dict(id="D2", type="owner_conflict", task="T-3", severity="medium",
         summary="Payment gateway is assigned to Rohan in Notion and GitHub, but Priya authored PR #17, its commits, and said she took it over.",
         sides=[("notion:n3", "owner", "rohan"), ("github:issue:3", "owner", "rohan"),
                ("github:pr:17", "owner", "priya"), ("discord:m013", "owner", "priya")]),
    dict(id="D3", type="deadline_conflict", task="T-4", severity="medium",
         summary="Tracking screen due 30 Oct in Notion, 5 Nov on the GitHub milestone, and Aarav now says 2 Nov.",
         sides=[("notion:n4", "deadline", "2026-10-30"), ("github:issue:4", "deadline", "2026-11-05"),
                ("discord:m030", "deadline", "2026-11-02")]),
    dict(id="D4", type="stale_plan", task="T-5", severity="low",
         summary="Notion still shows the recommendation model In progress (last edited 1 Oct), but PR #16 merged and closed the issue on 17 Oct.",
         sides=[("notion:n5", "status", "in_progress"), ("github:issue:5", "status", "done"),
                ("github:pr:16", "status", "done"), ("discord:m041", "status", "done")]),
    dict(id="D5", type="status_conflict", task="T-7", severity="medium",
         summary="Rohan said notifications are done (19 Oct, twice), but the issue is open with no PR or commits, and Notion says In progress.",
         sides=[("discord:m018", "status", "done"), ("discord:m051", "status", "done"),
                ("github:issue:7", "status", "open"), ("notion:n7", "status", "in_progress")]),
    dict(id="D6", type="stale_plan", task="T-8", severity="low",
         summary="Admin dashboard was dropped on 22 Oct (faculty said optional), but it's still an open issue and a Notion task.",
         sides=[("discord:m056", "status", "dropped"), ("github:issue:8", "status", "open"),
                ("notion:n8", "status", "todo")]),
    dict(id="D7", type="owner_conflict", task="T-10", severity="medium",
         summary="Cart UI is assigned to Aarav, but Siddhi built it (PR #18) and Aarav only reviewed.",
         sides=[("notion:n10", "owner", "aarav"), ("github:issue:10", "owner", "aarav"),
                ("github:pr:18", "owner", "siddhi"), ("discord:m027", "owner", "siddhi")]),
    dict(id="D8", type="deadline_conflict", task="T-12", severity="high",
         summary="Report submission is 8 Nov per faculty (Discord, 22 Oct), but Notion still says 10 Nov.",
         sides=[("notion:n12", "deadline", "2026-11-10"), ("discord:m058", "deadline", "2026-11-08")]),
]
CONSISTENT_TASKS = ["T-2", "T-6", "T-9", "T-11", "N-viva"]

# Claims a member made in chat about their own work, and whether artifacts back them up.
CHAT_CLAIMS = [
    dict(msg="m009", member="siddhi", task="T-2", claim="Menu API merged", verdict="verified", evidence=["github:pr:13"]),
    dict(msg="m024", member="aarav", task="T-1", claim="Login merged and done", verdict="verified_then_regressed",
         evidence=["github:pr:14", "github:issue:1"], note="True on 4 Oct; issue reopened 16 Oct."),
    dict(msg="m041", member="priya", task="T-5", claim="Reco model done", verdict="verified", evidence=["github:pr:16"]),
    dict(msg="m052", member="priya", task="T-3", claim="Payment done", verdict="verified", evidence=["github:pr:17"]),
    dict(msg="m048", member="rohan", task="T-9", claim="CI pipeline done", verdict="verified", evidence=["github:pr:15"]),
    dict(msg="m018", member="rohan", task="T-7", claim="Notifications done", verdict="unverified", evidence=[],
         note="No PR, commit or issue activity for #7."),
    dict(msg="m051", member="rohan", task="T-7", claim="Notifications done", verdict="unverified", evidence=[]),
    dict(msg="m027", member="siddhi", task="T-10", claim="Built cart UI", verdict="verified", evidence=["github:pr:18"]),
]

# Natural-language questions with expected answers, for the Week 3+ Q&A evaluation.
EVAL_QUESTIONS = [
    dict(q="Is the login feature finished?", expect="No. Notion says Done but issue #1 was reopened on 16 Oct and it still crashes after OTP (Discord, 23 Oct).",
         refs=["notion:n1", "github:issue:1", "discord:m034"]),
    dict(q="Who actually did the payment integration?", expect="Priya (PR #17 and commits), although it is assigned to Rohan.",
         refs=["github:pr:17", "discord:m013"]),
    dict(q="When is the report due?", expect="8 Nov per faculty (Discord m058); Notion still says 10 Nov.",
         refs=["discord:m058", "notion:n12"]),
    dict(q="What is blocking the tracking screen?", expect="It was blocked on a Google Maps API key (billing), resolved by switching to Leaflet + OpenStreetMap; draft PR #19 is open.",
         refs=["discord:m031", "discord:m032", "github:pr:19"]),
    dict(q="Are push notifications done?", expect="Unverified: Rohan said done on 19 Oct, but issue #7 is open with no PR or commits.",
         refs=["discord:m018", "github:issue:7"]),
    dict(q="Which blockers are still unresolved?", expect="Login crash after OTP (#1) has a suggested fix but the issue is still open; all other blockers got a solution.",
         refs=["discord:m034", "discord:m035", "github:issue:1"]),
    dict(q="Is the admin dashboard still planned?", expect="No, dropped on 22 Oct; Notion and GitHub haven't been updated.",
         refs=["discord:m056", "github:issue:8", "notion:n8"]),
    dict(q="Who helped others the most?", expect="Siddhi gave the most solutions in chat (6); Rohan and Priya one each.",
         refs=["discord:m005", "discord:m016", "discord:m020", "discord:m035", "discord:m039", "discord:m044"]),
]
