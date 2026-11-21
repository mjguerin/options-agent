"""Monthly compression of the raw-frame recordings: verified, idempotent, never this month's."""

import gzip
import json
from datetime import date

from tournament.live.recovery import read_journal
from tournament.ops import compress
from tournament.ops.compress import month_start, run_compress


def make(root, day, run="shadow", n=50):
    d = root / day / run
    d.mkdir(parents=True)
    f = d / "stream.jsonl"
    f.write_text(
        "".join(json.dumps({"r": "2026-10-26T14:00:00Z", "m": {"i": i}}) + "\n" for i in range(n))
    )
    return f


def test_month_start():
    assert month_start(date(2026, 12, 1), 0) == date(2026, 12, 1)
    assert month_start(date(2026, 1, 15), 1) == date(2025, 12, 1)


def test_sessions_before_the_month_are_compressed_and_still_readable(tmp_path):
    old, new = make(tmp_path, "2026-10-26"), make(tmp_path, "2026-11-20")
    before = list(read_journal(old))
    assert run_compress(date(2026, 11, 21), tmp_path, 0) == 0
    assert not old.exists() and old.with_name("stream.jsonl.gz").exists()
    assert new.exists() and not new.with_name("stream.jsonl.gz").exists()  # this month: untouched
    assert list(read_journal(old)) == before  # the reader falls back to the .gz
    assert not list(tmp_path.rglob("*.tmp"))


def test_a_second_run_changes_nothing_and_a_bad_copy_keeps_the_original(tmp_path, monkeypatch):
    f = make(tmp_path, "2026-10-26")
    assert run_compress(date(2026, 12, 1), tmp_path, 0) == 0
    gz = f.with_name("stream.jsonl.gz")
    stamp = gz.stat().st_mtime_ns
    assert run_compress(date(2026, 12, 1), tmp_path, 0) == 0 and gz.stat().st_mtime_ns == stamp

    g = make(tmp_path, "2026-10-27")
    real = gzip.open
    monkeypatch.setattr(
        compress.gzip,
        "open",
        lambda p, mode="rb", **kw: (
            real(p, mode, **kw)
            if "w" in mode
            else real(__import__("io").BytesIO(gzip.compress(b"different")), mode, **kw)
        ),
    )
    assert run_compress(date(2026, 12, 1), tmp_path, 0) == 1  # failed verification is reported
    assert g.exists() and not g.with_name("stream.jsonl.gz").exists()
