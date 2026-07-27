"""PyInstaller entry point for the standalone Windows distribution."""

import sys

from canvas_bot.__main__ import main


if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1] == "desktop":
        from canvas_bot.desktop import main as desktop_main

        desktop_args = [] if len(sys.argv) == 1 else sys.argv[2:]
        raise SystemExit(desktop_main(desktop_args))
    raise SystemExit(main())
