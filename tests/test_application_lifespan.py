import asyncio

import pytest

import app.main as app_main


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_export_cleanup(monkeypatch):
    settings = object()
    session_factory = object()
    started = asyncio.Event()
    observed = {}

    async def cleanup_loop(actual_session_factory, actual_settings):
        observed["session_factory"] = actual_session_factory
        observed["settings"] = actual_settings
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(app_main, "get_settings", lambda: settings)
    monkeypatch.setattr(app_main, "SessionLocal", session_factory)
    monkeypatch.setattr(app_main, "cleanup_loop", cleanup_loop)

    async with app_main.lifespan(app_main.app):
        await asyncio.wait_for(started.wait(), timeout=1)

    assert observed == {"session_factory": session_factory, "settings": settings}
