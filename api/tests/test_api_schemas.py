"""The response models' formatting of decimals and timestamps."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, Inexact

import pytest
from pydantic import TypeAdapter

from app.api.schemas import Timestamp, at_scale


def test_at_scale_pads_to_the_column_s_decimals():
    assert at_scale(Decimal("-300"), 3) == "-300.000"
    assert at_scale(Decimal("0.00085"), 6) == "0.000850"
    # Dropping trailing zeros isn't rounding.
    assert at_scale(Decimal("1.5000"), 3) == "1.500"


def test_at_scale_raises_rather_than_round():
    with pytest.raises(Inexact):
        at_scale(Decimal("1.0005"), 3)


def test_timestamps_are_converted_to_utc_and_written_with_a_z():
    adapter = TypeAdapter(Timestamp)
    amman = timezone(timedelta(hours=3))

    # The conversion runs on validation, which every response model goes through.
    value = adapter.validate_python(datetime(2026, 11, 2, 15, 0, tzinfo=amman))

    assert value == datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert value.tzinfo is UTC
    assert adapter.dump_json(value) == b'"2026-11-02T12:00:00Z"'
