"""OPS-18/OPS-41 source collectors for call-log ingestion.

Each collector normalizes one source into `CollectedRecord` objects and
defines an injectable transport protocol. Production-capable transports may
be defined here, but nothing in this package makes a live call on import or
at rest: a transport must be explicitly constructed and injected by the
caller, and this build's own tests only ever inject fakes.
"""

from __future__ import annotations
