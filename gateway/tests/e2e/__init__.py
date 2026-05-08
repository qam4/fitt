"""End-to-end test harness for the gateway.

See ``.kiro/specs/phase4.6-e2e-harness/`` for the design. The
harness drives the full gateway pipeline in-process via an ASGI
transport, with a stubbed LLM and explicit time control so
lifecycle tests stay deterministic and fast.
"""
