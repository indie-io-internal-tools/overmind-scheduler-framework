"""Entry point invoked by PM2 (via the `indie-scheduler-serve` console script).

Reads jobs/ from the current working directory (or SCHEDULER_WORKING_DIR if
set), then launches the FastAPI app with uvicorn.
"""

import uvicorn

from .app import config


def main() -> None:
    uvicorn.run(
        "indie_scheduler.app.main:app",
        host="127.0.0.1",
        port=config.PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
