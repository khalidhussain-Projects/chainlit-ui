from dotenv import load_dotenv
import chainlit as cl
from agents import Agent, Runner, function_tool
from openai.types.responses import ResponseTextDeltaEvent
import os
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

# Load the environment variables from the .env file
load_dotenv()


def _resolve_database_url() -> str:
    """Pick the right DB URL for the current environment.

    Local dev:  falls back to sqlite+aiosqlite if DATABASE_URL is unset.
    Render:     normalizes postgres:// and postgresql:// to the asyncpg form.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        return "sqlite+aiosqlite:///./agents.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


DATABASE_URL = _resolve_database_url()


def _make_session(thread_id: str | None) -> SQLAlchemySession:
    """Build a SQLAlchemySession keyed on a stable per-thread id."""
    sid = thread_id or cl.user_session.get("id") or "default"
    return SQLAlchemySession(session_id=sid, url=DATABASE_URL, create_tables=True)


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
        result = await Runner.run_streamed(
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
