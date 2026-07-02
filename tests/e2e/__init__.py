"""Stubbed-provider END-TO-END tests (2026-07-01 test audit).

The ~1100 unit tests exercise single seams; nothing walked a full user flow,
so cross-step regressions (approve → movement → compile handoffs, the
Reengineer gate chain) could ship silently. These tests drive the PUBLIC
surface — HTTP endpoints via fastapi.testclient + the runner entry points the
endpoints schedule — with every paid provider replaced by the fakes in
``e2e.fakes``. Zero billing, real ffmpeg, real state store (isolated to a tmp
dir by tests/conftest.py).
"""
