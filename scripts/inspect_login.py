"""Inspect an active E2B login session without reading cookie values."""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv
from e2b_desktop import Sandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sandbox_id")
    parser.add_argument("--screenshot", required=True)
    args = parser.parse_args()

    load_dotenv()
    desktop = Sandbox.connect(args.sandbox_id)
    window_id = desktop.get_current_window_id()
    print(f"TITLE={desktop.get_window_title(window_id)}")
    screenshot_path = Path(args.screenshot).resolve()
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(desktop.screenshot())
    result = desktop.commands.run(
        "find /home/user/state/profile-jd -name Cookies -type f "
        "-exec grep -aohE 'pt_key|pt_pin' {} + | sort -u"
    )
    names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    print(f"HAS_PT_KEY={'pt_key' in names}")
    print(f"HAS_PT_PIN={'pt_pin' in names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
