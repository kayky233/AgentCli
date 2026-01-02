import os
import sys

from agent.cli import main


def _probe(msg: str) -> None:
    if os.environ.get("AGENT_DEBUG_PROBE") == "1":
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    _probe("DEBUG PROBE [START]: Script is loading...")
    _probe("DEBUG PROBE [MAIN]: Entering main block...")
    try:
        _probe("DEBUG PROBE [FUNC]: Inside main function")
        main()
    except Exception:
        import traceback

        print("CRITICAL ERROR CAUGHT:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

