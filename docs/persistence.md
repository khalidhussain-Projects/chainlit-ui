# Persistence — `main.py`

This document describes how we get **durable conversation history** in the Chainlit + `openai-agents` app in `main.py`, and how that fits a **Render + Neon** deployment.

> **Status:** implemented. The session layer is owned by `SQLAlchemySession` from `openai-agents`; the engine is built once per worker process. See §11 for a changelog.

---

## 1. Goal

Every message in the chat app should be **persisted to a database** so the agent remembers prior turns in the same thread. The implementation must:

- Use the **first-party `SQLAlchemySession`** from `openai-agents` (no custom `Session` subclass).
- Be **async-friendly** to match `await Runner.run_streamed(...)`.
- Work on **Render** with a **Neon Postgres** branch (Render's disk is ephemeral ⇒ no SQLite on Render).
- Be **safe to develop locally** with SQLite so you don't burn a DB connection during iteration.
- Build a single async engine per worker process (not per request).

## 2. Library choice — why `SQLAlchemySession`

`openai-agents` ships built-in session support at `agents.extensions.memory.sqlalchemy_session.SQLAlchemySession`. It implements the `Session` ABC (the four methods the runner calls: `get_items(limit)`, `add_items(items)`, `pop_item()`, `clear_session()`) and stores conversation items as JSON in a single table. Reasons we use it instead of rolling our own:

- The runner already knows how to talk to it — just pass `session=...` into `Runner.run_streamed`.
- Schema is owned by the SDK, persistence is owned by us. Clean separation.
- We can later wrap it in `EncryptedSession` for at-rest encryption with one line.

## 3. Storage choice — Neon Postgres on Render, SQLite locally

| Environment | Driver | URL example | Why |
|---|---|---|---|
| Local dev | `sqlite+aiosqlite` | `sqlite+aiosqlite:///./agents.db` | Zero setup, file in repo (gitignored). |
| Render (Neon free tier) | `postgresql+asyncpg` | `postgresql+asyncpg://user:pass@host/db?sslmode=require&channel_binding=require` | Render's free tier gives an ephemeral disk, so SQLite would be wiped on every redeploy. Neon's connection string carries `sslmode=require`, which we strip before handing to asyncpg. |

The async driver matters because `SQLAlchemySession` is async, and the runner awaits it inside the event loop.

## 4. `session_id` — how we identify a thread

The session id is a **stable per-(user, thread) key**, composed at session-build time:

```python
def _make_session(thread_id: str | None) -> SQLAlchemySession:
    user = cl.user_session.get("user")
    user_id = user.identifier if user is not None else "anon"
    tid = thread_id or "default"
    sid = f"user:{user_id}:thread:{tid}"
    return SQLAlchemySession(session_id=sid, engine=_get_engine(), create_tables=True)
```

Three things this buys us:

1. **Cross-device history for a returning user.** Because `cl.user_session.get("user")` returns a `chainlit.User` whose `.identifier` is set by `@cl.password_auth_callback` (see §7), the same login on a second browser resolves to the same `user:…:thread:…` key — so the new device sees the old threads.
2. **Isolation between users on the same machine.** Two people logging in as `demo` and `alice` get distinct `user:demo:thread:…` and `user:alice:thread:…` keys. The agent's history for one never bleeds into the other's session.
3. **Greppable history rows.** The `user:<id>:thread:<id>` shape is searchable in the `agent_history` table — `SELECT session_id, count(*) FROM agent_history GROUP BY session_id` returns cleanly labeled rows.

The `anon:` fallback only fires if `cl.user_session.get("user")` returns `None` — which is rare when password auth is on, but it keeps an in-flight session during a deploy from crashing if the auth handshake hasn't completed yet. It also keeps any pre-auth rows (if you had history from before turning auth on) from being merged into a real user's session.

### Before/after cutover

If you turn auth on after history already exists, the old `session_id` values (bare `thread_id`s) won't match the new `user:<id>:thread:<id>` keys. Two options:

- **Fresh start** (recommended for a demo) — let the old rows sit, ignore them. New messages go into the new keys.
- **Continuity** — one-shot SQL in the Neon editor:
  ```sql
  UPDATE agent_history
     SET session_id = 'user:demo:thread:' || session_id
   WHERE session_id NOT LIKE 'user:%';
  ```
  This rebinds every anonymous row to the `demo` user. Run it once, then redeploy.

`create_tables=True` is idempotent — safe to leave on in dev and small Render instances.

## 5. Lifecycle — when sessions are created

| Hook | What runs | Why |
|---|---|---|
| `@cl.on_chat_start` | Just the welcome message. **No session is opened yet.** | We don't have a stable `thread_id` at this point. |
| `@cl.on_message` | `_make_session(...)` is called, then `session=session` is passed to `Runner.run_streamed`. | This is where the conversation actually exists. The runner auto-loads prior items and auto-appends new ones. |

The engine itself is built once per worker process by `@lru_cache(maxsize=1)`-wrapped `_get_engine()`. Sessions are short-lived (one request scope); the engine outlives them.

## 6. Configuration

`.env` holds:

```bash
OPENAI_API_KEY=...

DATABASE_URL=postgresql+asyncpg://user:password@host/db?sslmode=require&channel_binding=require

# Required for password auth (see §7). Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
CHAINLIT_AUTH_SECRET=...

# Passwords for the demo accounts in main.py's _DEMO_USERS dict.
# Defaults to "demo" / "alice123" if unset — set real values on Render.
CHAINLIT_AUTH_PASSWORD_DEMO=demo
CHAINLIT_AUTH_PASSWORD_ALICE=alice123

# (optional) SESSION_ENCRYPTION_KEY=<32-byte url-safe base64>  # only if we enable EncryptedSession
```

`main.py` normalizes the URL at engine-build time so it works for both Render and local dev:

```python
def _resolve_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        if os.getenv("RENDER"):  # Render sets this on every service
            raise RuntimeError(
                "DATABASE_URL is required on Render — the disk is ephemeral, "
                "so a SQLite fallback would lose history on every redeploy."
            )
        return "sqlite+aiosqlite:///./agents.db"  # local dev only
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url
```

Two notable details:

- **SSL keys are stripped before asyncpg sees them.** Neon's URL has `?sslmode=require&channel_binding=require`. asyncpg rejects those when we also pass `ssl=True` via `connect_args` (it complains "sslmode can only be specified once"). `_build_engine_url()` does `make_url(...).difference_update_query(["sslmode", "channel_binding", "sslcert", "sslkey", "sslrootcert"])` and then `connect_args={"ssl": True}` handles SSL cleanly.
- **Render hard-fail.** If `DATABASE_URL` is unset *and* `RENDER` is set, `_resolve_database_url()` raises at first call. The SQLite fallback is local-dev only — silently using it on Render would lose history on every redeploy.

### Why `NullPool`

The engine uses `poolclass=NullPool` so each `SQLAlchemySession` borrows a connection for one `add_items` / `get_items` call and returns it immediately, instead of holding an idle pool across requests. Neon's free tier allows ~100 connections, but its PgBouncer-style pooler doesn't support server-side prepared statements — so we also set `statement_cache_size=0` and `prepared_statement_cache_size=0` in `connect_args` to avoid `PreparedStatementError` on the second request through a pooled connection.

## 7. The data layer decision

`@cl.data_layer` returns `None`. That's deliberate: Chainlit's default `ChainlitDataLayer` would auto-initialize its own `Thread`/`Step`/`Element` schema on top of `DATABASE_URL`, but (a) its schema expectations don't match what `openai-agents` writes, and (b) it passes SSL query params that asyncpg rejects. Returning `None` opts us out of Chainlit's persistence entirely; the only persistence in this app is the `SQLAlchemySession` for agent memory. `db/schema.sql` is kept on disk for reference (it's the schema Chainlit's data layer would have used) but it is **not** applied at boot.

## 7a. Authentication

`@cl.password_auth_callback` (`auth_callback` in `main.py`) is the only auth in this app. It reads passwords from env vars (`CHAINLIT_AUTH_PASSWORD_DEMO`, `CHAINLIT_AUTH_PASSWORD_ALICE`) and returns a `cl.User(identifier=username, display_name=username.title())` on success, `None` on failure. With this callback registered, Chainlit shows a login form at `/login` before the chat UI is reachable.

The reason this matters for persistence: `cl.user_session.get("user")` returns the logged-in `User` object, and `user.identifier` is what `_make_session` uses to compose the `session_id`. Without auth, that key falls back to Chainlit's per-tab `user_session.id` — which means a second browser tab gets a *different* history from the first tab of the same user. With auth on, the key is stable across tabs, browsers, and devices.

Adding more users is a one-line change to `_DEMO_USERS`. Beyond a handful, swap the dict for a DB lookup (hashed passwords, ideally `bcrypt` or `argon2` — and use `hmac.compare_digest` instead of `==` to avoid timing leaks).

## 8. Out of scope (for this round)

- `EncryptedSession` wrapping — easy to add later (one env var, one wrapper line). Not blocking v1.
- Migrations — `create_tables=True` is enough until the schema evolves.
- Multi-user isolation hardening — fine for a single-user demo; revisit if exposed publicly.
- A separate `db.py` module — for now, helpers live at the top of `main.py` to keep the diff small.

## 9. Rollback

If anything misbehaves, the persistence layer is isolated to the helpers at the top of `main.py` (lines ~12-84). Reverting those to a pre-session, no-DB build reverts the file to its streaming-only behavior with no other side effects.

---

## 10. Render deployment notes

These are the settings to use when deploying this app to [Render](https://render.com) backed by [Neon](https://neon.tech) Postgres. Apply them in the Render dashboard under your Web Service.

### 10.1 Service type
- **Type:** Web Service
- **Runtime:** Python 3.13 (Render auto-detects from `pyproject.toml` / `requires-python`)
- **Region:** Pick the same region as your Neon project to minimize latency.

### 10.2 Build & start commands
| Field | Value |
|---|---|
| Build command | `uv sync --frozen` |
| Start command | `chainlit run main.py --host 0.0.0.0 --port $PORT` |

Notes:
- `--host 0.0.0.0` is required so Render can route external traffic to the container.
- `--port $PORT` uses the port Render injects at runtime. Don't hard-code a port.
- `--frozen` on `uv sync` makes the build fail if `uv.lock` is out of date, which catches accidental dep drift on Render.

### 10.3 Environment variables
Set these in the **Environment** tab of the Web Service:

| Key | Value | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Your OpenAI key | Same value as in your local `.env`. Mark as secret. |
| `DATABASE_URL` | Neon connection string for the branch you want this service to use | Comes from **Neon → Project → Connection Details**, with `pooled` toggled ON for serverless, OFF for long-lived. `?sslmode=require&channel_binding=require` is expected and stripped by `_build_engine_url()`. Mark as secret. |
| `CHAINLIT_AUTH_SECRET` | Long random string | **Required.** Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Used by Chainlit to sign JWT session cookies. The first login attempt will throw if this is unset. **Changing this value logs out every active session** — rotate deliberately. Mark as secret. |
| `CHAINLIT_AUTH_PASSWORD_DEMO` | Password for the `demo` account | Defaults to `"demo"` if unset, but set a real value on Render. Mark as secret. |
| `CHAINLIT_AUTH_PASSWORD_ALICE` | Password for the `alice` account | Defaults to `"alice123"` if unset, but set a real value on Render. Mark as secret. |
| `RENDER` | (auto-set) | Render injects `RENDER=true` on every service. Our code uses this to decide whether to hard-fail on missing `DATABASE_URL`. Don't set it manually. |
| `PYTHON_VERSION` | `3.13` | Optional but speeds up detection. |

Do **not** commit `.env` to git — your repo's `.gitignore` excludes it.

### 10.4 Provisioning the database
1. In Neon: create a project (free tier is fine for dev) and a branch.
2. Copy the **pooled** connection string for the branch from the Neon dashboard. That's what goes into `DATABASE_URL` on Render.
3. **No manual schema step is required.** `get_data_layer() -> None` disables Chainlit's data layer entirely, and `SQLAlchemySession(create_tables=True)` creates its own `agent_history` table on the first message. `db/schema.sql` is kept for reference but is not applied.
4. The Web Service needs to be **redeployed** after the env var changes.

### 10.5 Things to know about Neon's free tier
- **Connection limit** — Neon free allows ~100 concurrent connections. With `NullPool` plus per-request `SQLAlchemySession`s, this is comfortable for a demo. Each gunicorn worker on Render holds its own engine; with one worker, peak concurrent connections ≈ in-flight requests.
- **PgBouncer-style pooler** — Neon's pooled endpoint is transaction-mode and does not support server-side prepared statements, hence the `statement_cache_size=0` / `prepared_statement_cache_size=0` in `connect_args`. If you ever see `PreparedStatementError` or `statement_cache ...` errors, you've either removed those flags or pointed `DATABASE_URL` at the direct (un-pooled) endpoint by mistake.
- **Cold starts** — Render free Web Services spin down after ~15 min of inactivity. First request after a cold start may take 10–30s while the engine pool warms up.
- **SSL** — Neon requires SSL. The connection string includes `?sslmode=require&channel_binding=require`; our code strips those before handing the URL to asyncpg and uses `connect_args["ssl"]=True` instead.

### 10.6 Post-deploy smoke test
After the first successful deploy:

1. Open the public URL Render gives you — you should see the Chainlit chat UI and the welcome message "Welcome to the Panaversity AI Assistant!".
2. Send a message like "Hello, my name is Alice." — confirm the agent responds.
3. Send a follow-up like "What's my name?" — the agent should remember "Alice" **if** it's the same Chainlit thread (don't click "New Chat" between turns). This proves the session is loading prior history.
4. Click **New Chat** and ask "What's my name?" — the agent should *not* know, because it's a fresh thread with no history. This proves the session is scoped per thread.
5. In the Neon SQL editor, run:
   ```sql
   SELECT count(*) FROM agent_history;
   ```
   The count should be > 0 and grow as you send messages.
6. Tail the Web Service logs in the Render dashboard. You should see `[Tool Call detected]` when the `greeting_function` tool is invoked.

If any of those fail, check the Web Service logs first — connection errors and import errors will appear there. A `RuntimeError: DATABASE_URL is required on Render` at boot means the env var isn't set on this service.

### 10.7 Updating the deploy
After pulling new commits to your repo:
- Render auto-deploys on push to the connected branch if **Auto-Deploy** is enabled.
- If env vars change, manually trigger a redeploy from the **Manual Deploy** button.
- `pyproject.toml` changes trigger a fresh `uv sync` on the next build.

### 10.8 Local vs prod parity
The same `main.py` runs in both environments. Differences are only the env vars:

| | Local | Render |
|---|---|---|
| `OPENAI_API_KEY` | from `.env` | from Web Service env |
| `DATABASE_URL` | unset → SQLite file `./agents.db` | Neon pooled connection string with `?sslmode=require&channel_binding=require` (stripped before use) |
| `RENDER` | unset | `true` (auto-set by Render) |
| Port | `chainlit` default (8000) | `$PORT` injected by Render |

---

## 11. Refactor log

- **2026-09-03 — session handling refactor.**
  - Replaced per-`on_message` `create_async_engine(...)` with a module-level `@lru_cache(maxsize=1)` engine singleton (`_get_engine`). One engine per worker process instead of one per request.
  - Switched the engine to `poolclass=NullPool` so each session borrows a connection and returns it immediately, instead of holding idle connections across requests on Neon's pooled endpoint.
  - Added `_build_engine_url()` which calls `make_url(...).difference_update_query(["sslmode", "channel_binding", "sslcert", "sslkey", "sslrootcert"])`. Fixes the latent `sslmode can only be specified once` error that would have fired against any Neon URL once asyncpg received both the query string and `connect_args["ssl"]=True`.
  - Added a `RENDER` env-var guard in `_resolve_database_url()`: if `DATABASE_URL` is unset on Render, raise `RuntimeError` instead of silently falling back to ephemeral-disk SQLite.
  - Refreshed this doc to match (DB is Neon, not Render PG; `chainlit data create` is no longer required because `get_data_layer() -> None`; `db/schema.sql` is kept for reference only).
- **2026-09-03 — password auth + per-user session keys.**
  - Added `@cl.password_auth_callback` (`auth_callback`) backed by a 2-entry `_DEMO_USERS` dict keyed off `CHAINLIT_AUTH_PASSWORD_DEMO` / `CHAINLIT_AUTH_PASSWORD_ALICE` env vars.
  - Rewrote `_make_session` to compose `session_id = f"user:{user.identifier}:thread:{thread_id}"` instead of using Chainlit's per-tab `user_session.id`. A returning user now sees the same threads across browsers and devices; different users on the same machine stay isolated.
  - Documented the cutover in §4 (with a one-shot SQL to rebind pre-auth rows to the `demo` user if continuity matters) and the new env vars in §10.3 (`CHAINLIT_AUTH_SECRET` is required).
