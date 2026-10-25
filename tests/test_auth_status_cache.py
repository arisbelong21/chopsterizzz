"""Regression tests for the memoised ``AuthManager.statuses()`` snapshot.

The Automation page probes every connected account (a DPAPI decrypt plus, for
stale credentials, a real OAuth refresh). That probe cost ~400 ms and ran five
times per page rebuild plus once per 8 s tick, which is what made the tab feel
like it hung on every visit. ``statuses()`` now reuses the last probe for
``STATUS_CACHE_TTL_SEC``; connect/disconnect and any ``AUTH_STATE_CHANGED``
event clear it so the UI can never show a state older than the user's click.
"""
from __future__ import annotations

import gc
import time
import weakref

from chopster.automation import events
from chopster.automation.auth import STATUS_CACHE_TTL_SEC, AuthManager


class _Cfg:
    def get(self, key, default=None):
        # Client-credentials preflight must pass so connect() gets that far.
        if str(key).startswith("aut_"):
            return "stub"
        return default

    def set(self, _key, _value):
        return True


class _Logger:
    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None


class _DB:
    """Empty account list: ``_probe_statuses()`` then only walks the platforms."""

    def __init__(self):
        self.accounts: list[dict] = []

    def automation_list_accounts(self, platform=None):
        if platform is None:
            return list(self.accounts)
        return [a for a in self.accounts if a.get("platform") == platform]

    def automation_get_account(self, account_id):
        for acc in self.accounts:
            if acc.get("id") == account_id:
                return dict(acc)
        return None

    def automation_update_account(self, *_args, **_kwargs):
        return True


class _StubApp:
    def __init__(self):
        self.config = _Cfg()
        self.db = _DB()
        self.logger = _Logger()


class _PlatformStub:
    def __init__(self):
        self.calls: list[dict] = []

    def connect(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"id": "acc_stub", "platform": "youtube", "status": "connected"}


def _manager(monkeypatch):
    """An AuthManager whose real probe is wrapped in a call counter."""
    app = _StubApp()
    mgr = AuthManager(app)
    calls: list[int] = []
    real_probe = mgr._probe_statuses

    def _counting_probe():
        calls.append(1)
        return real_probe()

    monkeypatch.setattr(mgr, "_probe_statuses", _counting_probe)
    return mgr, calls


def test_statuses_reuses_one_probe_for_every_reader(monkeypatch):
    """Five callers per rebuild must not mean five DPAPI probes."""
    mgr, calls = _manager(monkeypatch)
    try:
        first = mgr.statuses()
        for _ in range(4):
            assert mgr.statuses() == first
        assert len(calls) == 1
    finally:
        mgr.close()


def test_callers_get_a_copy_so_the_cache_cannot_be_corrupted(monkeypatch):
    mgr, calls = _manager(monkeypatch)
    try:
        first = mgr.statuses()
        assert first is not mgr.statuses()
        first["youtube"] = {"state": "hacked"}
        assert mgr.statuses()["youtube"] != {"state": "hacked"}
        assert len(calls) == 1
    finally:
        mgr.close()


def test_force_and_ttl_expiry_both_re_probe(monkeypatch):
    mgr, calls = _manager(monkeypatch)
    try:
        mgr.statuses()
        assert len(calls) == 1

        mgr.statuses(force=True)  # explicit user refresh
        assert len(calls) == 2

        mgr._status_cached_at = time.time() - STATUS_CACHE_TTL_SEC - 1.0
        mgr.statuses()
        assert len(calls) == 3
    finally:
        mgr.close()


def test_connect_clears_the_snapshot(monkeypatch):
    mgr, calls = _manager(monkeypatch)
    try:
        mgr.statuses()
        assert mgr._status_cache is not None

        platform = _PlatformStub()
        monkeypatch.setitem(mgr._by_platform, "youtube", platform)
        result = mgr.connect("youtube")
        # PlatformAuth.connect() returns the flat account row (see YouTubeAuth).
        assert result["platform"] == "youtube"
        assert mgr._status_cache is None

        mgr.statuses()
        assert len(calls) == 2
    finally:
        mgr.close()


def test_disconnect_and_auth_events_clear_the_snapshot(monkeypatch):
    mgr, calls = _manager(monkeypatch)
    try:
        account = {"id": "acc_1", "platform": "youtube", "auth_mode": "oauth",
                   "status": "connected"}
        mgr.app.db.accounts = [account]

        class _SecretsStub:
            def account_key(self, account_id):
                return f"tok_{account_id}"

            def delete(self, _key):
                return True

        monkeypatch.setattr(mgr, "secrets", _SecretsStub())

        mgr.statuses()
        assert len(calls) == 1
        mgr.disconnect("acc_1")
        assert mgr._status_cache is None
        mgr.statuses()
        assert len(calls) == 2

        # Any platform emitting AUTH_STATE_CHANGED (login from another tab,
        # token revoked on the provider side, ...) must do the same.
        events.emit(events.AUTH_STATE_CHANGED, {"platform": "youtube", "status": "revoked"})
        assert mgr._status_cache is None
        mgr.statuses()
        assert len(calls) == 3
    finally:
        mgr.close()


def test_close_unsubscribes_from_the_process_wide_bus(monkeypatch):
    # No monkeypatched attribute here: the patch registry itself would keep the
    # manager alive and hide a real leak.
    mgr = AuthManager(_StubApp())
    stored = len(events._LISTENERS.get(events.AUTH_STATE_CHANGED, []))
    mgr.statuses()
    mgr.close()

    assert mgr._status_cache is None
    assert mgr.statuses() is not None
    # Listener removed: an event no longer invalidates this manager's snapshot…
    events.emit(events.AUTH_STATE_CHANGED, {"platform": "youtube", "status": "revoked"})
    assert mgr._status_cache is not None
    assert len(events._LISTENERS.get(events.AUTH_STATE_CHANGED, [])) == stored - 1

    ref = weakref.ref(mgr)
    del mgr
    gc.collect()
    assert ref() is None


def test_bus_listener_alone_never_keeps_a_manager_alive():
    """The listener holds a weakref, so a manager that is collected without
    close() (host crash, test teardown) cannot leak through the bus."""
    ref = weakref.ref(AuthManager(_StubApp()))
    gc.collect()
    assert ref() is None