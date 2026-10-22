import asyncio
import json
import os
import ssl
import time
from collections import deque
from datetime import datetime, timezone

import certifi
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Local testing: reads backend/.env if it exists.
# On Render there is no .env file; the real environment variables are used instead.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = FastAPI(title="Connect WebSocket Chat", version="2.0.0")

# ---------------------------------------------------------------------------
# CORS (only affects normal HTTP calls like /health; WebSockets don't use CORS)
# On Render set FRONTEND_ORIGINS to your Vercel URL(s), comma-separated.
# If it is not set (local testing) these defaults are used.
# ---------------------------------------------------------------------------
raw_origins = os.getenv(
    "FRONTEND_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:5500,http://127.0.0.1:5500,"
    "http://localhost:8080,http://127.0.0.1:8080,"
    "https://chit-chat-frontend-one.vercel.app",
)
allowed_origins = [origin.strip().rstrip("/") for origin in raw_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Supabase settings (set in backend/.env locally, or in Render > Environment)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_CONFIGURED = bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY)

if not SUPABASE_CONFIGURED:
    print("WARNING: SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_KEY are not all set. Logins will fail.")

HISTORY_LIMIT = 50      # how many past messages a user receives when joining
RATE_MAX = 8            # max messages per connection...
RATE_WINDOW = 10        # ...within this many seconds

# Use certifi's certificate list directly. This avoids a crash when the machine has a broken
# SSL_CERT_FILE environment variable (common with Anaconda on Windows).
ssl_context = ssl.create_default_context(cafile=certifi.where())
http = httpx.AsyncClient(timeout=10, verify=ssl_context)
background_tasks = set()


def service_headers():
    headers = {"apikey": SUPABASE_SERVICE_KEY}
    if SUPABASE_SERVICE_KEY.startswith("eyJ"):  # legacy JWT-style service key
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    return headers


async def verify_token(token: str):
    """Ask Supabase whether this login token is valid. Returns the user id, or None."""
    try:
        r = await http.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    return r.json().get("id")


async def get_username(user_id: str):
    """Read the permanent username from the profiles table. Returns None if missing."""
    try:
        r = await http.get(
            f"{SUPABASE_URL}/rest/v1/profiles",
            params={"id": f"eq.{user_id}", "select": "username"},
            headers=service_headers(),
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0]["username"] if rows else None


async def save_message(sender: str, recipient, content: str):
    """Store one message. recipient=None means a public message."""
    try:
        r = await http.post(
            f"{SUPABASE_URL}/rest/v1/messages",
            json={"sender": sender, "recipient": recipient, "content": content},
            headers={**service_headers(), "Prefer": "return=minimal"},
        )
        if r.status_code >= 300:
            print(f"[supabase] save failed {r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as exc:
        print(f"[supabase] save error: {exc}")


def save_in_background(sender: str, recipient, content: str):
    """Save without making the chat wait for the database."""
    task = asyncio.create_task(save_message(sender, recipient, content))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def load_history(username: str):
    """Last HISTORY_LIMIT messages this user may see: public + their own private chats."""
    try:
        r = await http.get(
            f"{SUPABASE_URL}/rest/v1/messages",
            params={
                "select": "sender,recipient,content,created_at",
                "or": f"(recipient.is.null,sender.eq.{username},recipient.eq.{username})",
                "order": "created_at.desc",
                "limit": str(HISTORY_LIMIT),
            },
            headers=service_headers(),
        )
    except httpx.HTTPError as exc:
        print(f"[supabase] history error: {exc}")
        return []
    if r.status_code != 200:
        print(f"[supabase] history failed {r.status_code}: {r.text[:200]}")
        return []
    rows = list(reversed(r.json()))  # oldest first
    return [
        {
            "type": "private" if row["recipient"] else "chat",
            "username": row["sender"],
            "recipient": row["recipient"],
            "message": row["content"],
            "timestamp": row["created_at"],
        }
        for row in rows
    ]


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
        # The socket is already accepted by the endpoint.
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

    async def find_user(self, name: str):
        """Return (websocket, exact_username) for an online user, or None."""
        async with self.lock:
            for ws, user in self.connections.items():
                if user.casefold() == name.casefold():
                    return ws, user
        return None

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
        # send() handles individual failures; gather keeps one failure from stopping the rest.
        await asyncio.gather(*(self.send(target, data) for target in targets), return_exceptions=True)

    async def update_users(self):
        users = await self.users()
        await self.broadcast(event("users", users=users, online_users=len(users)))


manager = ConnectionManager()


@app.get("/")
async def health():
    return {"status": "ok", "service": "Connect WebSocket Chat", "websocket": "/ws/{username}"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "supabase_configured": SUPABASE_CONFIGURED}


@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    # NOTE: the {username} in the URL is ignored. The real username comes from the database.
    await websocket.accept()

    if not SUPABASE_CONFIGURED:
        await websocket.send_json(event("error", message="Server is not configured yet."))
        await websocket.close(code=1011, reason="Server not configured")
        return

    # --- 1. Login check: first message must be {"type": "auth", "token": "<Supabase access token>"} ---
    try:
        raw_auth = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except WebSocketDisconnect:
        return
    except asyncio.TimeoutError:
        await websocket.send_json(event("error", message="Login required."))
        await websocket.close(code=1008, reason="Login required")
        return

    try:
        auth = json.loads(raw_auth)
    except json.JSONDecodeError:
        auth = None
    token = auth.get("token") if isinstance(auth, dict) and auth.get("type") == "auth" else None
    user_id = await verify_token(token) if isinstance(token, str) and token else None
    username = await get_username(user_id) if user_id else None

    if not username:
        await websocket.send_json(event("error", message="Please log in again."))
        await websocket.close(code=1008, reason="Not authenticated")
        return

    if not await manager.connect(websocket, username):
        await websocket.send_json(event("error", message="You are already connected in another tab or device."))
        await websocket.close(code=1008, reason="Already connected")
        return

    # --- 2. Send chat history to this user, then announce the join ---
    history = await load_history(username)
    await manager.send(websocket, event("history", messages=history))
    await manager.broadcast(event("system", message=f"{username} joined the chat"))
    await manager.update_users()

    recent = deque()  # timestamps of this user's recent messages (rate limit)

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

            # Rate limit: at most RATE_MAX messages per RATE_WINDOW seconds
            now = time.monotonic()
            while recent and now - recent[0] > RATE_WINDOW:
                recent.popleft()
            if len(recent) >= RATE_MAX:
                await manager.send(websocket, event("error", message="You are sending messages too fast. Please slow down."))
                continue
            recent.append(now)

            # --- Public message ---
            if recipient is None or recipient == "":
                await manager.broadcast(event("chat", username=username, message=message))
                save_in_background(username, None, message)
                continue

            # --- Private message ---
            if not isinstance(recipient, str):
                await manager.send(websocket, event("error", message="Invalid recipient."))
                continue
            recipient = recipient.strip()
            if not recipient or recipient.casefold() == username.casefold():
                await manager.send(websocket, event("error", message="Choose another user for a private message."))
                continue

            target = await manager.find_user(recipient)
            if target is None:
                await manager.send(websocket, event("error", message=f"{recipient} is not online. Message not sent."))
                continue

            target_ws, recipient = target  # use the exact stored spelling of the username
            private_event = event("private", username=username, recipient=recipient, message=message)
            if await manager.send(target_ws, private_event):
                await manager.send(websocket, private_event)  # echo to sender
                save_in_background(username, recipient, message)
            else:
                await manager.send(websocket, event("error", message=f"{recipient} is not online. Message not sent."))

    except WebSocketDisconnect:
        pass
    finally:
        disconnected_user = await manager.disconnect(websocket)
        if disconnected_user is not None:
            await manager.broadcast(event("system", message=f"{disconnected_user} left the chat"))
            await manager.update_users()