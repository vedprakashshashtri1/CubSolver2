import asyncio
import os
import threading
import time

import streamlit as st


st.set_page_config(
    page_title="Cub AI Solver",
    page_icon="🧠",
    layout="centered",
)


REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "GEMINI_MODELS",
]

missing = [
    key
    for key in REQUIRED
    if key not in st.secrets
]

if missing:
    st.error(
        "Missing Streamlit secrets:\n\n"
        + "\n".join(missing)
    )
    st.stop()


# Secrets -> environment variables before bot.py is imported.
for key in REQUIRED:
    os.environ[key] = str(st.secrets[key])

os.environ["MODEL_CONCURRENCY"] = str(
    st.secrets.get("MODEL_CONCURRENCY", 2)
)

os.environ["RENDER_CONCURRENCY"] = str(
    st.secrets.get("RENDER_CONCURRENCY", 2)
)

os.environ["MAX_CONCURRENT_UPDATES"] = str(
    st.secrets.get("MAX_CONCURRENT_UPDATES", 16)
)

os.environ["CHROMIUM_PATH"] = str(
    st.secrets.get(
        "CHROMIUM_PATH",
        "/usr/bin/chromium",
    )
)


@st.cache_resource
def start_bot_once():
    state = {
        "error": None,
        "started": False,
    }

    def runner():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Import only AFTER Streamlit secrets have been copied to env.
            from bot import run_bot

            state["started"] = True
            run_bot()

        except Exception as exc:
            state["error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            state["started"] = False

    thread = threading.Thread(
        target=runner,
        name="CubTelegramBot",
        daemon=True,
    )

    thread.start()
    time.sleep(2)

    return thread, state


thread, bot_state = start_bot_once()


st.title("🧠 Cub")
st.subheader("Multi-Model AI Question Solver")

st.write(
    """
This Streamlit app hosts the Telegram bot in a background thread.

The optimized pipeline is:

**Question image → Gemini HTML/CSS/LaTeX → Chromium screenshot → Telegram image**
"""
)

if bot_state["error"]:
    st.error("Telegram bot failed to start.")
    st.code(bot_state["error"])

elif thread.is_alive():
    st.success("✅ Telegram bot is running")

else:
    st.warning("⚠️ Telegram bot thread is not running")


st.divider()

st.subheader("Configured Gemini model pool")

models = [
    item.strip()
    for item in os.environ["GEMINI_MODELS"].split(",")
    if item.strip()
]

for index, model in enumerate(models, start=1):
    st.code(f"{index}. {model}", language=None)

st.write(
    f"**Per-model concurrency:** {os.environ['MODEL_CONCURRENCY']}"
)

st.write(
    f"**HTML render concurrency:** {os.environ['RENDER_CONCURRENCY']}"
)

st.write(
    f"**Concurrent Telegram updates:** {os.environ['MAX_CONCURRENT_UPDATES']}"
)


st.divider()

st.info(
    "Each user request is sent to one Gemini model. "
    "Different simultaneous users are distributed across the model pool. "
    "If a model is rate-limited or unavailable, Cub automatically tries another."
)

st.warning(
    "Streamlit Community Cloud may sleep when inactive. "
    "This setup is optimized for Streamlit, but a true 24/7 Telegram bot "
    "is better on an always-on host."
)
