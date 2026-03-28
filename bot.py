"""
Poll Bot — Telegram poll bot styled like @vote bot.
Inline buttons, voter names, percentages, images.
HTTP API on localhost for OpenClaw cron triggers.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poll-bot")

TOKEN = os.environ["POLL_BOT_TOKEN"]
API_PORT = int(os.environ.get("API_PORT", "18790"))
POLLS_FILE = Path(__file__).parent / "polls.json"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Default options with emojis
DEFAULT_OPTIONS = ["🏃 Буду!", "💀 Не смогу", "❓ Под вопросом"]

# Emoji for progress bar
BAR_CHAR = "👍"
MAX_BAR = 8  # max thumbs per line


# ---------------------------------------------------------------------------
# Polls storage
# ---------------------------------------------------------------------------

def load_polls() -> dict:
    if POLLS_FILE.exists():
        try:
            return json.loads(POLLS_FILE.read_text())
        except json.JSONDecodeError:
            log.error("polls.json is corrupted, starting with empty polls")
            return {}
    return {}


def save_polls(polls: dict):
    POLLS_FILE.write_text(json.dumps(polls, ensure_ascii=False, indent=2))


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 человек, 2-4 человека, 5+ человек."""
    if 11 <= n % 100 <= 19:
        return many
    mod10 = n % 10
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many


def voter_display(user_info: dict) -> str:
    """Format voter name — @username if available, otherwise first name."""
    username = user_info.get("username")
    if username:
        return f"@{username}"
    return user_info.get("name", "?")


def build_caption(poll: dict) -> str:
    """Build vote bot–style caption with percentages and voter names."""
    lines = [f"{poll['question']}", "", "📊 Публичный опрос", ""]

    total_votes = sum(len(poll["votes"].get(opt, [])) for opt in poll["options"])

    for opt in poll["options"]:
        voters = poll["votes"].get(opt, [])
        count = len(voters)
        pct = round(count / total_votes * 100) if total_votes > 0 else 0

        # Option header: emoji+name – count (pct%)
        lines.append(f"{opt} – {count} ({pct}%)")

        # Thumbs bar
        if count > 0:
            bar_len = max(1, round(count / max(total_votes, 1) * MAX_BAR))
            lines.append(BAR_CHAR * bar_len)

            # Voter names
            names = [voter_display(v) for v in voters]
            lines.append(", ".join(names))

        lines.append("")

    lines.append(f"👥 {total_votes} {_plural(total_votes, 'человек проголосовал', 'человека проголосовали', 'человек проголосовало')}")

    text = "\n".join(lines)
    # Caption limit is 1024 chars for photos
    if len(text) > 1020:
        text = text[:1017] + "..."
    return text


def build_keyboard(poll_id: str, poll: dict) -> InlineKeyboardMarkup:
    """Build inline keyboard with vote counts on buttons."""
    buttons = []
    for i, opt in enumerate(poll["options"]):
        count = len(poll["votes"].get(opt, []))
        # Short emoji label + count for button
        emoji = opt.split(" ")[0] if " " in opt else opt
        btn_text = f"{emoji} – {count}" if count > 0 else emoji
        buttons.append(
            InlineKeyboardButton(text=btn_text, callback_data=f"vote:{poll_id}:{i}")
        )
    # All buttons in one row
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


# ---------------------------------------------------------------------------
# Callback handler — vote processing
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("vote:"))
async def on_vote(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return

    poll_id = parts[1]
    try:
        option_idx = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка")
        return

    polls = load_polls()
    poll = polls.get(poll_id)
    if not poll:
        await callback.answer("Опрос не найден")
        return

    if option_idx < 0 or option_idx >= len(poll["options"]):
        await callback.answer("Ошибка")
        return

    user_id = str(callback.from_user.id)
    user_name = callback.from_user.first_name or "?"
    user_username = callback.from_user.username
    chosen_option = poll["options"][option_idx]

    # Check if user already voted for this option (toggle off)
    existing_voters = poll["votes"].get(chosen_option, [])
    already_voted = any(v["id"] == user_id for v in existing_voters)

    # Remove previous vote from all options
    for opt in poll["options"]:
        voters = poll["votes"].get(opt, [])
        poll["votes"][opt] = [v for v in voters if v["id"] != user_id]

    if already_voted:
        # Toggle off — just remove, don't re-add
        answer_text = "Голос снят"
    else:
        # Add new vote
        poll["votes"].setdefault(chosen_option, []).append({
            "id": user_id,
            "name": user_name,
            "username": user_username,
        })
        answer_text = f"Голос: {chosen_option}"

    save_polls(polls)

    # Update message — try caption first (photo), fall back to text
    new_caption = build_caption(poll)
    new_keyboard = build_keyboard(poll_id, poll)

    try:
        try:
            await bot.edit_message_caption(
                chat_id=poll["chat_id"],
                message_id=poll["message_id"],
                caption=new_caption,
                reply_markup=new_keyboard,
            )
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "there is no caption" in err or "message can't be edited" in err:
                # Not a photo message — use edit_message_text
                await bot.edit_message_text(
                    chat_id=poll["chat_id"],
                    message_id=poll["message_id"],
                    text=new_caption,
                    reply_markup=new_keyboard,
                )
            elif "message is not modified" in err:
                pass  # Caption didn't change, that's fine
            else:
                raise
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            pass
        else:
            log.error(f"Failed to edit poll message: {e}")
            answer_text = "⚠️ Голос принят, но сообщение не обновилось"
    except Exception as e:
        log.exception(f"Unexpected error editing poll message: {e}")
        answer_text = "⚠️ Голос принят, но сообщение не обновилось"

    await callback.answer(answer_text)


# ---------------------------------------------------------------------------
# HTTP API — poll creation
# ---------------------------------------------------------------------------

async def handle_create_poll(request: web.Request) -> web.Response:
    """POST /poll — create a new image poll.

    Body JSON:
        chat_id: int — target chat
        image_url: str — URL of the image (optional)
        question: str — poll question
        options: list[str] — answer options (default: emoji options)
        custom_emoji: list[dict] — optional custom emoji entities
            [{"offset": 0, "length": 2, "document_id": "5384088040677319401"}]
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    chat_id = data.get("chat_id")
    image_url = data.get("image_url", "")
    question = data.get("question", "Опрос")
    options = data.get("options", DEFAULT_OPTIONS)
    custom_emoji = data.get("custom_emoji", [])

    if not chat_id:
        return web.json_response({"error": "chat_id required"}, status=400)

    poll_id = str(int(asyncio.get_running_loop().time() * 1000))

    poll = {
        "chat_id": int(chat_id),
        "question": question,
        "options": options,
        "votes": {opt: [] for opt in options},
        "message_id": None,
        "has_image": bool(image_url),
    }

    caption = build_caption(poll)
    keyboard = build_keyboard(poll_id, poll)
    
    # Build custom emoji entities if provided
    caption_entities = None
    if custom_emoji:
        caption_entities = [
            MessageEntity(
                type="custom_emoji",
                offset=e["offset"],
                length=e["length"],
                custom_emoji_id=str(e["document_id"])
            )
            for e in custom_emoji
        ]

    try:
        if image_url:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"error": f"image download failed: HTTP {resp.status}"},
                            status=400,
                        )
                    image_data = await resp.read()
            photo = BufferedInputFile(image_data, filename="image.png")
            msg = await bot.send_photo(
                chat_id=int(chat_id),
                photo=photo,
                caption=caption,
                caption_entities=caption_entities,
                reply_markup=keyboard,
            )
        else:
            msg = await bot.send_message(
                chat_id=int(chat_id),
                text=caption,
                entities=caption_entities,
                reply_markup=keyboard,
            )

        poll["message_id"] = msg.message_id

        polls = load_polls()
        polls[poll_id] = poll
        save_polls(polls)

        return web.json_response({"ok": True, "poll_id": poll_id, "message_id": msg.message_id})

    except Exception as e:
        log.exception("Failed to create poll")
        return web.json_response({"error": str(e)}, status=500)


async def handle_results(request: web.Request) -> web.Response:
    """GET /poll/{id}/results"""
    poll_id = request.match_info["id"]
    polls = load_polls()
    poll = polls.get(poll_id)
    if not poll:
        return web.json_response({"error": "not found"}, status=404)

    results = {}
    for opt in poll["options"]:
        results[opt] = [voter_display(v) for v in poll["votes"].get(opt, [])]
    return web.json_response({"poll_id": poll_id, "question": poll["question"], "results": results})


async def handle_latest_results(request: web.Request) -> web.Response:
    """GET /poll/latest/results — results of the most recent poll."""
    polls = load_polls()
    if not polls:
        return web.json_response({"error": "no polls"}, status=404)

    latest_id = max(polls.keys(), key=lambda k: int(k))
    poll = polls[latest_id]

    results = {}
    for opt in poll["options"]:
        results[opt] = [voter_display(v) for v in poll["votes"].get(opt, [])]

    # Count "yes" votes — first option is always the "I'm in" option
    yes_option = poll["options"][0] if poll["options"] else ""
    total_yes = len(poll["votes"].get(yes_option, []))

    return web.json_response({
        "poll_id": latest_id,
        "question": poll["question"],
        "results": results,
        "total_yes": total_yes,
        "court": "целая" if total_yes > 9 else "половина",
    })


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Main — run bot polling + HTTP server concurrently
# ---------------------------------------------------------------------------

async def main():
    # HTTP server
    app = web.Application()
    app.router.add_post("/poll", handle_create_poll)
    app.router.add_get("/poll/latest/results", handle_latest_results)
    app.router.add_get("/poll/{id}/results", handle_results)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", API_PORT)
    await site.start()
    log.info(f"HTTP API listening on 127.0.0.1:{API_PORT}")

    # Bot polling
    log.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
