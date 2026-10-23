"""Arrow schemas of the shared history store. Adapters convert vendor data into these."""

import pyarrow as pa

TS = pa.timestamp("ns", tz="UTC")

BAR_SCHEMA = pa.schema(
    [
        ("ts", TS),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("trade_count", pa.int64()),
        ("vwap", pa.float64()),
    ]
)
OPTION_BAR_SCHEMA = pa.schema([("symbol", pa.string()), *BAR_SCHEMA])
TRADE_SCHEMA = pa.schema(
    [
        ("ts", TS),
        ("price", pa.float64()),
        ("size", pa.int64()),
        ("exchange", pa.string()),
        ("conditions", pa.list_(pa.string())),
        ("trade_id", pa.int64()),
        ("tape", pa.string()),
    ]
)
OPTION_TRADE_SCHEMA = pa.schema([("symbol", pa.string()), *TRADE_SCHEMA])
# Static contract terms only. A vendor's close_price and open_interest are as of
# the request date, so storing them against a past day would leak the future.
CONTRACT_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("expiration", pa.date32()),
        ("strike", pa.float64()),
        ("right", pa.string()),
        ("style", pa.string()),
        ("multiplier", pa.int64()),
    ]
)
CALENDAR_SCHEMA = pa.schema(
    [
        ("session_date", pa.date32()),
        ("open_utc", TS),
        ("close_utc", TS),
        ("early_close", pa.bool_()),
    ]
)
