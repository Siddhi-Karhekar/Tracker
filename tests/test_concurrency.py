"""Several people use the app at once. Streamlit runs each person's page on its own thread, so the database
connection must keep one person's transaction from leaking into, or being rolled back by, another's."""
import threading

from teampulse import db


def test_one_users_rollback_does_not_undo_another_users_write(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    started, committed = threading.Event(), threading.Event()
    errors = []

    def failing_edit():          # person A starts a change that will fail and roll back
        try:
            with conn:
                conn.execute("INSERT INTO settings VALUES ('a', '1')")
                started.set()
                committed.wait(5)
                raise RuntimeError("A's change fails")
        except RuntimeError:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def good_edit():             # person B saves a change in the meantime
        started.wait(5)
        try:
            with conn:
                conn.execute("INSERT INTO settings VALUES ('b', '1')")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        committed.set()

    threads = [threading.Thread(target=failing_edit), threading.Thread(target=good_edit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    keys = {r[0] for r in db.connect(str(tmp_path / "t.db")).execute("SELECT key FROM settings")}
    assert "a" not in keys, "A's failed change was committed by B"
    assert "b" in keys, "B's saved change was lost"
    assert not [e for e in errors if "locked" not in str(e)], errors


def test_many_threads_writing_at_once_lose_nothing(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))

    def worker(n):
        for i in range(20):
            with conn:
                conn.execute("INSERT INTO settings VALUES (?, ?)", (f"{n}-{i}", "x"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 160


def test_parallel_password_guesses_still_lock_the_account(tmp_path, monkeypatch):
    """Regression: the failure counter was read, then written, so simultaneous guesses overwrote each other's
    count and an attacker sending guesses in parallel could keep the account from ever locking."""
    from teampulse import auth
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    conn = db.connect(str(tmp_path / "t.db"))
    auth._insert_user(conn, "Neha Sharma", "neha", "leader", None, "violet-river-stone-42", False, None)
    barrier = threading.Barrier(12)

    def guess(n):
        barrier.wait()
        try:
            auth.login(conn, "neha", f"wrong-guess-{n}")
        except auth.AuthError:
            pass

    threads = [threading.Thread(target=guess, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    failed = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='auth.login' AND outcome='failed'").fetchone()[0]
    assert failed >= auth.MAX_FAILURES
    import pytest
    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.login(conn, "neha", "violet-river-stone-42")        # locked, even with the right password


def test_two_people_setting_up_at_once_make_only_one_leader(tmp_path):
    from teampulse import auth
    conn = db.connect(str(tmp_path / "t.db"))
    barrier = threading.Barrier(6)
    made = []

    def setup(n):
        barrier.wait()
        try:
            made.append(auth.create_first_leader(conn, f"Person {n}", f"person{n}"))
        except auth.AuthError:
            pass

    threads = [threading.Thread(target=setup, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(made) == 1
    assert conn.execute("SELECT COUNT(*) FROM users WHERE role = 'leader'").fetchone()[0] == 1


def test_first_build_runs_once_when_several_people_open_the_app(tmp_path, monkeypatch):
    import time

    from teampulse import actions, discrepancies
    conn = db.connect(str(tmp_path / "t.db"))
    runs = []

    def slow_build(conn, index, llm, refresh=False):
        runs.append(1)
        time.sleep(0.2)
        db.set_state(conn, "tasks:refreshed_at", "now")

    monkeypatch.setattr(discrepancies, "run", slow_build)
    threads = [threading.Thread(target=actions.first_build, args=(conn, None, None)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(runs) == 1


def test_a_threads_connection_is_closed_when_the_thread_is_gone(tmp_path):
    import gc
    conn = db.connect(str(tmp_path / "t.db"))
    seen = []
    t = threading.Thread(target=lambda: seen.append(conn._conn()))
    t.start(); t.join()
    del t
    gc.collect()
    import sqlite3

    import pytest
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")
