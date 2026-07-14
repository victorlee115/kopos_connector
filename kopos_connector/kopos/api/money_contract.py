from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


SEN_MONEY_CONTRACT_VERSION = "sen_v1"
LEGACY_DECIMAL_MONEY_CONTRACT_VERSION = "decimal_v1"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
SUPPORTED_MONEY_CONTRACT_VERSIONS = frozenset(
    {
        SEN_MONEY_CONTRACT_VERSION,
        LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
    }
)

_INTEGER_PATTERN = re.compile(r"^-?\d+$")
_DECIMAL_PATTERN = re.compile(r"^-?\d+(?:\.\d{1,2})?$")
_MISSING = object()


class MoneyContractValidationError(ValueError):
    """Raised when a wire-money value is ambiguous or loses sen precision."""


def require_money_contract_version(payload: Mapping[str, Any]) -> str:
    value = payload.get("money_contract_version")
    version = str(value).strip() if value is not None else ""
    if version not in SUPPORTED_MONEY_CONTRACT_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_MONEY_CONTRACT_VERSIONS))
        raise MoneyContractValidationError(
            f"money_contract_version must be one of: {supported}"
        )
    return version


def parse_sen(value: Any, fieldname: str) -> int:
    if isinstance(value, bool):
        raise MoneyContractValidationError(
            f"{fieldname} must be an integer number of sen"
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not _INTEGER_PATTERN.fullmatch(normalized):
            raise MoneyContractValidationError(
                f"{fieldname} must be an integer number of sen"
            )
        parsed = int(normalized)
    else:
        raise MoneyContractValidationError(
            f"{fieldname} must be an integer number of sen"
        )
    if abs(parsed) > MAX_SAFE_INTEGER:
        raise MoneyContractValidationError(
            f"{fieldname} exceeds the safe integer range"
        )
    return parsed


def parse_legacy_decimal_sen(value: Any, fieldname: str) -> int:
    if isinstance(value, bool) or value is None:
        raise MoneyContractValidationError(
            f"{fieldname} must be an exact decimal currency amount"
        )

    normalized = str(value).strip()
    if not _DECIMAL_PATTERN.fullmatch(normalized):
        raise MoneyContractValidationError(
            f"{fieldname} must be an exact decimal currency amount with at most 2 decimal places"
        )
    try:
        decimal_value = Decimal(normalized)
    except InvalidOperation as error:
        raise MoneyContractValidationError(
            f"{fieldname} must be an exact decimal currency amount"
        ) from error

    sen_value = decimal_value * Decimal("100")
    if sen_value != sen_value.to_integral_value():
        raise MoneyContractValidationError(
            f"{fieldname} contains a fractional sen"
        )
    parsed = int(sen_value)
    if abs(parsed) > MAX_SAFE_INTEGER:
        raise MoneyContractValidationError(
            f"{fieldname} exceeds the safe integer range"
        )
    return parsed


def parse_wire_money_sen(
    row: Mapping[str, Any],
    *,
    version: str,
    sen_field: str,
    legacy_fields: Sequence[str],
    required: bool = True,
    default: int = 0,
) -> int:
    sen_value = row.get(sen_field, _MISSING)
    present_legacy_fields = [
        fieldname
        for fieldname in legacy_fields
        if row.get(fieldname, _MISSING) is not _MISSING
    ]

    if version == SEN_MONEY_CONTRACT_VERSION:
        if present_legacy_fields:
            joined = ", ".join(present_legacy_fields)
            raise MoneyContractValidationError(
                f"{sen_field} is the only accepted live money field; remove legacy field(s): {joined}"
            )
        if sen_value is _MISSING or sen_value is None:
            if required:
                raise MoneyContractValidationError(f"{sen_field} is required")
            return default
        return parse_sen(sen_value, sen_field)

    if version != LEGACY_DECIMAL_MONEY_CONTRACT_VERSION:
        raise MoneyContractValidationError(
            f"unsupported money_contract_version: {version}"
        )
    if sen_value is not _MISSING:
        raise MoneyContractValidationError(
            f"{sen_field} is not accepted by the explicit legacy decimal contract"
        )

    for fieldname in legacy_fields:
        legacy_value = row.get(fieldname, _MISSING)
        if legacy_value is not _MISSING and legacy_value is not None:
            return parse_legacy_decimal_sen(legacy_value, fieldname)
    if required:
        joined = " or ".join(legacy_fields)
        raise MoneyContractValidationError(f"{joined} is required")
    return default


def sen_to_decimal(value_sen: int) -> Decimal:
    return Decimal(value_sen) / Decimal("100")


def parse_positive_integer_quantity(value: Any, fieldname: str) -> int:
    if isinstance(value, bool) or value is None:
        raise MoneyContractValidationError(
            f"{fieldname} must be a positive integer quantity"
        )
    normalized = str(value).strip()
    if not _INTEGER_PATTERN.fullmatch(normalized):
        # Frappe can materialize integer quantities as Decimal("1.000000").
        try:
            decimal_value = Decimal(normalized)
        except InvalidOperation as error:
            raise MoneyContractValidationError(
                f"{fieldname} must be a positive integer quantity"
            ) from error
        if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
            raise MoneyContractValidationError(
                f"{fieldname} must be a positive integer quantity"
            )
        quantity = int(decimal_value)
    else:
        quantity = int(normalized)
    if quantity <= 0:
        raise MoneyContractValidationError(
            f"{fieldname} must be a positive integer quantity"
        )
    if quantity > MAX_SAFE_INTEGER:
        raise MoneyContractValidationError(
            f"{fieldname} exceeds the safe integer range"
        )
    return quantity


def persisted_money_to_sen(value: Any, fieldname: str) -> int:
    """Read a persisted ERP currency value without tolerance or float arithmetic."""

    if isinstance(value, bool) or value is None:
        raise MoneyContractValidationError(
            f"{fieldname} must be an exact persisted currency amount"
        )
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise MoneyContractValidationError(
            f"{fieldname} must be an exact persisted currency amount"
        ) from error
    if not decimal_value.is_finite():
        raise MoneyContractValidationError(
            f"{fieldname} must be a finite persisted currency amount"
        )
    sen_value = decimal_value * Decimal("100")
    if sen_value != sen_value.to_integral_value():
        raise MoneyContractValidationError(f"{fieldname} contains a fractional sen")
    parsed = int(sen_value)
    if abs(parsed) > MAX_SAFE_INTEGER:
        raise MoneyContractValidationError(
            f"{fieldname} exceeds the safe integer range"
        )
    return parsed
