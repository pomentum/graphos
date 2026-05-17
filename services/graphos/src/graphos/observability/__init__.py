"""Observability package — Prometheus metrics, traceparent propagation,
write audit log. Each piece is independent middleware that the app
factory wires into the FastAPI app.
"""
