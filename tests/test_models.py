"""Test Scribe models."""

from custom_components.scribe.models import State, Event


def test_state_model():
    """Test State model definition."""
    assert State.__tablename__ == "states"
    assert hasattr(State, "time")
    assert hasattr(State, "entity_id")
    assert hasattr(State, "state")
    assert hasattr(State, "value")
    assert hasattr(State, "attributes")


def test_event_model():
    """Test Event model definition."""
    assert Event.__tablename__ == "events"
    assert hasattr(Event, "time")
    assert hasattr(Event, "event_type")
    assert hasattr(Event, "event_data")
    assert hasattr(Event, "origin")
    assert hasattr(Event, "context_id")
    assert hasattr(Event, "context_user_id")
    assert hasattr(Event, "context_parent_id")
