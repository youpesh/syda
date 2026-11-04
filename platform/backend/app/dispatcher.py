"""Recover database dispatch intent and expired worker leases every five seconds."""
import logging
import signal
from threading import Event
from .job_queue import reconcile_jobs


def main():
    logging.basicConfig(level=logging.INFO)
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            reconcile_jobs()
        except Exception as error:
            logging.warning("Generation dispatch pass failed (%s)", type(error).__name__)
        stop.wait(5)


if __name__ == "__main__":
    main()
