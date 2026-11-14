"""`python -m hbench_console` entry point."""
import argparse
import sys

from .api import create_app
from .config import Settings


def main(argv=None):
    try:
        settings = Settings.from_env()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="hbench evaluation console")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
