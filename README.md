# Connect — WebSocket Chat (Render + Vercel)

This repo is split into `backend/` (FastAPI WebSocket API) and `frontend/` (static HTML/CSS/JS).

## 1. Deploy backend to Render

1. Push this repository to GitHub.
2. In Render, create a **Blueprint** from the repository and let it read `backend/render.yaml`, OR create a Web Service manually with:
   - Root Directory: `backend`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. In the Render service's Environment settings, set `FRONTEND_ORIGINS` to the exact Vercel origin, e.g. `https://your-project.vercel.app`. For multiple origins, separate them with commas. No trailing slash.
4. Deploy. Verify `https://YOUR-RENDER-SERVICE.onrender.com/health` returns `{"status":"healthy"}`.

## 2. Configure and deploy frontend to Vercel

1. Edit `frontend/config.js` and replace `https://YOUR-RENDER-SERVICE.onrender.com` with your actual Render service URL (no trailing slash).
2. In Vercel, import the same GitHub repository.
3. Set **Root Directory** to `frontend`; framework preset: **Other**; Build Command: leave empty; Output Directory: `.`.
4. Deploy and open the Vercel URL.
5. If you use a custom domain or Vercel preview URL, add that exact origin to Render's `FRONTEND_ORIGINS` and redeploy/restart the backend.

## Local test

Backend:
```bash
cd backend
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

For local frontend testing, set `window.CHAT_BACKEND_URL` in `frontend/config.js` to `http://127.0.0.1:8000`, then serve the frontend using a local static server (do not open the file directly). Add the frontend origin to `FRONTEND_ORIGINS` if needed.

## Current limitations

- Usernames are not authenticated; someone can impersonate a name after it disconnects.
- Connections and online users are in memory. Use one Render instance for this prototype; multiple instances require shared coordination.
- Messages are not stored and private messages are not end-to-end encrypted.
- This is a learning prototype, not a hardened production chat service.
