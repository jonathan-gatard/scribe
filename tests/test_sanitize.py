"""Sanitizing attributes before they reach the jsonb codec.

Attributes come from any integration installed on the instance, so they can
hold anything at all. Whatever arrives, this has to produce something the
codec accepts: a single value it cannot handle used to raise inside
`json.dumps` and take the **whole flush batch** with it (issue #35).
"""

import dataclasses
import math
import uuid
from datetime import date, datetime, timezone

import pytest

from custom_components.scribe.writer import ScribeWriter, WriterConfig


@pytest.fixture
def writer(hass):
    return ScribeWriter(hass, WriterConfig(db_url="postgresql://u:p@h/d"))


@dataclasses.dataclass
class _Reading:
    channel: str
    value: int


class _Hostile(dict):
    """A mapping that refuses to be walked."""

    def items(self):
        raise RuntimeError("no items for you")


class _Unprintable:
    def __repr__(self):
        return "<TargetChannelInfo>"


def test_json_native_values_pass_through(writer):
    assert writer._sanitize_obj({"a": 1, "b": True, "c": None, "d": "x"}) == {
        "a": 1,
        "b": True,
        "c": None,
        "d": "x",
    }


def test_datetimes_are_left_for_the_codec(writer):
    """asyncpg encodes these itself; stringifying them broke the time column."""
    when = datetime(2026, 8, 1, tzinfo=timezone.utc)
    today = date(2026, 8, 1)

    assert writer._sanitize_obj(when) is when
    assert writer._sanitize_obj(today) is today


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_values_json_cannot_spell_become_null(writer, value):
    assert writer._sanitize_obj(value) is None


def test_a_finite_float_survives(writer):
    assert writer._sanitize_obj(1.5) == 1.5
    assert not math.isnan(writer._sanitize_obj(1.5))


def test_null_bytes_are_stripped_everywhere(writer):
    out = writer._sanitize_obj({"na\0me": ["a\0b", ("c\0d",)]})

    assert "\0" not in str(out)
    assert out == {"name": ["ab", ("cd",)]}


def test_a_tuple_stays_a_tuple(writer):
    assert writer._sanitize_obj((1, "a")) == (1, "a")


def test_a_dataclass_keeps_its_field_names(writer):
    """Stringifying it would lose the structure the user can query."""
    assert writer._sanitize_obj(_Reading(channel="left", value=3)) == {
        "channel": "left",
        "value": 3,
    }


def test_an_integration_object_becomes_its_string(writer):
    """The case that killed whole batches before 3.6.0."""
    assert writer._sanitize_obj(_Unprintable()) == "<TargetChannelInfo>"


def test_a_uuid_becomes_its_string(writer):
    value = uuid.uuid4()
    assert writer._sanitize_obj(value) == str(value)


def test_a_value_that_refuses_to_be_walked_does_not_escape(writer):
    """One hostile attribute must cost that attribute, not the batch."""
    out = writer._sanitize_obj({"ok": 1, "bad": _Hostile(x=1)})

    assert out["ok"] == 1
    assert isinstance(out["bad"], str)


def test_a_cycle_terminates(writer):
    """Attributes are arbitrary; a self-referencing dict must not recurse forever."""
    loop = {}
    loop["self"] = loop

    out = writer._sanitize_obj({"loop": loop})

    assert out is not None


def test_deep_nesting_is_cut_off_rather_than_overflowing(writer):
    deep = current = {}
    for _ in range(300):
        current["next"] = {}
        current = current["next"]

    out = writer._sanitize_obj(deep)

    # Walked to the guard, then stringified — no RecursionError either way.
    assert isinstance(out, dict)


def test_a_key_that_is_not_a_string_becomes_one(writer):
    """`json.dumps` refuses a tuple key outright, which fails the whole batch."""
    out = writer._sanitize_obj({(1, 2): "x", 3: "y"})

    assert set(out) == {"(1, 2)", "3"}


def test_the_sanitized_attributes_survive_json(writer):
    """The real bar: whatever comes out must encode for the jsonb codec."""
    import json

    hostile = {
        "na\0me": "a\0b",
        (1, 2): float("inf"),
        "nested": {"k\0": [float("nan"), _Unprintable()]},
    }

    encoded = json.dumps(writer._sanitize_obj(hostile))

    assert "\\u0000" not in encoded
