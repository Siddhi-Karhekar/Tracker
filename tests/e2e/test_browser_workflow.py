"""The task-assignment workflow in a real browser against a running TeamPulse server, as the plan's definition of done
requires:

  leader creates a task and assigns it → the member sees it in My work and is notified → the member updates progress
  with evidence → the leader sees the update and the history → the member can't change someone else's task.

Slow (starts a server and Chromium), so it runs only with TEAMPULSE_E2E=1:

    TEAMPULSE_E2E=1 python -m pytest tests/e2e -q
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TEAMPULSE_E2E") != "1", reason="set TEAMPULSE_E2E=1 to run")

ROOT = Path(__file__).resolve().parents[2]
LEADER_PW, MEMBER_PW = "violet-river-stone-42", "amber-cloud-lantern-7"
TITLE = "Write the API docs page"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from teampulse import auth, db
    from teampulse.seed_demo import seed

    path = str(tmp_path_factory.mktemp("e2e") / "team.db")
    seed(path)
    conn = db.connect(path)
    lid = auth._insert_user(conn, "Neha Sharma", "neha", "leader", "Neha", LEADER_PW, False, None)
    auth._insert_user(conn, "Kabir Rao", "kabir", "member", "Kabir", MEMBER_PW, False, lid)
    port = free_port()
    env = {**os.environ, "TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "off",
           "TEAMPULSE_AUTH": "on", "TEAMPULSE_PUBLIC_DEMO": "off"}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless", "true",
                             "--server.port", str(port), "--browser.gatherUsageStats", "false"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(url + "/_stcore/health", timeout=2)   # noqa: S310 (local test server)
            break
        except OSError:
            time.sleep(1)
    else:
        proc.kill()
        pytest.fail("TeamPulse didn't start")
    yield url, conn
    proc.terminate()
    proc.wait(timeout=20)


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def sign_in(browser, url, username, password):
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.set_default_timeout(60_000)
    page.goto(url)
    page.get_by_role("textbox", name="Username").fill(username)
    page.get_by_role("textbox", name="Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_text("Signed in as").wait_for()
    return page


@pytest.fixture(autouse=True)
def screenshots_on_failure(request, tmp_path_factory):
    """Each step's page is saved if the test fails, so the failure can be seen, not guessed."""
    request.node.pages = []
    yield
    if getattr(request.node, "rep_call", None) and request.node.rep_call.failed:
        out = Path(os.environ.get("TEAMPULSE_E2E_SHOTS", tmp_path_factory.mktemp("shots")))
        out.mkdir(parents=True, exist_ok=True)
        for i, page in enumerate(request.node.pages):
            page.screenshot(path=str(out / f"page{i}.png"), full_page=True)
        print(f"Screenshots in {out}")


def go(page, name):
    page.get_by_role("link", name=name).click()
    page.wait_for_load_state("networkidle")


def test_assignment_workflow_end_to_end(server, browser, request):
    url, conn = server
    lead = sign_in(browser, url, "neha", LEADER_PW)
    request.node.pages.append(lead)

    # 1. The leader creates a task and assigns it to Kabir.
    go(lead, "Project board")
    lead.get_by_text("New task", exact=True).click()
    form = lead.get_by_test_id("stForm").filter(has=lead.get_by_role("button", name="Create task"))
    form.get_by_label("Title").fill(TITLE)
    form.get_by_test_id("stMultiSelect").first.click()   # "Assign to"
    lead.get_by_role("option", name="Kabir").click()
    lead.keyboard.press("Escape")
    form.get_by_role("button", name="Create task").click()
    lead.get_by_text("Task created.").wait_for()
    tid = conn.execute("SELECT id FROM pm_tasks WHERE title = ?", (TITLE,)).fetchone()[0]
    assert conn.execute("SELECT member FROM pm_assignees WHERE task_id = ?", (tid,)).fetchone()[0] == "Kabir"

    # 2. Kabir sees it in My work, with a notification.
    member = sign_in(browser, url, "kabir", MEMBER_PW)
    request.node.pages.append(member)
    go(member, "My work")
    member.get_by_text(f"Neha assigned you “{TITLE}”").wait_for()
    member.get_by_role("tab", name="All tasks").click()
    member.get_by_role("button", name=f"Open #{tid}").first.click()

    # 3. Kabir reports progress with evidence and starts the task.
    member.get_by_text("Update your progress").wait_for()
    member.get_by_label("Status").click()
    member.get_by_role("option", name="In progress").click()
    slider = member.get_by_role("slider").first
    slider.focus()
    for _ in range(6):   # 0 → 30 in steps of 5
        member.keyboard.press("ArrowRight")
    member.get_by_label("What did you do?").fill("Outline and endpoints table written")
    member.get_by_label("Evidence link").fill("https://github.com/campuseats/app/pull/40")
    member.get_by_role("button", name="Save update").click()
    member.get_by_text("Saved.").wait_for()
    row = conn.execute("SELECT status, reported_progress FROM pm_tasks WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == ("in_progress", 30)

    # 4. The leader sees the update, the evidence and the history.
    go(lead, "Team dashboard")
    go(lead, "Project board")
    lead.get_by_role("button", name=f"Open #{tid}").first.click()
    lead.get_by_text("Self-reported: 30%").wait_for()
    lead.get_by_text("Progress updates (1)").click()
    assert lead.get_by_role("link", name="evidence").get_attribute("href") == "https://github.com/campuseats/app/pull/40"
    lead.get_by_text("History (3)").click()
    lead.get_by_text("moved it from To do to In progress").wait_for()

    # 5. Kabir can read and comment on Riya's task but has no way to change it.
    riya_task = conn.execute("SELECT id FROM pm_tasks WHERE title = 'Order history screen'").fetchone()[0]
    go(member, "Project board")
    member.get_by_role("button", name=f"Open #{riya_task}").first.click()
    member.get_by_text("isn't assigned to you").wait_for()
    assert member.get_by_role("button", name="Save update").count() == 0
    assert member.get_by_text("Edit task (team leader)").count() == 0
