"""Operational metrics and process heartbeats (Phase 10, ADR-0021).

* ``metrics``: process-local counters and histograms with bounded labels.
* ``heartbeat``: each process publishes its state and a snapshot of its metrics to Redis, with a
  time to live; nothing reads heartbeats to make a correctness decision.
* ``exposition``: the Prometheus text format, for the API's ``GET /metrics``.
* ``timing``: per-task timings that code deep in a task (the politeness gate) can add to.
"""
