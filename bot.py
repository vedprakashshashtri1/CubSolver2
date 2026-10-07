import asyncio
import html
import io
import logging
import os
import time

from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from model_pool import GeminiModelPool, parse_model_list
from renderer import HtmlImageRenderer, image_dimensions


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# IMPORTANT:
# Put the exact model IDs available in YOUR AI Studio project here via secrets.
# Different users/projects can have different available models/quotas.
GEMINI_MODELS_RAW = os.getenv(
    "GEMINI_MODELS",
    "gemini-3.1-flash-lite",
)

MODEL_CONCURRENCY = int(os.getenv("MODEL_CONCURRENCY", "2"))
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "2"))
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "16"))
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "").strip()

MAX_IMAGE_SIZE = 20 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}


if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing.")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("Cub")

# Avoid printing Telegram token in request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


MODEL_POOL = None
RENDERER = None
STARTED_AT = time.time()


def configured_models():
    models = parse_model_list(GEMINI_MODELS_RAW)
    if not models:
        raise RuntimeError("GEMINI_MODELS contains no model IDs.")
    return models


def message_has_image(message) -> bool:
    if message.photo:
        return True

    if message.document:
        mime = (message.document.mime_type or "").lower()
        return mime.startswith("image/")

    return False


async def get_image_bytes(message):
    if message.photo:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())

        if len(data) > MAX_IMAGE_SIZE:
            raise ValueError("Image is larger than 20 MB.")

        return data, "image/jpeg"

    if message.document:
        mime_type = (message.document.mime_type or "").lower()

        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise ValueError("Please send JPG, PNG or WEBP.")

        if (
            message.document.file_size
            and message.document.file_size > MAX_IMAGE_SIZE
        ):
            raise ValueError("Image is larger than 20 MB.")

        tg_file = await message.document.get_file()
        data = bytes(await tg_file.download_as_bytearray())

        return data, mime_type

    raise ValueError("No image found.")


async def solve_image(
    update: Update,
    source_message,
    user_instruction: str = "",
):
    message = update.effective_message

    if not message:
        return

    status = await message.reply_text(
        "🔍 <b>Reading question...</b>\n"
        "🧠 Selecting the fastest available Gemini model...",
        parse_mode=ParseMode.HTML,
    )

    try:
        image_bytes, mime_type = await get_image_bytes(source_message)

        await status.edit_text(
            "🧠 <b>Solving step by step...</b>\n"
            "📄 Generating the solution sheet...",
            parse_mode=ParseMode.HTML,
        )

        html_code, used_model = await MODEL_POOL.generate_html(
            image_bytes=image_bytes,
            mime_type=mime_type,
            user_instruction=user_instruction,
        )

        await status.edit_text(
            "✅ <b>Solution generated</b>\n"
            "🖼 Rendering HTML → image...",
            parse_mode=ParseMode.HTML,
        )

        png_bytes = await RENDERER.render_png(html_code)

        try:
            await status.delete()
        except Exception:
            pass

        buffer = io.BytesIO(png_bytes)
        buffer.name = "cub_solution.png"

        width, height = image_dimensions(png_bytes)
        size = len(png_bytes)

        caption = (
            "✅ Step-by-step solution\n"
            f"🤖 {used_model}"
        )

        # Telegram photo constraints can be awkward for very tall pages.
        # Send huge images as documents to preserve full resolution.
        if (
            size > 9 * 1024 * 1024
            or max(width, height) > 9500
        ):
            await message.reply_document(
                document=buffer,
                caption=caption,
            )
        else:
            await message.reply_photo(
                photo=buffer,
                caption=caption,
            )

    except asyncio.TimeoutError:
        await status.edit_text(
            "❌ The request timed out. Please try again."
        )

    except ValueError as exc:
        await status.edit_text(f"❌ {exc}")

    except Exception as exc:
        logger.exception("Solve failed")

        safe = html.escape(str(exc))[:700]

        try:
            await status.edit_text(
                "❌ <b>Failed to solve the question.</b>\n\n"
                f"<code>{safe}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        """
🤖 <b>Cub • Multi-Model Question Solver</b>

Send a clear picture of a question.

Cub will:
1️⃣ Read the image
2️⃣ Route it to an available Gemini model
3️⃣ Solve it step by step
4️⃣ Generate complete HTML + CSS + LaTeX
5️⃣ Render the HTML directly to an image
6️⃣ Send the image back here

You can also reply to an image with:

<code>/solve</code>

or:

<code>/solve explain every calculation</code>

/models - Show the model pool
/ping - Bot status
/help - Help
""".strip(),
        parse_mode=ParseMode.HTML,
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        """
📚 <b>How to use Cub</b>

<b>Fastest way:</b>
Just send a question image.

Or reply to an existing image with:

<code>/solve</code>

Optional instruction:

<code>/solve solve only question 4</code>

<code>/solve use the easiest method</code>

<code>/solve explain every calculation</code>

Cub always asks Gemini to solve step by step and returns a rendered solution image.
""".strip(),
        parse_mode=ParseMode.HTML,
    )


async def models_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    names = MODEL_POOL.model_names() if MODEL_POOL else configured_models()

    lines = [
        "🤖 <b>Gemini Model Pool</b>",
        "",
    ]

    for i, name in enumerate(names, start=1):
        lines.append(f"{i}. <code>{html.escape(name)}</code>")

    lines += [
        "",
        f"Per-model concurrency: <b>{MODEL_CONCURRENCY}</b>",
        f"Render concurrency: <b>{RENDER_CONCURRENCY}</b>",
        "",
        "Requests are distributed across models. "
        "If one is rate-limited/unavailable, Cub tries another.",
    ]

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def ping_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    uptime = int(time.time() - STARTED_AT)
    mins = uptime // 60

    await update.effective_message.reply_text(
        "🏓 <b>Pong!</b>\n\n"
        "✅ Telegram bot: Online\n"
        f"✅ Models: {len(configured_models())}\n"
        f"✅ Renderer: {'Ready' if RENDERER and RENDERER.browser else 'Starting'}\n"
        f"⏱ Uptime: {mins} min",
        parse_mode=ParseMode.HTML,
    )


async def solve_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    replied = message.reply_to_message
    instruction = " ".join(context.args).strip()

    if replied and message_has_image(replied):
        await solve_image(
            update,
            replied,
            instruction,
        )
        return

    await message.reply_text(
        "📸 Reply to a question image with <code>/solve</code>, "
        "or simply send the image directly.",
        parse_mode=ParseMode.HTML,
    )


async def direct_image_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    caption = (message.caption or "").strip()

    # Caption is treated as an optional instruction.
    if caption.lower().startswith("/solve"):
        parts = caption.split(maxsplit=1)
        caption = parts[1] if len(parts) > 1 else ""

    await solve_image(
        update,
        message,
        caption,
    )


async def unknown_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "❓ Unknown command. Use /help."
    )


async def post_init(application: Application):
    global MODEL_POOL
    global RENDERER

    MODEL_POOL = GeminiModelPool(
        api_key=GEMINI_API_KEY,
        model_names=configured_models(),
        per_model_concurrency=MODEL_CONCURRENCY,
        timeout_seconds=120,
    )

    RENDERER = HtmlImageRenderer(
        chromium_path=CHROMIUM_PATH,
        max_concurrency=RENDER_CONCURRENCY,
    )

    await RENDERER.start()

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start Cub"),
            BotCommand("solve", "Solve a replied question image"),
            BotCommand("help", "How to use Cub"),
            BotCommand("models", "Show Gemini model pool"),
            BotCommand("ping", "Check bot status"),
        ]
    )

    logger.info(
        "Cub started with models: %s",
        ", ".join(configured_models()),
    )


async def post_shutdown(application: Application):
    if RENDERER:
        await RENDERER.close()

    if MODEL_POOL:
        await MODEL_POOL.close()


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram update error: %s",
        context.error,
        exc_info=context.error,
    )


def run_bot():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("solve", solve_command))

    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE,
            direct_image_handler,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        )
    )

    app.add_error_handler(error_handler)

    print()
    print("==============================================")
    print(" CUB • MULTI-MODEL HTML → IMAGE SOLVER")
    print("==============================================")
    print(" Models :", ", ".join(configured_models()))
    print(" Model concurrency :", MODEL_CONCURRENCY, "per model")
    print(" Render concurrency:", RENDER_CONCURRENCY)
    print(" Telegram updates  :", MAX_CONCURRENT_UPDATES)
    print("==============================================")
    print()

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        stop_signals=None,
        close_loop=False,
    )


if __name__ == "__main__":
    run_bot()
