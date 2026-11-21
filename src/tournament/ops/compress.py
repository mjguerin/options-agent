"""`tournament ops compress`: gzip the raw-frame recordings of sessions before the current month.

`stream.jsonl` is about 0.25 GB a session and compresses roughly tenfold. Each file is written to a
temporary name, read back and compared with the original line by line (count and hash), and only
then swapped in and the original removed. A file that does not verify is left alone and reported.
Idempotent; today's and this month's sessions are never touched. Readers (recovery.read_journal)
open the .gz when the plain file is gone.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import shutil
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)
FILE = "stream.jsonl"


def _digest(opener) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    with opener() as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def month_start(today: date, months_back: int) -> date:
    m = today.year * 12 + today.month - 1 - months_back
    return date(m // 12, m % 12 + 1, 1)


def compress_file(path: Path) -> tuple[int, int]:
    """(bytes before, bytes after). Raises if the compressed copy does not read back identically."""
    tmp = path.with_name(path.name + ".gz.tmp")
    final = path.with_name(path.name + ".gz")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    if _digest(lambda: gzip.open(tmp, "rb")) != _digest(lambda: path.open("rb")):
        tmp.unlink()
        raise OSError(f"{path}: compressed copy does not match")
    before = path.stat().st_size
    tmp.replace(final)
    path.unlink()
    return before, final.stat().st_size


def run_compress(today: date, live_root: Path, keep_months: int = 0) -> int:
    cutoff = month_start(today, keep_months)
    done = before = after = 0
    failed: list[str] = []
    for day_dir in sorted(live_root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")):
        try:
            d = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if d >= cutoff:
            continue
        for path in sorted(day_dir.glob(f"*/{FILE}")):
            if path.with_name(path.name + ".gz").exists():
                continue  # a verified copy exists already; never overwrite it
            try:
                b, a = compress_file(path)
                done, before, after = done + 1, before + b, after + a
            except Exception as exc:  # noqa: BLE001 - report and carry on with the others
                failed.append(f"{path}: {type(exc).__name__}: {exc}")
                log.exception("could not compress %s", path)
    log.info(
        "compress: %d file(s), %.2f GB to %.2f GB, %d failed (sessions before %s)",
        done,
        before / 1e9,
        after / 1e9,
        len(failed),
        cutoff,
    )
    for f in failed:
        log.error("%s", f)
    return 1 if failed else 0
