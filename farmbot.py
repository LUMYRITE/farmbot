"""Telegram moderation bot for a specific supergroup chat."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from telegram import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.helpers import mention_html
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


DEFAULT_BANNED_WORDS: Final[tuple[str, ...]] = (
    "spam",
    "scam",
    "fraud",
)


@dataclass(slots=True, frozen=True)
class Settings:
    """Configuration for running the Telegram bot."""

    token: str = "8302607798:AAHs5j6gBulehc4bTYs8OWpZmp8tIQm6VKk"
    chat_id: int = -1002122951481
    primary_admin_id: int = 6_959_383_028
    banned_words_file: str = "banned_words.txt"
    faq_file: str = "faq_entries.json"

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from the environment."""

        token = os.getenv("TELEGRAM_TOKEN", cls.token)
        if not token:
            raise RuntimeError(
                "Environment variable TELEGRAM_TOKEN must be defined with the bot token"
            )
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID", cls.chat_id))
        primary_admin_id = int(
            os.getenv("TELEGRAM_PRIMARY_ADMIN_ID", cls.primary_admin_id)
        )
        banned_words_file = os.getenv(
            "TELEGRAM_BANNED_WORDS_FILE", cls.banned_words_file
        )
        faq_file = os.getenv("TELEGRAM_FAQ_FILE", cls.faq_file)
        return cls(
            token=token,
            chat_id=chat_id,
            primary_admin_id=primary_admin_id,
            banned_words_file=banned_words_file,
            faq_file=faq_file,
        )


class BannedWordsManager:
    """Manage the banned words list backed by a file."""

    def __init__(
        self,
        path: str | Path,
        defaults: Iterable[str] = DEFAULT_BANNED_WORDS,
    ) -> None:
        self.path = Path(path)
        self._defaults = {
            self._normalize(word)
            for word in defaults
            if self._normalize(word)
        }
        self._words: set[str] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize(word: str) -> str:
        return word.strip().lower()

    async def initialize(self) -> None:
        """Load the banned words from disk, creating the file if necessary."""

        await asyncio.to_thread(self._ensure_file_exists)
        words = await asyncio.to_thread(self._read_words)
        self._words = words

    def contains(self, text: str) -> bool:
        """Return ``True`` if any banned word is present in ``text``."""

        return any(word in text for word in self._words)

    async def add_word(self, word: str) -> bool:
        """Add ``word`` to the banned list. Returns ``True`` if added."""

        normalized = self._normalize(word)
        if not normalized:
            return False
        async with self._lock:
            if normalized in self._words:
                return False
            self._words.add(normalized)
            await asyncio.to_thread(self._write_words)
        return True

    async def remove_word(self, word: str) -> bool:
        """Remove ``word`` from the banned list. Returns ``True`` if removed."""

        normalized = self._normalize(word)
        if not normalized:
            return False
        async with self._lock:
            if normalized not in self._words:
                return False
            self._words.remove(normalized)
            await asyncio.to_thread(self._write_words)
        return True

    def _ensure_file_exists(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for word in sorted(self._defaults):
                handle.write(f"{word}\n")

    def _read_words(self) -> set[str]:
        words: set[str] = set()
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    normalized = self._normalize(line)
                    if normalized:
                        words.add(normalized)
        return words

    def _write_words(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            for word in sorted(self._words):
                handle.write(f"{word}\n")


@dataclass(slots=True)
class FAQEntry:
    question: str
    answer: str


class FAQManager:
    """Manage frequently asked questions stored in a JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[FAQEntry] = []
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize(text: str) -> str:
        return text.strip().casefold()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._ensure_file_exists)
        entries = await asyncio.to_thread(self._read_entries)
        self._entries = entries

    async def add_entry(self, question: str, answer: str) -> int | None:
        """Add a new FAQ entry.

        Returns the 1-based position of the added entry or ``None`` if the question
        already exists or provided values are empty.
        """

        normalized = self._normalize(question)
        if not normalized or not answer.strip():
            return None
        async with self._lock:
            if any(self._normalize(entry.question) == normalized for entry in self._entries):
                return None
            self._entries.append(FAQEntry(question=question.strip(), answer=answer.strip()))
            await asyncio.to_thread(self._write_entries)
            return len(self._entries)

    async def remove_entry(self, index: int) -> FAQEntry | None:
        """Remove an entry by its 1-based index."""

        async with self._lock:
            if index < 1 or index > len(self._entries):
                return None
            removed = self._entries.pop(index - 1)
            await asyncio.to_thread(self._write_entries)
            return removed

    def list_questions(self) -> list[str]:
        return [entry.question for entry in self._entries]

    def find_answer(self, text: str) -> str | None:
        normalized = self._normalize(text)
        if not normalized:
            return None
        for entry in self._entries:
            if self._normalize(entry.question) == normalized:
                return entry.answer
        return None

    def _ensure_file_exists(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump([], handle, ensure_ascii=False, indent=2)

    def _read_entries(self) -> list[FAQEntry]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            logger.warning("Не удалось загрузить файл ЧАВО, используется пустой список.")
            return []
        entries: list[FAQEntry] = []
        if isinstance(data, list):
            for item in data:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("question"), str)
                    and isinstance(item.get("answer"), str)
                ):
                    entries.append(
                        FAQEntry(question=item["question"].strip(), answer=item["answer"].strip())
                    )
        return entries

    def _write_entries(self) -> None:
        data = [
            {"question": entry.question, "answer": entry.answer}
            for entry in self._entries
        ]
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)


def is_primary_admin(user: User | None, settings: Settings) -> bool:
    """Return ``True`` when ``user`` matches the configured primary admin."""

    return bool(user and user.id == settings.primary_admin_id)


async def delete_message(message: Message) -> None:
    """Try to delete a Telegram message, ignoring failures."""

    try:
        await message.delete()
    except asyncio.CancelledError:
        raise
    except TelegramError:  # pragma: no cover - best effort cleanup
        logger.debug("Unable to delete message %s", message.message_id, exc_info=True)


def mention_list(users: Iterable[User]) -> str:
    """Build a comma-separated list of HTML mentions."""

    mentions = []
    for user in users:
        display_name = user.full_name or user.username or "пользователь"
        mentions.append(mention_html(user.id, display_name))
    return ", ".join(mentions)


async def delete_later(message: Message, delay: float) -> None:
    """Delete ``message`` after ``delay`` seconds."""

    try:
        await asyncio.sleep(delay)
        with suppress(TelegramError):
            await message.delete()
    except asyncio.CancelledError:
        raise


def is_target_chat(chat: Chat | None, target_chat_id: int) -> bool:
    """Check that the update belongs to the configured supergroup chat."""

    if not chat:
        return False
    if chat.type in {ChatType.PRIVATE, ChatType.CHANNEL}:
        return False
    is_target = chat.id == target_chat_id
    if not is_target:
        logger.debug("Ignoring message from chat %s", chat.id)
    return is_target


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.new_chat_members:
        return
    settings: Settings = context.application.settings  # type: ignore[attr-defined]
    if not is_target_chat(message.chat, settings.chat_id):
        return

    await delete_message(message)
    welcome_text = (
        "{mentions}, добро пожаловать в наш уютный уголок Farm Together 2!\n\n"
        "Рады, что ты заглянул к нам 🌿\n"
        "Здесь мы работаем бок о бок, растим урожай, заботимся о зверюшках\n"
        "и создаём самые душевные фермы вместе.\n\n"
        "🌱 делимся советами и вдохновением\n"
        "🚜 помогаем друг другу расти\n"
        "🏡 украшаем мир вокруг себя\n"
        "🤗 и просто приятно проводим время рядом\n\n"
        "📌 Чтобы здесь было тепло и комфортно всем —\n"
        "будет чудесно, если ты сначала заглянешь в правила\n"
        "и пару слов скажешь в чат — мы любим знакомиться с новыми фермерами 💚\n\n"
        "Устраивайся поудобнее, рассказывай о своей ферме,\n"
        "и пусть каждый день приносит тебе хороший урожай 🌻✨"
    ).format(mentions=mention_list(message.new_chat_members))
    sent_message = await context.bot.send_message(
        chat_id=message.chat_id,
        text=welcome_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Правила 📚", url="https://telegra.ph/Pravila-chata-Farm-Together-2-11-04")]]
        ),
    )
    context.application.create_task(delete_later(sent_message, delay=300))


async def on_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.left_chat_member:
        return
    settings: Settings = context.application.settings  # type: ignore[attr-defined]
    if not is_target_chat(message.chat, settings.chat_id):
        return

    await delete_message(message)
    farewell = "{mentions} покинул(а) чат.".format(
        mentions=mention_list([message.left_chat_member])
    )
    await context.bot.send_message(chat_id=message.chat_id, text=farewell)


async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    settings: Settings = context.application.settings  # type: ignore[attr-defined]
    if not is_target_chat(message.chat, settings.chat_id):
        return
    await delete_message(message)


async def handle_banned_word_commands(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    banned_words: BannedWordsManager,
) -> bool:
    """Handle admin commands to manage banned words."""

    text = message.text or ""
    stripped = text.strip()
    lowered = stripped.lower()

    if not stripped:
        return False

    parts = stripped.split(maxsplit=1)
    command, argument = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""

    match command:
        case "запретить":
            if not is_primary_admin(message.from_user, settings):
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Команда доступна только главному администратору.",
                )
                return True
            if not argument:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Укажите слово для добавления в список запрещённых.",
                )
                return True
            added = await banned_words.add_word(argument)
            if added:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"Слово «{argument}» добавлено в список запрещённых.",
                )
            else:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"Слово «{argument}» уже находится в списке запрещённых.",
                )
            return True
        case "разрешить":
            if not is_primary_admin(message.from_user, settings):
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Команда доступна только главному администратору.",
                )
                return True
            if not argument:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Укажите слово для удаления из списка запрещённых.",
                )
                return True
            removed = await banned_words.remove_word(argument)
            if removed:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"Слово «{argument}» удалено из списка запрещённых.",
                )
            else:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"Слова «{argument}» нет в списке запрещённых.",
                )
            return True
        case _:
            return False


async def handle_faq_commands(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    faq: FAQManager,
) -> bool:
    """Handle primary admin commands for managing FAQ entries."""

    text = message.text or ""
    lines = text.splitlines()
    if not lines:
        return False

    command = lines[0].strip().lower()

    match command:
        case "+чаво":
            if not is_primary_admin(message.from_user, settings):
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Команда доступна только главному администратору.",
                )
                return True
            if len(lines) < 3:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=(
                        "Использование: +чаво\nвопрос\nответ."
                        " Убедитесь, что указаны и вопрос, и ответ."
                    ),
                )
                return True
            question = lines[1].strip()
            answer = "\n".join(lines[2:]).strip()
            if not question or not answer:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Вопрос и ответ не должны быть пустыми.",
                )
                return True
            position = await faq.add_entry(question, answer)
            if position is not None:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"Вопрос добавлен под номером {position}.",
                )
            else:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Такой вопрос уже существует в списке.",
                )
            return True
        case "чаво" if len(lines) == 1:
            if not is_primary_admin(message.from_user, settings):
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Команда доступна только главному администратору.",
                )
                return True
            questions = faq.list_questions()
            if not questions:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Список вопросов пуст.",
                )
                return True
            lines_to_send = [
                f"{idx}. {question}" for idx, question in enumerate(questions, start=1)
            ]
            await context.bot.send_message(
                chat_id=message.chat_id, text="\n".join(lines_to_send)
            )
            return True
        case _ if command.startswith("-чаво"):
            if not is_primary_admin(message.from_user, settings):
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Команда доступна только главному администратору.",
                )
                return True
            number_text = ""
            parts = command.split(maxsplit=1)
            if len(parts) > 1:
                number_text = parts[1].strip()
            elif len(lines) > 1:
                number_text = lines[1].strip()
            if not number_text:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Укажите номер вопроса для удаления.",
                )
                return True
            try:
                index = int(number_text)
            except ValueError:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Номер вопроса должен быть числом.",
                )
                return True
            removed = await faq.remove_entry(index)
            if removed is None:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text="Вопрос с таким номером не найден.",
                )
                return True
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"Вопрос «{removed.question}» удалён.",
            )
            return True
        case _:
            return False


async def moderate_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    settings: Settings = context.application.settings  # type: ignore[attr-defined]
    if not is_target_chat(message.chat, settings.chat_id):
        return

    banned_words: BannedWordsManager = context.application.banned_words_manager  # type: ignore[attr-defined]
    faq: FAQManager = context.application.faq_manager  # type: ignore[attr-defined]

    if await handle_banned_word_commands(message, context, settings, banned_words):
        return

    if await handle_faq_commands(message, context, settings, faq):
        return

    answer = faq.find_answer(message.text)
    if answer:
        await context.bot.send_message(chat_id=message.chat_id, text=answer)
        return

    lowered = message.text.lower()
    if is_primary_admin(message.from_user, settings):
        return

    if banned_words.contains(lowered):
        await delete_message(message)
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=(
                "Сообщение пользователя удалено из-за нарушения правил."
                " Пожалуйста, избегайте запрещённых слов."
            ),
        )


async def on_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    settings: Settings = context.application.settings  # type: ignore[attr-defined]
    if not is_target_chat(update.effective_chat, settings.chat_id):
        return

    if not is_primary_admin(update.effective_user, settings):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Команда доступна только главному администратору.",
        )
        return

    await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Бот активен")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling update: %s", update)


def build_application(
    settings: Settings, banned_words: BannedWordsManager, faq: FAQManager
) -> Application:
    application = ApplicationBuilder().token(settings.token).build()
    application.settings = settings  # type: ignore[attr-defined]
    application.banned_words_manager = banned_words  # type: ignore[attr-defined]
    application.faq_manager = faq  # type: ignore[attr-defined]

    application.add_handler(CommandHandler("ping", on_ping))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_member_left))
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.ALL
            & ~filters.StatusUpdate.NEW_CHAT_MEMBERS
            & ~filters.StatusUpdate.LEFT_CHAT_MEMBER,
            on_service_message,
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderate_text))
    application.add_error_handler(error_handler)
    return application


async def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    banned_words = BannedWordsManager(settings.banned_words_file)
    faq = FAQManager(settings.faq_file)
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(banned_words.initialize())
        task_group.create_task(faq.initialize())
    application = build_application(settings, banned_words, faq)

    logger.info("Bot started and monitoring chat %s", settings.chat_id)
    await application.run_polling(stop_signals=None, close_loop=False)


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
