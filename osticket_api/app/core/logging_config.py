import logging
import sys

from app.core.config import settings


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # El eco de SQLAlchemy en INFO llena el log con cada SELECT.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
