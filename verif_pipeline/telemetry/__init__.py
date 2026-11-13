"""Rain-gauge telemetry ingest for the verification pipeline.

A TCP gateway ships bucket-tip pulses in a small framed binary protocol;
the server persists receive progress in SQLite and only exposes batches
whose sequence *and* digest have been verified by a ``seal``.  A separate
export command turns accepted batches into the versioned station-obs CSV
consumed by :mod:`pipeline.runner`.

No message broker, external account or outbound network access is used.
"""
