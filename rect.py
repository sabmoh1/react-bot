# -*- coding: utf-8 -*-
"""
Telegram Auto Reaction Bot - Diagnostic / Production Edition
==============================================================

IMPORTANT
---------
1) Bot tokens are read ONLY from Tokens.txt.
2) No Bot token is hard-coded in this Python file.
3) No HTML file is used for token storage.
4) This program uses the official Telegram Bot API through python-telegram-bot.
5) The program reports the real view count contained in the incoming channel-post
   update when Telegram provides it. It does NOT fabricate or inject views.

Reaction model
--------------
One bot token can set up to one normal emoji reaction per message.
Therefore, to place multiple different emoji reactions on one post, use multiple
bot tokens, with one emoji assigned to each token.

Files
-----
rect.py
Tokens.txt
requirements.txt
README.txt
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telegram import Bot, ReactionTypeEmoji, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, ContextTypes, MessageHandler, filters


# ============================================================
# CONFIG
# ============================================================

CHANNEL = "@react_tes"

TOKENS_FILE_NAME = "Tokens.txt"
MAX_TOKENS_PER_EMOJI = 10

# Be conservative with request frequency.
REQUEST_DELAY = 0.40
RETRY_DELAY = 1.25
MAX_RETRIES = 2

# Print exception details. Keep True while diagnosing configuration.
DEBUG = False

# Drop old pending channel posts at startup.
DROP_PENDING_UPDATES = True

REACTIONS: List[Tuple[str, str]] = [
    ("❤", "قلب"),
    ("🔥", "نار"),
    ("😁", "ضحك"),
    ("😍", "حب"),
    ("😢", "حزن"),
    ("👍", "لايك"),
    ("👏", "تصفيق"),
    ("🎉", "احتفال"),
    ("🤩", "متحمس"),
    ("😎", "كول"),
]

KNOWN_EMOJIS = {emoji for emoji, _ in REACTIONS}

BASE_DIR = Path(__file__).resolve().parent
TOKENS_FILE = BASE_DIR / TOKENS_FILE_NAME


# ============================================================
# LOGGING
# ============================================================

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else logging.WARNING,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # Silence repetitive HTTP polling output.
    for name in (
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
        "telegram.bot",
    ):
        logging.getLogger(name).setLevel(
            logging.DEBUG if DEBUG else logging.ERROR
        )


# ============================================================
# DATA
# ============================================================

@dataclass
class TokenEntry:
    emoji: str
    label: str
    token: str
    username: str = ""
    bot_id: int = 0
    enabled: bool = True
    status: str = "unknown"
    admin: Optional[bool] = None
    available_emojis: Optional[set[str]] = None
    max_reaction_count: Optional[int] = None


@dataclass
class AttemptOutcome:
    ok: bool
    reason: str = ""


# ============================================================
# TOKENS.TXT
# ============================================================

SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")


def load_tokens() -> Dict[str, List[str]]:
    """Read and parse Tokens.txt only."""
    if not TOKENS_FILE.exists():
        raise FileNotFoundError(
            f"Tokens.txt غير موجود بجانب rect.py: {TOKENS_FILE}"
        )

    sections: Dict[str, List[str]] = {
        emoji: [] for emoji, _ in REACTIONS
    }

    current_emoji: Optional[str] = None

    text = TOKENS_FILE.read_text(encoding="utf-8-sig")

    for raw in text.splitlines():
        line = raw.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        match = SECTION_RE.match(line)
        if match:
            emoji = match.group(1).strip()

            if emoji in KNOWN_EMOJIS:
                current_emoji = emoji
            else:
                current_emoji = None
                print(f"⚠️ قسم غير معروف تم تجاهله: [{emoji}]")
            continue

        if current_emoji is None:
            print(f"⚠️ سطر خارج قسم Emoji تم تجاهله: {line[:40]}")
            continue

        # Remove accidental ordinary whitespace only.
        token = line.replace(" ", "").replace("\t", "")

        if len(sections[current_emoji]) >= MAX_TOKENS_PER_EMOJI:
            print(
                f"⚠️ [{current_emoji}] وصل إلى الحد "
                f"{MAX_TOKENS_PER_EMOJI}; تم تجاهل الزيادة."
            )
            continue

        sections[current_emoji].append(token)

    return sections


# ============================================================
# UI
# ============================================================

def banner() -> None:
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║          🤖 Telegram Auto Reaction Bot                  ║")
    print("║          Diagnostic / Final Edition                    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()


def summary(tokens: Dict[str, List[str]]) -> None:
    print(f"📂 ملف التوكنات المقروء: {TOKENS_FILE}")
    print(f"📡 القناة: {CHANNEL}")
    print(f"📄 مصدر التوكنز: {TOKENS_FILE_NAME}")
    print()
    print("📊 التوكنات:")

    for emoji, label in REACTIONS:
        count = len(tokens.get(emoji, []))
        icon = "🟢" if count else "⚪"
        print(
            f"   {icon} {emoji:<3} {label:<8} "
            f"{count}/{MAX_TOKENS_PER_EMOJI}"
        )

    print()


def safe_error(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__

    # Keep logs compact.
    text = text.replace("\n", " ")
    if len(text) > 180:
        text = text[:177] + "..."
    return f"{type(exc).__name__}: {text}"


def reaction_name(emoji: str) -> str:
    for known, label in REACTIONS:
        if known == emoji:
            return label
    return emoji


def extract_available_emojis(chat) -> Optional[set[str]]:
    """
    Convert ChatFullInfo.available_reactions into a normal emoji set.
    Return None when Telegram doesn't provide the field.
    """
    available = getattr(chat, "available_reactions", None)

    if available is None:
        return None

    result: set[str] = set()

    for item in available:
        if isinstance(item, ReactionTypeEmoji):
            result.add(item.emoji)
        else:
            # Ignore custom emoji here; this project only uses normal emoji.
            emoji = getattr(item, "emoji", None)
            if emoji:
                result.add(emoji)

    return result


# ============================================================
# TOKEN / CHAT DIAGNOSTICS
# ============================================================

async def validate_token_and_channel(
    entry: TokenEntry,
) -> Tuple[bool, str]:
    """
    Validate token, channel access, bot membership/status, and current
    channel reaction configuration.
    """
    bot = Bot(token=entry.token)

    try:
        me = await bot.get_me()
        entry.username = f"@{me.username}" if me.username else str(me.id)
        entry.bot_id = me.id

        chat = await bot.get_chat(CHANNEL)

        entry.available_emojis = extract_available_emojis(chat)
        entry.max_reaction_count = getattr(chat, "max_reaction_count", None)

        # Try to inspect this bot's own membership/status.
        try:
            member = await bot.get_chat_member(CHANNEL, me.id)
            entry.status = getattr(member, "status", "unknown")

            # Telegram has can_manage_chat in several permission models;
            # presence of administrator status is enough for a useful report.
            entry.admin = entry.status == "administrator" or entry.status == "creator"
        except TelegramError as exc:
            entry.status = f"check-failed ({type(exc).__name__})"
            entry.admin = None

        return True, "OK"

    except InvalidToken:
        return False, "التوكن غير صالح"

    except Forbidden as exc:
        return False, f"الوصول مرفوض: {safe_error(exc)}"

    except BadRequest as exc:
        return False, f"القناة/الوصول: {safe_error(exc)}"

    except (TimedOut, NetworkError) as exc:
        return False, f"شبكة/مهلة: {safe_error(exc)}"

    except TelegramError as exc:
        return False, safe_error(exc)

    except Exception as exc:
        return False, safe_error(exc)

    finally:
        await bot.shutdown()


def print_channel_configuration(entries: List[TokenEntry]) -> None:
    """
    Print the actual reaction configuration seen by Telegram.
    All valid bots normally see the same channel settings.
    """
    reference = next(
        (e for e in entries if e.available_emojis is not None),
        None,
    )

    if reference is None:
        print("⚠️ لم تُرجع Telegram قائمة available_reactions.")
        print()
        return

    print("🔧 إعدادات التفاعلات التي يراها Telegram:")
    limit = reference.max_reaction_count
    print(
        "   📌 Max reactions per post:",
        "غير مُرجع" if limit is None else limit,
    )

    allowed = reference.available_emojis or set()

    # Show only normal emojis used by this project.
    status_parts = []
    for emoji, label in REACTIONS:
        status_parts.append(
            f"{emoji} {'✅' if emoji in allowed else '❌'}"
        )

    print("   📌 Emoji:")
    print("      " + " | ".join(status_parts))

    not_allowed = [
        emoji for emoji, _ in REACTIONS
        if emoji not in allowed
    ]

    if not_allowed:
        print()
        print(
            "⚠️ هذه الإيموجيات غير ظاهرة ضمن available_reactions: "
            + " ".join(not_allowed)
        )
        print(
            "   رفع Max reactions per post وحده لا يضيف Emoji جديدة؛ "
            "قائمة التفاعلات المسموح بها منفصلة."
        )
    else:
        print("   ✅ الإيموجيات العشرة ظاهرة ضمن available_reactions.")

    print()


async def validate_all(
    tokens_by_emoji: Dict[str, List[str]],
) -> List[TokenEntry]:
    entries: List[TokenEntry] = []

    total = sum(len(v) for v in tokens_by_emoji.values())
    print(f"🔎 جاري التحقق من {total} Token...")

    if total == 0:
        return []

    flat: List[TokenEntry] = []
    for emoji, label in REACTIONS:
        for token in tokens_by_emoji.get(emoji, []):
            flat.append(
                TokenEntry(
                    emoji=emoji,
                    label=label,
                    token=token,
                )
            )

    for i, entry in enumerate(flat, 1):
        ok, detail = await validate_token_and_channel(entry)

        if ok:
            entries.append(entry)

            admin_text = (
                "admin"
                if entry.admin is True
                else "member/unknown"
            )

            print(
                f"   🟢 {i}/{total} {entry.emoji} "
                f"{entry.username} [{entry.status} | {admin_text}]"
            )

        else:
            entry.enabled = False
            print(
                f"   🔴 {i}/{total} {entry.emoji} "
                f"فشل: {detail}"
            )

    print()
    print(f"🟢 Tokens صالحة وقابلة للوصول إلى القناة: {len(entries)}/{total}")
    print()

    return entries


# ============================================================
# REACTION ENGINE
# ============================================================

class ReactionEngine:
    def __init__(self, entries: List[TokenEntry]) -> None:
        self.entries = entries
        self.dead_tokens: set[str] = set()
        self.send_lock = asyncio.Lock()
        self.counter = 0

    def listener(self) -> Optional[TokenEntry]:
        for entry in self.entries:
            if entry.enabled and entry.token not in self.dead_tokens:
                return entry
        return None

    async def disable(self, entry: TokenEntry) -> None:
        entry.enabled = False
        self.dead_tokens.add(entry.token)

    async def set_reaction(
        self,
        entry: TokenEntry,
        chat_id: int,
        message_id: int,
    ) -> AttemptOutcome:
        if not entry.enabled or entry.token in self.dead_tokens:
            return AttemptOutcome(False, "disabled")

        # Serialize calls intentionally.
        async with self.send_lock:
            bot = Bot(token=entry.token)

            try:
                for attempt in range(MAX_RETRIES + 1):
                    try:
                        ok = await bot.set_message_reaction(
                            chat_id=chat_id,
                            message_id=message_id,
                            reaction=[
                                ReactionTypeEmoji(
                                    emoji=entry.emoji
                                )
                            ],
                        )
                        if ok:
                            return AttemptOutcome(True, "OK")

                        return AttemptOutcome(False, "API returned False")

                    except RetryAfter as exc:
                        if attempt >= MAX_RETRIES:
                            return AttemptOutcome(
                                False,
                                f"RetryAfter exhausted ({exc.retry_after}s)",
                            )

                        await asyncio.sleep(float(exc.retry_after))

                    except (TimedOut, NetworkError) as exc:
                        if attempt >= MAX_RETRIES:
                            return AttemptOutcome(
                                False,
                                safe_error(exc),
                            )
                        await asyncio.sleep(
                            RETRY_DELAY * (attempt + 1)
                        )

                    except InvalidToken:
                        await self.disable(entry)
                        return AttemptOutcome(False, "InvalidToken")

                    except Forbidden as exc:
                        return AttemptOutcome(
                            False,
                            safe_error(exc),
                        )

                    except BadRequest as exc:
                        # IMPORTANT:
                        # Do not hide the Telegram error. This is the
                        # diagnostic information needed to know whether
                        # the Emoji is unavailable, the bot lacks access,
                        # or Telegram rejected the request for another
                        # reason.
                        return AttemptOutcome(
                            False,
                            safe_error(exc),
                        )

                    except TelegramError as exc:
                        if attempt >= MAX_RETRIES:
                            return AttemptOutcome(
                                False,
                                safe_error(exc),
                            )
                        await asyncio.sleep(
                            RETRY_DELAY * (attempt + 1)
                        )

                    except Exception as exc:
                        return AttemptOutcome(
                            False,
                            safe_error(exc),
                        )

                return AttemptOutcome(False, "unknown")

            finally:
                await bot.shutdown()

    async def process_post(
        self,
        chat_id: int,
        message_id: int,
        initial_views: Optional[int],
    ) -> None:
        self.counter += 1

        print()
        print(
            f"📨 منشور جديد #{self.counter} | message_id={message_id}"
        )

        if initial_views is None:
            print("👁️ المشاهدات عند الاستلام: غير متاحة في هذا التحديث")
        else:
            print(f"👁️ المشاهدات عند الاستلام: {initial_views}")

        active = [
            e for e in self.entries
            if e.enabled and e.token not in self.dead_tokens
        ]

        if not active:
            print("❌ لا توجد Tokens فعالة.")
            return

        grouped: Dict[str, List[TokenEntry]] = defaultdict(list)
        for entry in active:
            grouped[entry.emoji].append(entry)

        # First run every configured token exactly once.
        failures: List[Tuple[TokenEntry, str]] = []
        success_counter = Counter()

        for emoji, _label in REACTIONS:
            for entry in grouped.get(emoji, []):
                result = await self.set_reaction(
                    entry,
                    chat_id,
                    message_id,
                )

                if result.ok:
                    success_counter[emoji] += 1
                else:
                    failures.append((entry, result.reason))

                await asyncio.sleep(REQUEST_DELAY)

        # Compact summary by emoji.
        summary_parts = []
        for emoji, _label in REACTIONS:
            total = len(grouped.get(emoji, []))
            if total == 0:
                continue

            good = success_counter[emoji]
            summary_parts.append(
                f"{emoji} {good}/{total}"
                + (" ⚠️" if good != total else "")
            )

        print("⚙️ " + " | ".join(summary_parts))

        success_total = sum(success_counter.values())
        attempted_total = sum(len(v) for v in grouped.values())

        if success_total == attempted_total:
            print(f"✅ اكتمل: {success_total}/{attempted_total}")
        else:
            print(
                f"⚠️ انتهى: {success_total}/{attempted_total} "
                "نجاح"
            )

            print()
            print("🔍 تفاصيل الإخفاقات:")
            for entry, reason in failures:
                print(
                    f"   ❌ {entry.emoji} {entry.username}: {reason}"
                )

            # Give the operator a useful grouped diagnosis.
            groups = Counter(
                f"{entry.emoji} -> {reason}"
                for entry, reason in failures
            )

            print()
            print("🧩 ملخص أسباب الإخفاق:")
            for reason, count in groups.most_common():
                print(f"   • {count}x {reason}")

        print()


# ============================================================
# GLOBALS
# ============================================================

ENGINE: Optional[ReactionEngine] = None


# ============================================================
# CHANNEL POST HANDLER
# ============================================================

async def channel_post_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if ENGINE is None:
        return

    message = update.channel_post
    if message is None:
        return

    await ENGINE.process_post(
        chat_id=message.chat.id,
        message_id=message.message_id,
        initial_views=getattr(message, "views", None),
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def global_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if DEBUG and context.error:
        print(
            f"⚠️ Application error: "
            f"{type(context.error).__name__}: {context.error}"
        )


# ============================================================
# MAIN APP
# ============================================================

async def run_listener(
    listener_entry: TokenEntry,
    entries: List[TokenEntry],
) -> None:
    global ENGINE
    ENGINE = ReactionEngine(entries)

    print(
        f"🎧 Listener: {listener_entry.username} "
        f"(Bot واحد للمراقبة، وباقي Bots للتفاعل)"
    )
    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🟢 جاهز")
    print(f"📡 أراقب: {CHANNEL}")
    print("👀 في انتظار منشور جديد...")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    app = (
        Application.builder()
        .token(listener_entry.token)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            channel_post_handler,
        )
    )
    app.add_error_handler(global_error_handler)

    await app.initialize()
    await app.start()

    try:
        await app.updater.start_polling(
            allowed_updates=["channel_post"],
            drop_pending_updates=DROP_PENDING_UPDATES,
        )

        while True:
            await asyncio.sleep(3600)

    finally:
        try:
            await app.updater.stop()
        except Exception:
            pass

        try:
            await app.stop()
        except Exception:
            pass

        try:
            await app.shutdown()
        except Exception:
            pass


async def main() -> None:
    configure_logging()
    banner()

    try:
        tokens = load_tokens()
    except Exception as exc:
        print(f"❌ خطأ في قراءة Tokens.txt: {exc}")
        return

    summary(tokens)

    entries = await validate_all(tokens)

    if not entries:
        print("❌ لا يوجد أي Token صالح للوصول إلى القناة.")
        return

    print_channel_configuration(entries)

    listener = ReactionEngine(entries).listener()

    if listener is None:
        print("❌ لم يتم العثور على Listener صالح.")
        return

    await run_listener(listener, entries)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("🛑 تم إيقاف البرنامج بواسطة المستخدم.")
    except Exception as exc:
        print()
        print(f"❌ خطأ رئيسي: {type(exc).__name__}: {exc}")
