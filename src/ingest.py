import csv
import logging
from pathlib import Path

REQUIRED_COLUMNS = ("name", "website_url", "target_type")
logger = logging.getLogger(__name__)


def load_creators(path: str | Path) -> list[dict[str, str]]:
    """Load complete creator records and warn about rows that cannot be used."""
    creators = []
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty or has no CSV header")

        missing_columns = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing_columns:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing_columns)}")

        for row_number, row in enumerate(reader, start=2):
            creator = {column: (row.get(column) or "").strip() for column in REQUIRED_COLUMNS}
            missing_values = [column for column, value in creator.items() if not value]
            if missing_values:
                logger.warning(
                    "Skipping input row %d; missing required values: %s",
                    row_number,
                    ", ".join(missing_values),
                )
                continue
            creators.append(creator)

    if not creators:
        raise ValueError(f"{path} contains no complete creator records")
    return creators
