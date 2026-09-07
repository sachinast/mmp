"""Event ingestion: wire format, stream transport, batch persistence."""

from mmp_ingest.dedup import IdempotencyWindow
from mmp_ingest.schema import (
    MAX_EVENTS_PER_BATCH,
    EventBatch,
    IncomingEvent,
    QueuedEvent,
    ValidationFailure,
    validate_event,
)
from mmp_ingest.stream import (
    CLICKS_GROUP,
    CLICKS_STREAM,
    EVENTS_GROUP,
    EVENTS_STREAM,
    StreamConsumer,
    StreamProducer,
)
from mmp_ingest.writer import EventWriter, WriteResult

__all__ = [
    "CLICKS_GROUP",
    "CLICKS_STREAM",
    "EVENTS_GROUP",
    "EVENTS_STREAM",
    "MAX_EVENTS_PER_BATCH",
    "EventBatch",
    "EventWriter",
    "IdempotencyWindow",
    "IncomingEvent",
    "QueuedEvent",
    "StreamConsumer",
    "StreamProducer",
    "ValidationFailure",
    "WriteResult",
    "validate_event",
]
