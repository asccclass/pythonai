"""Explicit development bypass and bounded inbound event freshness."""
import math
import os
import time


def development_mode():
    return os.environ.get("COMM_WEBHOOK_MODE", "production") == "development"


def fresh_timestamp(value, milliseconds=False):
    if development_mode():
        return True
    try:
        timestamp = float(value) / (1000 if milliseconds else 1)
        age = float(os.environ.get("COMM_WEBHOOK_MAX_AGE_SECONDS", "300"))
        return math.isfinite(timestamp) and math.isfinite(age) and age > 0 and -30 <= time.time() - timestamp <= age
    except (TypeError, ValueError, OverflowError):
        return False


def validate_api_result(result, platform):
    # Custom transport stubs may intentionally return None; real HTTP transports return bytes/dict.
    if result is None:
        return
    import json
    if isinstance(result, (bytes, str)):
        try:
            result = json.loads(result)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError(f"{platform} returned invalid JSON") from None
    if not isinstance(result, dict) or result.get("ok") is False or result.get("error"):
        raise RuntimeError(f"{platform} rejected the outbound message")
