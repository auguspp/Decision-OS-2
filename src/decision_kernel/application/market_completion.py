"""Explicit completion clocks; the legacy next-day contract remains the default."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from decision_kernel.domain.records import require

EOD_COMPLETION_POLICY = "ashare-eod-1835-v1"
EOD_CUTOFF = time(18, 35)
SHANGHAI = timezone(timedelta(hours=8))


def completed_close_clock(value, end, completion_policy=None):
    """Return observed clock and completed date, never a provider tick timestamp.

    Only explicitly opted-in FTShare plans may use the same-day 18:35 boundary.
    Existing Relay and FTShare plans keep their original next-day semantics.
    """
    if completion_policy is None:
        from decision_kernel.application.market_collection_inputs import _reading_clock
        return _reading_clock(value, end)
    require(completion_policy == EOD_COMPLETION_POLICY, "FTSHARE_COMPLETION_POLICY")
    if value is None:
        return None, None
    require(isinstance(value, str) and len(value) <= 64, "FTSHARE_COMPLETION_CLOCK")
    try:
        clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
        final = date.fromisoformat(end)
    except (ValueError, TypeError):
        require(False, "FTSHARE_COMPLETION_CLOCK")
    require(clock.utcoffset() is not None, "FTSHARE_COMPLETION_CLOCK")
    local = clock.astimezone(SHANGHAI)
    through = local.date() - timedelta(days=int(local.time() < EOD_CUTOFF))
    require(final <= through, "FTSHARE_COMPLETION_BEFORE_END")
    return clock.isoformat(), through.isoformat()
