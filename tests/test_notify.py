import httpx

from wildcat3.notify import Notifier
from wildcat3.storage import Store


async def test_notification_retries_are_durable_and_no_secret_is_logged(tmp_path):
    store = Store(tmp_path / "db")
    store.event("FILL", "buy TAO/EUR", True)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(store, "test-secret-token", "123", client)
        await notifier.drain()
        assert len(calls) == 1
        event = store.events()[0]
        assert event["sent"] == 0 and event["attempts"] == 1
        assert store.notifications() == []  # exponential backoff, no busy retry
        assert all("test-secret-token" not in e["message"] for e in store.events())
    store.close()
    resumed = Store(tmp_path / "db")
    resumed.db.execute("UPDATE events SET next_try=0")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))) as client:
        await Notifier(resumed, "token", "123", client).drain()
    assert resumed.events()[0]["sent"] == 1
    resumed.close()


async def test_unconfigured_notification_makes_no_network_calls(tmp_path):
    store = Store(tmp_path / "db")
    store.event("FILL", "fill", True)
    def unexpected(request):
        raise AssertionError("must not call network")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        await Notifier(store, client=client).drain()
    assert len(store.notifications()) == 1
    store.close()
