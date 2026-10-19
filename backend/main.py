import asyncio
import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Connect WebSocket Chat", version="1.0.0")

# Set FRONTEND_ORIGINS on Render to your Vercel URL(s), comma-separated.
# Example: https://your-chat.vercel.app
raw_origins = os.getenv("FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:5500", "https://chit-chat-frontend-one.vercel.app/")
allowed_origins = [origin.strip().rstrip("/") for origin in raw_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def event(event_type: str, **payload):
    return {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }


class ConnectionManager:
    def __init__(self):
        self.connections: dict[WebSocket, str] = {}
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, username: str) -> bool:
        await websocket.accept()
        async with self.lock:
            if any(name.casefold() == username.casefold() for name in self.connections.values()):
                return False
            self.connections[websocket] = username
            return True

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            return self.connections.pop(websocket, None)

    async def users(self):
        async with self.lock:
            return sorted(self.connections.values(), key=str.casefold)

    async def send(self, websocket: WebSocket, data: dict) -> bool:
        try:
            await websocket.send_json(data)
            return True
        except Exception:
            await self.disconnect(websocket)
            return False

    async def broadcast(self, data: dict):
        async with self.lock:
            targets = list(self.connections.keys())
        if not targets:
            return
        results = await asyncio.gather(*(self.send(target, data) for target in targets), return_exceptions=True)
        # send() handles individual connection failures; gather prevents one failure stopping others.

    async def update_users(self):
        users = await self.users()
        await self.broadcast(event("users", users=users, online_users=len(users)))

    async def send_private(self, recipient: str, data: dict) -> bool:
        async with self.lock:
            target = next((ws for ws, name in self.connections.items() if name.casefold() == recipient.casefold()), None)
        if target is None:
            return False
        return await self.send(target, data)


manager = ConnectionManager()


@app.get("/")
async def health():
    return {"status": "ok", "service": "Connect WebSocket Chat", "websocket": "/ws/{username}"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    username = username.strip()
    if not username or len(username) > 25 or any(ord(ch) < 32 for ch in username):
        await websocket.close(code=1008, reason="Username must be 1-25 characters")
        return

    if not await manager.connect(websocket, username):
        await websocket.send_json(event("error", message="That username is already in use."))
        await websocket.close(code=1008, reason="Username already in use")
        return

    await manager.broadcast(event("system", message=f"{username} joined the chat"))
    await manager.update_users()

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 5000:
                await manager.send(websocket, event("error", message="Request is too large."))
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send(websocket, event("error", message="Invalid message format."))
                continue
            if not isinstance(data, dict):
                continue

            message = data.get("message")
            recipient = data.get("recipient")
            if not isinstance(message, str):
                continue
            message = message.strip()
            if not message:
                continue
            message = message[:2000]

            if recipient is None or recipient == "":
                await manager.broadcast(event("chat", username=username, message=message))
                continue
            if not isinstance(recipient, str):
                await manager.send(websocket, event("error", message="Invalid recipient."))
                continue
            recipient = recipient.strip()
            if not recipient or recipient.casefold() == username.casefold():
                await manager.send(websocket, event("error", message="Choose another user for a private message."))
                continue

            private_event = event("private", username=username, recipient=recipient, message=message)
            delivered = await manager.send_private(recipient, private_event)
            if delivered:
                await manager.send(websocket, private_event)  # echo to sender
            else:
                await manager.send(websocket, event("error", message=f"{recipient} is not online. Message not sent."))

    except WebSocketDisconnect:
        pass
    finally:
        disconnected_user = await manager.disconnect(websocket)
        if disconnected_user is not None:
            await manager.broadcast(event("system", message=f"{disconnected_user} left the chat"))
            await manager.update_users()
