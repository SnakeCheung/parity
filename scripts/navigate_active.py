"""Navigate the active browser window in an E2B desktop."""

from __future__ import annotations

import argparse
import time

from dotenv import load_dotenv
from e2b_desktop import Sandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sandbox_id")
    parser.add_argument("url")
    args = parser.parse_args()

    load_dotenv()
    desktop = Sandbox.connect(args.sandbox_id)
    desktop.press(["CTRL", "L"])
    desktop.write(args.url)
    desktop.press("ENTER")
    time.sleep(8)
    print("NAVIGATION_SENT=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
