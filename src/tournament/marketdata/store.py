"""Daily Parquet store for historical market data (BRD 9.1: Parquet + DuckDB).

Layout: <root>/<dataset>/<YYYY>/<YYYY-MM-DD>.parquet, plus <root>/calendar.parquet.
Files are written to a temp name and renamed, so a file that exists is complete.
Provenance (source, feed, code commit, parameters) is stored in Parquet metadata.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = "1"


class HistoryStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, dataset: str, session_date: date) -> Path:
        return self.root / dataset / f"{session_date:%Y}" / f"{session_date:%Y-%m-%d}.parquet"

    def has(self, dataset: str, session_date: date) -> bool:
        return self.path(dataset, session_date).is_file()

    def write(self, dataset: str, session_date: date | None, table: pa.Table, meta: dict) -> Path:
        path = (
            self.root / "calendar.parquet"
            if session_date is None
            else self.path(dataset, session_date)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        provenance = {
            "dataset": dataset,
            "schema_version": SCHEMA_VERSION,
            "rows": table.num_rows,
            **meta,
        }
        table = table.replace_schema_metadata(
            {
                **(table.schema.metadata or {}),
                b"tournament": json.dumps(provenance, default=str).encode(),
            }
        )
        tmp = path.with_suffix(".parquet.partial")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
        return path

    def read(self, dataset: str, session_date: date) -> pa.Table:
        return pq.read_table(self.path(dataset, session_date))

    @staticmethod
    def provenance(path: Path) -> dict:
        meta = pq.read_schema(path).metadata or {}
        return json.loads(meta.get(b"tournament", b"{}"))

    def days(self, dataset: str) -> list[date]:
        return sorted(date.fromisoformat(p.stem) for p in (self.root / dataset).glob("*/*.parquet"))
