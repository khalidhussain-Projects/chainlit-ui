from dotenv import load_dotenv
import chainlit as cl
from agents import Agent, Runner, function_tool
from openai.types.responses import ResponseTextDeltaEvent
import os
from functools import lru_cache
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

# Load the environment variables from the .env file
load_dotenv()


def _resolve_database_url() -> str:
    """Pick the right DB URL for the current environment.

    Local dev:  falls back to sqlite+aiosqlite if DATABASE_URL is unset.
    Render:     normalizes postgres:// and postgresql:// to the asyncpg form,
                and raises (rather than silently degrading to SQLite) if
                DATABASE_URL is missing — Render's disk is ephemeral, so a
                SQLite file would be wiped on every redeploy.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        if os.getenv("RENDER"):  # Render sets this on every service
            raise RuntimeError(
                "DATABASE_URL is required on Render — the disk is ephemeral, "
                "so a SQLite fallback would lose history on every redeploy."
            )
        return "sqlite+aiosqlite:///./agents.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _build_engine_url() -> str:
    """Resolve DATABASE_URL and strip SSL-related query keys before asyncpg
    sees them. Neon (and most managed Postgres providers) append
    `?sslmode=require&channel_binding=require` to the URL; passing those
    through to asyncpg while also setting `ssl=True` in connect_args causes a
    "sslmode value ... can only be specified once" error.
    """
    from sqlalchemy.engine.url import make_url

    url = make_url(_resolve_database_url()).difference_update_query(
        ["sslmode", "channel_binding", "sslcert", "sslkey", "sslrootcert"]
    )
    return url.render_as_string(hide_password=False)


@lru_cache(maxsize=1)
def _get_engine():
    """Build the async SQLAlchemy engine exactly once per worker process.

    Cached so that we don't open a fresh connection pool on every incoming
    message. Uses NullPool so each SQLAlchemySession borrows a connection
    for the duration of one `add_items`/`get_items` call and returns it,
    rather than holding idle connections across requests — important on
    managed Postgres (Neon) where connection counts are capped.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # statement_cache_size=0 / prepared_statement_cache_size=0 are required
    # when the database sits behind PgBouncer-style transaction pooling
    # (Neon's default), which does not support server-side prepared statements.
    return create_async_engine(
        _build_engine_url(),
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "ssl": True,
        },
        poolclass=NullPool,
    )


def _make_session(thread_id: str | None) -> SQLAlchemySession:
    """Build a SQLAlchemySession keyed on a stable per-thread id, reusing the
    shared engine from `_get_engine()`.
    """
    sid = thread_id or cl.user_session.get("id") or "default"
    return SQLAlchemySession(session_id=sid, engine=_get_engine(), create_tables=True)


@cl.data_layer
def get_data_layer():
    """Disable Chainlit's built-in data layer.

    Chainlit's default ChainlitDataLayer auto-initializes when DATABASE_URL
    is set, but its schema expectations don't match the openai-agents SDK
    and it tries to use sslmode query params that asyncpg rejects. Returning
    None here opts us out, while still letting our SQLAlchemySession use
    DATABASE_URL for agent memory (with SSL keys stripped by _build_engine_url).
    """
    return None


@function_tool
def greeting_function(name: str) -> str:
    print("\n [Tool Call detected]\n")
    return f"Hello, {name}!"


greeting_agent = Agent(
    name="Greeting Agent",
    tools=[greeting_function],
    instructions=(
        "You are a helpful assistant and you can use your tools when required"
    ),
)


@cl.on_chat_start
async def start():
    await cl.Message(
        content="Welcome to the Panaversity AI Assistant! How can I help you today?"
    ).send()


@cl.on_message
async def main(message: cl.Message):
    """Process incoming messages and generate responses."""
    # Send an empty message that we will stream into
    msg = cl.Message(content="")
    await msg.send()

    # Build a per-thread session so conversation history persists.
    session = _make_session(message.thread_id)

    try:
        result = Runner.run_streamed(
            starting_agent=greeting_agent,
            input=message.content,
            session=session,
        )

        async for event in result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                await msg.stream_token(event.data.delta)
    except Exception as e:
        await msg.stream_token(f"\n\nError: {e}")
        print(f"Error: {e}")
