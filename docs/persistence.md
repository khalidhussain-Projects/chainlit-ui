# Persistence Plan — `main.py`

This document describes how we add **durable conversation history** to the Chainlit + `openai-agents` app in `main.py`, and how that fits a **Render** deployment.

> **Status:** plan, not yet implemented. Each numbered step below will be applied to `main.py` (and supporting files) one at a time, with explicit approval before each edit.

---

## 1. Goal

Every message in the chat app should be **persisted to a database** so the agent remembers prior turns in the same thread. The implementation must:

- Use the **first-party `SQLAlchemySession`** from `openai-agents` (no custom `Session` subclass).
- Be **async-friendly** to match `await Runner.run_streamed(...)`.
- Work on **Render** with their free Postgres (ephemeral filesystem ⇒ no SQLite on Render).
- Be **safe to develop locally** with SQLite so you don't burn a DB connection during iteration.

## 2. Library choice — why `SQLAlchemySession`

`openai-agents` ships built-in session support at `agents.extensions.memory.sqlalchemy_session.SQLAlchemySession`. It implements the `Session` ABC (the two methods the runner calls: `get_items(limit)` and `add_items(items)`) and stores conversation items as JSON in a single table. Reasons we use it instead of rolling our own:

- The runner already knows how to talk to it — just pass `session=...` into `Runner.run_streamed`.
- Schema is owned by us, persistence is owned by the SDK. Clean separation.
- We can later wrap it in `EncryptedSession` for at-rest encryption with one line.

## 3. Storage choice — Postgres on Render, SQLite locally

| Environment | Driver | URL example | Why |
|---|---|---|---|
| Local dev | `sqlite+aiosqlite` | `sqlite+aiosqlite:///./agents.db` | Zero setup, file in repo (gitignored). |
| Render (free Postgres) | `postgresql+asyncpg` | `postgresql+asyncpg://user:pass@host/db` | Render's free tier gives an ephemeral disk, so SQLite would be wiped on every redeploy. Postgres is the only safe option. |

The async driver matters because `SQLAlchemySession` is async, and the runner awaits it inside the event loop.

## 4. `session_id` — how we identify a thread

The session id is a **stable per-conversation key**. Source order (first non-empty wins):

1. `message.thread_id` — Chainlit's per-conversation thread id; flips when the user clicks **New Chat**.
2. `cl.user_session.id` — per-tab fallback so a threadless reload still gets persistence.
3. **Never** generate a random id at this layer — that would erase history on every reload.

The factory looks like:

```python
def _make_session(thread_id: str | None) -> SQLAlchemySession:
    sid = thread_id or cl.user_session.get("id") or "default"
    return SQLAlchemySession(session_id=sid, url=DATABASE_URL, create_tables=True)
```

`create_tables=True` is idempotent — safe to leave on in dev and small Render instances.

## 5. Lifecycle — when sessions are created

| Hook | What runs | Why |
|---|---|---|
| `@cl.on_chat_start` | Just the welcome message. **No session is opened yet.** | We don't have a stable `thread_id` at this point. |
| `@cl.on_message` | `_make_session(...)` is called, then `session=session` is passed to `Runner.run_streamed`. | This is where the conversation actually exists. The runner auto-loads prior items and auto-appends new ones. |

## 6. Configuration

`.env` will hold:

```bash
# already present
OPENAI_API_KEY=...

# new
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
# (optional) SESSION_ENCRYPTION_KEY=<32-byte url-safe base64>  # only if we enable EncryptedSession
```

`main.py` will normalize the URL at startup so it works for both Render and local dev:

```python
def _resolve_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        return "sqlite+aiosqlite:///./agents.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url
```

Render sometimes hands out `postgres://...`; we rewrite to `postgresql+asyncpg://...` in one place.

## 7. Implementation steps (each requires approval)

These mirror the Step 1–5 style: I'll show the diff and wait for your OK before each edit.

1. **Step 7 — Add session infrastructure to `main.py`.** Imports, `_resolve_database_url`, `_make_session`. No behavior change yet.
2. **Step 8 — Wire session into `on_message`.** Build the session, pass `session=...` to `Runner.run_streamed`, add a `try/except` so DB failures surface as a chat message.
3. **Step 9 — Update `pyproject.toml`.** Add `asyncpg` and `aiosqlite` to dependencies so Render builds install the right drivers.
4. **Step 10 — Render deployment notes.** Append a short section to this file with the env vars, the build command, and the start command Render needs.

## 8. Out of scope (for this round)

- `EncryptedSession` wrapping — easy to add later (one env var, one wrapper line). Not blocking v1.
- Migrations — `create_tables=True` is enough until the schema evolves.
- Multi-user isolation hardening — fine for a single-user demo; revisit if exposed publicly.
- A separate `db.py` module — for now, helpers live at the top of `main.py` to keep the diff small.

## 9. Rollback

If anything misbehaves, the persistence layer is isolated to the helpers added in Step 7. Removing them reverts `main.py` to its current streaming-only behavior with no other side effects.
