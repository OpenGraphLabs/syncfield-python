"""``python -m syncfield.replay <session_dir>`` — convenience entry point.

Mostly used during frontend development with the Vite dev server's proxy
pointed at this Python process. Same arguments as :func:`launch`.
"""

from __future__ import annotations

import argparse
import sys

from syncfield.replay import launch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m syncfield.replay",
        description="Open a SyncField session in the local replay viewer.",
    )
    parser.add_argument("session_dir", help="Path to a recorded session folder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="Do not auto-open the browser (useful with vite dev server).",
    )
    args = parser.parse_args(argv)

    launch(
        args.session_dir,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
