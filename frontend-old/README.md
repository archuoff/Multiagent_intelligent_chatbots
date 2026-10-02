# Demo chat UI

A single static page (`index.html`, no build step) that talks to the real
`GET /agents` / `POST /chat` endpoints in `backend/api/routes.py`. No mock
data -- every answer and citation shown is generated live by the actual
query pipeline against whatever is indexed in Qdrant.

## Run it

1. Start the backend (from the repo root, with your `.env` and ingested
   Qdrant data already in place):

   ```
   uvicorn backend.api.app:app --reload --port 8000
   ```

2. Serve this folder on a port the backend's CORS config already allows
   (`localhost:3000` or `localhost:5178` -- see `backend/api/app.py`).
   Opening `index.html` directly as a `file://` URL will not work; the
   browser needs to send an allowed origin.

   ```
   cd frontend
   python -m http.server 3000
   ```

3. Open `http://localhost:3000` in a browser.

If the backend runs on a different host/port, change the "Backend URL"
field in the sidebar -- no code edit needed.

## Notes

- Auth is currently a dev stub on the backend (`backend/api/dependencies.py`),
  so there is no login step here either.
- Conversation history is kept in memory in the browser tab only (per
  agent, last 10 turns) and is lost on refresh -- there is no server-side
  session store yet.
