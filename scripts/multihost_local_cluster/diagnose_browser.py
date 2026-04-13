"""Diagnostic — what does a SessionBrowser actually see on this machine?

Run this in a terminal while a follower (or leader) is advertising. It
opens an unfiltered SessionBrowser, prints every announcement it sees
for 10 seconds, then exits. Useful for isolating mDNS visibility
issues on macOS loopback.

Usage:
    uv run python scripts/multihost_local_cluster/diagnose_browser.py
"""

from __future__ import annotations

import logging
import time

from syncfield.multihost.browser import SessionBrowser


def main() -> None:
    # Verbose so we see every callback firing path
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    print("Opening SessionBrowser (no filter — sees ALL syncfield sessions)…")
    browser = SessionBrowser()  # no session_id filter
    browser.start()

    try:
        for i in range(10):
            time.sleep(1.0)
            sessions = browser.current_sessions()
            print(f"\n[t+{i+1}s] {len(sessions)} session(s) observed:")
            for ann in sessions:
                print(
                    f"  - host_id={ann.host_id} session_id={ann.session_id} "
                    f"status={ann.status} port={ann.control_plane_port} "
                    f"address={ann.resolved_address}"
                )
    finally:
        browser.close()
        print("\nBrowser closed.")


if __name__ == "__main__":
    main()
