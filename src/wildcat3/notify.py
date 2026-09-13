"""Telegram delivery from the durable event outbox. Never blocks execution on retries."""
import asyncio

import httpx


class Notifier:
    def __init__(self, store, token: str = "", chat: str = "", client=None):
        self.store, self.token, self.chat = store, token, chat
        self.enabled = bool(token and chat)
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_client = client is None
        self.stopped = asyncio.Event()

    async def drain(self):
        if not self.enabled:
            return
        for event in self.store.notifications():
            try:
                response = await self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat, "text": f"WildCat-3 · {event['level']}\n{event['message']}"[:4000]})
                success = response.status_code == 200 and response.json().get("ok") is True
            except Exception:
                success = False  # URL contains the secret token; never log exception text.
            self.store.notification_result(event["id"], success)
            if not success:
                self.store.set("notification_health", "Delivery failed; durable retry pending")
                break
            self.store.set("notification_health", "Telegram connected")

    async def run(self):
        self.store.set("notification_health", "Telegram configured" if self.enabled else "Telegram not configured")
        while not self.stopped.is_set():
            await self.drain()
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=2)
            except TimeoutError:
                pass

    async def close(self):
        self.stopped.set()
        if self.owns_client:
            await self.client.aclose()
