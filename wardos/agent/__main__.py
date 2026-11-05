"""python -m wardos.agent worker --server https://..."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from uuid import uuid4


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wardos.agent")
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="pick up the doctors' tasks and run them on eHospital")
    worker.add_argument("--server", default=os.environ.get("WARDOS_AGENT_SERVER") or "http://127.0.0.1:8793")
    worker.add_argument("--headed", action="store_true", help="show the browser window")
    worker.add_argument("--poll", type=float, default=5.0, help="seconds between looks for a new task")
    worker.add_argument("--resident", action="store_true",
                        help="cloud job: work while there is anything to do, then leave (instead of running forever)")
    worker.add_argument("--idle-exit", type=float, default=float(os.environ.get("WARDOS_ROBOT_IDLE_EXIT_S") or 600),
                        help="resident only: seconds of nothing to do before leaving")
    worker.add_argument("--max-run", type=float, default=float(os.environ.get("WARDOS_ROBOT_MAX_RUN_S") or 5.5 * 3600),
                        help="resident only: longest stretch in one job run, in seconds")
    args = parser.parse_args(argv)

    from .client import ServerClient
    from .hands import EHospitalHands
    from .worker import Worker
    resident = args.resident or os.environ.get("WARDOS_ROBOT_RESIDENT") == "1"
    worker_id = ("cloud-" if resident else "laptop-") + uuid4().hex[:8]
    client = ServerClient(args.server, worker_id)
    hands = EHospitalHands(headless=not args.headed)
    if resident:
        print(f"WardOS cloud robot on {args.server}.", flush=True)
        left = asyncio.run(Worker(client, hands, poll_s=args.poll).run_resident(idle_exit_s=args.idle_exit,
                                                                              max_run_s=args.max_run))
        print(f"Cloud robot leaving: {left}.", flush=True)
        return 0
    print(f"WardOS laptop worker on {args.server}. Keep this window open and the laptop awake. Ctrl+C stops it.", flush=True)
    try:
        asyncio.run(Worker(client, hands, poll_s=args.poll).run_forever())
    except KeyboardInterrupt:
        print("Worker stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
