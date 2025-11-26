"""SQLAlchemy models for Scribe."""
from sqlalchemy import Column, DateTime, String, Float, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class State(Base):
    """State model."""
    __tablename__ = "states"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    entity_id = Column(String, primary_key=True, nullable=False)
    state = Column(String)
    value = Column(Float)
    attributes = Column(JSONB)

class Event(Base):
    """Event model."""
    __tablename__ = "events"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    event_type = Column(String, primary_key=True, nullable=False)
    event_data = Column(JSONB)
    origin = Column(String)
    context_id = Column(String)
    context_user_id = Column(String)
    context_parent_id = Column(String)
