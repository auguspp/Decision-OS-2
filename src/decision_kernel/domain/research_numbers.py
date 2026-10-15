"""Bounded extraction of the existing Claim Audit v1 five Decimal operations.

Original: decision-kernel@cb3bbc87, claim_audit_contract_v1.py:
calculate_claim_audit_v1_result. No old snapshot, Pydantic or Odds dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context, Decimal, DecimalException, DivisionByZero, Inexact, InvalidOperation,
    Overflow, ROUND_HALF_EVEN, Underflow, localcontext,
)
import re
from types import MappingProxyType

from decision_kernel.domain.records import RecordError, require

VERSION = "research-numbers-v1"
CONTEXT_FIELDS = ("subject", "period", "scope")
# Fixed current uses, not a currency/physical-unit framework. No implicit FX.
_UNITS = {
    "ratio": ("1", ()), "percent": ("0.01", ()),
    "CNY": ("1", (("CNY", 1),)), "CNY_10K": ("10000", (("CNY", 1),)),
    "CNY_100M": ("100000000", (("CNY", 1),)),
    "USD": ("1", (("USD", 1),)), "USD_1M": ("1000000", (("USD", 1),)),
    "share": ("1", (("share", 1),)), "share_10K": ("10000", (("share", 1),)),
    "share_100M": ("100000000", (("share", 1),)),
    "CNY/share": ("1", (("CNY", 1), ("share", -1))),
    "USD/share": ("1", (("USD", 1), ("share", -1))),
    "project": ("1", (("project", 1),)),
    "CNY/project": ("1", (("CNY", 1), ("project", -1))),
}
UNITS = MappingProxyType(_UNITS)
_ALIASES = {"人民币元": "CNY", "人民币万元": "CNY_10K", "人民币亿元": "CNY_100M",
            "股": "share", "万股": "share_10K", "亿股": "share_100M", "%": "percent",
            "人民币元/股": "CNY/share"}
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?\Z")


def arithmetic_context() -> Context:
    # Do not inherit caller precision, rounding, flags or traps (including Inexact).
    return Context(prec=80, rounding=ROUND_HALF_EVEN, Emin=-512, Emax=512,
                   traps=[InvalidOperation, DivisionByZero, Overflow, Underflow])


def number(value: str) -> Decimal:
    require(isinstance(value, str) and 0 < len(value) <= 128
            and _NUMBER.fullmatch(value.strip()) is not None, "DECIMAL_STRING_REQUIRED")
    try:
        result = Decimal(value.strip().replace(",", ""))
    except DecimalException:
        raise RecordError("DECIMAL_STRING_REQUIRED") from None
    require(result.is_finite() and len(result.as_tuple().digits) <= 80
            and abs(result.as_tuple().exponent) <= 128
            and (result == 0 or abs(result.adjusted()) <= 128), "NUMERIC_SIZE_LIMIT")
    return result


def unit(value: str) -> str:
    require(isinstance(value, str), "UNSUPPORTED_UNIT")
    result = _ALIASES.get(value, value)
    require(result in UNITS, "UNSUPPORTED_UNIT")
    return result


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: str
    context: tuple[str, str, str]
    warnings: tuple[str, ...] = ()


def calculate(operation: str, operands: list[Quantity], target: str,
              places: int) -> tuple[Decimal, str, bool]:
    """Same five arithmetic meanings as legacy v1, with explicit unit/display checks."""
    require(isinstance(operation, str) and operation in (
        "identity", "sum", "difference", "product", "ratio"), "UNSUPPORTED_NUMERIC_OPERATION")
    require(isinstance(operands, (list, tuple)) and 1 <= len(operands) <= 16, "NUMERIC_OPERAND_LIMIT")
    require(all(isinstance(item, Quantity) and isinstance(item.value, Decimal) and item.value.is_finite()
                for item in operands), "INVALID_NUMERIC_OPERAND")
    require((operation != "identity" or len(operands) == 1)
            and (operation not in ("difference", "ratio") or len(operands) == 2),
            "NUMERIC_OPERATION_ARITY")
    require(type(places) is int and 0 <= places <= 12, "INVALID_DECIMAL_PLACES")
    target = unit(target)
    dimensions = [UNITS[unit(item.unit)][1] for item in operands]
    if operation in ("identity", "sum", "difference"):
        require(all(d == dimensions[0] for d in dimensions), "OPERAND_UNIT_MISMATCH")
        result_dimension = dimensions[0]
    else:
        totals: dict[str, int] = {}
        for index, dimension in enumerate(dimensions):
            for name, power in dimension:
                totals[name] = totals.get(name, 0) + power * (-1 if operation == "ratio" and index == 1 else 1)
        result_dimension = tuple(sorted((name, power) for name, power in totals.items() if power))
    require(result_dimension == UNITS[target][1], "RESULT_UNIT_MISMATCH")
    try:
        with localcontext(arithmetic_context()) as ctx:
            values = [item.value * Decimal(UNITS[unit(item.unit)][0]) for item in operands]
            if operation == "identity":
                value = values[0]
            elif operation == "sum":
                value = sum(values, Decimal("0"))
            elif operation == "difference":
                value = values[0] - values[1]
            elif operation == "product":
                value = Decimal("1")
                for operand in values:
                    value *= operand
            else:
                require(values[1] != 0, "ZERO_DENOMINATOR")
                value = values[0] / values[1]
            value /= Decimal(UNITS[target][0])
            require(value.is_finite() and (value == 0 or abs(value.adjusted()) <= 128), "NUMERIC_SIZE_LIMIT")
            rounded = bool(ctx.flags[Inexact])
            displayed = value.quantize(Decimal(1).scaleb(-places))
            # No negative zero in financial display. Internal source value is retained.
            if displayed == 0:
                displayed = abs(displayed)
            return value, format(displayed, f".{places}f"), rounded
    except DecimalException:
        raise RecordError("NUMERIC_PRECISION_OR_RANGE_LIMIT") from None
