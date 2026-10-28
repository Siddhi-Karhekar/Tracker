"""Run the GitHub and Notion pollers once or on a loop, and optionally keep the search index and chat
classification up to date.

    python -m teampulse.ingest.poll                  # loop every POLL_MINUTES (default 5)
    python -m teampulse.ingest.poll --once           # single pass
    python -m teampulse.ingest.poll --scan-chat      # also classify chat, rebuild tasks + claims, check discrepancies
"""
import argparse
import logging
import time

from teampulse import config, db
from teampulse.ingest import github_poller, notion_poller

log = logging.getLogger("teampulse.poll")


def run_once(conn, scan_chat: bool = False, state: dict | None = None) -> None:
    for name, poller in (("GitHub", github_poller), ("Notion", notion_poller)):
        try:
            poller.poll_all(conn)
        except Exception as exc:  # one failing source shouldn't stop the other
            log.error("%s poll failed: %s", name, exc)
    if scan_chat:
        try:
            from teampulse import discrepancies
            from teampulse.index import EventIndex
            from teampulse.llm import LLM

            state = state if state is not None else {}
            index = state.setdefault("index", EventIndex(conn))   # load the model once, reuse every pass
            # scan chat → task registry → AI chat links → claims → discrepancies (all incremental / cached)
            stats = discrepancies.run(conn, index, LLM(conn), refresh=True)
            log.info("Refresh: %d tasks, %d claims, %d new chat message(s); discrepancies: %d open, %d new, %d resolved",
                     stats["refresh"]["registry"]["tasks"], stats["refresh"]["claims"],
                     stats["refresh"]["scan"]["classified"], stats["found"], stats["new"], stats["resolved"])
        except Exception as exc:
            log.error("Refresh failed: %s", exc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument("--scan-chat", action="store_true", help="index + classify new chat messages each pass")
    args = parser.parse_args()
    conn = db.connect()
    minutes = config.get_int("POLL_MINUTES", 5)
    state: dict = {}
    while True:
        run_once(conn, args.scan_chat, state)
        if args.once:
            break
        time.sleep(minutes * 60)


if __name__ == "__main__":
    main()
