"""
Bridge WebSocket registry — maps user_id → active bridge WebSocket connection.
All operations are async-safe (single event loop on Railway).
"""
import asyncio
import uuid
from typing import Dict, Optional
from fastapi import WebSocket


class BridgeRegistry:
    def __init__(self):
        self._ws: Dict[int, WebSocket] = {}          # user_id → websocket
        self._pending: Dict[str, "asyncio.Future"] = {}  # request_id → future

    def register(self, user_id: int, ws: WebSocket):
        self._ws[user_id] = ws

    def unregister(self, user_id: int):
        self._ws.pop(user_id, None)

    def is_connected(self, user_id: int) -> bool:
        return user_id in self._ws

    async def proxy(
        self,
        user_id: int,
        method: str,
        path: str,
        body=None,
        params: Optional[dict] = None,
        timeout: float = 60.0,
    ) -> dict:
        ws = self._ws.get(user_id)
        if ws is None:
            return {"__bridge_offline__": True}

        rid = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut

        try:
            await ws.send_json({
                "type": "request",
                "request_id": rid,
                "method": method,
                "path": path,
                "body": body,
                "params": params or {},
            })
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            return {"__bridge_timeout__": True}
        except Exception as e:
            return {"__bridge_error__": str(e)}
        finally:
            self._pending.pop(rid, None)

    def resolve(self, rid: str, data: dict):
        """Called from WS receive loop when bridge sends back a response."""
        fut = self._pending.get(rid)
        if fut and not fut.done():
            fut.set_result(data)


bridge_registry = BridgeRegistry()
