"""Bounded, optional diagnostics for the existing daily runtime owners.

No source state, permission, budget, retry, or write outcome is inferred here.
Only fixed labels and numeric measurements enter an operation's receipt.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
import time

from decision_kernel.domain.records import encoded


MAX_RECEIPT_BYTES = 128 * 1024
MAX_ORDERED_SAMPLES = 512
MAX_PHASE_SAMPLES = 32
MAX_COUNTER = 2 ** 31 - 1
MAX_SECONDS = 86400
PHASES = frozenset({
    "daily.execute", "daily.calendar", "daily.masters", "daily.ft", "daily.guard",
    "daily.capacity", "daily.read_scope", "daily.resolve", "daily.ft_read", "daily.health",
    "master.unit", "master.validate", "master.initial_read", "master.preflight", "master.promote",
    "checkpoint.validate", "checkpoint.save", "checkpoint.page_save", "checkpoint.control_save",
    "checkpoint.readback", "query.save", "query.readback", "cycle.save", "cycle.readback",
    "ft.unit", "ft.reserve", "ft.request", "ft.capture", "ft.capture_save", "ft.settle",
    "ft.control_save", "ft.control_readback", "ft.wait",
    "relay.http", "relay.body", "relay.json", "relay.wait", "ft.http", "ft.body", "ft.json",
    "relay.error_body", "relay.error_json", "ft.error_body", "ft.error_json",
    "github.get", "github.post", "github.body", "github.json",
    "store.cas", "store.cas_post", "store.cas_verify",
    "snapshot.admit", "snapshot.fetch", "snapshot.init", "snapshot.read", "snapshot.exit",
    "discovery.execute", "discovery.build", "discovery.save", "discovery.read",
    "reading.execute", "reading.model", "reading.render", "quota.wait",
})
COUNTERS = frozenset({"relay_requests", "ft_requests", "relay_wire_attempts", "ft_wire_attempts", "github_api_requests", "github_get_requests", "github_post_requests"})
UNIT_PHASES = frozenset({"daily.calendar", "master.unit", "ft.unit"})
ORDERED_PHASES = PHASES - frozenset({
    "relay.error_body", "relay.error_json", "ft.error_body", "ft.error_json",
    "github.get", "github.post", "github.body", "github.json", "store.cas_verify",
    "snapshot.admit", "snapshot.fetch", "snapshot.init", "snapshot.read", "snapshot.exit",
    "daily.guard", "daily.capacity", "master.validate", "master.preflight", "checkpoint.validate",
})
_CURRENT = ContextVar("daily_runtime_performance", default=None)
_UNIT = ContextVar("daily_runtime_performance_unit", default=None)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class _Performance:
    def __init__(self, clock):
        self.clock = clock
        self.clock_errors = self.invalid_labels = self.observer_errors = self.sequence = self.dropped = 0
        self.phases, self.samples, self.counts = {}, [], {k: 0 for k in sorted(COUNTERS)}
        self.started = self.now()

    def now(self):
        try:
            value = self.clock()
            if _number(value):
                return value
        except Exception:
            pass
        self.clock_errors = min(MAX_COUNTER, self.clock_errors + 1)
        return None

    @staticmethod
    def duration(start, end):
        if start is None or end is None or not 0 <= end - start <= MAX_SECONDS:
            return None
        return round(end - start, 6)

    def enter(self, phase):
        self.sequence = min(MAX_COUNTER, self.sequence + 1)
        row = self.phases.setdefault(phase, {"entered": 0, "returned": 0, "raised": 0,
            "unknown_duration_count": 0, "observed_seconds": 0.0,
            "minimum_seconds": None, "maximum_seconds": None, "samples_seconds": [],
            "count_deltas": {k: 0 for k in sorted(COUNTERS)}})
        row["entered"] = min(MAX_COUNTER, row["entered"] + 1)
        return self.sequence, self.now()

    def begin_sample(self, phase, sequence, start, unit):
        if phase not in ORDERED_PHASES:
            return None
        if len(self.samples) >= MAX_ORDERED_SAMPLES:
            self.dropped = min(MAX_COUNTER, self.dropped + 1)
            return None
        sample = {"sequence": sequence, "unit_sequence": unit, "phase": phase,
            "started_seconds": self.duration(self.started, start),
            "elapsed_seconds": None, "outcome": "running"}
        self.samples.append(sample)
        return sample

    def leave(self, phase, start, outcome, sample, before):
        elapsed = self.duration(start, self.now())
        row = self.phases[phase]
        key = "returned" if outcome == "return" else "raised"
        row[key] = min(MAX_COUNTER, row[key] + 1)
        if elapsed is None:
            row["unknown_duration_count"] = min(MAX_COUNTER, row["unknown_duration_count"] + 1)
        else:
            row["observed_seconds"] = round(row["observed_seconds"] + elapsed, 6)
            row["minimum_seconds"] = elapsed if row["minimum_seconds"] is None else min(row["minimum_seconds"], elapsed)
            row["maximum_seconds"] = elapsed if row["maximum_seconds"] is None else max(row["maximum_seconds"], elapsed)
            if len(row["samples_seconds"]) < MAX_PHASE_SAMPLES:
                row["samples_seconds"].append(elapsed)
        for name in COUNTERS:
            row["count_deltas"][name] = min(MAX_COUNTER, row["count_deltas"][name] + self.counts[name] - before[name])
        if sample is not None:
            sample.update(elapsed_seconds=elapsed, outcome=outcome)
            if phase in UNIT_PHASES:
                sample["count_deltas"] = {k: self.counts[k] - before[k] for k in sorted(COUNTERS)}

    def receipt(self):
        phases = {}
        for name, original in sorted(self.phases.items()):
            row = {**original, "samples_seconds": list(original["samples_seconds"]),
                "count_deltas": dict(original["count_deltas"])}
            values = sorted(row["samples_seconds"])
            observed = row["returned"] + row["raised"] - row["unknown_duration_count"]
            if not observed:
                row["observed_seconds"] = None
            row.update(sample_count=len(values), samples_truncated=observed > len(values),
                p50_sample_seconds=values[math.ceil(.50 * len(values)) - 1] if values else None,
                p95_sample_seconds=values[math.ceil(.95 * len(values)) - 1] if values else None)
            phases[name] = row
        value = {"schema": "daily-runtime-performance-v1", "clock": "monotonic",
            "elapsed_seconds": self.duration(self.started, self.now()),
            "duration_semantics": "inclusive-nested-not-additive; unobserved phases are unknown",
            "outcome_semantics": "running-return-or-raise-only-not-write-effect-or-retry-permission",
            "percentile_semantics": "nearest-rank-of-first-bounded-completed-return-and-raise-mixed-samples-not-population",
            "limits": {"receipt_bytes": MAX_RECEIPT_BYTES, "ordered_samples": MAX_ORDERED_SAMPLES,
                       "samples_per_phase": MAX_PHASE_SAMPLES},
            "count_semantics": "inclusive-span-deltas; *_requests follows owner increments except github_get/post_requests and *_wire_attempts count dispatches",
            "counts": dict(self.counts), "clock_errors": self.clock_errors,
            "observer_errors": self.observer_errors,
            "invalid_labels": self.invalid_labels, "phases": phases,
            "unobserved_phases": sorted(PHASES - phases.keys()),
            "samples": [{**row, **({"count_deltas": dict(row["count_deltas"])}
                         if "count_deltas" in row else {})} for row in self.samples],
            "dropped_ordered_samples": self.dropped,
            "truncated": bool(self.dropped) or any(row["samples_truncated"] for row in phases.values())}
        # All state is bounded before serialization. Keep a byte limit as well,
        # without exposing arbitrary user/source strings or changing the task.
        def size():
            return len(encoded(value))
        while value["samples"] and size() > MAX_RECEIPT_BYTES:
            value["samples"].pop()
            value["dropped_ordered_samples"] += 1
            value["truncated"] = True
        return value


@contextmanager
def performance_session(*, clock=time.monotonic):
    """Nested daily/FT owners share one collector, never reset its counters."""
    if _CURRENT.get() is not None:
        yield
        return
    token = _CURRENT.set(_Performance(clock))
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def measure_phase(phase):
    collector = _CURRENT.get()
    if collector is None:
        yield
        return
    if type(phase) is not str or phase not in PHASES:
        collector.invalid_labels = min(MAX_COUNTER, collector.invalid_labels + 1)
        yield
        return
    token = None
    try:
        sequence, start = collector.enter(phase)
        token = _UNIT.set(sequence) if phase in UNIT_PHASES else None
        before = dict(collector.counts)
        sample = collector.begin_sample(phase, sequence, start, _UNIT.get())
    except Exception:
        collector.observer_errors = min(MAX_COUNTER, collector.observer_errors + 1)
        if token is not None:
            _UNIT.reset(token)
        yield
        return
    outcome = "return"
    try:
        yield
    except BaseException:
        outcome = "raise"
        raise
    finally:
        try:
            collector.leave(phase, start, outcome, sample, before)
        except Exception:
            collector.observer_errors = min(MAX_COUNTER, collector.observer_errors + 1)
        finally:
            if token is not None:
                _UNIT.reset(token)


def timed(phase):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with measure_phase(phase):
                return function(*args, **kwargs)
        return call
    return decorate


def count_event(name):
    collector = _CURRENT.get()
    if collector is not None:
        if type(name) is str and name in COUNTERS:
            collector.counts[name] = min(MAX_COUNTER, collector.counts[name] + 1)
        else:
            collector.invalid_labels = min(MAX_COUNTER, collector.invalid_labels + 1)


def performance_receipt():
    collector = _CURRENT.get()
    if collector is None:
        return None
    try:
        return collector.receipt()
    except Exception:
        return {"schema": "daily-runtime-performance-v1", "status": "diagnostic-unavailable",
            "truncated": True}


def diagnosed(function):
    @wraps(function)
    def call(*args, **kwargs):
        with performance_session():
            return function(*args, **kwargs)
    return call


def annotate_performance(value, *, max_bytes=1024 * 1024):
    """Add optional diagnostics without pushing an existing receipt over its limit."""
    diagnostic = performance_receipt()
    if diagnostic is None:
        return value
    try:
        candidate = {**value, "performance": diagnostic}
        if len(encoded(candidate)) <= max_bytes:
            return candidate
        candidate["performance"] = {"schema": "daily-runtime-performance-v1",
            "status": "omitted-for-receipt-bound", "truncated": True}
        if len(encoded(candidate)) <= max_bytes:
            return candidate
        return value  # Absent optional diagnostics are unknown, never a zero-duration proof.
    except Exception:
        return value
