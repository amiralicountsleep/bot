import asyncio
import logging
import sqlite3
import json
import zipfile
import uuid
import os
import time
from html import escape
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram import BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
import aiohttp
from urllib.parse import urlparse
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
Message,
CallbackQuery,
ReplyKeyboardMarkup,
KeyboardButton,
InlineKeyboardMarkup,
InlineKeyboardButton,
CopyTextButton,
FSInputFile,
BotCommand,
BotCommandScopeDefault,
BotCommandScopeChat,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from database import (
DB_NAME,
init_db,
save_user,
create_order,
get_order,
update_order_status,
set_order_status_if_current,
save_config,
finalize_order_delivery,
claim_order_for_delivery,
release_order_delivery_claim,
set_dates,
get_user_orders,
get_user_services,
get_all_users,
get_all_orders,
get_pending_orders,
get_payment_pending_orders,
get_active_services,
get_order_count,
get_user_count,
get_order_count_by_status,
get_total_sales,
search_orders,
get_services_for_expiry_check,
mark_reminder_sent,
claim_reminder,
reset_reminder_claim,
mark_expired,
mark_expired_notified,
claim_expired_notification,
reset_expired_notification_claim,
has_scheduled_renewal,
get_next_support_admin,
create_support_ticket,
get_support_ticket,
get_user_open_support_ticket,
support_ticket_is_open,
close_support_ticket,
create_suggestion,
get_suggestion,
suggestion_is_open,
claim_suggestion,
complete_suggestion,
reopen_suggestion,
create_discount_code,
get_discount_code,
update_discount_code,
delete_discount_code,
get_discount_usage_stats,
has_user_used_discount,
get_all_discount_codes,
set_discount_active,
increment_discount_usage,
get_setting,
set_setting,
clear_data_section,
get_admin_dashboard_stats,
get_database_health,
save_receipt,
calculate_discount,
create_service,
get_service,
get_all_services,
get_active_service_catalog,
update_service,
set_service_active,
delete_service,
seed_services,
set_service_category as db_set_service_category,
get_service_category as db_get_service_category,
save_receipt_review as db_save_receipt_review,
add_cart_item,
remove_cart_item,
get_cart_items,
clear_cart,
create_cart_child_orders,
complete_cart_parent_if_ready,
init_admins,
get_admins,
is_admin as db_is_admin,
add_admin as db_add_admin,
remove_admin as db_remove_admin,
update_admin_name as db_update_admin_name,
get_main_admin_id as db_get_main_admin_id,
get_admin_permissions as db_get_admin_permissions,
set_admin_permission as db_set_admin_permission,
has_admin_permission as db_has_admin_permission,
log_admin_action as db_log_admin_action,
get_admin_logs as db_get_admin_logs,
get_referral_settings as db_get_referral_settings,
set_referral_setting as db_set_referral_setting,
register_referral as db_register_referral,
get_referral_info as db_get_referral_info,
reward_referral_invite as db_reward_referral_invite,
reward_referral_purchase as db_reward_referral_purchase,
get_referral_stats as db_get_referral_stats,
create_notification,
notification_delivery_claimed,
init_connected_groups_table,
get_connected_groups,
save_connected_group,
deactivate_connected_group,
)

#=========================================================
# CONFIG
#=========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# ادمین‌ها دیگر از این فایل به‌صورت دستی مدیریت نمی‌شوند.
# منبع اصلی و دائمی لیست ادمین‌ها جدول admins در دیتابیس است.
# فقط در اولین راه‌اندازی یک دیتابیس کاملاً خالی، می‌توان ادمین اولیه را
# از متغیر محیطی INITIAL_ADMIN_IDS به‌صورت comma-separated تعیین کرد.
INITIAL_ADMIN_IDS_RAW = os.getenv("INITIAL_ADMIN_IDS", "").strip()
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()


# شناسه گروه بررسی رسیدها. بعد از اجرای /id در گروه، عدد را اینجا قرار دهید.
# تا وقتی 0 باشد، رسیدها مثل قبل به ADMIN_IDS ارسال می‌شوند.
RECEIPT_GROUP_ID = -1004480269950

# شناسه گروه ثبت کاربران جدید؛ کاملاً جدا از ADMIN_IDS و سیستم ادمین.
# /id را داخل گروه اجرا کنید و عدد نمایش‌داده‌شده را اینجا قرار دهید.
USER_LOG_GROUP_ID = -1004456314683

# شناسه گروه دریافت پیشنهادات کاربران
SUGGESTION_GROUP_ID = -1004309665929

# کیف پول و شارژ حساب
WALLET_DB = os.getenv("WALLET_DATABASE_PATH", str(Path(str(DB_NAME)).with_name("wallet.db")))
MIN_WALLET_TOPUP = 10000
# وضعیت کیف پول در settings دیتابیس نگهداری می‌شود تا با ری‌استارت ربات حفظ شود.
def _wallet_is_enabled():
    value = get_setting("wallet_enabled", "1")
    return str(value).strip().lower() not in {"0", "false", "off", "no"}

def _set_wallet_enabled(enabled):
    set_setting("wallet_enabled", "1" if enabled else "0")

# Runtime cache only. The database is the single source of truth.
# This list is refreshed automatically after add/remove and on every restart.
ADMIN_IDS = []

def _parse_initial_admin_ids():
    """Parse optional bootstrap admin IDs used only when the admins table is empty."""
    ids = []
    for value in INITIAL_ADMIN_IDS_RAW.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            admin_id = int(value)
        except (TypeError, ValueError):
            continue
        if admin_id > 0 and admin_id not in ids:
            ids.append(admin_id)
    return ids

def _load_admins_from_database(seed=False):
    """Load the persistent admin list from SQLite into the runtime cache."""
    global ADMIN_IDS
    if seed:
        # init_admins() seeds only an empty admins table and never re-adds a
        # removed admin, so the database remains authoritative after bootstrap.
        init_admins(_parse_initial_admin_ids())
    rows = get_admins()
    # Mutate in place so any filters that keep a reference to ADMIN_IDS stay
    # synchronized after an admin is added or removed.
    ADMIN_IDS[:] = [int(row["admin_id"]) for row in rows]
    return ADMIN_IDS

def _main_admin_id():
    main_id = db_get_main_admin_id()
    return int(main_id) if main_id is not None else (int(ADMIN_IDS[0]) if ADMIN_IDS else None)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")
if not CARD_NUMBER:
    raise RuntimeError("CARD_NUMBER environment variable is not set.")

#=========================================================
# DEFAULT SERVICES
#=========================================================

DEFAULT_SERVICES = [
{
"service_key": "1gb",
"name": "۱ گیگ - تک کاربره",
"volume": "۱ گیگ",
"price": 10000,
"duration_days": 30,
"active": False,
"category": "other",
},
{
"service_key": "2gb",
"name": "۲ گیگ - تک کاربره",
"volume": "۲ گیگ",
"price": 20000,
"duration_days": 30,
"active": False,
"category": "other",
},
{
"service_key": "5gb",
"name": "۵ گیگ - تک کاربره",
"volume": "۵ گیگ",
"price": 50000,
"duration_days": 30,
"active": False,
"category": "other",
},
{
"service_key": "10gb",
"name": "۱۰ گیگ - تک کاربره",
"volume": "۱۰ گیگ",
"price": 100000,
"duration_days": 30,
"active": True,
"category": "other",
},
{
"service_key": "20gb",
"name": "۲۰ گیگ - 🌐 IP ثابت - تک کاربره",
"volume": "۲۰ گیگ",
"price": 195000,
"duration_days": 30,
"active": True,
"category": "fixed_ip",
},
{
"service_key": "30gb_multi",
"name": "🌍 ۳۰ گیگ - Multi-Location - تک کاربره",
"volume": "۳۰ گیگ",
"price": 190000,
"duration_days": 30,
"active": True,
"category": "multi_location",
},
{
"service_key": "50gb",
"name": "۵۰ گیگ - 🌐 IP ثابت - تک کاربره",
"volume": "۵۰ گیگ",
"price": 450000,
"duration_days": 30,
"active": True,
"category": "fixed_ip",
},
{
"service_key": "unlimited",
"name": "♾️ نامحدود - تک کاربره",
"volume": "نامحدود",
"price": 195000,
"duration_days": 30,
"active": True,
"category": "unlimited",
},
]

#=========================================================
# LOGGING
#=========================================================

logging.basicConfig(
level=logging.INFO,
format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

#=========================================================
# BOT / DP
#=========================================================

# Automatic HTTP/SOCKS proxy rotation for Telegram Bot API.
# The source must contain one proxy per line, preferably as:
#   http://IP:PORT
#   socks5://IP:PORT
# or simply IP:PORT (treated as HTTP).
PROXY_SOURCE_URL = os.getenv(
    "PROXY_SOURCE_URL",
    "https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/all.txt",
).strip()
PROXY_REFRESH_SECONDS = int(os.getenv("PROXY_REFRESH_SECONDS", "900"))
PROXY_TEST_LIMIT = int(os.getenv("PROXY_TEST_LIMIT", "40"))
PROXY_TEST_TIMEOUT = float(os.getenv("PROXY_TEST_TIMEOUT", "4"))
PROXY_TEST_CONCURRENCY = int(os.getenv("PROXY_TEST_CONCURRENCY", "10"))
NETWORK_WATCHDOG_SECONDS = float(os.getenv("NETWORK_WATCHDOG_SECONDS", "4"))
NETWORK_FAILURES_BEFORE_RECONNECT = int(os.getenv("NETWORK_FAILURES_BEFORE_RECONNECT", "1"))
RAILWAY_MODE = os.getenv("RAILWAY_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}

_active_proxy_url = None
_active_connection_mode = None  # "vpn" or "proxy"


def _normalize_proxy_line(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        line = f"http://{line}"
    parsed = urlparse(line)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks4a", "socks5"}:
        return None
    if not parsed.hostname or not parsed.port:
        return None
    return line


async def _fetch_proxy_list() -> list[str]:
    """Download and normalize a public HTTP/SOCKS proxy list."""
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(PROXY_SOURCE_URL) as response:
                response.raise_for_status()
                text = await response.text()
    except Exception as exc:
        logger.warning("Could not download proxy list: %s", exc)
        return []

    result = []
    seen = set()
    for line in text.splitlines():
        proxy = _normalize_proxy_line(line)
        if proxy and proxy not in seen:
            seen.add(proxy)
            result.append(proxy)
    return result


async def _test_proxy(proxy_url: str):
    """Return Telegram getMe latency in milliseconds, or None on failure."""
    test_bot = None
    try:
        session = AiohttpSession(proxy=proxy_url)
        test_bot = Bot(BOT_TOKEN, session=session)
        started = time.monotonic()
        await asyncio.wait_for(test_bot.get_me(), timeout=PROXY_TEST_TIMEOUT)
        return (time.monotonic() - started) * 1000.0
    except Exception as exc:
        logger.debug("Proxy failed %s: %s", proxy_url, exc)
        return None
    finally:
        if test_bot is not None:
            try:
                await test_bot.session.close()
            except Exception:
                pass


async def _find_fast_proxies(proxies: list[str]):
    """Test several proxies concurrently and return the fastest working ones."""
    candidates = proxies[:max(1, PROXY_TEST_LIMIT)]
    sem = asyncio.Semaphore(max(1, PROXY_TEST_CONCURRENCY))

    async def check(proxy_url):
        async with sem:
            latency = await _test_proxy(proxy_url)
            if latency is None:
                return None
            return latency, proxy_url

    results = await asyncio.gather(*(check(p) for p in candidates), return_exceptions=True)
    working = []
    for result in results:
        if isinstance(result, tuple) and len(result) == 2:
            working.append(result)
    working.sort(key=lambda item: item[0])
    return working


def _vpn_interface_present():
    """Best-effort detection of Android/Pydroid VPN tunnel interfaces."""
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            interfaces = []
            for line in f:
                if ":" in line:
                    name = line.split(":", 1)[0].strip()
                    interfaces.append(name.lower())
        vpn_names = ("tun", "tun0", "ppp", "ppp0", "wg", "wg0", "ipsec")
        return any(name.startswith(vpn_names) for name in interfaces)
    except Exception:
        return False


async def _test_system_connection():
    """Test Telegram through the Android/Pydroid system network (including VPN)."""
    test_bot = None
    try:
        session = AiohttpSession()
        test_bot = Bot(BOT_TOKEN, session=session)
        started = time.monotonic()
        await asyncio.wait_for(test_bot.get_me(), timeout=PROXY_TEST_TIMEOUT)
        return (time.monotonic() - started) * 1000.0
    except Exception as exc:
        logger.debug("System Telegram connection failed: %s", exc)
        return None
    finally:
        if test_bot is not None:
            try:
                await test_bot.session.close()
            except Exception:
                pass


async def _build_bot_with_working_proxy():
    """Choose Telegram connectivity for Railway or Android/Pydroid safely."""
    global _active_proxy_url, _active_connection_mode

    # Railway has no Android VPN interface. Prefer the platform's normal
    # outbound connection first, then keep the existing proxy fallback.
    if RAILWAY_MODE:
        latency = await _test_system_connection()
        if latency is not None:
            _active_proxy_url = None
            _active_connection_mode = "system"
            session = AiohttpSession()
            bot_instance = Bot(BOT_TOKEN, session=session)
            logger.info(
                "Railway direct Telegram connection is available (latency %.0f ms); using it without proxy.",
                latency,
            )
            return bot_instance, session
        logger.warning("Railway direct Telegram connection failed; falling back to proxy selection.")

    # Existing Android/Pydroid behavior: prefer an active VPN when available.
    vpn_detected = _vpn_interface_present()
    if vpn_detected:
        latency = await _test_system_connection()
        if latency is not None:
            _active_proxy_url = None
            _active_connection_mode = "vpn"
            session = AiohttpSession()
            bot_instance = Bot(BOT_TOKEN, session=session)
            logger.info(
                "VPN detected and Telegram is reachable; using VPN/system connection "
                "instead of proxy (latency %.0f ms).",
                latency,
            )
            return bot_instance, session
        logger.warning(
            "VPN detected, but Telegram is not reachable through the VPN/system path; "
            "falling back to proxy."
        )

    # No usable VPN path: keep the existing low-latency proxy selection.
    if _active_proxy_url:
        latency = await _test_proxy(_active_proxy_url)
        if latency is not None:
            try:
                session = AiohttpSession(proxy=_active_proxy_url)
                bot_instance = Bot(BOT_TOKEN, session=session)
                _active_connection_mode = "proxy"
                logger.info("Reusing Telegram proxy (latency %.0f ms).", latency)
                return bot_instance, session
            except Exception:
                logger.debug("Could not recreate the active proxy session.", exc_info=True)

    proxies = await _fetch_proxy_list()
    if not proxies:
        logger.warning("No proxy list available; waiting for network/proxy access.")
        return None, None

    working = await _find_fast_proxies(proxies)
    if not working:
        logger.warning("No working proxy found in the first %s candidates.", min(len(proxies), max(1, PROXY_TEST_LIMIT)))
        return None, None

    for latency, proxy_url in working[:5]:
        try:
            session = AiohttpSession(proxy=proxy_url)
            bot_instance = Bot(BOT_TOKEN, session=session)
            _active_proxy_url = proxy_url
            _active_connection_mode = "proxy"
            logger.info("Using fastest Telegram proxy: %s (latency %.0f ms).", proxy_url, latency)
            return bot_instance, session
        except Exception:
            logger.debug("Could not create session for proxy: %s", proxy_url, exc_info=True)

    return None, None


async def _network_watchdog():
    """Watch the active Telegram connection without reacting to brief glitches.

    A few consecutive failed checks are required before polling is restarted.
    Once polling is restarted, the main reconnect loop rebuilds the connection;
    the proxy/VPN selector then decides whether to reuse the current path or
    switch to another working path.
    """
    failures = 0
    required_failures = max(1, NETWORK_FAILURES_BEFORE_RECONNECT)
    while True:
        await asyncio.sleep(max(1.0, NETWORK_WATCHDOG_SECONDS))

        # If the bot is currently using a proxy and Android/Pydroid reports
        # that a VPN has become active, test the VPN immediately. If it works,
        # stop polling so main() rebuilds the Bot without a proxy.
        if (not RAILWAY_MODE) and _active_connection_mode == "proxy" and _vpn_interface_present():
            vpn_latency = await _test_system_connection()
            if vpn_latency is not None:
                logger.info(
                    "VPN became available while proxy was active; switching from "
                    "proxy to VPN/system connection (latency %.0f ms).",
                    vpn_latency,
                )
                try:
                    await dp.stop_polling()
                except Exception:
                    logger.debug("Could not stop polling for VPN switch.", exc_info=True)
                return

        try:
            await asyncio.wait_for(bot.get_me(), timeout=5)
            if failures:
                logger.info("Telegram network recovered after %s failed check(s).", failures)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            logger.warning(
                "Telegram network check failed (%s/%s): %s",
                failures,
                required_failures,
                exc,
            )
            # Do not restart polling for one short network hiccup. This is
            # important on Android/Pydroid and during VPN/proxy transitions.
            if failures < required_failures:
                continue

            logger.warning(
                "Telegram connection appears unavailable after %s consecutive checks; "
                "restarting polling so the main reconnect loop can select a working path.",
                failures,
            )
            try:
                await dp.stop_polling()
            except Exception:
                logger.debug("Could not stop polling from watchdog.", exc_info=True)
            return


bot = Bot(BOT_TOKEN)
dp = Dispatcher()

#=========================================================
# STATES
#=========================================================

class PurchaseState(StatesGroup):
    waiting_for_discount = State()
    waiting_for_receipt = State()

class WalletState(StatesGroup):
    waiting_for_amount = State()
    waiting_for_receipt = State()

class SupportState(StatesGroup):
    waiting_for_message = State()


class SuggestionState(StatesGroup):
    waiting_for_message = State()


class SuggestionAdminReplyState(StatesGroup):
    waiting_for_message = State()


class AdminSupportReplyState(StatesGroup):
    waiting_for_message = State()

class AdminConfigState(StatesGroup):
    waiting_for_config = State()

class AdminSearchState(StatesGroup):
    waiting_for_query = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

class DiscountState(StatesGroup):
    waiting_for_code = State()
    waiting_for_type = State()
    waiting_for_value = State()
    waiting_for_max_uses = State()
    waiting_for_expiry = State()

class ServiceAdminState(StatesGroup):
    waiting_for_category = State()
    waiting_for_key = State()
    waiting_for_name = State()
    waiting_for_volume = State()
    waiting_for_price = State()
    waiting_for_duration = State()

class ServiceEditState(StatesGroup):
    waiting_for_value = State()

class ServiceSearchState(StatesGroup):
    waiting_for_query = State()

class ServiceCopyState(StatesGroup):
    waiting_for_key = State()


class AdminManagementState(StatesGroup):
    waiting_for_add_id = State()
    waiting_for_remove_id = State()


#=========================================================
# NAVIGATION / FSM SAFETY
#=========================================================
# Reply-keyboard navigation buttons must always be treated as navigation,
# even when the user is currently inside another FSM state. Otherwise a
# broad state handler (for example SupportState.waiting_for_message) can
# consume the next menu button as if it were the requested text/photo.
_NAVIGATION_BUTTON_TEXTS = {
    # Main user menu
    "🛒 خرید سرویس",
    "🛒 سبد خرید",
    "📡 سرویس‌های من",
    "📋 سفارش‌های من",
    "💰 کیف پول",
    "👤 حساب کاربری",
    "🎧 پشتیبانی",
    "📚 راهنما",
    "💡 پیشنهادات",
    "🎁 دعوت دوستان",
    # Admin menu
    "📦 سفارش‌های جدید",
    "💳 پرداخت‌های در انتظار",
    "👥 کاربران",
    "📡 مدیریت سرویس‌ها",
    "📊 آمار فروش",
    "🔍 جستجوی سفارش",
    "🎟 کدهای تخفیف",
    "📢 پیام همگانی",
    "🔔 اطلاع‌رسانی بروزرسانی",
    "💰 کنترل کیف پول",
    "📡 مانیتور اتصال",
    "💾 Backup / Restore",
    "📊 داشبورد مدیریتی",
    "🎁 تنظیمات Referral",
    "⚙️ تنظیمات ربات",
    "👥 گروه‌های متصل",
    "🎧 پشتیبانی",
    "💡 پیشنهادات",
}


class NavigationStateResetMiddleware(BaseMiddleware):
    """Clear stale FSM input states before processing menu navigation."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            text = str(getattr(event, "text", None) or "").strip()
            if text in _NAVIGATION_BUTTON_TEXTS:
                state = data.get("state")
                if state is not None:
                    try:
                        await state.clear()
                    except Exception:
                        logger.debug("Could not clear FSM state before navigation", exc_info=True)
        return await handler(event, data)


#=========================================================
# RUNTIME DATA
#=========================================================

approval_locks = set()

# Time when this Python process started. Used by the admin connection monitor.
BOT_STARTED_AT = datetime.now()

# Persistent local backup directory. On Railway, point BACKUP_DIR to a mounted
# volume if backups must survive container replacement.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(Path(str(DB_NAME)).parent / "backups")))
BACKUP_KEEP_COUNT = max(1, int(os.getenv("BACKUP_KEEP_COUNT", "7")))
BACKUP_INTERVAL_SECONDS = max(3600, int(os.getenv("BACKUP_INTERVAL_SECONDS", "86400")))

# پیشنهادهایی که یک ادمین با زدن «پاسخ به کاربر» برای پاسخ عادی در گروه انتخاب کرده است.
# این نگاشت عمداً جدا از FSM نگهداری می‌شود تا اگر state در گروه از بین رفت،
# اولین پیام عادی همان ادمین باز هم به‌عنوان پاسخ پیشنهاد تشخیص داده شود.
_pending_suggestion_replies = {}

_pending_admin_configs = {}

# سرویس‌ها داخل دکمه‌ها عرض ثابت دارند؛ سرویس‌های طولانی به‌صورت اسکرول‌شونده نمایش داده می‌شوند.
SERVICE_BUTTON_WIDTH = 40
SERVICE_MARQUEE_INTERVAL = 1.35
SERVICE_MARQUEE_DURATION = 600
SERVICE_MARQUEE_GAP = "       "
SERVICE_BUTTON_PAD = "\u00a0"
_service_marquee_tasks = {}


def _service_button_text(service):
    """Build the complete one-line text used inside a service purchase button."""
    volume = str(service["volume"] or "").strip()
    name = str(service["name"] or "").strip()
    category = _get_service_category(service["service_key"])
    service_type = {
        "fixed_ip": "🌐 IP ثابت",
        "multi_location": "🌍 Multi",
        "unlimited": "♾️ نامحدود",
        "other": "📦 سرویس",
        "gaming": "🎮 گیمینگ",
    }.get(category, "📦 سرویس")

    _persian_digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    price_text = format_money(service["price"]).translate(_persian_digits)
    volume_text = volume.translate(_persian_digits)

    if category == "unlimited":
        return f"📦 {name} | 📊 {volume_text} | 💰 {price_text}"
    return f"📦 {name} | 📊 {volume_text} | {service_type} | 💰 {price_text}"


def _service_marquee_frame(text, offset=0):
    """Return a fixed-width frame. Long text loops continuously; short text is padded."""
    text = " ".join(str(text or "").split())
    width = max(20, int(SERVICE_BUTTON_WIDTH))
    if len(text) <= width:
        return text + (SERVICE_BUTTON_PAD * (width - len(text)))

    gap = SERVICE_MARQUEE_GAP
    tape = text + gap + text
    cycle = len(text) + len(gap)
    start = int(offset) % cycle
    frame = tape[start:start + width]
    if len(frame) < width:
        frame += tape[:width - len(frame)]
    return frame


def _service_purchase_keyboard(services, offset=0):
    rows = []
    for service in services:
        full_text = _service_button_text(service)
        rows.append([
            InlineKeyboardButton(
                text=_service_marquee_frame(full_text, offset),
                callback_data=f"buy:{service['service_key']}"
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="🔙 دسته‌بندی سرویس‌ها",
            callback_data="buy_categories"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _stop_service_marquee(chat_id, message_id):
    key = (int(chat_id), int(message_id))
    task = _service_marquee_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _run_service_marquee(chat_id, message_id, services):
    """Animate long service-button labels by editing only the inline keyboard."""
    key = (int(chat_id), int(message_id))
    started = time.monotonic()
    offset = 0
    try:
        # مکث کوتاه در ابتدای نمایش تا حرکت ناگهانی نباشد.
        await asyncio.sleep(2.5)
        while time.monotonic() - started < SERVICE_MARQUEE_DURATION:
            await asyncio.sleep(max(0.8, float(SERVICE_MARQUEE_INTERVAL)))
            offset += 1
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=_service_purchase_keyboard(services, offset),
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.debug("Stopping service marquee for %s/%s: %s", chat_id, message_id, exc)
                    break
            except (TelegramForbiddenError, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.debug("Service marquee update failed for %s/%s: %s", chat_id, message_id, exc)
    finally:
        current = _service_marquee_tasks.get(key)
        if current is asyncio.current_task():
            _service_marquee_tasks.pop(key, None)


def _start_service_marquee(message, services):
    """Start one bounded animation task for a service-list message."""
    if not any(len(" ".join(_service_button_text(s).split())) > SERVICE_BUTTON_WIDTH for s in services):
        return
    key = (int(message.chat.id), int(message.message_id))
    old = _service_marquee_tasks.pop(key, None)
    if old and not old.done():
        old.cancel()
    _service_marquee_tasks[key] = asyncio.create_task(
        _run_service_marquee(message.chat.id, message.message_id, services)
    )

#=========================================================
# KEYBOARDS
#=========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🛒 خرید سرویس"),
                KeyboardButton(text="🛒 سبد خرید"),
            ],
            [
                KeyboardButton(text="📡 سرویس‌های من"),
            ],
            [
                KeyboardButton(text="📋 سفارش‌های من"),
                KeyboardButton(text="💰 کیف پول"),
            ],
            [
                KeyboardButton(text="👤 حساب کاربری"),
                KeyboardButton(text="🎧 پشتیبانی"),
            ],
            [
                KeyboardButton(text="📚 راهنما"),
            ],
            [
                KeyboardButton(text="💡 پیشنهادات"),
                KeyboardButton(text="🎁 دعوت دوستان"),
            ],
        ],
        resize_keyboard=True
    )

def _bot_is_enabled():
    value = get_setting("bot_enabled", "1")
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


def _set_bot_enabled(enabled):
    set_setting("bot_enabled", "1" if enabled else "0")


class BotAvailabilityMiddleware(BaseMiddleware):
    """Blocks normal users while the bot is disabled, but keeps the main admin online."""
    async def __call__(self, handler, event, data):
        try:
            user = getattr(event, "from_user", None)
            user_id = int(user.id) if user else None
        except (TypeError, ValueError):
            user_id = None

        if _bot_is_enabled() or (user_id is not None and _is_main_admin(user_id)):
            return await handler(event, data)

        # Let the main admin be the only person able to turn the bot back on.
        if isinstance(event, Message):
            await event.answer("🔴 ربات موقتاً غیرفعال است.\n\nلطفاً کمی بعد دوباره تلاش کنید.")
        elif isinstance(event, CallbackQuery):
            await event.answer("🔴 ربات موقتاً غیرفعال است.", show_alert=True)
        return None


def admin_keyboard(show_user_list=True, is_main_admin=False, admin_id=None):
    # For secondary admins, permission checks must use that admin's own ID.
    uid = _main_admin_id() if is_main_admin else (int(admin_id) if admin_id is not None else None)
    # The main admin always sees the complete panel. Other admins only see
    # sections for which their permission is currently enabled.
    rows=[]
    def add(text, perm):
        if is_main_admin or (uid is not None and _has_perm(uid, perm)):
            return KeyboardButton(text=text)
        return None
    a=add("📦 سفارش‌های جدید","orders"); b=add("💳 پرداخت‌های در انتظار","payments")
    if a or b: rows.append([x for x in (a,b) if x])
    a=add("👥 کاربران","users"); b=add("📡 مدیریت سرویس‌ها","services")
    if a or b: rows.append([x for x in (a,b) if x])
    a=add("📊 آمار فروش","stats"); b=add("🔍 جستجوی سفارش","orders")
    if a or b: rows.append([x for x in (a,b) if x])
    a=add("🎟 کدهای تخفیف","discounts"); b=add("📢 پیام همگانی","broadcast")
    if a or b: rows.append([x for x in (a,b) if x])
    a=add("🔔 اطلاع‌رسانی بروزرسانی","broadcast")
    if a: rows.append([a])
    a=add("👥 گروه‌های متصل","groups"); b=add("👤 مدیریت ادمین‌ها","settings")
    if a or b: rows.append([x for x in (a,b) if x])
    if is_main_admin:
        bot_status_text = "🟢 خاموش کردن ربات" if _bot_is_enabled() else "🔴 روشن کردن ربات"
        rows.append([KeyboardButton(text=bot_status_text), KeyboardButton(text="🧹 پاکسازی اطلاعات")])
        rows.append([KeyboardButton(text="🎁 تنظیمات Referral")])
        rows.append([KeyboardButton(text="💰 کنترل کیف پول")])
        rows.append([KeyboardButton(text="📝 لاگ ادمین‌ها"), KeyboardButton(text="📡 مانیتور اتصال")])
        rows.append([KeyboardButton(text="💾 Backup / Restore")])
    if is_main_admin or (uid is not None and _has_perm(uid,"stats")):
        rows.append([KeyboardButton(text="📊 داشبورد مدیریتی")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _is_main_admin(user_id):
    try:
        return db_is_admin(user_id) and int(user_id) == int(_main_admin_id())
    except (TypeError, ValueError):
        return False


class RateLimitMiddleware(BaseMiddleware):
    """Simple per-user message rate limit to reduce burst spam."""
    def __init__(self, limit=25, window=10):
        super().__init__(); self.limit=limit; self.window=window; self._buckets={}
    async def __call__(self, handler, event, data):
        user=getattr(event,"from_user",None)
        if user is None: return await handler(event,data)
        uid=int(user.id); now=time.monotonic(); bucket=self._buckets.setdefault(uid,[])
        bucket[:]=[t for t in bucket if now-t < self.window]
        if len(bucket) >= self.limit:
            if isinstance(event, Message): await event.answer("🛡️ درخواست‌های شما خیلی سریع ارسال می‌شوند. چند ثانیه صبر کنید.")
            elif isinstance(event, CallbackQuery): await event.answer("🛡️ کمی صبر کنید.",show_alert=True)
            return None
        bucket.append(now)
        return await handler(event,data)

class AdminPermissionMiddleware(BaseMiddleware):
    """Enforce admin permissions both for visible buttons and direct callbacks/messages."""
    def _permission_for_event(self, event):
        text = getattr(event, "text", None) or getattr(event, "data", None) or ""
        if text in {"📦 سفارش‌های جدید"}: return "orders"
        if text in {"💳 پرداخت‌های در انتظار"}: return "payments"
        if text in {"👥 کاربران"}: return None  # User management is available to every admin.
        if text in {"📡 مدیریت سرویس‌ها"}: return "services"
        if text in {"📊 آمار فروش", "📊 داشبورد مدیریتی"}: return "stats"
        if text in {"🔍 جستجوی سفارش"}: return "orders"
        if text in {"🎟 کدهای تخفیف"}: return "discounts"
        if text in {"📢 پیام همگانی", "🔔 اطلاع‌رسانی بروزرسانی"}: return "broadcast"
        if text in {"👥 گروه‌های متصل"}: return "groups"
        if text in {"🎧 پشتیبانی"}: return "support"
        if text in {"💡 پیشنهادات"}: return "suggestions"
        if isinstance(event, CallbackQuery):
            for prefix,perm in (("approve:","payments"),("reject:","payments"),("config:","config"),("service:","services"),("discount_","discounts"),("connected_group:","groups"),("connected_groups:","groups"),("support_","support"),("suggestion_","suggestions"),("broadcast_","broadcast"),("admin_search:","orders")):
                if str(text).startswith(prefix): return perm
        return None
    async def __call__(self, handler, event, data):
        uid=getattr(getattr(event,"from_user",None),"id",None)
        if uid is not None and db_is_admin(uid) and not _is_main_admin(uid):
            perm=self._permission_for_event(event)
            allowed = True
            if perm == "config":
                # Sending a configuration is part of the payment/receipt delivery
                # workflow. A secondary admin may use it when either permission is enabled.
                allowed = (
                    db_has_admin_permission(uid, "receipts")
                    or db_has_admin_permission(uid, "payments")
                )
            elif perm == "payments" and isinstance(event, CallbackQuery) and str(getattr(event, "data", "")).startswith(("approve:", "reject:")):
                # Receipt review is allowed by either the dedicated receipts permission
                # or the broader payments permission.
                allowed = (
                    db_has_admin_permission(uid, "receipts")
                    or db_has_admin_permission(uid, "payments")
                )
            elif perm:
                allowed = db_has_admin_permission(uid,perm)
            if perm and not allowed:
                if isinstance(event, CallbackQuery): await event.answer("❌ دسترسی ارسال کانفیگ برای شما فعال نیست.",show_alert=True)
                elif isinstance(event, Message): await event.answer("❌ این بخش برای شما غیرفعال است.")
                return None
        return await handler(event,data)

# Register the availability middleware only after its class and its
# admin-check dependency have been defined.
class AdminAuditMiddleware(BaseMiddleware):
    """Persist a lightweight audit record for every admin interaction."""
    async def __call__(self, handler, event, data):
        uid = getattr(getattr(event, "from_user", None), "id", None)
        try:
            if uid is not None and db_is_admin(uid):
                if isinstance(event, CallbackQuery):
                    payload = str(event.data or "")
                    if payload.startswith("admin_logs:"):
                        return await handler(event, data)
                    action = "callback:" + (payload.split(":", 1)[0] if payload else "unknown")
                    _audit(uid, action, target=payload[:120] if payload else None)
                elif isinstance(event, Message):
                    text = str(getattr(event, "text", None) or "").strip()
                    if text.startswith("/"):
                        action = "command:" + text.split()[0][:50]
                    elif getattr(event, "photo", None):
                        action = "photo"
                    else:
                        action = "message"
                    _audit(uid, action, details=f"length={len(text)}" if text else None)
        except Exception:
            logger.exception("Could not write admin audit entry")
        return await handler(event, data)

# Navigation reset must run before any broad FSM handler.
dp.message.outer_middleware(NavigationStateResetMiddleware())
dp.message.outer_middleware(BotAvailabilityMiddleware())
dp.callback_query.outer_middleware(BotAvailabilityMiddleware())
dp.message.outer_middleware(RateLimitMiddleware())
dp.callback_query.outer_middleware(RateLimitMiddleware())
dp.message.outer_middleware(AdminPermissionMiddleware())
dp.callback_query.outer_middleware(AdminPermissionMiddleware())
dp.message.outer_middleware(AdminAuditMiddleware())
dp.callback_query.outer_middleware(AdminAuditMiddleware())

# Config delivery has a second, early path in addition to the FSM handler.
# The order/config is also kept in _pending_admin_configs when the admin
# presses "ارسال کانفیگ". If another stale FSM state, a restart, or a state
# mismatch prevents AdminConfigState.waiting_for_config from matching, this
# handler still consumes the admin's next text in the receipt group and sends
# the config through the same validated delivery/finalization flow.
@dp.message(F.text)
async def pending_config_early_handler(message: Message, state: FSMContext):
    uid = int(message.from_user.id)
    order_code = _pending_admin_configs.get(uid)
    if not order_code:
        raise SkipHandler

    if RECEIPT_GROUP_ID and int(message.chat.id) != int(RECEIPT_GROUP_ID):
        raise SkipHandler

    if not db_is_admin(uid) or not _has_config_perm(uid):
        _pending_admin_configs.pop(uid, None)
        await state.clear()
        raise SkipHandler

    # Let the canonical handler perform all validation, delivery, DB state
    # transitions, referral rewards, audit logging, and user feedback.
    await config_received_handler(message, state)

# Admin navigation actions that must always win over stale FSM states.
# Without this early handler, an admin who is still inside another FSM state
# can press the log button and have the text consumed by that state's handler.
ADMIN_LOG_PAGE_SIZE = 8
ADMIN_LOG_MAX_ROWS = 500


def _admin_logs_keyboard(page: int, total_pages: int):
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ جدیدتر", callback_data=f"admin_logs:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="قدیمی‌تر ➡️", callback_data=f"admin_logs:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=f"📄 صفحه {page + 1}/{total_pages}", callback_data="admin_logs:noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_admin_logs(target, page: int = 0, edit: bool = False):
    """Render audit logs in small pages so Telegram never rejects the message."""
    try:
        rows = db_get_admin_logs(ADMIN_LOG_MAX_ROWS) or []
    except Exception:
        logger.exception("Could not load admin audit logs")
        text = "❌ <b>خواندن لاگ ادمین‌ها انجام نشد.</b>\n\nخطا در لاگ سیستم ثبت شد. لطفاً دوباره تلاش کنید."
        if edit:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=admin_keyboard(is_main_admin=True))
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=admin_keyboard(is_main_admin=True))
        return

    if not rows:
        text = "📝 <b>لاگ ادمین‌ها</b>\n━━━━━━━━━━━━━━\n\nℹ️ هنوز هیچ فعالیتی در جدول لاگ ثبت نشده است."
        if edit:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=admin_keyboard(is_main_admin=True))
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=admin_keyboard(is_main_admin=True))
        return

    total_pages = max(1, (len(rows) + ADMIN_LOG_PAGE_SIZE - 1) // ADMIN_LOG_PAGE_SIZE)
    page = max(0, min(int(page), total_pages - 1))
    start = page * ADMIN_LOG_PAGE_SIZE
    page_rows = rows[start:start + ADMIN_LOG_PAGE_SIZE]

    lines = [
        "📝 <b>لاگ فعالیت ادمین‌ها</b>",
        "━━━━━━━━━━━━━━",
        f"📌 صفحه <b>{page + 1}</b> از <b>{total_pages}</b> | کل لاگ‌ها: <b>{len(rows)}</b>",
        "",
    ]
    for offset, row in enumerate(page_rows, start + 1):
        admin_id = escape(str(row["admin_id"] or "-"))
        action = escape(str(row["action"] or "-"))
        target_value = escape(str(row["target"] or "-"))
        details = escape(str(row["details"] or "")).strip()
        created_at = escape(str(row["created_at"] or "-"))
        block = [
            f"<b>#{offset}</b> 👤 <code>{admin_id}</code>",
            f"⚙️ عملیات: <code>{action}</code>",
            f"🎯 هدف: <code>{target_value}</code>",
            f"⏰ زمان: <code>{created_at}</code>",
        ]
        if details:
            block.append(f"📝 جزئیات: {details}")
        lines.append("\n".join(block))
        lines.append("──────────────")

    text = "\n".join(lines)
    markup = _admin_logs_keyboard(page, total_pages)
    if edit:
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.message(F.text == "📝 لاگ ادمین‌ها")
async def admin_logs_view(message: Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id):
        await message.answer("❌ فقط ادمین اصلی به لاگ ادمین‌ها دسترسی دارد.")
        return
    await state.clear()
    await _render_admin_logs(message, page=0, edit=False)


@dp.callback_query(F.data.startswith("admin_logs:"))
async def admin_logs_page_callback(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی به لاگ ادمین‌ها دسترسی دارد.", show_alert=True)
        return
    value = str(callback.data).split(":", 1)[1]
    if value == "noop":
        await callback.answer()
        return
    try:
        page = int(value)
    except (TypeError, ValueError):
        await callback.answer("❌ صفحه نامعتبر است.", show_alert=True)
        return
    try:
        await _render_admin_logs(callback, page=page, edit=True)
        await callback.answer()
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await callback.answer()
        else:
            logger.exception("Could not change admin log page")
            await callback.answer("❌ نمایش صفحه انجام نشد.", show_alert=True)
    except Exception:
        logger.exception("Could not change admin log page")
        await callback.answer("❌ نمایش صفحه انجام نشد.", show_alert=True)


# Referral settings is a navigation action and must run before FSM-specific
# admin message handlers. This prevents stale search/broadcast/service states
# from consuming the reply-keyboard button.
@dp.message(F.text == "🎁 تنظیمات Referral")
async def referral_settings_menu(message: Message, state: FSMContext):
    if not db_is_admin(message.from_user.id):
        await message.answer("❌ دسترسی ندارید.")
        return

    await state.clear()
    try:
        text, enabled = _referral_settings_text()
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_referral_settings_keyboard(enabled),
        )
    except Exception:
        logger.exception("Could not open Referral settings menu for admin %s", message.from_user.id)
        await message.answer("❌ باز کردن تنظیمات Referral انجام نشد؛ خطا در لاگ ثبت شد.")

def _admin_keyboard_for(user_id):
    """Build the admin panel for the actual Telegram user.

    Main admins get the full panel; secondary admins get the sections
    allowed by their own permissions. The previous implementation passed
    the main-admin flag as the first positional argument and never passed
    the secondary admin's ID, so a newly added admin received an empty
    keyboard from /admin.
    """
    main = _is_main_admin(user_id)
    if main:
        return admin_keyboard(is_main_admin=True)

    uid = int(user_id)
    rows = []

    def add(text, perm):
        if _has_perm(uid, perm):
            return KeyboardButton(text=text)
        return None

    a = add("📦 سفارش‌های جدید", "orders")
    b = add("💳 پرداخت‌های در انتظار", "payments")
    if a or b:
        rows.append([x for x in (a, b) if x])

    # همه ادمین‌ها می‌توانند بخش کاربران را ببینند؛ این بخش وابسته به permission نیست.
    a = KeyboardButton(text="👥 کاربران")
    b = add("📡 مدیریت سرویس‌ها", "services")
    if a or b:
        rows.append([x for x in (a, b) if x])

    a = add("📊 آمار فروش", "stats")
    b = add("🔍 جستجوی سفارش", "orders")
    if a or b:
        rows.append([x for x in (a, b) if x])

    a = add("🎟 کدهای تخفیف", "discounts")
    b = add("📢 پیام همگانی", "broadcast")
    if a or b:
        rows.append([x for x in (a, b) if x])

    a = add("🔔 اطلاع‌رسانی بروزرسانی", "broadcast")
    if a:
        rows.append([a])

    # مدیریت ادمین‌ها فقط برای ادمین اصلی است و برای ادمین‌های فرعی نمایش داده نمی‌شود.
    a = add("👥 گروه‌های متصل", "groups")
    if a:
        rows.append([a])

    if _has_perm(uid, "stats"):
        rows.append([KeyboardButton(text="📊 داشبورد مدیریتی")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _connected_group_setting_key(chat_id, name):
    return f"connected_group:{int(chat_id)}:{name}"


def _connected_group_activity_enabled(chat_id):
    value = get_setting(_connected_group_setting_key(chat_id, "activity"), "1")
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


def _set_connected_group_activity(chat_id, enabled):
    set_setting(
        _connected_group_setting_key(chat_id, "activity"),
        "1" if enabled else "0",
    )


def _connected_groups_keyboard(groups, show_inactive=False):
    rows = []
    for group in groups:
        title = str(group["title"] or "گروه بدون نام").strip()
        if len(title) > 42:
            title = title[:39] + "..."
        status = "🟢" if int(group["active"]) else "🔴"
        rows.append([
            InlineKeyboardButton(
                text=f"{status} {title}",
                callback_data=f"connected_group:{int(group['chat_id'])}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="🔄 بروزرسانی",
            callback_data="connected_groups:refresh",
        ),
        InlineKeyboardButton(
            text="🗂 غیرفعال‌ها" if not show_inactive else "🟢 فعال‌ها",
            callback_data=(
                "connected_groups:inactive"
                if not show_inactive
                else "connected_groups:active"
            ),
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="📊 آمار کلی گروه‌ها",
            callback_data="connected_groups:stats",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# سازگاری با هر بخشی از فایل که قبلاً این نام را استفاده می‌کند.
def connected_groups_keyboard(groups):
    return _connected_groups_keyboard(groups, show_inactive=False)


def _connected_groups_text(groups, show_inactive=False):
    title = "🗂 <b>گروه‌های غیرفعال</b>" if show_inactive else "👥 <b>گروه‌های متصل به ربات</b>"
    if not groups:
        if show_inactive:
            return (
                f"{title}\n\n"
                "گروه غیرفعالی در سابقه ثبت نشده است."
            )
        return (
            f"{title}\n\n"
            "هنوز هیچ گروهی در فهرست ثبت نشده است.\n\n"
            "💡 ربات را به گروه اضافه کنید؛ گروه به‌صورت خودکار در این بخش ثبت می‌شود."
        )

    active_count = sum(1 for group in groups if int(group["active"]) == 1)
    inactive_count = len(groups) - active_count
    lines = [
        title,
        "━━━━━━━━━━━━━━",
        f"📌 تعداد نمایش داده‌شده: <b>{len(groups)}</b>",
    ]
    if not show_inactive:
        lines.append(f"🟢 فعال: <b>{active_count}</b>")
    else:
        lines.append(f"🔴 غیرفعال: <b>{inactive_count}</b>")
    lines.append("")

    for i, group in enumerate(groups, 1):
        group_title = escape(str(group["title"] or "گروه بدون نام"))
        lines.append(
            f"{i}. {'🟢' if int(group['active']) else '🔴'} <b>{group_title}</b>\n"
            f"   🆔 <code>{int(group['chat_id'])}</code>"
        )
        if group["username"]:
            lines.append(f"   🔗 @{escape(str(group['username']))}")

    return "\n".join(lines)


def _connected_group_stats_text(total_groups):
    active = sum(1 for group in total_groups if int(group["active"]) == 1)
    inactive = len(total_groups) - active
    group_types = {}
    for group in total_groups:
        chat_type = str(group["chat_type"] or "group")
        group_types[chat_type] = group_types.get(chat_type, 0) + 1

    type_text = "\n".join(
        f"• {escape(chat_type)}: <b>{count}</b>"
        for chat_type, count in sorted(group_types.items())
    ) or "• موردی ثبت نشده است"

    return (
        "📊 <b>آمار کلی گروه‌های متصل</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 کل گروه‌های ثبت‌شده: <b>{len(total_groups)}</b>\n"
        f"🟢 فعال: <b>{active}</b>\n"
        f"🔴 غیرفعال: <b>{inactive}</b>\n\n"
        "📚 نوع چت‌ها:\n"
        f"{type_text}"
    )


async def _connected_group_open_url(group):
    """Return a reliable Telegram URL for opening a connected group."""
    username = str(group["username"] or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"

    chat_id = int(group["chat_id"])
    setting_key = _connected_group_setting_key(chat_id, "invite_link")
    cached = str(get_setting(setting_key, "") or "").strip()
    if cached.startswith("https://t.me/"):
        return cached

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=chat_id,
            name="انتقال ادمین به گروه",
        )
        invite_url = str(invite.invite_link).strip()
        if invite_url:
            set_setting(setting_key, invite_url)
            return invite_url
    except Exception as exc:
        logger.warning(
            "Could not create invite link for connected group %s: %s",
            chat_id,
            exc,
        )

    return f"tg://openmessage?chat_id={chat_id}"

async def _connected_group_details_keyboard(group):
    chat_id = int(group["chat_id"])
    active = int(group["active"]) == 1
    activity_enabled = _connected_group_activity_enabled(chat_id)
    open_url = await _connected_group_open_url(group)

    rows = [
        [
            InlineKeyboardButton(
                text="🚀 انتقال به گروه",
                url=open_url,
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 همگام‌سازی اطلاعات",
                callback_data=f"connected_group:sync:{chat_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 آمار گروه",
                callback_data=f"connected_group:stats:{chat_id}",
            ),
            InlineKeyboardButton(
                text="⚙️ تنظیمات",
                callback_data=f"connected_group:settings:{chat_id}",
            ),
        ],
    ]

    if active:
        rows.append([
            InlineKeyboardButton(
                text="🔴 غیرفعال‌سازی",
                callback_data=f"connected_group:deactivate:{chat_id}",
            )
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                text="🟢 فعال‌سازی مجدد",
                callback_data=f"connected_group:activate:{chat_id}",
            )
        ])

    rows.extend([
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به لیست فعال‌ها",
                callback_data="connected_groups:active",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"📡 ثبت فعالیت: {'روشن' if activity_enabled else 'خاموش'}",
                callback_data=f"connected_group:activity:{chat_id}",
            )
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _connected_group_details_text(group):
    title = escape(str(group["title"] or "گروه بدون نام"))
    status = "🟢 فعال" if int(group["active"]) else "🔴 غیرفعال"
    username = f"@{escape(str(group['username']))}" if group["username"] else "ندارد"
    chat_type = escape(str(group["chat_type"] or "group"))
    added_at = escape(str(group["added_at"] or "نامشخص"))
    updated_at = escape(str(group["updated_at"] or "نامشخص"))
    activity = "روشن" if _connected_group_activity_enabled(group["chat_id"]) else "خاموش"

    return (
        "👥 <b>جزئیات گروه</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📌 نام: <b>{title}</b>\n"
        f"🆔 شناسه: <code>{int(group['chat_id'])}</code>\n"
        f"🔗 یوزرنیم: <code>{username}</code>\n"
        f"🧩 نوع چت: <code>{chat_type}</code>\n"
        f"📊 وضعیت: {status}\n"
        f"📡 ثبت فعالیت: <b>{activity}</b>\n"
        f"📅 اولین ثبت: <code>{added_at}</code>\n"
        f"🔄 آخرین بروزرسانی: <code>{updated_at}</code>"
    )


async def _refresh_connected_group_from_telegram(chat_id):
    chat = await bot.get_chat(int(chat_id))
    chat_type = str(getattr(chat, "type", "group") or "group")
    if chat_type not in {"group", "supergroup"}:
        raise ValueError("این چت دیگر یک گروه معتبر نیست.")
    save_connected_group(
        chat.id,
        getattr(chat, "title", None),
        getattr(chat, "username", None),
        chat_type,
    )
    groups = get_connected_groups(active_only=False)
    return next(
        (group for group in groups if int(group["chat_id"]) == int(chat_id)),
        None,
    )


async def _sync_all_connected_groups():
    """Synchronize known/configured groups with Telegram without stopping on one failure."""
    configured_ids = {
        int(chat_id)
        for chat_id in (RECEIPT_GROUP_ID, USER_LOG_GROUP_ID, SUGGESTION_GROUP_ID)
        if chat_id
    }
    known_groups = get_connected_groups(active_only=False)
    known_ids = {int(group["chat_id"]) for group in known_groups}
    chat_ids = sorted(known_ids | configured_ids)

    synced = 0
    deactivated = 0
    failed = 0

    for chat_id in chat_ids:
        try:
            group = await _refresh_connected_group_from_telegram(chat_id)
            if group is not None:
                synced += 1
        except TelegramForbiddenError:
            # A configured/known group that is no longer accessible should not
            # remain visible as an active connection.
            if chat_id in known_ids:
                if deactivate_connected_group(chat_id):
                    deactivated += 1
            else:
                failed += 1
        except TelegramBadRequest as exc:
            logger.warning("Could not refresh connected group %s: %s", chat_id, exc)
            if chat_id in known_ids and "chat not found" in str(exc).lower():
                if deactivate_connected_group(chat_id):
                    deactivated += 1
            else:
                failed += 1
        except Exception as exc:
            logger.warning("Could not refresh connected group %s: %s", chat_id, exc)
            failed += 1

    return synced, deactivated, failed


def _connected_group_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به لیست",
                callback_data="connected_groups:active",
            )
        ]
    ])


@dp.message(F.text == "👥 گروه‌های متصل")
async def connected_groups_handler(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    groups = get_connected_groups(active_only=True)
    await message.answer(
        _connected_groups_text(groups),
        reply_markup=_connected_groups_keyboard(groups),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "connected_groups:refresh")
async def connected_groups_refresh(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    try:
        synced, deactivated, failed = await _sync_all_connected_groups()
        groups = get_connected_groups(active_only=True)
        text = _connected_groups_text(groups)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=_connected_groups_keyboard(groups),
                parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            # Telegram returns "message is not modified" when the refreshed
            # content is identical. This is not a real refresh failure.
            if "message is not modified" not in str(exc).lower():
                raise

        status = f"{len(groups)} گروه فعال"
        if deactivated:
            status += f"، {deactivated} گروه غیرفعال شد"
        if failed:
            status += f"، {failed} مورد قابل همگام‌سازی نبود"
        await callback.answer(f"لیست بروزرسانی شد: {status}")
    except Exception as exc:
        logger.exception("Connected groups refresh failed: %s", exc)
        await callback.answer(
            "بروزرسانی گروه‌ها انجام نشد؛ خطا در لاگ ثبت شد.",
            show_alert=True,
        )


@dp.callback_query(F.data == "connected_groups:active")
async def connected_groups_active(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    groups = get_connected_groups(active_only=True)
    await callback.message.edit_text(
        _connected_groups_text(groups),
        reply_markup=_connected_groups_keyboard(groups, show_inactive=False),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "connected_groups:inactive")
async def connected_groups_inactive(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    groups = [g for g in get_connected_groups(active_only=False) if int(g["active"]) == 0]
    await callback.message.edit_text(
        _connected_groups_text(groups, show_inactive=True),
        reply_markup=_connected_groups_keyboard(groups, show_inactive=True),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "connected_groups:stats")
async def connected_groups_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    groups = get_connected_groups(active_only=False)
    await callback.message.edit_text(
        _connected_group_stats_text(groups),
        reply_markup=_connected_group_back_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^connected_group:-?\d+$"))
async def connected_group_details(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    parts = callback.data.split(":")
    # عملیات‌های زیر هندلرهای اختصاصی خودشان را دارند؛ این هندلر فقط
    # callback ساده connected_group:<chat_id> را نمایش می‌دهد.
    if len(parts) != 2:
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await callback.answer("شناسه گروه نامعتبر است.", show_alert=True)
        return

    groups = get_connected_groups(active_only=False)
    group = next((g for g in groups if int(g["chat_id"]) == chat_id), None)
    if group is None:
        await callback.answer("گروه پیدا نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        _connected_group_details_text(group),
        reply_markup=await _connected_group_details_keyboard(group),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("connected_group:sync:"))
async def connected_group_sync(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    try:
        chat_id = int(callback.data.rsplit(":", 1)[1])
        group = await _refresh_connected_group_from_telegram(chat_id)
        if group is None:
            raise ValueError("گروه پیدا نشد.")
    except TelegramForbiddenError:
        await callback.answer("ربات دیگر به این گروه دسترسی ندارد.", show_alert=True)
        deactivate_connected_group(chat_id)
        return
    except Exception as exc:
        logger.warning("Could not sync connected group %s: %s", callback.data, exc)
        await callback.answer(f"همگام‌سازی انجام نشد: {str(exc)[:120]}", show_alert=True)
        return

    await callback.message.edit_text(
        _connected_group_details_text(group),
        reply_markup=await _connected_group_details_keyboard(group),
        parse_mode="HTML",
    )
    await callback.answer("اطلاعات گروه بروزرسانی شد.")


@dp.callback_query(F.data.startswith("connected_group:deactivate:"))
async def connected_group_deactivate(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    chat_id = int(callback.data.rsplit(":", 1)[1])
    if not deactivate_connected_group(chat_id):
        await callback.answer("گروه پیدا نشد.", show_alert=True)
        return

    groups = get_connected_groups(active_only=False)
    group = next((g for g in groups if int(g["chat_id"]) == chat_id), None)
    await callback.message.edit_text(
        _connected_group_details_text(group),
        reply_markup=await _connected_group_details_keyboard(group),
        parse_mode="HTML",
    )
    await callback.answer("گروه غیرفعال شد.")


@dp.callback_query(F.data.startswith("connected_group:activate:"))
async def connected_group_activate(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    chat_id = int(callback.data.rsplit(":", 1)[1])
    try:
        group = await _refresh_connected_group_from_telegram(chat_id)
    except TelegramForbiddenError:
        await callback.answer(
            "ربات هنوز به گروه دسترسی ندارد؛ ابتدا ربات را به گروه اضافه کنید.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Could not reactivate connected group %s", chat_id)
        await callback.answer(
            "فعال‌سازی انجام نشد؛ دسترسی ربات به گروه را بررسی کنید.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        _connected_group_details_text(group),
        reply_markup=await _connected_group_details_keyboard(group),
        parse_mode="HTML",
    )
    await callback.answer("گروه دوباره فعال شد.")


@dp.callback_query(F.data.startswith("connected_group:activity:"))
async def connected_group_activity_toggle(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    chat_id = int(callback.data.rsplit(":", 1)[1])
    enabled = not _connected_group_activity_enabled(chat_id)
    _set_connected_group_activity(chat_id, enabled)

    groups = get_connected_groups(active_only=False)
    group = next((g for g in groups if int(g["chat_id"]) == chat_id), None)
    if group is None:
        await callback.answer("گروه پیدا نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        _connected_group_details_text(group),
        reply_markup=await _connected_group_details_keyboard(group),
        parse_mode="HTML",
    )
    await callback.answer(
        "ثبت فعالیت روشن شد." if enabled else "ثبت فعالیت خاموش شد."
    )


@dp.callback_query(F.data.startswith("connected_group:settings:"))
async def connected_group_settings(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    chat_id = int(callback.data.rsplit(":", 1)[1])
    groups = get_connected_groups(active_only=False)
    group = next((g for g in groups if int(g["chat_id"]) == chat_id), None)
    if group is None:
        await callback.answer("گروه پیدا نشد.", show_alert=True)
        return

    enabled = _connected_group_activity_enabled(chat_id)
    text = (
        "⚙️ <b>تنظیمات گروه</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 <b>{escape(str(group['title'] or 'گروه بدون نام'))}</b>\n\n"
        f"📡 ثبت فعالیت گروه: <b>{'روشن' if enabled else 'خاموش'}</b>\n\n"
        "وقتی ثبت فعالیت خاموش باشد، پیام‌های گروه باعث بروزرسانی "
        "اطلاعات گروه در فهرست نمی‌شوند؛ ثبت ورود/خروج ربات همچنان فعال می‌ماند."
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📡 خاموش کردن" if enabled else "📡 روشن کردن",
                    callback_data=f"connected_group:activity:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به جزئیات",
                    callback_data=f"connected_group:{chat_id}",
                )
            ],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("connected_group:stats:"))
async def connected_group_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    chat_id = int(callback.data.rsplit(":", 1)[1])
    groups = get_connected_groups(active_only=False)
    group = next((g for g in groups if int(g["chat_id"]) == chat_id), None)
    if group is None:
        await callback.answer("گروه پیدا نشد.", show_alert=True)
        return

    try:
        member_count = await bot.get_chat_member_count(chat_id)
    except Exception:
        member_count = None

    try:
        administrators = await bot.get_chat_administrators(chat_id)
        admin_count = len(administrators)
    except Exception:
        admin_count = None

    member_text = str(member_count) if member_count is not None else "دردسترس نیست"
    admin_text = str(admin_count) if admin_count is not None else "دردسترس نیست"

    text = (
        "📊 <b>آمار گروه</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 نام: <b>{escape(str(group['title'] or 'گروه بدون نام'))}</b>\n"
        f"🆔 شناسه: <code>{chat_id}</code>\n"
        f"👤 اعضا: <b>{member_text}</b>\n"
        f"🛡 مدیران: <b>{admin_text}</b>\n"
        f"📌 وضعیت ثبت: {'🟢 فعال' if int(group['active']) else '🔴 غیرفعال'}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی آمار",
                    callback_data=f"connected_group:stats:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 بازگشت به جزئیات",
                    callback_data=f"connected_group:{chat_id}",
                )
            ],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


def _connected_group_admins_keyboard(chat_id, administrators):
    rows = []
    for admin in administrators[:30]:
        user = admin.user
        name = escape(str(user.full_name or user.username or user.id))
        role = "👑" if getattr(admin, "status", "") == "creator" else "🛡️"
        rows.append([
            InlineKeyboardButton(
                text=f"{role} {name[:42]}",
                callback_data=f"connected_group:{int(chat_id)}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data=f"connected_group:{int(chat_id)}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_management_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن ادمین", callback_data="admins:add"), InlineKeyboardButton(text="➖ حذف ادمین", callback_data="admins:remove")],
        [InlineKeyboardButton(text="📋 لیست ادمین‌ها", callback_data="admins:list")],
        [InlineKeyboardButton(text="🔐 مدیریت دسترسی‌ها", callback_data="admins:permissions")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="admins:cancel")],
    ])


def admin_search_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو جستجو", callback_data="admin_search:cancel")]
        ]
    )


def _service_category_key(category):
    return {
        "♾️ نامحدود": "unlimited",
        "🌐 آی‌پی ثابت": "fixed_ip",
        "🌍 مولتی‌لوکیشن": "multi_location",
        "📦 سایر سرویس‌ها": "other",
        "🎮 گیمینگ": "gaming",
    }.get(category, category)


def _service_category_label(category):
    return {
        "unlimited": "♾️ نامحدود",
        "fixed_ip": "🌐 آی‌پی ثابت",
        "multi_location": "🌍 مولتی‌لوکیشن",
        "other": "📦 سایر سرویس‌ها",
        "gaming": "🎮 گیمینگ",
    }.get(str(category or ""), "📦 سایر سرویس‌ها")


def _init_service_categories():
    """Migrate legacy category data from wallet.db to the main database once."""
    conn = wallet_db()
    try:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='service_categories'").fetchone()
        if not exists:
            return
        rows = conn.execute("SELECT service_key, category FROM service_categories").fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            db_set_service_category(row["service_key"], row["category"])
        except Exception:
            logger.exception("Could not migrate service category %s", row["service_key"])


def _set_service_category(service_key, category):
    return db_set_service_category(service_key, category)


def _get_service_category(service_key):
    return db_get_service_category(service_key)


def _set_service_category_compat(service_key, category):
    """Set a service category, including the newer gaming category.

    Older database.py versions validate categories against the original four
    values. For gaming we update the same services.category column directly,
    so this bot remains compatible with those database versions too.
    """
    category = str(category or "other")
    if category != "gaming":
        return db_set_service_category(service_key, category)

    try:
        return db_set_service_category(service_key, category)
    except (ValueError, sqlite3.IntegrityError):
        try:
            import database as _database_module
            get_connection = getattr(_database_module, "get_connection", None)
            if get_connection is None:
                return False
            conn = get_connection()
            try:
                cur = conn.execute(
                    "UPDATE services SET category=? WHERE service_key=?",
                    (category, str(service_key)),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            logger.exception("Could not set gaming category for %s", service_key)
            return False

def _get_services_by_category(category):
    # دسته‌بندی‌ها در جدول اصلی services ذخیره می‌شوند؛
    # برای نمایش خرید نیازی به دسترسی مجدد به wallet.db نیست.
    services = get_active_service_catalog()
    return [s for s in services if _get_service_category(s["service_key"]) == category]


def service_keyboard():
    categories = [
        ("unlimited", "♾️ نامحدود"),
        ("fixed_ip", "🌐 آی‌پی ثابت"),
        ("multi_location", "🌍 مولتی‌لوکیشن"),
        ("other", "📦 سایر سرویس‌ها"),
        # گیمینگ حتی قبل از اضافه شدن سرویس هم نمایش داده می‌شود.
        ("gaming", "🎮 گیمینگ"),
    ]
    rows = []
    for key, label in categories:
        if key == "gaming" or _get_services_by_category(key):
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"buy_category:{key}"
                )
            ])
    rows.append([InlineKeyboardButton(text="🛒 مشاهده سبد خرید", callback_data="cart:view")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_category_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾️ نامحدود", callback_data="service_add_category:unlimited")],
        [InlineKeyboardButton(text="🌐 آی‌پی ثابت", callback_data="service_add_category:fixed_ip")],
        [InlineKeyboardButton(text="🌍 مولتی‌لوکیشن", callback_data="service_add_category:multi_location")],
        [InlineKeyboardButton(text="📦 سایر سرویس‌ها", callback_data="service_add_category:other")],
        [InlineKeyboardButton(text="🎮 گیمینگ", callback_data="service_add_category:gaming")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="service_add_cancel")],
    ])


def card_payment_keyboard(order_code, amount=None):
    rows=[]
    if amount is not None:
        rows.append([InlineKeyboardButton(text="💰 کپی مبلغ", copy_text=CopyTextButton(text=str(int(amount))))])
        rows.append([InlineKeyboardButton(text="💳 کپی شماره کارت", copy_text=CopyTextButton(text=CARD_NUMBER))])
    rows.append([InlineKeyboardButton(text="🧾 مشاهده فاکتور", callback_data=f"invoice:{order_code}")])
    rows.append([InlineKeyboardButton(text="❌ لغو سفارش", callback_data=f"cancel_order:{order_code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cart_actions_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن به سبد خرید", callback_data="cart:add_menu")],
        [InlineKeyboardButton(text="🗑 حذف از سبد خرید", callback_data="cart:remove_menu")],
        [InlineKeyboardButton(text="💳 پرداخت", callback_data="cart:checkout")],
    ])

def cart_empty_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن به سبد خرید", callback_data="cart:add_menu")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="buy_categories")],
    ])

def cart_remove_keyboard(items):
    rows=[[InlineKeyboardButton(text=f"🗑 {str(s['name'])[:45]}", callback_data=f"cart:remove:{s['service_key']}")] for s in items]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به سبد", callback_data="cart:view")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cart_add_keyboard(services):
    rows=[[InlineKeyboardButton(text=f"➕ {str(s['name'])[:45]}", callback_data=f"cart:add:{s['service_key']}")] for s in services]
    rows.append([InlineKeyboardButton(text="🛒 مشاهده سبد", callback_data="cart:view")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def discount_choice_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ کد تخفیف ندارم",
                    callback_data="discount:none"
                ),
                InlineKeyboardButton(
                    text="🎟 کد تخفیف دارم",
                    callback_data="discount:yes"
                )
            ]
        ]
    )


def discount_continue_without_code_keyboard():
    # این کیبورد باید در تمام حالت‌هایی که کد تخفیف نامعتبر است
    # به کاربر نمایش داده شود تا بتواند بدون کد ادامه دهد.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➡️ ادامه بدون کد تخفیف",
                    callback_data="discount:continue_without",
                )
            ]
        ]
    )


def receipt_keyboard(order_code):
    return InlineKeyboardMarkup(
inline_keyboard=[
[
InlineKeyboardButton(
text="❌ لغو سفارش",
callback_data=f"cancel_order:{order_code}"
)
]
]
)

def admin_payment_keyboard(order_code):
    return InlineKeyboardMarkup(
inline_keyboard=[
[
InlineKeyboardButton(
text="✅ تأیید پرداخت",
callback_data=f"approve:{order_code}"
),
InlineKeyboardButton(
text="❌ رد پرداخت",
callback_data=f"reject:{order_code}"
)
]
]
)

def config_keyboard(order_code):
    return InlineKeyboardMarkup(
inline_keyboard=[
[
InlineKeyboardButton(
text="📡 ارسال کانفیگ",
callback_data=f"config:{order_code}"
)
]
]
)


def cart_config_keyboard(children):
    """Create one config-delivery button for every child order in a cart."""
    rows = []
    for child in children or []:
        order_code = str(child.get("order_code") or "").strip()
        if not order_code:
            continue
        service_name = str(child.get("service_name") or "سرویس").strip()
        volume = str(child.get("volume") or "").strip()
        label = f"📡 ارسال کانفیگ | {service_name}"
        if volume:
            label += f" | {volume}"
        rows.append([
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"config:{order_code}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def renewal_keyboard(order_code):
    return InlineKeyboardMarkup(
inline_keyboard=[
[
InlineKeyboardButton(
text="🔄 تمدید سرویس",
callback_data=f"renew:{order_code}"
)
]
]
)

#=========================================================
# WALLET STORAGE
#=========================================================

def wallet_db():
    conn = sqlite3.connect(WALLET_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_wallet_db():
    conn = wallet_db()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tx_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            description TEXT,
            reference TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallet_topups (
            topup_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            receipt_file_id TEXT,
            created_at TEXT NOT NULL,
            processed_at TEXT
        );
        """)
        conn.commit()
    finally:
        conn.close()

def ensure_wallet(user_id):
    conn = wallet_db()
    try:
        conn.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (int(user_id),))
        conn.commit()
    finally:
        conn.close()

def get_wallet_balance(user_id):
    ensure_wallet(user_id)
    conn = wallet_db()
    try:
        row = conn.execute("SELECT balance FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()
        return int(row["balance"]) if row else 0
    finally:
        conn.close()

def change_wallet_balance(user_id, amount, tx_type, description, reference=None):
    """Credit/debit the wallet; repeated references are safely idempotent."""
    amount = int(amount); user_id = int(user_id)
    conn = wallet_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if reference:
            existing = conn.execute(
                "SELECT balance_after FROM wallet_transactions WHERE user_id=? AND tx_type=? AND reference=? ORDER BY id DESC LIMIT 1",
                (user_id, str(tx_type), str(reference)),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return True, int(existing["balance_after"])
        conn.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (user_id,))
        row=conn.execute("SELECT balance FROM wallets WHERE user_id=?",(user_id,)).fetchone()
        new_balance=(int(row["balance"]) if row else 0)+amount
        conn.execute("UPDATE wallets SET balance=? WHERE user_id=?",(new_balance,user_id))
        conn.execute("INSERT INTO wallet_transactions(user_id,tx_type,amount,balance_after,description,reference,created_at) VALUES(?,?,?,?,?,?,?)",
                     (user_id,tx_type,amount,new_balance,description,reference,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit(); return True,new_balance
    except Exception:
        conn.rollback(); raise
    finally: conn.close()

def get_wallet_transactions(user_id, limit=10):
    conn = wallet_db()
    try:
        return conn.execute(
            "SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit))
        ).fetchall()
    finally:
        conn.close()

def create_wallet_topup(user_id, amount, receipt_file_id):
    topup_id = "WLT-" + uuid.uuid4().hex[:8].upper()
    conn = wallet_db()
    try:
        conn.execute(
            "INSERT INTO wallet_topups(topup_id,user_id,amount,status,receipt_file_id,created_at) VALUES(?,?,?,?,?,?)",
            (topup_id, int(user_id), int(amount), "pending", receipt_file_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        return topup_id
    finally:
        conn.close()

def get_wallet_topup(topup_id):
    conn = wallet_db()
    try:
        return conn.execute("SELECT * FROM wallet_topups WHERE topup_id=?", (topup_id,)).fetchone()
    finally:
        conn.close()

def process_wallet_topup(topup_id, approve=True):
    conn = wallet_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        topup = conn.execute("SELECT * FROM wallet_topups WHERE topup_id=?", (topup_id,)).fetchone()
        if not topup or topup["status"] != "pending":
            conn.rollback()
            return None
        status = "approved" if approve else "rejected"
        conn.execute(
            "UPDATE wallet_topups SET status=?, processed_at=? WHERE topup_id=?",
            (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), topup_id)
        )
        new_balance = None
        if approve:
            conn.execute("INSERT OR IGNORE INTO wallets(user_id,balance) VALUES(?,0)", (int(topup["user_id"]),))
            row = conn.execute("SELECT balance FROM wallets WHERE user_id=?", (int(topup["user_id"]),)).fetchone()
            new_balance = int(row["balance"]) + int(topup["amount"])
            conn.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_balance, int(topup["user_id"])))
            conn.execute(
                "INSERT INTO wallet_transactions(user_id,tx_type,amount,balance_after,description,reference,created_at) VALUES(?,?,?,?,?,?,?)",
                (int(topup["user_id"]), "topup", int(topup["amount"]), new_balance, "شارژ کیف پول", topup_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        conn.commit()
        return {"topup": topup, "balance": new_balance, "status": status}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

#=========================================================
# HELPERS
#=========================================================

def generate_order_code():
    # شماره سفارش فقط عددی است تا برای کاربر و ادمین ساده و خوانا باشد.
    return str(10000000 + (uuid.uuid4().int % 90000000))

def format_money(value):
    return f"{int(value):,}"

def calculate_renewal_dates(source_order, duration_days=30):
    """Calculate a renewal period without shortening an active service."""
    now = datetime.now()
    expiry_text = source_order["expiry_date"]

    source_expiry = now
    if expiry_text:
        try:
            source_expiry = datetime.strptime(
                expiry_text,
                "%Y-%m-%d %H:%M:%S"
            )
        except (TypeError, ValueError):
            source_expiry = now

    start = source_expiry if source_expiry > now else now
    expiry = start + timedelta(days=duration_days)

    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        expiry.strftime("%Y-%m-%d %H:%M:%S")
    )

def get_service_duration(service_key):
    service = get_service(service_key)

    if service:
        return int(service["duration_days"])

    return 30

def get_order_final_price(order):
    if order["final_price"] is not None:
        return int(order["final_price"])

    return int(order["price"])

async def send_to_admins(text, reply_markup=None):
    sent = []

    for admin_id in ADMIN_IDS:
        try:
            message = await bot.send_message(
            admin_id,
            text,
            reply_markup=reply_markup
            )
            sent.append(message)
        except Exception as e:
            logger.warning(
                "Could not send message to admin %s: %s",
                admin_id,
                e
            )

    return sent

#=========================================================
# PERSISTENT USER NOTIFICATIONS
#=========================================================

UPDATE_NOTIFICATION_TEXT = (
    "🔔 <b>به‌روزرسانی جدید ربات</b>\n\n"
    "ربات با موفقیت به‌روزرسانی شد و قابلیت‌های جدیدی به آن اضافه شده است.\n\n"
    "برای نمایش منوی جدید و فعال شدن قابلیت‌های تازه، لطفاً یک بار /start را بزنید.\n\n"
    "ممنون که همراه ما هستید ❤️"
)

async def _send_persistent_notification(notification_id, text=None):
    """Send one persistent campaign to each currently registered user only once."""
    users = get_all_users() or []
    success = 0
    failed = 0
    for user in users:
        user_id = int(user["user_id"])
        try:
            # Claim first so a restart/retry can never send this campaign twice.
            if not notification_delivery_claimed(notification_id, user_id):
                continue
            await bot.send_message(
                user_id,
                text or "",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
            success += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1
            logger.exception("Persistent notification failed for user %s", user_id)
    return len(users), success, failed


async def _create_and_send_notification(kind, text):
    notification_id = create_notification(kind, text)
    return notification_id, await _send_persistent_notification(notification_id, text)

#=========================================================
# TELEGRAM COMMAND MENU
#=========================================================

async def setup_bot_commands():
    """ثبت دستورهایی که با زدن / در تلگرام نمایش داده می‌شوند."""
    user_commands = [
        BotCommand(command="start", description="شروع ربات"),
    ]

    admin_commands = [
        BotCommand(command="start", description="شروع ربات"),
        BotCommand(command="admin", description="ورود به پنل مدیریت"),
        BotCommand(command="addservice", description="افزودن سرویس"),
        BotCommand(command="service_on", description="فعال کردن سرویس"),
        BotCommand(command="service_off", description="غیرفعال کردن سرویس"),
        BotCommand(command="service_price", description="تغییر قیمت سرویس"),
        BotCommand(command="discount", description="ایجاد کد تخفیف"),
        BotCommand(command="discount_off", description="غیرفعال کردن کد تخفیف"),
        BotCommand(command="reply", description="پاسخ به تیکت پشتیبانی"),
        BotCommand(command="close", description="بستن تیکت پشتیبانی"),
        BotCommand(command="id", description="نمایش شناسه چت"),
    ]

    # دستورات عمومی برای همه کاربران
    await bot.set_my_commands(
        user_commands,
        scope=BotCommandScopeDefault()
    )

    # دستورات کامل فقط برای ادمین‌ها
    for admin_id in ADMIN_IDS:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id)
        )

    logger.info("Telegram command menu registered.")


#=========================================================
# START
#=========================================================

@dp.message(Command("id"))
async def chat_id_handler(message: Message):
    # ربات را به گروه اضافه کنید و /id را داخل گروه بفرستید.
    await message.answer(
        f"🆔 شناسه این چت:\n<code>{message.chat.id}</code>",
        parse_mode="HTML"
    )

@dp.my_chat_member()
async def my_chat_member_handler(event):
    """Keep the admin group list synchronized with the bot's membership."""
    chat = event.chat
    if chat.type not in {"group", "supergroup"}:
        return
    status = str(event.new_chat_member.status)
    if status in {"member", "administrator", "creator"}:
        save_connected_group(
            chat.id,
            chat.title,
            getattr(chat, "username", None),
            chat.type,
        )
    elif status in {"left", "kicked"}:
        deactivate_connected_group(chat.id)


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_activity_tracker(message: Message):
    """Register group activity without consuming admin FSM messages."""
    if db_is_admin(message.from_user.id):
        # این handler نباید پیام‌های ادمین را consume کند؛
        # مخصوصاً پاسخ ادمین به پیشنهادات باید به handlerهای بعدی برسد.
        raise SkipHandler
    save_connected_group(
        message.chat.id,
        message.chat.title,
        getattr(message.chat, "username", None),
        message.chat.type,
    )


@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    user_number, is_new_user = save_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )

    # Referral start payload: /start ref_<telegram_id>
    try:
        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        if payload.startswith("ref_") and is_new_user:
            referrer_id = int(payload[4:])
            if db_register_referral(message.from_user.id, referrer_id):
                reward = db_reward_referral_invite(message.from_user.id)
                if reward:
                    ref_id, amount = reward
                    try:
                        ok, balance = change_wallet_balance(
                            ref_id, amount, "referral_invite",
                            "پاداش دعوت موفق", f"ref:{message.from_user.id}",
                        )
                        if not ok:
                            raise RuntimeError("Referral invite wallet credit was not applied")
                        await bot.send_message(ref_id, "🎁 <b>پاداش دعوت</b>\n\n"
                            f"💰 مبلغ پاداش: <b>{format_money(amount)} تومان</b>\n"
                            f"💳 موجودی کیف پول: <b>{format_money(balance)} تومان</b>", parse_mode="HTML")
                    except Exception:
                        logger.exception("Referral invite reward delivery failed for referrer %s", ref_id)
            else:
                # اگر ثبت دعوت قبلاً انجام شده باشد، فقط تراکنش کیف پول را با همان
                # reference به‌صورت idempotent دوباره امتحان می‌کنیم.
                existing_referral = db_get_referral_info(message.from_user.id)
                if existing_referral:
                    retry_referrer_id = int(existing_referral["referrer_id"])
                    retry_amount = int((db_get_referral_settings() or {}).get("invite_reward", 0) or 0)
                    if retry_amount > 0:
                        try:
                            ok, balance = change_wallet_balance(
                                retry_referrer_id, retry_amount, "referral_invite",
                                "پاداش دعوت موفق", f"ref:{message.from_user.id}",
                            )
                            if ok:
                                await bot.send_message(retry_referrer_id, "🎁 <b>پاداش دعوت</b>\n\n"
                                    f"💰 مبلغ پاداش: <b>{format_money(retry_amount)} تومان</b>\n"
                                    f"💳 موجودی کیف پول: <b>{format_money(balance)} تومان</b>", parse_mode="HTML")
                        except Exception:
                            logger.exception("Referral invite reward retry failed for referrer %s", retry_referrer_id)
    except (TypeError, ValueError):
        pass

    # برای کاربر جدید، مشخصات را فقط یک بار در گروه ثبت کاربران ارسال می‌کنیم.
    if is_new_user and USER_LOG_GROUP_ID:
        username_text = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else "ندارد"
        )
        display_name = escape(
            message.from_user.full_name
            or message.from_user.first_name
            or "بدون نام"
        )
        new_user_text = (
            "🆕 <b>کاربر جدید</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"🔢 شماره کاربری: <code>{user_number}</code>\n"
            f"👤 نام: <b>{display_name}</b>\n"
            f"🔹 یوزرنیم: <code>{escape(username_text)}</code>\n"
            f"🆔 آیدی تلگرام: <code>{message.from_user.id}</code>\n"
            "━━━━━━━━━━━━━━"
        )
        try:
            await bot.send_message(
                USER_LOG_GROUP_ID,
                new_user_text,
                parse_mode="HTML"
            )
        except Exception:
            logger.exception(
                "Could not send new-user notification to group %s",
                USER_LOG_GROUP_ID
            )

    # نام نمایشی کاربر را مستقیماً از پروفایل تلگرام می‌گیریم.
    # اگر نام کوچک خالی بود، از نام کاربری و در نهایت یک عبارت عمومی استفاده می‌کنیم.
    user_name = (message.from_user.first_name or message.from_user.username or "دوست عزیز").strip()
    user_name = escape(user_name)

    text = (
        f"🌐 <b>سلام {user_name}، خوش اومدی!</b> 👋\n\n"
        f"🔢 شماره کاربری شما: <code>{user_number}</code>\n\n"
        "اینجا قراره خرید سرویس رو ساده، سریع و بی‌دردسر انجام بدی. ✨\n\n"
        "📦 سرویس‌های متنوع با حجم‌های مختلف\n"
        "👤 سرویس‌های تک‌کاربره\n"
        "⏳ اعتبار استاندارد ۳۰ روزه\n"
        "⚡️ ارسال کانفیگ پس از تأیید پرداخت\n"
        "🎧 پشتیبانی در کنارت هست\n\n"
        "🛒 برای شروع، از منوی پایین «خرید سرویس» رو انتخاب کن.\n\n"
        "💙 خوشحالیم که همراه مایی."
    )

    await message.answer(
    text,
    reply_markup=main_keyboard(),
    parse_mode="HTML"
    )

#=========================================================
# ADMIN
#=========================================================

@dp.message(Command("admin"))
async def admin_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    await message.answer(
    "🛠 <b>پنل مدیریت</b> 👋\n\n"
        "همه ابزارهای مدیریت اینجاست؛ بخش موردنظرت رو از منوی زیر انتخاب کن. ✨",
    reply_markup=_admin_keyboard_for(message.from_user.id),
    parse_mode="HTML"
    )

#=========================================================
# BOT STATUS / DATA CLEANUP (MAIN ADMIN ONLY)
#=========================================================

def _cleanup_keyboard():
    rows = [
        [InlineKeyboardButton(text="👥 کاربران", callback_data="cleanup:users"),
         InlineKeyboardButton(text="📦 سفارش‌ها", callback_data="cleanup:orders")],
        [InlineKeyboardButton(text="🎧 پشتیبانی", callback_data="cleanup:support"),
         InlineKeyboardButton(text="💡 پیشنهادات", callback_data="cleanup:suggestions")],
        [InlineKeyboardButton(text="🎟 کدهای تخفیف", callback_data="cleanup:discounts"),
         InlineKeyboardButton(text="🔔 اعلان‌ها", callback_data="cleanup:notifications")],
        [InlineKeyboardButton(text="🧾 بررسی رسیدها", callback_data="cleanup:receipts"),
         InlineKeyboardButton(text="👥 گروه‌های متصل", callback_data="cleanup:groups")],
        [InlineKeyboardButton(text="📡 سرویس‌ها", callback_data="cleanup:services")],
        [InlineKeyboardButton(text="❌ بستن", callback_data="cleanup:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text.in_({"🟢 خاموش کردن ربات", "🔴 روشن کردن ربات"}))
async def bot_status_toggle(message: Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id):
        return
    await state.clear()
    enabled = _bot_is_enabled()
    _set_bot_enabled(not enabled)
    if enabled:
        await message.answer(
            "🔴 <b>ربات خاموش شد.</b>\n\nکاربران دیگر نمی‌توانند از ربات استفاده کنند؛ خود ربات و Polling همچنان فعال می‌مانند تا بتوانید آن را دوباره روشن کنید.",
            reply_markup=_admin_keyboard_for(message.from_user.id), parse_mode="HTML")
    else:
        await message.answer(
            "🟢 <b>ربات دوباره فعال شد.</b>",
            reply_markup=_admin_keyboard_for(message.from_user.id), parse_mode="HTML")


@dp.message(F.text == "🧹 پاکسازی اطلاعات")
async def cleanup_menu(message: Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🧹 <b>پاکسازی اطلاعات</b>\n\nبخش موردنظر را انتخاب کنید. حذف هر بخش مستقل است و تنظیمات ربات و ادمین اصلی حذف نمی‌شود.",
        reply_markup=_cleanup_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("cleanup:"))
async def cleanup_callback(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action == "close":
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("بسته شد.")
        return

    labels = {
        "users": "کاربران", "orders": "سفارش‌ها", "support": "تیکت‌های پشتیبانی",
        "suggestions": "پیشنهادات", "discounts": "کدهای تخفیف", "notifications": "اعلان‌ها",
        "receipts": "بررسی رسیدها", "groups": "گروه‌های متصل", "services": "سرویس‌ها",
    }
    if action not in labels:
        await callback.answer("❌ گزینه نامعتبر است.", show_alert=True)
        return
    confirm = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ بله، حذف کن", callback_data=f"cleanup_confirm:{action}"),
         InlineKeyboardButton(text="❌ لغو", callback_data="cleanup_back")]
    ])
    await callback.message.edit_text(
        f"⚠️ <b>تأیید پاکسازی</b>\n\nآیا مطمئنی اطلاعات بخش «{labels[action]}» حذف شود؟\n\nاین عملیات قابل بازگشت نیست.",
        reply_markup=confirm, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "cleanup_back")
async def cleanup_back_callback(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🧹 <b>پاکسازی اطلاعات</b>\n\nبخش موردنظر را انتخاب کنید.",
        reply_markup=_cleanup_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("cleanup_confirm:"))
async def cleanup_confirm_callback(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    try:
        deleted = clear_data_section(action)
    except Exception as exc:
        logger.exception("Cleanup failed for section %s", action)
        await callback.answer("❌ پاکسازی انجام نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"✅ پاکسازی «{action}» انجام شد.\n\n🗑 تعداد رکوردهای حذف‌شده: <b>{deleted}</b>",
        reply_markup=_cleanup_keyboard(), parse_mode="HTML")
    await callback.answer("پاکسازی انجام شد.")


#=========================================================
# ADMIN MANAGEMENT
#=========================================================

@dp.message(F.text == "👤 مدیریت ادمین‌ها")
async def admin_management(message: Message, state: FSMContext):
    if not db_is_admin(message.from_user.id):
        return
    await state.clear()
    if int(message.from_user.id) != int(_main_admin_id()):
        await message.answer("🔐 مدیریت ادمین‌ها فقط در اختیار ادمین اصلی است.")
        return
    await message.answer(
        "👤 <b>مدیریت ادمین‌ها</b>\n\nاز گزینه موردنظر استفاده کنید:",
        reply_markup=admin_management_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admins:list")
async def admin_list_callback(callback: CallbackQuery):
    if not db_is_admin(callback.from_user.id) or int(callback.from_user.id) != int(_main_admin_id()):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    rows = get_admins()
    lines = ["👤 <b>لیست ادمین‌ها</b>", ""]
    main_id = _main_admin_id()
    for row in rows:
        name = escape(str(row["admin_name"] or "نام ثبت نشده"))
        role = "⭐ ادمین اصلی" if int(row["admin_id"]) == int(main_id) else "👤 ادمین"
        lines.append(f"{role}\n🆔 <code>{int(row['admin_id'])}</code>\n📝 {name}\n")
    if not rows:
        lines.append("هیچ ادمینی ثبت نشده است.")
    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=admin_management_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admins:add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if not db_is_admin(callback.from_user.id) or int(callback.from_user.id) != int(_main_admin_id()):
        await callback.answer("❌ فقط ادمین اصلی می‌تواند ادمین اضافه کند.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminManagementState.waiting_for_add_id)
    await callback.message.answer("➕ آیدی عددی تلگرام ادمین جدید را ارسال کنید.\n\nمثال: <code>123456789</code>\nبرای لغو /cancel را بفرستید.", parse_mode="HTML")
    await callback.answer()


@dp.message(AdminManagementState.waiting_for_add_id)
async def admin_add_id(message: Message, state: FSMContext):
    if not db_is_admin(message.from_user.id) or int(message.from_user.id) != int(_main_admin_id()):
        await state.clear()
        return
    value = (message.text or "").strip()
    if value == "/cancel":
        await state.clear()
        await message.answer("❌ عملیات لغو شد.", reply_markup=admin_keyboard())
        return
    try:
        admin_id = int(value)
        if admin_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ آیدی نامعتبر است. فقط آیدی عددی تلگرام را ارسال کنید.")
        return
    if db_is_admin(admin_id):
        await state.clear()
        await message.answer("⚠️ این کاربر از قبل ادمین است.", reply_markup=admin_keyboard())
        return
    ok = db_add_admin(admin_id, None, message.from_user.id)
    if not ok:
        await message.answer("❌ افزودن ادمین انجام نشد. دوباره تلاش کنید.")
        return
    _load_admins_from_database()
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="شروع ربات"),
            BotCommand(command="admin", description="ورود به پنل مدیریت"),
            BotCommand(command="addservice", description="افزودن سرویس"),
            BotCommand(command="service_on", description="فعال کردن سرویس"),
            BotCommand(command="service_off", description="غیرفعال کردن سرویس"),
            BotCommand(command="service_price", description="تغییر قیمت سرویس"),
            BotCommand(command="discount", description="ایجاد کد تخفیف"),
            BotCommand(command="discount_off", description="غیرفعال کردن کد تخفیف"),
            BotCommand(command="reply", description="پاسخ به تیکت پشتیبانی"),
            BotCommand(command="close", description="بستن تیکت پشتیبانی"),
            BotCommand(command="id", description="نمایش شناسه چت"),
        ], scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception:
        logger.exception("Could not register commands for new admin %s", admin_id)
    await state.clear()
    await message.answer(f"✅ ادمین با آیدی <code>{admin_id}</code> اضافه شد.", parse_mode="HTML", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admins:remove")
async def admin_remove_start(callback: CallbackQuery, state: FSMContext):
    if not db_is_admin(callback.from_user.id) or int(callback.from_user.id) != int(_main_admin_id()):
        await callback.answer("❌ فقط ادمین اصلی می‌تواند ادمین حذف کند.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminManagementState.waiting_for_remove_id)
    await callback.message.answer("➖ آیدی عددی ادمینی که می‌خواهید حذف کنید را ارسال کنید.\n\nبرای لغو /cancel را بفرستید.")
    await callback.answer()


@dp.message(AdminManagementState.waiting_for_remove_id)
async def admin_remove_id(message: Message, state: FSMContext):
    if not db_is_admin(message.from_user.id) or int(message.from_user.id) != int(_main_admin_id()):
        await state.clear()
        return
    value = (message.text or "").strip()
    if value == "/cancel":
        await state.clear()
        await message.answer("❌ عملیات لغو شد.", reply_markup=admin_keyboard())
        return
    try:
        admin_id = int(value)
        if admin_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ آیدی نامعتبر است. فقط آیدی عددی تلگرام را ارسال کنید.")
        return
    if admin_id == int(_main_admin_id()):
        await message.answer("🔐 ادمین اصلی قابل حذف نیست.")
        return
    if not db_is_admin(admin_id):
        await state.clear()
        await message.answer("⚠️ این آیدی در لیست ادمین‌ها نیست.", reply_markup=admin_keyboard())
        return
    ok = db_remove_admin(admin_id)
    _load_admins_from_database()
    try:
        await bot.set_my_commands([], scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception:
        pass
    await state.clear()
    await message.answer("✅ ادمین حذف شد.", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admins:cancel")
async def admin_management_cancel(callback: CallbackQuery, state: FSMContext):
    if not db_is_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("✅ مدیریت ادمین‌ها بسته شد.", reply_markup=admin_keyboard())
    await callback.answer()


#=========================================================
# SHOPPING CART
#=========================================================

def _cart_services_for_user(user_id):
    services=[]
    for row in get_cart_items(int(user_id)):
        service=get_service(row["service_key"])
        if service and int(service["active"])==1: services.append(service)
    return services

def _cart_text(services):
    if not services: return "🛒 <b>سبد خرید شما خالی است.</b>\n\nاز بخش خرید سرویس، سرویس موردنظر را به سبد اضافه کنید."
    lines=["🛒 <b>سبد خرید شما</b>",""]; total=0
    for i,s in enumerate(services,1):
        price=int(s["price"]); total+=price
        lines.append(f"{i}️⃣ {escape(str(s['name']))}\n   📊 {escape(str(s['volume']))} | 💰 {format_money(price)} تومان")
    lines += ["",f"💵 <b>مجموع: {format_money(total)} تومان</b>"]
    return "\n".join(lines)

async def _show_cart(target,user_id):
    services=_cart_services_for_user(user_id)
    await target.answer(_cart_text(services),parse_mode="HTML",reply_markup=cart_actions_keyboard() if services else cart_empty_keyboard())

@dp.message(F.text == "🛒 سبد خرید")
async def cart_menu_message(message: Message):
    await _show_cart(message,message.from_user.id)

@dp.callback_query(F.data == "cart:view")
async def cart_view_callback(callback: CallbackQuery):
    await _show_cart(callback.message,callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.data == "cart:add_menu")
async def cart_add_menu_callback(callback: CallbackQuery):
    services=get_active_service_catalog()
    if not services:
        await callback.answer("❌ فعلاً سرویس فعالی برای افزودن وجود ندارد.",show_alert=True); return
    await callback.message.answer("➕ <b>افزودن به سبد خرید</b>\n\nسرویس موردنظر را انتخاب کن:",parse_mode="HTML",reply_markup=cart_add_keyboard(services))
    await callback.answer()

@dp.callback_query(F.data.startswith("cart:add:"))
async def cart_add_callback(callback: CallbackQuery):
    key=callback.data.split(":",2)[2]; service=get_service(key)
    if not service or int(service["active"])!=1:
        await callback.answer("❌ این سرویس دیگر فعال نیست.",show_alert=True); return
    added=add_cart_item(callback.from_user.id,key)
    await callback.answer("✅ سرویس به سبد اضافه شد." if added else "ℹ️ این سرویس از قبل داخل سبد است.",show_alert=True)
    await _show_cart(callback.message, callback.from_user.id)

@dp.callback_query(F.data == "cart:remove_menu")
async def cart_remove_menu_callback(callback: CallbackQuery):
    services=_cart_services_for_user(callback.from_user.id)
    if not services: await callback.answer("🛒 سبد خرید خالی است.",show_alert=True); return
    await callback.message.answer("🗑 <b>حذف از سبد خرید</b>\n\nسرویس موردنظر را انتخاب کن:",parse_mode="HTML",reply_markup=cart_remove_keyboard(services)); await callback.answer()

@dp.callback_query(F.data.startswith("cart:remove:"))
async def cart_remove_callback(callback: CallbackQuery):
    key=callback.data.split(":",2)[2]
    if not remove_cart_item(callback.from_user.id,key): await callback.answer("⚠️ این سرویس داخل سبد نبود.",show_alert=True); return
    await callback.answer("🗑 سرویس از سبد حذف شد."); await _show_cart(callback.message,callback.from_user.id)

@dp.callback_query(F.data == "cart:checkout")
async def cart_checkout_callback(callback: CallbackQuery,state: FSMContext):
    services=_cart_services_for_user(callback.from_user.id)
    if not services: await callback.answer("🛒 سبد خرید خالی است.",show_alert=True); return
    total=sum(int(s["price"]) for s in services); volumes=" + ".join(str(s["volume"]) for s in services)
    order_code=generate_order_code()
    try:
        create_order(order_code=order_code,user_id=callback.from_user.id,service_key="__cart__",service_name=f"🛒 سبد خرید ({len(services)} سرویس)",volume=volumes,price=total,discount_code=None,discount_amount=0,final_price=total)
        cart_snapshot = [
            {
                "service_key": str(s["service_key"]),
                "service_name": str(s["name"]),
                "volume": str(s["volume"]),
                "price": int(s["price"]),
            }
            for s in services
        ]
        set_setting(f"cart_order:{order_code}", json.dumps(cart_snapshot, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        logger.exception("Could not create cart checkout order")
        await callback.answer("❌ ثبت سفارش سبد خرید انجام نشد.",show_alert=True); return
    await state.update_data(order_code=order_code)
    await state.set_state(PurchaseState.waiting_for_receipt)
    await callback.message.answer(
        "🛒 <b>سفارش سبد خرید</b>\n\n"
        f"🧾 سفارش: <code>{order_code}</code>\n"
        f"📦 تعداد سرویس: <b>{len(services)}</b>\n"
        f"💰 مبلغ قابل پرداخت: <b>{format_money(total)} تومان</b>\n\n"
        "💳 <b>پرداخت با شماره کارت</b>\n\n"
        f"💳 شماره کارت: <code>{CARD_NUMBER}</code>\n"
        "👤 به نام: <b>یوسفی واحد</b>\n\n"
        "📸 بعد از واریز، عکس واضح رسید را همینجا ارسال کن.",
        parse_mode="HTML",
        reply_markup=card_payment_keyboard(order_code, total)
    )
    await callback.answer("✅ سفارش آماده پرداخت شد.")


#=========================================================
# BUY
#=========================================================

@dp.message(F.text == "🛒 خرید سرویس")
async def buy_handler(message: Message, state: FSMContext):
    await state.clear()

    services = get_active_service_catalog()

    if not services:
        await message.answer(
        "❌ در حال حاضر هیچ سرویسی برای فروش فعال نیست.",
        reply_markup=main_keyboard()
        )
        return

    await message.answer(
    "🛒 <b>انتخاب سرویس</b> ✨\n\n"
        "سرویسی که مناسبته رو انتخاب کن تا مشخصات و مبلغش رو برات نمایش بدم. 👇",
    reply_markup=service_keyboard(),
    parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("buy_category:"))
async def buy_category_callback(callback: CallbackQuery):
    category = callback.data.split(":", 1)[1]
    services = _get_services_by_category(category)
    if not services:
        if category == "gaming":
            await callback.answer("🎮 بخش گیمینگ به‌زودی فعال خواهد شد.", show_alert=True)
        else:
            await callback.answer("❌ در این دسته سرویسی برای فروش فعال نیست.", show_alert=True)
        return
    # همه دکمه‌ها یک عرض ثابت دارند؛ متن‌های طولانی داخل همان عرض اسکرول می‌شوند.
    rows = []
    for service in services:
        full_text = _service_button_text(service)
        rows.append([
            InlineKeyboardButton(
                text=_service_marquee_frame(full_text, 0),
                callback_data=f"buy:{service['service_key']}"
            )
        ])
    rows.append([InlineKeyboardButton(text="🔙 دسته‌بندی سرویس‌ها", callback_data="buy_categories")])
    sent_message = await callback.message.answer(
        f"{_service_category_label(category)} <b>سرویس‌ها</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )
    _start_service_marquee(sent_message, services)
    await callback.answer()


@dp.callback_query(F.data == "buy_categories")
async def buy_categories_callback(callback: CallbackQuery):
    _stop_service_marquee(callback.message.chat.id, callback.message.message_id)
    await callback.message.answer(
        "🛒 <b>دسته‌بندی سرویس‌ها</b>",
        reply_markup=service_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_service_callback(
callback: CallbackQuery,
state: FSMContext
):
    _stop_service_marquee(callback.message.chat.id, callback.message.message_id)
    service_key = callback.data.split(":", 1)[1]

    service = get_service(service_key)

    if not service or int(service["active"]) != 1:
        await callback.answer(
        "❌ این سرویس در حال حاضر فعال نیست.",
        show_alert=True
        )
        return

    await callback.message.answer(
        "📦 <b>جزئیات سرویس</b>\n\n"
        f"📦 سرویس: <b>{escape(str(service['name']))}</b>\n"
        f"📊 حجم: <b>{escape(str(service['volume']))}</b>\n"
        f"⏱ مدت: <b>{int(service['duration_days'])} روز</b>\n"
        f"💰 قیمت: <b>{format_money(service['price'])} تومان</b>\n\n"
        "برای اضافه کردن این سرویس به سبد، دکمه زیر را بزن.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن به سبد خرید",callback_data=f"cart:add:{service_key}")],
            [InlineKeyboardButton(text="🛒 مشاهده سبد خرید",callback_data="cart:view")],
        ])
    )
    await callback.answer()

#=========================================================
# RENEWAL
#=========================================================

@dp.callback_query(F.data.startswith("renew:"))
async def renew_service_callback(
    callback: CallbackQuery,
    state: FSMContext
):
    """Start a NEW purchase for the selected service as a renewal.

    The old order is only used as the source of the renewal dates.
    A new order and a new config are created after payment approval.
    """
    order_code = callback.data.split(":", 1)[1]
    order = get_order(order_code)

    if not order:
        await callback.answer("❌ سرویس موردنظر پیدا نشد.", show_alert=True)
        return

    if int(order["user_id"]) != callback.from_user.id:
        await callback.answer("❌ این سرویس متعلق به حساب شما نیست.", show_alert=True)
        return

    if order["status"] not in ("delivered", "expired"):
        await callback.answer("⚠️ این سرویس در حال حاضر قابل تمدید نیست.", show_alert=True)
        return

    if not order["expiry_date"]:
        await callback.answer("❌ تاریخ انقضای سرویس مشخص نیست.", show_alert=True)
        return

    service = None
    service_key = order["service_key"]

    if service_key:
        service = get_service(service_key)

    if service is None:
        for item in get_all_services():
            if item["name"] == order["service_name"]:
                service = item
                break

    if service is None:
        await callback.answer(
            "❌ اطلاعات این سرویس در کاتالوگ پیدا نشد.",
            show_alert=True
        )
        return

    if int(service["active"]) != 1:
        await callback.answer(
            "⚠️ این سرویس در حال حاضر برای تمدید فعال نیست.",
            show_alert=True
        )
        return

    # مشخصات سرویس از سفارش قبلی حفظ می‌شود؛ قیمت از کاتالوگ فعلی خوانده می‌شود.
    # بنابراین تمدید همیشه برای همان سرویس/حجم انجام می‌شود و کانفیگ قبلی استفاده نمی‌شود.
    await state.update_data(
        service_key=service["service_key"],
        service_name=order["service_name"],
        volume=order["volume"],
        price=int(service["price"]),
        duration_days=int(service["duration_days"]),
        renewal_for_order_code=order_code,
        discount_code=None,
        discount_amount=0,
        final_price=int(service["price"]),
    )

    await state.set_state(PurchaseState.waiting_for_discount)

    await callback.message.answer(
        "🔄 <b>درخواست تمدید آماده شد</b>\n\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n"
        f"⏱ مدت جدید: <b>{service['duration_days']} روز</b>\n"
        f"💰 مبلغ: <b>{format_money(service['price'])} تومان</b>\n\n"
        "🎟 در مرحله بعد می‌توانید کد تخفیف خود را وارد کنید.\n"
        "در صورت نداشتن کد تخفیف، گزینه «کد تخفیف ندارم» را انتخاب کنید.",
        reply_markup=discount_choice_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("🔄 تمدید آماده شد.")

#=========================================================
# DISCOUNT
#=========================================================

@dp.callback_query(F.data == "discount:none")
async def discount_none_callback(callback: CallbackQuery, state: FSMContext):
    """ادامه خرید بدون کد تخفیف."""
    try:
        data = await state.get_data()

        required = ("service_key", "service_name", "volume", "price")
        missing = [key for key in required if key not in data]
        if missing:
            await callback.answer(
                "❌ اطلاعات خرید پیدا نشد. لطفاً خرید را دوباره شروع کنید.",
                show_alert=True,
            )
            await callback.message.answer(
                "❌ اطلاعات خرید کامل نیست.\n\n"
                "لطفاً دوباره از «🛒 خرید سرویس» شروع کن.",
                reply_markup=main_keyboard(),
            )
            await state.clear()
            return

        price = int(data["price"])
        await state.update_data(
            discount_code=None,
            discount_amount=0,
            final_price=price,
        )

        await callback.answer("✅ بدون کد تخفیف ادامه می‌دیم.")
        await create_payment_order(
            callback.message,
            state,
            user_id=callback.from_user.id,
        )

    except Exception:
        logger.exception(
            "Discount none callback failed for user %s",
            callback.from_user.id,
        )
        try:
            await callback.answer(
                "❌ هنگام ثبت سفارش خطایی رخ داد.",
                show_alert=True,
            )
        except Exception:
            pass

        await callback.message.answer(
            "❌ هنگام ثبت سفارش خطایی رخ داد.\n"
            "لطفاً دوباره «🛒 خرید سرویس» را بزن.",
            reply_markup=main_keyboard(),
        )
        await state.clear()


@dp.callback_query(F.data == "discount:continue_without")
async def discount_continue_without_callback(callback: CallbackQuery, state: FSMContext):
    """ادامه مستقیم خرید بدون اعمال کد تخفیف."""
    try:
        data = await state.get_data()
        required = ("service_key", "service_name", "volume", "price")
        if any(key not in data for key in required):
            await callback.answer("❌ اطلاعات خرید پیدا نشد. لطفاً خرید را دوباره شروع کنید.", show_alert=True)
            await callback.message.answer("❌ اطلاعات خرید کامل نیست.\n\nلطفاً دوباره از «🛒 خرید سرویس» شروع کن.", reply_markup=main_keyboard())
            await state.clear()
            return
        price = int(data["price"])
        await state.update_data(discount_code=None, discount_amount=0, final_price=price)
        await callback.answer("✅ بدون کد تخفیف ادامه می‌دیم.")
        await create_payment_order(callback.message, state, user_id=callback.from_user.id)
    except Exception:
        logger.exception("Discount continue-without-code callback failed for user %s", callback.from_user.id)
        await callback.answer("❌ هنگام ادامه خرید خطایی رخ داد.", show_alert=True)
        await callback.message.answer("❌ هنگام ثبت سفارش خطایی رخ داد.\nلطفاً دوباره «🛒 خرید سرویس» را بزن.", reply_markup=main_keyboard())
        await state.clear()


@dp.callback_query(F.data == "discount:yes")
async def discount_yes_callback(callback: CallbackQuery, state: FSMContext):
    """ورود به مرحله دریافت کد تخفیف."""
    try:
        data = await state.get_data()

        required = ("service_key", "service_name", "volume", "price")
        missing = [key for key in required if key not in data]
        if missing:
            await callback.answer(
                "❌ اطلاعات خرید پیدا نشد. لطفاً خرید را دوباره شروع کنید.",
                show_alert=True,
            )
            await callback.message.answer(
                "❌ اطلاعات خرید کامل نیست.\n\n"
                "لطفاً دوباره از «🛒 خرید سرویس» شروع کن.",
                reply_markup=main_keyboard(),
            )
            await state.clear()
            return

        await state.set_state(PurchaseState.waiting_for_discount)
        await callback.answer("🎟 کد تخفیف را ارسال کن.")
        await callback.message.answer(
            "🎟 <b>کد تخفیف رو وارد کن</b>\n\n"
            "کد رو دقیقاً همون‌طور که دریافت کردی ارسال کن تا بررسی و اعمال بشه.\n"
            "اگر کد معتبر باشه، تخفیف قبل از پرداخت روی مبلغ اعمال می‌شه.",
            parse_mode="HTML",
        )

    except Exception:
        logger.exception(
            "Discount yes callback failed for user %s",
            callback.from_user.id,
        )
        try:
            await callback.answer(
                "❌ خطا در مرحله کد تخفیف.",
                show_alert=True,
            )
        except Exception:
            pass

def _normalize_discount_input(value):
    """Normalize common Persian/Arabic characters and whitespace in discount input."""
    value = str(value or "").strip()
    return (
        value.replace("ي", "ی")
             .replace("ى", "ی")
             .replace("ك", "ک")
             .replace("\u200c", "")
             .replace("\u200d", "")
             .replace("\ufeff", "")
             .strip()
    )


@dp.message(PurchaseState.waiting_for_discount, F.text)
async def discount_handler(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        code = _normalize_discount_input(message.text)

        if "price" not in data:
            await message.answer(
                "❌ اطلاعات سفارش پیدا نشد. لطفاً دوباره خرید را شروع کنید.",
                reply_markup=main_keyboard(),
            )
            await state.clear()
            return

        price = int(data["price"])

        # اگر کاربر به‌جای دکمه، متن «کد تخفیف ندارم» را هم بفرستد، خرید ادامه پیدا می‌کند.
        no_discount_values = {
            "ندارم",
            "ندارم.",
            "ندارم؟",
            "کد تخفیف ندارم",
            "کد تخفیف ندارم.",
            "-",
            "0",
        }

        if code in no_discount_values:
            await state.update_data(
                discount_code=None,
                discount_amount=0,
                final_price=price,
            )
            await create_payment_order(message, state)
            return

        if not code:
            await message.answer("⚠️ لطفاً کد تخفیف را ارسال کن.")
            return

        discount_code = code.upper()
        discount = get_discount_code(discount_code)

        if discount is None:
            await message.answer(
                "❌ <b>کد تخفیف اشتباه است</b>\n\n"
                "کد واردشده معتبر نیست. می‌تونی کد دیگری وارد کنی یا از دکمه زیر بدون کد تخفیف ادامه بدی.",
                parse_mode="HTML",
                reply_markup=discount_continue_without_code_keyboard(),
            )
            return

        expires_at = discount["expires_at"]
        if expires_at:
            try:
                expiry = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
                if datetime.now() >= expiry:
                    await message.answer(
                        "⏰ <b>کد تخفیف منقضی شده است</b>\n\n"
                        "می‌تونی کد دیگری وارد کنی یا از دکمه زیر بدون کد تخفیف ادامه بدی.",
                        parse_mode="HTML",
                        reply_markup=discount_continue_without_code_keyboard(),
                    )
                    return
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid discount expiry for code %s: %r",
                    discount_code,
                    expires_at,
                )

        if int(discount["active"]) != 1:
            await message.answer(
                "⏰ <b>این کد تخفیف دیگر قابل استفاده نیست</b>\n\n"
                "می‌تونی کد دیگری وارد کنی یا از دکمه زیر بدون کد تخفیف ادامه بدی.",
                parse_mode="HTML",
                reply_markup=discount_continue_without_code_keyboard(),
            )
            return

        max_uses = int(discount["max_uses"])
        if max_uses > 0 and int(discount["used_count"]) >= max_uses:
            set_discount_active(discount_code, False)
            await message.answer(
                "⏰ <b>ظرفیت کد تخفیف تکمیل شده است</b>\n\n"
                f"این کد حداکثر {max_uses} بار قابل استفاده بوده و سهمیه آن تمام شده است.\n"
                "می‌تونی کد دیگری وارد کنی یا بدون کد تخفیف ادامه بدی.",
                parse_mode="HTML",
                reply_markup=discount_continue_without_code_keyboard(),
            )
            return

        if has_user_used_discount(discount_code, message.from_user.id):
            await message.answer(
                "⚠️ <b>این کد تخفیف قبلاً توسط شما استفاده شده است.</b>\n\n"
                "هر کاربر فقط یک‌بار می‌تواند از هر کد تخفیف استفاده کند.\n"
                "می‌تونی بدون کد تخفیف ادامه بدی.",
                parse_mode="HTML",
                reply_markup=discount_continue_without_code_keyboard(),
            )
            return

        discount_amount_result = calculate_discount(discount_code, price)

        if (
            not isinstance(discount_amount_result, tuple)
            or len(discount_amount_result) != 2
        ):
            raise RuntimeError(
                f"Unexpected calculate_discount result: {discount_amount_result!r}"
            )

        discount_amount, discount_error = discount_amount_result

        if discount_error is not None:
            await message.answer(
                f"❌ <b>{escape(str(discount_error))}</b>\n\n"
                "می‌تونی کد دیگری وارد کنی یا بدون کد تخفیف ادامه بدی.",
                parse_mode="HTML",
                reply_markup=discount_continue_without_code_keyboard(),
            )
            return

        discount_amount = int(discount_amount)
        final_price = max(0, price - discount_amount)

        await state.update_data(
            discount_code=discount_code,
            discount_amount=discount_amount,
            final_price=final_price,
        )

        await create_payment_order(message, state)

    except Exception:
        logger.exception(
            "Discount handler failed for user %s",
            message.from_user.id,
        )
        await message.answer(
            "❌ هنگام بررسی کد تخفیف خطایی رخ داد.\n"
            "لطفاً کد را دوباره ارسال کن یا خرید را از ابتدا شروع کن.",
            reply_markup=main_keyboard(),
        )


#=========================================================
# CREATE PAYMENT ORDER
#=========================================================

async def create_payment_order(
    message: Message,
    state: FSMContext,
    user_id=None
):
    data = await state.get_data()

    required = ("service_key", "service_name", "volume", "price")
    if any(key not in data for key in required):
        await message.answer(
            "⚠️ <b>اطلاعات سفارش کامل نیست</b>\n\n"
            "نگران نباش؛ از منوی «🛒 خرید سرویس» دوباره شروع کن. ✨",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    order_code = generate_order_code()
    price = int(data["price"])
    discount_amount = int(data.get("discount_amount", 0) or 0)
    final_price = int(data.get("final_price", price) or price)
    renewal_for = data.get("renewal_for_order_code")

    create_order(
        order_code=order_code,
        user_id=user_id if user_id is not None else message.from_user.id,
        service_key=data["service_key"],
        service_name=data["service_name"],
        volume=data["volume"],
        price=price,
        discount_code=data.get("discount_code"),
        discount_amount=discount_amount,
        final_price=final_price,
        renewal_for_order_code=renewal_for
    )

    title = "🔄 <b>تمدید سرویس</b>" if renewal_for else "🛒 <b>سفارش جدید</b>"
    text = (
        f"{title}\n\n"
        f"✅ سفارش شما با موفقیت ثبت شد.\n"
        f"🧾 شماره سفارش: <code>{order_code}</code>\n\n"
        f"📦 سرویس: <b>{data['service_name']}</b>\n"
        f"📊 حجم: <b>{data['volume']}</b>\n"
        f"⏱ مدت: <b>{data.get('duration_days', get_service_duration(data['service_key']))} روز</b>\n"
        f"💰 قیمت اصلی: <b>{format_money(price)} تومان</b>\n"
    )

    if discount_amount > 0:
        text += (
            f"🎟 تخفیف: <b>{format_money(discount_amount)} تومان</b>\n"
            f"💵 مبلغ قابل پرداخت: <b>{format_money(final_price)} تومان</b>\n"
        )
    else:
        text += f"💵 مبلغ قابل پرداخت: <b>{format_money(final_price)} تومان</b>\n"

    text += (
        "\n💳 <b>پرداخت با شماره کارت</b>\n\n"
        f"💳 شماره کارت: <code>{CARD_NUMBER}</code>\n"
        "👤 به نام: <b>یوسفی واحد</b>\n\n"
        "📸 بعد از واریز، عکس واضح رسید را همینجا ارسال کن."
    )

    await state.update_data(order_code=order_code)
    await state.set_state(PurchaseState.waiting_for_receipt)
    await message.answer(
        text,
        reply_markup=card_payment_keyboard(order_code, final_price),
        parse_mode="HTML"
    )

#=========================================================
# RECEIPT
#=========================================================

@dp.message(
PurchaseState.waiting_for_receipt,
F.photo
)
async def receipt_handler(
    message: Message,
    state: FSMContext
):
    data = await state.get_data()
    order_code = data.get("order_code")

    if not order_code:
        await message.answer(
            "❌ سفارش فعال پیدا نشد.\n\nلطفاً دوباره از منوی خرید شروع کنید.",
            reply_markup=main_keyboard()
        )
        await state.clear()
        return

    order = get_order(order_code)

    if not order or int(order["user_id"]) != message.from_user.id:
        await message.answer("❌ این سفارش معتبر نیست.", reply_markup=main_keyboard())
        await state.clear()
        return


    if order["status"] not in ("waiting_payment", "waiting"):
        await message.answer(
            "⚠️ برای این سفارش قبلاً رسید ثبت شده یا وضعیت سفارش تغییر کرده است.",
            reply_markup=main_keyboard()
        )
        await state.clear()
        return

    file_id = message.photo[-1].file_id

    try:
        saved = save_receipt(order_code, file_id)
        if not saved:
            raise RuntimeError("receipt could not be saved for the current order status")
    except Exception:
        logger.exception("Could not save receipt %s", order_code)
        await message.answer(
            "❌ ذخیره رسید با مشکل مواجه شد. لطفاً دوباره تلاش کنید.",
            reply_markup=main_keyboard()
        )
        await state.clear()
        return

    is_renewal = bool(order["renewal_for_order_code"])
    order_type = "🔄 تمدید سرویس" if is_renewal else "🛒 خرید جدید"

    username = message.from_user.username
    display_name = message.from_user.full_name or "بدون نام"
    username_text = f"@{username}" if username else "ندارد"

    admin_text = (
        "💳 <b>رسید پرداخت جدید</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 شماره سفارش: <code>{order_code}</code>\n"
        f"👤 نام کاربر: <b>{escape(display_name)}</b>\n"
        f"🔹 یوزرنیم: <code>{escape(username_text)}</code>\n"
        f"🆔 آیدی عددی: <code>{message.from_user.id}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n"
        f"🛒 انتخاب کاربر: <b>{order['service_name']} ({order['volume']})</b>\n"
        f"🏷 نوع سفارش: <b>{order_type}</b>\n"
        f"💰 مبلغ پرداختی: <b>{format_money(get_order_final_price(order))} تومان</b>"
    )
    if is_renewal:
        admin_text += f"\n🔗 سفارش قبلی: <code>{order['renewal_for_order_code']}</code>"

    sent_to_any_admin = False

    # وقتی گروه تنظیم شده باشد، رسیدها به گروه ارسال می‌شوند.
    # تا قبل از تنظیم گروه، روش قبلی حفظ می‌شود تا سیستم از کار نیفتد.
    if RECEIPT_GROUP_ID:
        try:
            sent_receipt_message = await bot.send_photo(
                RECEIPT_GROUP_ID,
                file_id,
                caption=admin_text,
                reply_markup=admin_payment_keyboard(order_code),
                parse_mode="HTML"
            )
            sent_to_any_admin = True
        except Exception:
            logger.exception(
                "Could not send receipt %s to group %s",
                order_code,
                RECEIPT_GROUP_ID
            )
    else:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_photo(
                    admin_id,
                    file_id,
                    caption=admin_text,
                    reply_markup=admin_payment_keyboard(order_code),
                    parse_mode="HTML"
                )
                sent_to_any_admin = True
            except Exception:
                logger.exception("Could not send receipt %s to admin %s", order_code, admin_id)

    if sent_to_any_admin:
        await message.answer(
            "✅ <b>رسید شما با موفقیت دریافت و برای بررسی ارسال شد</b>\n\n"
            f"🧾 شماره سفارش: <code>{order_code}</code>\n"
            f"📦 سرویس: <b>{order['service_name']}</b>\n"
            f"💰 مبلغ: <b>{format_money(get_order_final_price(order))} تومان</b>\n\n"
            "⏳ پرداخت شما در حال بررسی است.\n"
            "پس از تأیید، مراحل آماده‌سازی سرویس انجام می‌شود و نتیجه برای شما ارسال خواهد شد. 💙",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
    else:
        logger.error("Receipt %s was saved but could not be delivered to any admin/review group", order_code)
        await message.answer(
            "⚠️ <b>در ارسال رسید شما برای بخش بررسی مشکلی پیش آمد</b>\n\n"
            f"🧾 شماره سفارش: <code>{order_code}</code>\n\n"
            "رسید شما در سیستم ثبت شده، اما به بخش بررسی پرداخت ارسال نشد.\n"
            "لطفاً چند دقیقه بعد دوباره تلاش کنید یا با پشتیبانی تماس بگیرید. 🙏",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )

    await state.clear()

@dp.message(PurchaseState.waiting_for_receipt)
async def receipt_not_photo_handler(message: Message):
    await message.answer(
    "📸 <b>رسید پرداخت رو بفرست</b>\n\n"
        "لطفاً یک عکس واضح و خوانا از رسید پرداخت همینجا ارسال کن تا بررسی رو شروع کنیم. 💳",
        parse_mode="HTML",
    )

#=========================================================
# CANCEL ORDER
#=========================================================

@dp.callback_query(F.data.startswith("cancel_order:"))
async def cancel_order_callback(
callback: CallbackQuery,
state: FSMContext
):
    order_code = callback.data.split(":", 1)[1]

    order = get_order(order_code)

    if order and int(order["user_id"]) == callback.from_user.id:
        # در مرحله پرداخت، سفارش هنوز وضعیت waiting_payment دارد؛
        # بعد از ارسال رسید به waiting تغییر می‌کند. هر دو وضعیت باید قابل لغو باشند.
        if order["status"] in ("waiting_payment", "waiting"):
            update_order_status(order_code, "cancelled")

            await state.clear()

            # دکمه‌های پیام پرداخت را هم غیرفعال می‌کنیم تا دوباره روی لغو/پرداخت زده نشود.
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            await callback.message.answer(
                "🚫 <b>سفارش لغو شد</b>\n\n"
                "سفارش شما با موفقیت لغو شد. حالا می‌توانید دوباره از منوی اصلی خرید جدیدی انجام دهید. 🛒",
                reply_markup=main_keyboard(),
                parse_mode="HTML"
            )

            await callback.answer("سفارش لغو شد.")
            return

    await callback.answer("⚠️ این سفارش دیگر قابل لغو نیست.", show_alert=True)

#=========================================================
# RECEIPT REVIEW AUDIT
#=========================================================

def _receipt_review_db_init():
    """Kept for compatibility; receipt review storage is initialized by init_db()."""
    return None


def _save_receipt_review(order_code, reviewer_id, reviewer_name, result):
    try:
        db_update_admin_name(reviewer_id, reviewer_name)
        return db_save_receipt_review(order_code, reviewer_id, reviewer_name, result)
    except Exception:
        logger.exception("Could not save receipt review for %s", order_code)
        return False


async def _update_receipt_group_message(callback, result_text, reply_markup=None):
    """Update the original receipt message without making review depend on Telegram edit permissions."""
    try:
        current_caption = callback.message.caption or ""
        if "این رسید توسط ادمین" in current_caption:
            return True

        new_caption = f"{current_caption}\n\n{result_text}"
        await callback.message.edit_caption(
            caption=new_caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except Exception:
        logger.exception("Could not update original receipt message for review.")
        return False


#=========================================================
# ADMIN APPROVE / REJECT
#=========================================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve_payment_callback(callback: CallbackQuery):
    uid = int(callback.from_user.id)

    # ادمین‌ها از دیتابیس منبع اصلی هستند؛ ADMIN_IDS فقط کش runtime است.
    # ادمین اصلی همیشه دسترسی دارد و ادمین فرعی باید دسترسی payments یا receipts داشته باشد.
    if not db_is_admin(uid):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    if not _is_main_admin(uid) and not (
        db_has_admin_permission(uid, "payments")
        or db_has_admin_permission(uid, "receipts")
    ):
        await callback.answer("❌ دسترسی بررسی رسید برای شما فعال نیست.", show_alert=True)
        return

    order_code = callback.data.split(":", 1)[1].strip()
    if not order_code:
        await callback.answer("❌ شماره سفارش نامعتبر است.", show_alert=True)
        return

    if order_code in approval_locks:
        await callback.answer("⏳ این سفارش در حال پردازش است.", show_alert=True)
        return

    approval_locks.add(order_code)
    try:
        order = get_order(order_code)
        if not order:
            await callback.answer("❌ سفارش پیدا نشد.", show_alert=True)
            return

        current_status = str(order["status"] or "").strip()
        if current_status not in ("waiting", "payment_review"):
            await callback.answer(
                f"⚠️ این سفارش قبلاً پردازش شده است.\nوضعیت: {current_status}",
                show_alert=True,
            )
            return

        # تغییر وضعیت اتمیک است تا دو ادمین همزمان نتوانند یک رسید را دوبار تأیید کنند.
        if not set_order_status_if_current(order_code, "approved", current_status):
            await callback.answer(
                "⚠️ این رسید قبلاً توسط ادمین دیگری پردازش شده است.",
                show_alert=True,
            )
            return

        reviewer_name = (
            callback.from_user.full_name
            or callback.from_user.username
            or str(uid)
        )
        reviewer_name_html = escape(reviewer_name)
        review_text = f"✅ این رسید توسط ادمین «{reviewer_name_html}» بررسی و تأیید شد."

        _save_receipt_review(order_code, uid, reviewer_name, "approved")
        _audit(uid, "receipt_approved", target=order_code)

        # تأیید واقعی همین‌جا انجام شده؛ پاسخ callback را قبل از عملیات‌های جانبی می‌فرستیم
        # تا خطای ویرایش پیام گروه باعث نمایش «خطا در تأیید» نشود.
        await callback.answer("✅ رسید با موفقیت تأیید شد.")

        # در سبد خرید یک پرداخت برای کل سبد انجام می‌شود؛ بعد از تأیید،
        # برای هر سرویس یک سفارش آماده ارسال کانفیگ می‌سازیم تا جریان تحویل
        # فعلی ربات برای هر سرویس مستقل و واقعی باقی بماند.
        config_markup = config_keyboard(order_code)
        if str(order["service_key"]) == "__cart__":
            raw_snapshot = str(get_setting(f"cart_order:{order_code}", "") or "")
            children = []
            try:
                snapshot = json.loads(raw_snapshot) if raw_snapshot.startswith("[") else None
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot = None

            if isinstance(snapshot, list):
                for item in snapshot:
                    if not isinstance(item, dict):
                        continue
                    if not item.get("service_key") or not item.get("service_name"):
                        continue
                    children.append({
                        "order_code": generate_order_code(),
                        "service_name": str(item["service_name"]),
                        "volume": str(item.get("volume", "-")),
                        "price": int(item.get("price", 0)),
                        "service_key": str(item["service_key"]),
                    })
            else:
                # Backward compatibility for cart orders created by older versions.
                keys = [k for k in raw_snapshot.split("|") if k]
                for key in keys:
                    service = get_service(key)
                    if service:
                        children.append({
                            "order_code": generate_order_code(),
                            "service_name": service["name"],
                            "volume": service["volume"],
                            "price": int(service["price"]),
                            "service_key": service["service_key"],
                        })

            if not children:
                # Do not leave an approved parent with a receipt UI that can never be delivered.
                await _update_receipt_group_message(
                    callback,
                    "⚠️ پرداخت تأیید شد، اما اطلاعات سرویس‌های این سبد پیدا نشد. بررسی دستی ادمین لازم است.",
                    reply_markup=None,
                )
                _audit(uid, "cart_children_missing", target=order_code)
                return

            create_cart_child_orders(order_code, order["user_id"], children)
            config_markup = cart_config_keyboard(children)

        await _update_receipt_group_message(
            callback,
            review_text,
            reply_markup=config_markup,
        )

        is_renewal = bool(order["renewal_for_order_code"])
        kind = "🔄 تمدید سرویس" if is_renewal else "🛒 خرید جدید"

        try:
            await callback.bot.send_message(
                order["user_id"],
                "✅ <b>پرداخت شما تأیید شد</b>\n\n"
                f"🧾 سفارش: <code>{order_code}</code>\n"
                f"📦 سرویس: <b>{order['service_name']}</b>\n"
                f"🏷 نوع سفارش: <b>{kind}</b>\n\n"
                "📡 پرداخت با موفقیت تأیید شد و سفارش شما وارد مرحله آماده‌سازی شده است.\n"
                "⏳ به‌محض آماده شدن کانفیگ، آن را برایتان ارسال می‌کنیم.",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not notify user %s after approval", order["user_id"])

    except Exception as e:
        logger.exception("Approve payment failed for %s: %s", order_code, e)
        try:
            await callback.answer("❌ خطا در تأیید پرداخت.", show_alert=True)
        except Exception:
            pass
    finally:
        approval_locks.discard(order_code)

@dp.callback_query(F.data.startswith("reject:"))
async def reject_payment_callback(callback: CallbackQuery):
    uid = int(callback.from_user.id)
    if not db_is_admin(uid):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    if not _is_main_admin(uid) and not (
        db_has_admin_permission(uid, "payments")
        or db_has_admin_permission(uid, "receipts")
    ):
        await callback.answer("❌ دسترسی بررسی رسید برای شما فعال نیست.", show_alert=True)
        return

    order_code = callback.data.split(":", 1)[1].strip()

    if order_code in approval_locks:
        await callback.answer("⏳ این سفارش در حال پردازش است.", show_alert=True)
        return

    approval_locks.add(order_code)
    try:
        order = get_order(order_code)
        if not order:
            await callback.answer("❌ سفارش پیدا نشد.", show_alert=True)
            return

        if order["status"] not in ("waiting", "payment_review"):
            await callback.answer(
                f"⚠️ این سفارش در صف بررسی پرداخت نیست.\nوضعیت فعلی: {order['status']}",
                show_alert=True
            )
            return

        current_status = str(order["status"] or "").strip()
        if not set_order_status_if_current(order_code, "rejected", current_status):
            await callback.answer("⚠️ این رسید قبلاً توسط ادمین دیگری پردازش شده است.", show_alert=True)
            return

        reviewer_name = callback.from_user.full_name or callback.from_user.username or str(callback.from_user.id)
        reviewer_name_html = escape(reviewer_name)
        review_text = f"❌ این رسید توسط ادمین «{reviewer_name_html}» بررسی و رد شد."
        _save_receipt_review(
            order_code,
            uid,
            reviewer_name,
            "rejected",
        )
        _audit(uid, "receipt_rejected", target=order_code)
        await _update_receipt_group_message(callback, review_text, reply_markup=None)
        await callback.answer("❌ رسید رد شد.")

        try:
            await bot.send_message(
                order["user_id"],
                "❌ <b>پرداخت سفارش تأیید نشد</b>\n\n"
                f"🧾 سفارش: <code>{order_code}</code>\n"
                f"📦 سرویس: <b>{order['service_name']}</b>\n\n"
                "رسید ارسالی مورد تأیید قرار نگرفت.\n"
                "اگر فکر می‌کنید این نتیجه اشتباه است، لطفاً از بخش «🎧 پشتیبانی» با ما در ارتباط باشید.",
                reply_markup=main_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            logger.exception("Could not notify rejected order %s", order_code)

        await callback.message.answer(
            "❌ <b>پرداخت رد شد</b>\n\n"
            f"🧾 سفارش: <code>{order_code}</code>\n"
            "نتیجه به کاربر اطلاع داده شد.",
            parse_mode="HTML"
        )
    finally:
        approval_locks.discard(order_code)

#=========================================================
# ADMIN CONFIG
#=========================================================

async def _send_config_to_user(user_id, user_message, config, telegram_bot=None):
    """Send the customer's config reliably, with fallbacks and useful logging."""
    sender_bot = telegram_bot or bot

    try:
        target_id = int(user_id)
    except (TypeError, ValueError):
        logger.error(
            "CONFIG DELIVERY: invalid customer user_id=%r",
            user_id,
        )
        return False

    logger.info(
        "CONFIG DELIVERY: attempting order recipient user_id=%s",
        target_id,
    )

    # 1) Normal formatted message.
    try:
        await sender_bot.send_message(
            chat_id=target_id,
            text=user_message,
            parse_mode="HTML",
        )
        logger.info(
            "CONFIG DELIVERY: formatted message sent successfully to user_id=%s",
            target_id,
        )
        return True
    except TelegramForbiddenError:
        logger.error(
            "CONFIG DELIVERY: TelegramForbiddenError for user_id=%s "
            "(user blocked the bot or the bot cannot initiate this chat).",
            target_id,
        )
        return False
    except TelegramBadRequest as exc:
        logger.warning(
            "CONFIG DELIVERY: HTML message rejected for user_id=%s: %s. "
            "Trying plain text.",
            target_id,
            exc,
        )
    except Exception as exc:
        logger.exception(
            "CONFIG DELIVERY: HTML send failed for user_id=%s: %r. "
            "Trying plain text.",
            target_id,
            exc,
        )

    # 2) Plain-text fallback. This avoids HTML parsing errors and handles
    # configs longer than Telegram's 4096-character message limit.
    plain = (
        "🎉 سرویس شما آماده شد\n"
        "━━━━━━━━━━━━━━\n"
        f"🔐 کانفیگ جدید شما:\n\n{config}\n\n"
        "⚠️ لطفاً کانفیگ خود را در اختیار دیگران قرار ندهید."
    )
    chunks = [plain[i:i + 3900] for i in range(0, len(plain), 3900)] or [plain]

    try:
        for chunk in chunks:
            await sender_bot.send_message(
                chat_id=target_id,
                text=chunk,
            )
        logger.info(
            "CONFIG DELIVERY: plain-text fallback sent successfully to user_id=%s",
            target_id,
        )
        return True
    except TelegramForbiddenError:
        logger.error(
            "CONFIG DELIVERY: plain-text fallback forbidden for user_id=%s.",
            target_id,
        )
        return False
    except TelegramBadRequest as exc:
        logger.error(
            "CONFIG DELIVERY: plain-text fallback rejected for user_id=%s: %s",
            target_id,
            exc,
        )
    except Exception as exc:
        logger.exception(
            "CONFIG DELIVERY: plain-text fallback failed for user_id=%s: %r",
            target_id,
            exc,
        )

    # 3) Last-resort attempt: send only the raw config. This is useful if
    # the surrounding message is the part Telegram rejects.
    try:
        raw_chunks = [config[i:i + 3900] for i in range(0, len(config), 3900)] or [config]
        for chunk in raw_chunks:
            await sender_bot.send_message(
                chat_id=target_id,
                text=chunk,
            )
        logger.info(
            "CONFIG DELIVERY: raw-config fallback sent successfully to user_id=%s",
            target_id,
        )
        return True
    except Exception as exc:
        logger.exception(
            "CONFIG DELIVERY: all delivery attempts failed for user_id=%s: %r",
            target_id,
            exc,
        )
        return False


@dp.callback_query(F.data.startswith("config:"))
async def config_callback(
    callback: CallbackQuery,
    state: FSMContext
):
    uid = int(callback.from_user.id)

    if not db_is_admin(uid) or not _has_config_perm(uid):
        await callback.answer(
            "❌ دسترسی «ارسال کانفیگ» برای شما فعال نیست.",
            show_alert=True
        )
        return

    # این جریان فقط باید از گروه رسیدها اجرا شود.
    # ادمین بعد از زدن دکمه، کانفیگ را در همان گروه ارسال می‌کند.
    if RECEIPT_GROUP_ID and int(callback.message.chat.id) != int(RECEIPT_GROUP_ID):
        await callback.answer(
            "❌ دکمه «ارسال کانفیگ» فقط در گروه رسیدها قابل استفاده است.",
            show_alert=True
        )
        return

    order_code = callback.data.split(":", 1)[1].strip()
    order = get_order(order_code)

    if not order:
        await callback.answer("❌ سفارش پیدا نشد.", show_alert=True)
        return

    if order["status"] != "approved":
        await callback.answer(
            "⚠️ این سفارش هنوز در وضعیت آماده دریافت کانفیگ نیست.",
            show_alert=True
        )
        return

    # سفارش برای همان ادمین نگهداری می‌شود و FSM نیز در همان چت گروه ثبت می‌گردد.
    _pending_admin_configs[uid] = order_code
    await state.update_data(
        config_order_code=order_code,
        config_group_id=int(callback.message.chat.id),
    )
    await state.set_state(AdminConfigState.waiting_for_config)
    _audit(uid, "config_requested", target=order_code)

    await callback.message.answer(
        "📡 <b>ارسال کانفیگ</b>\n\n"
        f"🧾 سفارش: <code>{order_code}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n\n"
        "🔐 لطفاً کانفیگ <b>جدید</b> این سرویس را همین‌جا در <b>همین گروه</b> "
        "به صورت متن ارسال کنید.\n"
        "کانفیگ قبلی را ارسال نکنید.",
        parse_mode="HTML"
    )

    await callback.answer("📡 درخواست ثبت شد؛ کانفیگ را همین‌جا در گروه ارسال کنید.")

@dp.message(AdminConfigState.waiting_for_config)
async def config_received_handler(
    message: Message,
    state: FSMContext
):
    uid = int(message.from_user.id)
    if not db_is_admin(uid) or not _has_config_perm(uid):
        await state.clear()
        _pending_admin_configs.pop(uid, None)
        return

    data = await state.get_data()

    # State باید مربوط به همان گروه رسیدها باشد؛ این guard جلوی پردازش
    # ناخواسته در چت دیگری را هم می‌گیرد.
    config_group_id = data.get("config_group_id")
    if config_group_id and int(message.chat.id) != int(config_group_id):
        return

    if RECEIPT_GROUP_ID and int(message.chat.id) != int(RECEIPT_GROUP_ID):
        return

    order_code = data.get("config_order_code") or _pending_admin_configs.get(int(message.from_user.id))

    if not order_code:
        await message.answer("❌ سفارش انتخاب نشده است.", reply_markup=admin_keyboard(admin_id=uid))
        await state.clear()
        return

    order = get_order(order_code)
    if not order:
        await message.answer("❌ سفارش پیدا نشد.", reply_markup=admin_keyboard(admin_id=uid))
        await state.clear()
        return

    if order["status"] != "approved":
        await message.answer(
            f"⚠️ وضعیت سفارش مناسب نیست: {order['status']}",
            reply_markup=admin_keyboard(admin_id=uid)
        )
        await state.clear()
        return

    config = (message.text or "").strip()
    if not config:
        await message.answer("❌ کانفیگ باید به صورت متن ارسال شود.")
        return

    now = datetime.now()
    purchase_date = now.strftime("%Y-%m-%d %H:%M:%S")
    duration_days = get_service_duration(order["service_key"])

    if order["renewal_for_order_code"]:
        source_order = get_order(order["renewal_for_order_code"])
        if not source_order:
            await message.answer("❌ سفارش اصلی تمدید پیدا نشد.", reply_markup=admin_keyboard(admin_id=uid))
            await state.clear()
            return

        service_start_date, expiry_date = calculate_renewal_dates(source_order, duration_days)
    else:
        service_start = now
        expiry = now + timedelta(days=duration_days)
        service_start_date = service_start.strftime("%Y-%m-%d %H:%M:%S")
        expiry_date = expiry.strftime("%Y-%m-%d %H:%M:%S")

    # اول سفارش را اتمیک قفل می‌کنیم، اما هنوز delivered نمی‌کنیم.
    # تا وقتی ارسال واقعی به کاربر موفق نشده، سفارش باید قابل برگشت باشد.
    if not claim_order_for_delivery(order_code):
        await message.answer(
            "⚠️ این سفارش در حال پردازش است یا قبلاً تحویل شده است.",
            reply_markup=admin_keyboard(admin_id=uid),
            parse_mode="HTML",
        )
        await state.clear()
        _pending_admin_configs.pop(uid, None)
        return

    is_renewal = bool(order["renewal_for_order_code"])
    if is_renewal:
        header = "🔄 <b>تمدید سرویس با موفقیت انجام شد</b>"
        extra = f"🔗 سفارش قبلی: <code>{order['renewal_for_order_code']}</code>\n"
    else:
        header = "🎉 <b>سرویس شما آماده شد</b>"
        extra = ""

    user_message = (
        f"{header}\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 سفارش: <code>{order_code}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n"
        f"📅 شروع: <b>{service_start_date}</b>\n"
        f"⏳ انقضا: <b>{expiry_date}</b>\n"
        f"{extra}\n"
        "🔐 <b>کانفیگ جدید شما:</b>\n\n"
        f"<code>{escape(config)}</code>\n\n"
        "⚠️ لطفاً کانفیگ خود را در اختیار دیگران قرار ندهید."
    )

    logger.info(
        "CONFIG DELIVERY: order=%s admin=%s customer=%s",
        order_code,
        uid,
        order["user_id"],
    )

    sent_successfully = await _send_config_to_user(
        order["user_id"],
        user_message,
        config,
        telegram_bot=message.bot,
    )

    if not sent_successfully:
        release_order_delivery_claim(order_code)
        _audit(uid, "config_delivery_failed", target=order_code)
        await message.answer(
            "❌ <b>ارسال کانفیگ به کاربر ناموفق بود.</b>\n\n"
            f"🧾 سفارش: <code>{order_code}</code>\n"
            "سفارش به حالت آماده ارسال برگشت و دوباره قابل پردازش است.",
            reply_markup=admin_keyboard(admin_id=uid),
            parse_mode="HTML"
        )
        _pending_admin_configs.pop(uid, None)
        await state.clear()
        return

    finalized = finalize_order_delivery(
        order_code,
        config,
        purchase_date,
        expiry_date,
        service_start_date,
    )
    if not finalized:
        logger.error("Config sent to user %s but order %s could not be finalized", order["user_id"], order_code)
        _audit(uid, "config_finalize_failed", target=order_code)
        await message.answer(
            "⚠️ کانفیگ به کاربر ارسال شد، اما ثبت نهایی سفارش با خطا مواجه شد. "
            "این مورد باید از لاگ بررسی شود.",
            reply_markup=admin_keyboard(admin_id=uid),
            parse_mode="HTML"
        )
        _pending_admin_configs.pop(uid, None)
        await state.clear()
        return

    if order["parent_order_code"]:
        try:
            parent_completed = complete_cart_parent_if_ready(order["parent_order_code"])
            if parent_completed:
                clear_cart(int(order["user_id"]))
                set_setting(f"cart_order:{order['parent_order_code']}", "")
        except Exception:
            logger.exception("Could not finalize cart parent %s", order["parent_order_code"])

    # پاداش خرید فقط بعد از ارسال موفق کانفیگ و ثبت نهایی سفارش پرداخت می‌شود.
    try:
        reward = None if order["parent_order_code"] else db_reward_referral_purchase(order_code, int(order["user_id"]))
        if reward:
            ref_id, amount = reward
            ok, balance = change_wallet_balance(
                ref_id,
                amount,
                "referral_purchase",
                "پاداش خرید موفق کاربر دعوت‌شده",
                order_code,
            )
            if ok:
                try:
                    await message.bot.send_message(
                        ref_id,
                        "🎁 <b>پاداش خرید دعوت‌شده</b>\n\n"
                        f"🧾 سفارش: <code>{order_code}</code>\n"
                        f"💰 مبلغ پاداش: <b>{format_money(amount)} تومان</b>\n"
                        f"💳 موجودی کیف پول: <b>{format_money(balance)} تومان</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception(
                        "Could not notify referrer %s about purchase reward for %s",
                        ref_id, order_code,
                    )
    except Exception:
        logger.exception("Referral purchase reward failed for %s", order_code)

    _audit(uid, "config_delivered", target=order_code)
    await message.answer(
        "✅ <b>کانفیگ ثبت و با موفقیت برای کاربر ارسال شد</b>\n\n"
        f"🧾 سفارش: <code>{order_code}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"⏳ انقضا: <b>{expiry_date}</b>",
        reply_markup=admin_keyboard(admin_id=uid),
        parse_mode="HTML"
    )

    _pending_admin_configs.pop(int(message.from_user.id), None)
    await state.clear()

#=========================================================
# MY SERVICES
#=========================================================

@dp.message(F.text == "📡 سرویس‌های من")
async def my_services_handler(message: Message):
    services = get_user_services(
    message.from_user.id
    )

    if not services:
        await message.answer(
        "📡 <b>سرویس‌های من</b>\n\nدر حال حاضر سرویس فعالی برای حساب شما ثبت نشده است.",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
        )
        return

    for service in services:
        status_text = (
        "🟢 فعال"
        if service["status"] == "delivered"
        else "🔴 منقضی"
        )

        text = (
        f"{status_text}\n\n"
        f"📦 {service['service_name']}\n"
        f"📊 حجم: {service['volume']}\n"
        f"🧾 سفارش: {service['order_code']}\n"
        f"📅 شروع: {service['service_start_date'] or '-'}\n"
        f"⏳ انقضا: {service['expiry_date'] or '-'}\n"
        )

        await message.answer(
        text,
        reply_markup=renewal_keyboard(
        service["order_code"]
        )
        )

#=========================================================
# MY ORDERS
#=========================================================

@dp.message(F.text == "📋 سفارش‌های من")
async def my_orders_handler(message: Message):
    orders = get_user_orders(
    message.from_user.id
    )

    if not orders:
        await message.answer(
        "📋 <b>سفارش‌های من</b>\n\nهنوز سفارشی برای حساب شما ثبت نشده است.",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
        )
        return

    lines = ["📋 <b>سوابق سفارش‌های شما</b>\n"]

    status_map = {
    "waiting_payment": "💳 در انتظار پرداخت / ارسال رسید",
    "waiting": "🔎 رسید ارسال شده — در انتظار بررسی پرداخت",
    "payment_review": "🔎 رسید ارسال شده — در انتظار بررسی پرداخت",
    "approved": "✅ پرداخت تأیید شده",
    "rejected": "❌ پرداخت رد شده",
    "delivered": "📡 تحویل شده",
    "expired": "🔴 منقضی شده",
    "cancelled": "🚫 لغو شده",
    }

    for order in orders[:30]:
        status = status_map.get(
        order["status"],
        order["status"]
        )

        lines.append(
        f"🧾 {order['order_code']}\n"
        f"📦 {order['service_name']}\n"
        f"💰 {format_money(get_order_final_price(order))} تومان\n"
        f"📌 وضعیت: {status}\n"
        )

    await message.answer(
        "\n".join(lines),
        reply_markup=main_keyboard()
    )

@dp.message(F.text == "💰 کنترل کیف پول")
async def wallet_admin_control(message: Message):
    if not _is_main_admin(message.from_user.id):
        await message.answer("❌ فقط ادمین اصلی می‌تواند وضعیت کیف پول را تغییر دهد.")
        return
    enabled = _wallet_is_enabled()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔴 غیرفعال کردن کیف پول" if enabled else "🟢 فعال کردن کیف پول",
            callback_data="wallet_admin:toggle",
        )
    ]])
    status = "🟢 فعال" if enabled else "🔴 غیرفعال"
    await message.answer(
        "💰 <b>کنترل کیف پول</b>\n\n"
        f"وضعیت فعلی: <b>{status}</b>\n\n"
        "با تغییر این گزینه، وضعیت کیف پول در دیتابیس ذخیره می‌شود و بعد از ری‌استارت هم حفظ خواهد شد.\n"
        "موجودی‌ها و تراکنش‌های قبلی حذف نمی‌شوند.",
        parse_mode="HTML",
        reply_markup=kb,
    )

@dp.callback_query(F.data == "wallet_admin:toggle")
async def wallet_admin_toggle(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    new_enabled = not _wallet_is_enabled()
    _set_wallet_enabled(new_enabled)
    status = "🟢 فعال" if new_enabled else "🔴 غیرفعال"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔴 غیرفعال کردن کیف پول" if new_enabled else "🟢 فعال کردن کیف پول",
            callback_data="wallet_admin:toggle",
        )
    ]])
    await callback.message.edit_text(
        "💰 <b>کنترل کیف پول</b>\n\n"
        f"وضعیت فعلی: <b>{status}</b>\n\n"
        "وضعیت با موفقیت ذخیره شد و با ری‌استارت ربات باقی می‌ماند.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer("وضعیت کیف پول تغییر کرد.")

#=========================================================
# WALLET / ACCOUNT
#=========================================================

def wallet_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ شارژ کیف پول", callback_data="wallet_topup")],
        [InlineKeyboardButton(text="📜 تاریخچه تراکنش‌ها", callback_data="wallet_history")],
    ])

@dp.message(F.text == "💰 کیف پول")
async def wallet_handler(message: Message, state: FSMContext):
    await state.clear()
    if not _wallet_is_enabled():
        await message.answer(
            "💰 <b>کیف پول موقتاً غیرفعال است</b>\n\n"
            "⏳ این بخش در حال بروزرسانی است. لطفاً صبور باشید و بعداً دوباره امتحان کنید.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        return
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    balance = get_wallet_balance(message.from_user.id)
    await message.answer(
        "💰 <b>کیف پول شما</b>\n\n"
        f"💵 موجودی: <b>{format_money(balance)} تومان</b>\n\n"
        "برای شارژ حساب یا مشاهده تراکنش‌ها یکی از گزینه‌های زیر را انتخاب کنید.",
        reply_markup=wallet_menu_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "👤 حساب کاربری")
async def account_handler(message: Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    balance = get_wallet_balance(message.from_user.id)
    orders = get_user_orders(message.from_user.id)
    username = f"@{message.from_user.username}" if message.from_user.username else "ندارد"
    await message.answer(
        "👤 <b>حساب کاربری</b>\n\n"
        f"🆔 آیدی: <code>{message.from_user.id}</code>\n"
        f"👤 نام: <b>{escape(message.from_user.first_name or '-')}</b>\n"
        f"🔗 یوزرنیم: <b>{escape(username)}</b>\n"
        f"💰 موجودی کیف پول: <b>{format_money(balance)} تومان</b>\n"
        f"🧾 تعداد سفارش‌ها: <b>{len(orders)}</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "wallet_topup")
async def wallet_topup_start(callback: CallbackQuery, state: FSMContext):
    if not _wallet_is_enabled():
        await callback.answer("💰 کیف پول موقتاً غیرفعال است؛ لطفاً صبور باشید.", show_alert=True)
        return
    await state.set_state(WalletState.waiting_for_amount)
    await callback.message.answer(
        "➕ <b>شارژ کیف پول</b>\n\n"
        f"حداقل مبلغ شارژ: <b>{format_money(MIN_WALLET_TOPUP)} تومان</b>\n\n"
        "مبلغ موردنظر را فقط به صورت عدد وارد کنید.",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(WalletState.waiting_for_amount)
async def wallet_topup_amount(message: Message, state: FSMContext):
    if not _wallet_is_enabled():
        await state.clear()
        await message.answer("💰 کیف پول موقتاً غیرفعال است؛ لطفاً صبور باشید.", reply_markup=main_keyboard())
        return
    raw = (message.text or "").replace(",", "").replace("٬", "").strip()
    try:
        amount = int(raw)
    except ValueError:
        await message.answer("❌ مبلغ نامعتبر است. مثال: <code>50000</code>", parse_mode="HTML")
        return
    if amount < MIN_WALLET_TOPUP:
        await message.answer(f"❌ حداقل شارژ {format_money(MIN_WALLET_TOPUP)} تومان است.")
        return
    await state.update_data(wallet_topup_amount=amount)
    await state.set_state(WalletState.waiting_for_receipt)
    await message.answer(
        "💳 <b>اطلاعات پرداخت شارژ کیف پول</b>\n\n"
        f"💰 مبلغ: <b>{format_money(amount)} تومان</b>\n"
        f"💳 شماره کارت: <code>{CARD_NUMBER}</code>\n\n"
        "📸 پس از واریز، تصویر واضح رسید را همینجا ارسال کنید.\n"
        "رسید به گروه بررسی پرداخت ارسال می‌شود و پس از تأیید، موجودی شما شارژ خواهد شد.",
        parse_mode="HTML"
    )

@dp.message(WalletState.waiting_for_receipt, F.photo)
async def wallet_topup_receipt(message: Message, state: FSMContext):
    if not _wallet_is_enabled():
        await state.clear()
        await message.answer("💰 کیف پول موقتاً غیرفعال است؛ لطفاً صبور باشید.", reply_markup=main_keyboard())
        return
    data = await state.get_data()
    amount = int(data.get("wallet_topup_amount", 0) or 0)
    if amount < MIN_WALLET_TOPUP:
        await state.clear()
        await message.answer("❌ اطلاعات شارژ معتبر نیست. دوباره از کیف پول شروع کنید.", reply_markup=main_keyboard())
        return
    file_id = message.photo[-1].file_id
    topup_id = create_wallet_topup(message.from_user.id, amount, file_id)
    text = (
        "💰 <b>درخواست شارژ کیف پول</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 درخواست: <code>{topup_id}</code>\n"
        f"👤 کاربر: <code>{message.from_user.id}</code>\n"
        f"👤 نام: <b>{escape(message.from_user.first_name or '-')}</b>\n"
        f"💵 مبلغ: <b>{format_money(amount)} تومان</b>"
    )
    try:
        await bot.send_photo(
            RECEIPT_GROUP_ID, file_id, caption=text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ تأیید شارژ", callback_data=f"wallet_approve:{topup_id}"),
                InlineKeyboardButton(text="❌ رد شارژ", callback_data=f"wallet_reject:{topup_id}"),
            ]]),
            parse_mode="HTML"
        )
    except Exception:
        logger.exception("Could not send wallet topup %s to group", topup_id)
        await message.answer("⚠️ ارسال رسید به گروه بررسی انجام نشد. رسید شما ثبت شد؛ لطفاً کمی بعد با پشتیبانی تماس بگیرید.", reply_markup=main_keyboard())
        await state.clear()
        return
    await message.answer(
        "✅ <b>درخواست شارژ ثبت شد</b>\n\n"
        f"🧾 درخواست: <code>{topup_id}</code>\n"
        f"💰 مبلغ: <b>{format_money(amount)} تومان</b>\n\n"
        "⏳ پس از تأیید رسید، موجودی کیف پول شما به‌صورت خودکار افزایش پیدا می‌کند.",
        reply_markup=main_keyboard(), parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("wallet_approve:"))
async def wallet_approve_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    topup_id = callback.data.split(":", 1)[1]
    result = process_wallet_topup(topup_id, approve=True)
    if not result:
        await callback.answer("⚠️ این درخواست قبلاً پردازش شده است.", show_alert=True)
        return
    topup = result["topup"]
    await bot.send_message(
        int(topup["user_id"]),
        "✅ <b>شارژ کیف پول تأیید شد</b>\n\n"
        f"🧾 درخواست: <code>{topup_id}</code>\n"
        f"💰 مبلغ افزوده‌شده: <b>{format_money(topup['amount'])} تومان</b>\n"
        f"💵 موجودی جدید: <b>{format_money(result['balance'])} تومان</b>",
        reply_markup=main_keyboard(), parse_mode="HTML"
    )
    await send_to_admins(
        "✅ <b>شارژ کیف پول تأیید شد</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 درخواست: <code>{topup_id}</code>\n"
        f"👤 کاربر: <code>{topup['user_id']}</code>\n"
        f"💰 مبلغ: <b>{format_money(topup['amount'])} تومان</b>\n"
        f"💵 موجودی جدید: <b>{format_money(result['balance'])} تومان</b>\n"
        f"👮 تأیید توسط: <code>{callback.from_user.id}</code>"
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ شارژ تأیید شد.")

@dp.callback_query(F.data.startswith("wallet_reject:"))
async def wallet_reject_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    topup_id = callback.data.split(":", 1)[1]
    result = process_wallet_topup(topup_id, approve=False)
    if not result:
        await callback.answer("⚠️ این درخواست قبلاً پردازش شده است.", show_alert=True)
        return
    topup = result["topup"]
    await bot.send_message(
        int(topup["user_id"]),
        "❌ <b>درخواست شارژ کیف پول رد شد</b>\n\n"
        f"🧾 درخواست: <code>{topup_id}</code>\n"
        f"💰 مبلغ: <b>{format_money(topup['amount'])} تومان</b>\n\n"
        "اگر فکر می‌کنید اشتباهی رخ داده، از بخش پشتیبانی پیام دهید.",
        reply_markup=main_keyboard(), parse_mode="HTML"
    )
    await send_to_admins(
        "❌ <b>درخواست شارژ کیف پول رد شد</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 درخواست: <code>{topup_id}</code>\n"
        f"👤 کاربر: <code>{topup['user_id']}</code>\n"
        f"💰 مبلغ: <b>{format_money(topup['amount'])} تومان</b>\n"
        f"👮 رد توسط: <code>{callback.from_user.id}</code>"
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("❌ شارژ رد شد.")

@dp.callback_query(F.data == "wallet_history")
async def wallet_history_callback(callback: CallbackQuery):
    if not _wallet_is_enabled():
        await callback.answer("💰 کیف پول موقتاً غیرفعال است؛ لطفاً صبور باشید.", show_alert=True)
        return
    txs = get_wallet_transactions(callback.from_user.id, 10)
    if not txs:
        text = "📜 <b>تاریخچه تراکنش‌ها</b>\n\nهنوز تراکنشی ثبت نشده است."
    else:
        lines = ["📜 <b>آخرین تراکنش‌ها</b>", ""]
        for tx in txs:
            sign = "+" if int(tx["amount"]) > 0 else ""
            lines.append(f"{tx['created_at']} | {tx['description']} | <b>{sign}{format_money(tx['amount'])}</b> تومان")
        text = "\n".join(lines)
    await callback.message.answer(text, reply_markup=wallet_menu_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("wallet_pay:"))
async def wallet_pay_callback(callback: CallbackQuery):
    if not _wallet_is_enabled():
        await callback.answer("💰 پرداخت با کیف پول موقتاً غیرفعال است؛ لطفاً صبور باشید.", show_alert=True)
        return
    order_code = callback.data.split(":", 1)[1]
    order = get_order(order_code)
    if not order or int(order["user_id"]) != callback.from_user.id:
        await callback.answer("❌ سفارش معتبر نیست.", show_alert=True)
        return
    if order["status"] != "waiting":
        await callback.answer("⚠️ این سفارش قبلاً پردازش شده است.", show_alert=True)
        return
    amount = get_order_final_price(order)
    balance = get_wallet_balance(callback.from_user.id)
    if balance < amount:
        await callback.answer(f"موجودی کافی نیست. موجودی: {format_money(balance)} تومان", show_alert=True)
        return
    ok, new_balance = change_wallet_balance(
        callback.from_user.id, -amount, "purchase",
        f"خرید {order['service_name']}", order_code
    )
    if not ok:
        await callback.answer("❌ پرداخت انجام نشد.", show_alert=True)
        return
    status_changed = set_order_status_if_current(order_code, "approved", "waiting")
    if not status_changed:
        current = get_order(order_code)
        if current is None or str(current["status"]) != "approved":
            try:
                change_wallet_balance(
                    callback.from_user.id, amount, "purchase_refund",
                    f"بازگشت وجه خرید ناموفق {order_code}",
                    f"refund:{order_code}",
                )
            except Exception:
                logger.exception("Wallet payment refund failed for order %s", order_code)
            await callback.answer("❌ ثبت پرداخت انجام نشد؛ مبلغ به کیف پول برگردانده شد.", show_alert=True)
            return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    kind = "🔄 تمدید سرویس" if order["renewal_for_order_code"] else "🛒 خرید جدید"
    await bot.send_message(
        callback.from_user.id,
        "✅ <b>پرداخت با کیف پول انجام شد</b>\n\n"
        f"🧾 سفارش: <code>{order_code}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"💵 مبلغ: <b>{format_money(amount)} تومان</b>\n"
        f"💰 موجودی باقی‌مانده: <b>{format_money(new_balance)} تومان</b>\n\n"
        "⏳ سفارش برای آماده‌سازی کانفیگ به مدیریت ارسال شد.",
        reply_markup=main_keyboard(), parse_mode="HTML"
    )
    admin_text = (
        "💰 <b>سفارش پرداخت‌شده با کیف پول</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 سفارش: <code>{order_code}</code>\n"
        f"👤 کاربر: <code>{order['user_id']}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n"
        f"🏷 نوع: <b>{kind}</b>\n"
        f"💰 مبلغ: <b>{format_money(amount)} تومان</b>"
    )
    await send_to_admins(admin_text, reply_markup=config_keyboard(order_code))
    await callback.answer("✅ پرداخت با کیف پول موفق بود.")

#=========================================================
# GUIDE
#=========================================================

def guide_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 چطور سرویس بخرم؟", callback_data="guide:buy")],
            [InlineKeyboardButton(text="💳 روش پرداخت", callback_data="guide:payment")],
            [InlineKeyboardButton(text="📡 نحوه استفاده از سرویس", callback_data="guide:usage")],
            [InlineKeyboardButton(text="🔄 تمدید سرویس", callback_data="guide:renewal")],
            [InlineKeyboardButton(text="💰 کیف پول", callback_data="guide:wallet")],
            [InlineKeyboardButton(text="⏳ مدت تحویل", callback_data="guide:delivery")],
            [InlineKeyboardButton(text="❓ مشکل دارم", callback_data="guide:problem")],
            [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="guide:back")],
        ]
    )


GUIDE_TEXTS = {
    "buy": (
        "🛒 <b>خرید سرویس؛ سریع و ساده</b> ✨\n\n"
        "فقط چند قدم تا ثبت سفارش فاصله داری:\n\n"
        "1️⃣ از منوی اصلی «🛒 خرید سرویس» رو بزن.\n"
        "2️⃣ سرویس موردنظرت رو انتخاب کن.\n"
        "3️⃣ اگر کد تخفیف داری، واردش کن. 🎟️\n"
        "4️⃣ مبلغ نهایی و اطلاعات پرداخت نمایش داده می‌شه.\n"
        "5️⃣ پرداخت رو انجام بده و عکس واضح رسید رو بفرست. 📸\n"
        "6️⃣ بعد از تأیید پرداخت، سفارش وارد مرحله آماده‌سازی می‌شه و کانفیگ برات ارسال خواهد شد. 🚀\n\n"
        "💡 <b>نکته:</b> قبل از ارسال رسید، مبلغ و مشخصات سفارش رو یک‌بار بررسی کن تا روند بررسی سریع‌تر انجام بشه."
    ),
    "payment": (
        "💳 <b>پرداخت چطور انجام می‌شه؟</b>\n\n"
        "بعد از انتخاب سرویس، مبلغ دقیق و اطلاعات پرداخت داخل ربات نمایش داده می‌شه.\n\n"
        "💸 مبلغ رو دقیقاً مطابق سفارش واریز کن.\n"
        "📸 بعدش یک عکس واضح و خوانا از رسید همینجا بفرست.\n"
        "🔎 رسید توسط مدیریت بررسی می‌شه و بعد از تأیید، سفارش وارد مرحله آماده‌سازی خواهد شد.\n\n"
        "⚠️ لطفاً رسید مربوط به همین سفارش رو ارسال کن تا بررسی بدون دردسر انجام بشه."
    ),
    "usage": (
        "📡 <b>بعد از خرید چطور از سرویس استفاده کنم؟</b> 🚀\n\n"
        "بعد از تأیید پرداخت و آماده شدن سرویس، اطلاعات اتصال و کانفیگ برات ارسال می‌شه.\n\n"
        "🔐 کانفیگ مخصوص سرویس خودته؛ برای حفظ امنیت، اون رو در اختیار دیگران قرار نده.\n\n"
        "📱 اگر برای وارد کردن کانفیگ یا اتصال به سرویس نیاز به راهنمایی داشتی، از «🎧 پشتیبانی» پیام بده؛ همراهت هستیم."
    ),
    "renewal": (
        "🔄 <b>تمدید سرویس؛ بدون شروع دوباره</b> ♻️\n\n"
        "از بخش «📡 سرویس‌های من» سرویس موردنظرت رو پیدا کن.\n\n"
        "اگر گزینه «🔄 تمدید سرویس» رو دیدی، با انتخابش می‌تونی تمدید رو شروع کنی. تاریخ شروع و انقضا هم برایت نمایش داده می‌شه.\n\n"
        "✨ ساده، سریع و بدون دردسر."
    ),
    "wallet": (
        "💰 <b>کیف پول</b> 💳\n\n"
        "اگر کیف پول فعال باشه، می‌تونی موجودی حسابت رو ببینی و اون رو شارژ کنی.\n\n"
        "🧾 بعد از ارسال رسید، درخواست شارژ برای بررسی مدیریت می‌ره.\n"
        "✅ پس از تأیید، مبلغ به موجودی حسابت اضافه می‌شه.\n\n"
        "ℹ️ اگر کیف پول موقتاً غیرفعال باشه، می‌تونی از خرید مستقیم سرویس استفاده کنی."
    ),
    "delivery": (
        "⏳ <b>سرویس چه زمانی تحویل می‌شه؟</b> 🚀\n\n"
        "بعد از ارسال رسید، پرداخت وارد مرحله بررسی می‌شه.\n\n"
        "⚡ <b>زمان معمول بررسی و تحویل: ۵ تا ۱۲۰ دقیقه</b>\n\n"
        "پس از تأیید پرداخت، سفارش آماده‌سازی می‌شه و کانفیگ برات ارسال خواهد شد. 📡\n\n"
        "💬 اگر بیشتر از این زمان منتظر موندی یا مشکلی پیش اومد، از «🎧 پشتیبانی» با ما در ارتباط باش."
    ),
    "problem": (
        "🆘 <b>مشکلی پیش اومده؟ خیالت راحت، همراهتیم.</b> 💙\n\n"
        "اگر هرکدوم از این موارد برات پیش اومده، از پشتیبانی کمک بگیر:\n\n"
        "💳 پرداخت کردی ولی هنوز تأیید نشده\n"
        "📡 کانفیگ کار نمی‌کنه یا در اتصال مشکل داری\n"
        "📦 سرویس یا حجم درست نمایش داده نمی‌شه\n"
        "🔄 درباره خرید یا تمدید سؤال داری\n\n"
        "🎧 روی «ارتباط با پشتیبانی» بزن و مشکلت رو برامون بنویس.\n"
        "📨 پیامت مستقیم برای تیم پشتیبانی ارسال می‌شه و در اولین فرصت پیگیری می‌کنیم."
    ),
}


def guide_detail_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎧 ارتباط با پشتیبانی", callback_data="guide:support")],
            [InlineKeyboardButton(text="⬅️ بازگشت به راهنما", callback_data="guide:menu")],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="guide:back")],
        ]
    )


@dp.message(F.text == "📚 راهنما")
async def guide_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📚 <b>راهنمای ربات</b> 🌟\n\n"
        "همه‌چیز برای یک خرید راحت و استفاده بی‌دردسر اینجاست.\n"
        "👇 موضوع موردنظرت رو انتخاب کن تا مرحله‌به‌مرحله همراهت باشیم.",
        parse_mode="HTML",
        reply_markup=guide_keyboard()
    )


@dp.callback_query(F.data == "guide:menu")
async def guide_menu_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "📚 <b>راهنمای ربات</b> 🌟\n\n"
        "همه‌چیز برای یک خرید راحت و استفاده بی‌دردسر اینجاست.\n"
        "👇 موضوع موردنظرت رو انتخاب کن تا مرحله‌به‌مرحله همراهت باشیم.",
        parse_mode="HTML",
        reply_markup=guide_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("guide:") & ~F.data.in_({"guide:menu", "guide:back", "guide:support"}))
async def guide_detail_callback(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    text = GUIDE_TEXTS.get(key)

    if not text:
        await callback.answer("❌ راهنما پیدا نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=guide_detail_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "guide:back")
async def guide_back_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "🏠 <b>به منوی اصلی برگشتیم</b>\n\n"
        "هر کاری داری، از گزینه‌های پایین انتخاب کن. ✨",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()


#=========================================================
# SUGGESTIONS
#=========================================================

@dp.message(F.text == "💡 پیشنهادات")
async def suggestion_handler(message: Message, state: FSMContext):
    await state.clear()
    suggestion_id = create_suggestion(message.from_user.id)
    await state.update_data(suggestion_id=suggestion_id)
    await state.set_state(SuggestionState.waiting_for_message)

    await message.answer(
        "💡 <b>به بخش پیشنهادات خوش آمدید</b> 🌟\n\n"
        "ایده، پیشنهاد یا انتقادت رو همینجا برامون بفرست.\n"
        "پیامت مستقیماً برای تیم مدیریت ارسال می‌شه و بررسی خواهد شد.\n\n"
        "👤 نام و شناسه کاربری شما همراه پیشنهاد ثبت می‌شه.\n"
        "📝 حالا پیام پیشنهادت رو ارسال کن.\n\n"
        "برای لغو، /cancel را بفرست.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def _send_suggestion_to_group(message: Message, suggestion_id: str):
    username = f"@{message.from_user.username}" if message.from_user.username else "ندارد"
    display_name = escape(message.from_user.full_name or message.from_user.first_name or "بدون نام")
    header = (
        "💡 <b>پیشنهاد جدید کاربر</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🎟 شماره پیشنهاد: <code>{escape(str(suggestion_id))}</code>\n"
        f"👤 نام: <b>{display_name}</b>\n"
        f"🔹 یوزرنیم: <code>{escape(username)}</code>\n"
        f"🆔 شناسه کاربر: <code>{message.from_user.id}</code>\n\n"
        "💬 <b>متن پیشنهاد:</b>\n"
    )
    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 پاسخ به کاربر", callback_data=f"suggestion_reply:{suggestion_id}")]
    ])

    if message.photo:
        caption = header + escape(message.caption or "[بدون توضیح]")
        sent = await bot.send_photo(
            SUGGESTION_GROUP_ID,
            message.photo[-1].file_id,
            caption=caption,
            reply_markup=reply_kb,
            parse_mode="HTML"
        )
    elif message.document:
        caption = header + escape(message.caption or "[فایل ارسال شده]")
        sent = await bot.send_document(
            SUGGESTION_GROUP_ID,
            message.document.file_id,
            caption=caption,
            reply_markup=reply_kb,
            parse_mode="HTML"
        )
    else:
        body = escape(message.text or message.caption or "[پیام غیرمتنی]")
        sent = await bot.send_message(
            SUGGESTION_GROUP_ID,
            header + body,
            reply_markup=reply_kb,
            parse_mode="HTML"
        )


@dp.message(SuggestionState.waiting_for_message)
async def suggestion_message_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    suggestion_id = str(data.get("suggestion_id") or "").strip()
    if not suggestion_id:
        await state.clear()
        await message.answer("⚠️ شماره پیشنهاد پیدا نشد. دوباره وارد بخش «💡 پیشنهادات» شو.", reply_markup=main_keyboard())
        return

    if not suggestion_is_open(suggestion_id):
        await state.clear()
        await message.answer("⚠️ این پیشنهاد قبلاً ثبت شده است. برای ارسال پیشنهاد جدید دوباره «💡 پیشنهادات» را بزن.", reply_markup=main_keyboard())
        return

    try:
        await _send_suggestion_to_group(message, suggestion_id)
        await state.clear()
        await message.answer(
            "✅ <b>پیشنهادت با موفقیت ارسال شد.</b> 💡\n\n"
            f"🎟 شماره پیگیری: <code>{suggestion_id}</code>\n\n"
            "ممنون که برای بهتر شدن ربات ایده‌ات رو با ما در میون گذاشتی. ❤️",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    except Exception:
        logger.exception("Could not deliver suggestion %s to group %s", suggestion_id, SUGGESTION_GROUP_ID)
        await message.answer(
            "⚠️ <b>ارسال پیشنهاد انجام نشد.</b>\n\n"
            "لطفاً چند لحظه بعد دوباره تلاش کن.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


@dp.callback_query(F.data.startswith("suggestion_reply:"))
async def suggestion_reply_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return

    suggestion_id = callback.data.split(":", 1)[1]
    suggestion = get_suggestion(suggestion_id)
    if not suggestion or suggestion["status"] != "open":
        await callback.answer("⚠️ این پیشنهاد قبلاً توسط ادمین دیگری پاسخ داده شده یا نامعتبر است.", show_alert=True)
        return

    if not claim_suggestion(suggestion_id, callback.from_user.id):
        await callback.answer("⚠️ یک ادمین دیگر در حال پاسخ به این پیشنهاد است.", show_alert=True)
        return

    # علاوه بر FSM، انتخاب ادمین را در یک نگاشت runtime نگه می‌داریم.
    # بنابراین پاسخ عادی ادمین در همین گروه، حتی بدون Reply، قابل تشخیص است.
    _pending_suggestion_replies[int(callback.from_user.id)] = {
        "suggestion_id": str(suggestion_id),
        "group_message_id": int(callback.message.message_id),
        "group_id": int(callback.message.chat.id),
    }

    await state.clear()
    await state.update_data(
        suggestion_reply_id=suggestion_id,
        suggestion_group_message_id=callback.message.message_id,
    )
    await state.set_state(SuggestionAdminReplyState.waiting_for_message)

    await callback.message.answer(
        "💬 <b>پاسخ به پیشنهاد</b>\n\n"
        f"🎟 شماره پیشنهاد: <code>{suggestion_id}</code>\n\n"
        "✍️ حالا فقط متن پاسخ را در همین گروه ارسال کن؛ نیازی به Reply کردن نیست.\n"
        "ربات پاسخ عادی ادمینی را به‌صورت خودکار برای صاحب پیشنهاد ارسال می‌کند.\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML"
    )
    await callback.answer("✏️ آماده دریافت پاسخ است.")


@dp.message(SuggestionAdminReplyState.waiting_for_message, F.text)
async def suggestion_admin_reply_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    suggestion_id = str(data.get("suggestion_reply_id") or "").strip()
    if not suggestion_id:
        await state.clear()
        await message.answer("⚠️ اطلاعات پیشنهاد پیدا نشد.", reply_markup=admin_keyboard())
        return

    suggestion = get_suggestion(suggestion_id)
    if not suggestion or suggestion["status"] != "answering" or int(suggestion["admin_id"] or 0) != int(message.from_user.id):
        await state.clear()
        await message.answer("⚠️ این پیشنهاد دیگر در دسترس شما نیست.", reply_markup=admin_keyboard())
        return

    reply_text = (message.text or "").strip()
    if not reply_text:
        await message.answer("⚠️ متن پاسخ نمی‌تواند خالی باشد.")
        return

    user_id = int(suggestion["user_id"])
    try:
        await bot.send_message(
            user_id,
            "💡 <b>پاسخ به پیشنهاد شما</b> 💙\n"
            "━━━━━━━━━━━━━━\n"
            f"🎟 شماره پیشنهاد: <code>{suggestion_id}</code>\n\n"
            f"{escape(reply_text)}",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        reopen_suggestion(suggestion_id, message.from_user.id)
        await message.answer("❌ ارسال پاسخ انجام نشد؛ پیشنهاد دوباره برای پاسخ ادمین‌ها باز شد.", reply_markup=admin_keyboard())
        return

    complete_suggestion(suggestion_id, message.from_user.id)
    _pending_suggestion_replies.pop(int(message.from_user.id), None)
    await state.clear()
    try:
        await bot.edit_message_reply_markup(
            chat_id=SUGGESTION_GROUP_ID,
            message_id=int(data.get("suggestion_group_message_id"))
        )
    except Exception:
        pass
    await message.answer(
        f"✅ پاسخ پیشنهاد <code>{suggestion_id}</code> برای کاربر ارسال شد.",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


@dp.message(
    F.chat.id == SUGGESTION_GROUP_ID,
    F.text,
)
async def suggestion_group_plain_admin_reply(message: Message, state: FSMContext):
    """
    Automatic suggestion reply fallback.

    After an admin presses «پاسخ به کاربر», the next ordinary text sent by
    that same admin in the suggestion group is treated as the reply.
    Replying to the suggestion message is NOT required.

    This handler is deliberately limited to SUGGESTION_GROUP_ID and to an
    admin that has an active pending suggestion, so ordinary admin messages
    elsewhere are never forwarded to users.
    """
    admin_id = int(message.from_user.id)
    if not db_is_admin(admin_id):
        return
    pending = _pending_suggestion_replies.get(admin_id)
    if not pending:
        return

    reply_text = (message.text or "").strip()
    if not reply_text or reply_text.startswith("/"):
        return

    suggestion_id = str(pending.get("suggestion_id") or "").strip()
    if not suggestion_id:
        _pending_suggestion_replies.pop(admin_id, None)
        return

    suggestion = get_suggestion(suggestion_id)
    if (
        not suggestion
        or suggestion["status"] != "answering"
        or int(suggestion["admin_id"] or 0) != admin_id
    ):
        _pending_suggestion_replies.pop(admin_id, None)
        return

    user_id = int(suggestion["user_id"])
    try:
        await bot.send_message(
            user_id,
            "💡 <b>پاسخ به پیشنهاد شما</b> 💙\n"
            "━━━━━━━━━━━━━━\n"
            f"🎟 شماره پیشنهاد: <code>{suggestion_id}</code>\n\n"
            f"{escape(reply_text)}",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        reopen_suggestion(suggestion_id, admin_id)
        _pending_suggestion_replies.pop(admin_id, None)
        await message.answer(
            "❌ ارسال پاسخ انجام نشد؛ پیشنهاد دوباره برای پاسخ ادمین‌ها باز شد.",
            reply_markup=admin_keyboard(),
        )
        return

    complete_suggestion(suggestion_id, admin_id)
    group_message_id = pending.get("group_message_id")
    _pending_suggestion_replies.pop(admin_id, None)

    # پاسخ‌های Reply شده و بدون Reply هر دو همین مسیر را طی می‌کنند؛
    # دکمه پاسخ پیشنهاد پس از ارسال موفق غیرفعال می‌شود.
    if group_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=SUGGESTION_GROUP_ID,
                message_id=int(group_message_id),
                reply_markup=None,
            )
        except Exception:
            pass

    await state.clear()
    await message.answer(
        f"✅ پاسخ پیشنهاد <code>{suggestion_id}</code> برای کاربر ارسال شد.",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


@dp.message(Command("cancel"), SuggestionAdminReplyState.waiting_for_message)
async def cancel_suggestion_reply(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    suggestion_id = str(data.get("suggestion_reply_id") or "").strip()
    if suggestion_id:
        reopen_suggestion(suggestion_id, message.from_user.id)
    _pending_suggestion_replies.pop(int(message.from_user.id), None)
    await state.clear()
    await message.answer("❌ پاسخ لغو شد و پیشنهاد دوباره برای ادمین‌ها قابل پاسخ است.", reply_markup=admin_keyboard())


#=========================================================
# SUPPORT
#=========================================================

@dp.message(F.text == "🎧 پشتیبانی")
async def support_handler(message: Message, state: FSMContext):
    admin_id = get_next_support_admin(ADMIN_IDS)
    ticket_id, admin_id = create_support_ticket(message.from_user.id, admin_id)

    await state.update_data(support_ticket_id=ticket_id, support_admin_id=admin_id)
    await state.set_state(SupportState.waiting_for_message)

    await message.answer(
        "🎧 <b>مرکز پشتیبانی</b> 💙\n\n"
        f"🎟 شماره پیگیری شما: <code>{ticket_id}</code>\n\n"
        "پیام، سؤال یا مشکلت رو همینجا بفرست.\n"
        "📨 پیام شما مستقیماً به کارشناس پشتیبانی مربوط می‌شه و پاسخ از همین ربات برات ارسال خواهد شد.\n\n"
        "💡 برای اینکه سریع‌تر راهنماییت کنیم، توضیحاتت رو کامل و دقیق بنویس.\n\n"
        "🔒 این شماره پیگیری رو نگه دار؛ برای ادامه گفت‌وگو بهش نیاز داری.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "guide:support")
async def guide_support_callback(callback: CallbackQuery, state: FSMContext):
    admin_id = get_next_support_admin(ADMIN_IDS)
    ticket_id, admin_id = create_support_ticket(callback.from_user.id, admin_id)

    await state.update_data(support_ticket_id=ticket_id, support_admin_id=admin_id)
    await state.set_state(SupportState.waiting_for_message)

    await callback.message.answer(
        "🎧 <b>مرکز پشتیبانی</b> 💙\n\n"
        f"🎟 شماره پیگیری شما: <code>{ticket_id}</code>\n\n"
        "پیام، سؤال یا مشکلت رو همینجا بفرست.\n"
        "📨 پیام شما مستقیماً به کارشناس پشتیبانی مربوط می‌شه و پاسخ از همین ربات برات ارسال خواهد شد.\n\n"
        "💡 برای اینکه سریع‌تر راهنماییت کنیم، توضیحاتت رو کامل و دقیق بنویس.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )
    await callback.answer()


async def deliver_support_message(message: Message, state: FSMContext, ticket_id=None):
    data = await state.get_data()
    ticket_id = str(ticket_id or data.get("support_ticket_id") or get_user_open_support_ticket(message.from_user.id) or "")

    if not ticket_id:
        admin_id = get_next_support_admin(ADMIN_IDS)
        ticket_id, admin_id = create_support_ticket(message.from_user.id, admin_id)
    else:
        ticket_data = get_support_ticket(ticket_id)
        if not ticket_data:
            admin_id = get_next_support_admin(ADMIN_IDS)
            ticket_id, admin_id = create_support_ticket(message.from_user.id, admin_id)
        else:
            user_id, admin_id = ticket_data
            if user_id != message.from_user.id or not support_ticket_is_open(ticket_id):
                await message.answer(
                    "⚠️ <b>گفت‌وگوی پشتیبانی معتبر نیست.</b>\n\n"
                    "لطفاً از بخش «🎧 پشتیبانی» یک گفت‌وگوی جدید باز کن.",
                    reply_markup=main_keyboard(),
                    parse_mode="HTML"
                )
                return

    username = f"@{message.from_user.username}" if message.from_user.username else "ندارد"
    display_name = escape(message.from_user.full_name or message.from_user.first_name or "بدون نام")
    body = escape(message.text) if message.text else "[پیام غیرمتنی]"

    admin_text = (
        "🎧 <b>پیام جدید پشتیبانی</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🎟 تیکت: <code>{ticket_id}</code>\n"
        f"👤 نام: <b>{display_name}</b>\n"
        f"🔹 یوزرنیم: <code>{escape(username)}</code>\n"
        f"🆔 آیدی: <code>{message.from_user.id}</code>\n\n"
        f"💬 پیام:\n{body}\n\n"
        "برای پاسخ، دکمه «💬 پاسخ به کاربر» را بزنید.\n"
        "برای بستن گفت‌وگو، دکمه «🔒 بستن تیکت» را بزنید."
    )

    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 بستن تیکت", callback_data=f"support_close:{ticket_id}")],
        [InlineKeyboardButton(text="💬 پاسخ به کاربر", callback_data=f"support_reply:{ticket_id}")]
    ])

    # پیام پشتیبانی برای همه ادمین‌های فعلی ارسال می‌شود؛
    # اگر یکی در دسترس نباشد، ارسال برای بقیه متوقف نمی‌شود.
    delivered_admins = []
    admin_targets = []
    for target_admin in ADMIN_IDS:
        try:
            target_admin = int(target_admin)
        except (TypeError, ValueError):
            continue
        if target_admin > 0 and target_admin not in admin_targets:
            admin_targets.append(target_admin)

    if not admin_targets and admin_id:
        try:
            admin_targets.append(int(admin_id))
        except (TypeError, ValueError):
            pass

    for target_admin in admin_targets:
        try:
            if message.photo:
                await bot.send_photo(target_admin, message.photo[-1].file_id, caption=admin_text, reply_markup=reply_kb, parse_mode="HTML")
            elif message.document:
                await bot.send_document(target_admin, message.document.file_id, caption=admin_text, reply_markup=reply_kb, parse_mode="HTML")
            else:
                await bot.send_message(target_admin, admin_text, reply_markup=reply_kb, parse_mode="HTML")
            delivered_admins.append(target_admin)
        except Exception:
            logger.exception("Could not deliver support ticket %s to admin %s", ticket_id, target_admin)

    delivered = bool(delivered_admins)

    if delivered:
        # مالک اصلی تیکت حفظ می‌شود؛ هر دو ادمین پیام و دکمه‌های پاسخ/بستن را می‌بینند.
        primary_admin = admin_id or delivered_admins[0]
        set_setting(f"support_ticket_admin_{ticket_id}", str(primary_admin))
        for target_admin in delivered_admins:
            set_setting(f"support_last_user_{target_admin}", str(message.from_user.id))
        await state.update_data(support_ticket_id=ticket_id, support_admin_id=primary_admin)
        await state.set_state(SupportState.waiting_for_message)
        await message.answer(
            "✅ <b>پیامت به پشتیبانی رسید.</b>\n\n"
            f"🎟 شماره پیگیری: <code>{ticket_id}</code>\n\n"
            "از همین گفت‌وگو می‌تونی پیام‌های بعدی رو هم بفرستی؛ پاسخ پشتیبانی برایت در همین ربات ارسال می‌شه. 💙",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "⚠️ <b>ارسال پیام به پشتیبانی موقتاً انجام نشد.</b>\n\n"
            "لطفاً چند لحظه بعد دوباره تلاش کن.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )


@dp.message(SupportState.waiting_for_message)
async def support_message_handler(message: Message, state: FSMContext):
    await deliver_support_message(message, state)


@dp.message(F.photo)
async def support_photo_handler(message: Message, state: FSMContext):
    # رسیدهای خرید که در PurchaseState هستند توسط هندلر اختصاصی خودشان گرفته می‌شوند.
    current = await state.get_state()
    if current == PurchaseState.waiting_for_receipt.state:
        return
    if get_user_open_support_ticket(message.from_user.id):
        await deliver_support_message(message, state)


@dp.callback_query(F.data.startswith("support_reply:"))
async def support_reply_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    ticket_id = callback.data.split(":", 1)[1]
    ticket = get_support_ticket(ticket_id)
    if not ticket or not support_ticket_is_open(ticket_id):
        await callback.answer("⚠️ این تیکت بسته یا نامعتبر است.", show_alert=True)
        return
    await state.clear()
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminSupportReplyState.waiting_for_message)

    await callback.message.answer(
        "💬 <b>پاسخ به کاربر</b>\n\n"
        f"🎟 تیکت: <code>{ticket_id}</code>\n\n"
        "حالا متن پاسخ را همینجا ارسال کن؛ پیام تو مستقیم برای کاربر فرستاده می‌شود. ✨\n\n"
        "برای لغو پاسخ: /cancel",
        parse_mode="HTML"
    )
    await callback.answer("✏️ آماده دریافت پاسخ است.")


@dp.message(AdminSupportReplyState.waiting_for_message, F.text)
async def admin_support_reply_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    ticket_id = str(data.get("reply_ticket_id") or "").strip()

    if not ticket_id:
        await state.clear()
        await message.answer("⚠️ اطلاعات تیکت پیدا نشد. دوباره از دکمه «💬 پاسخ به کاربر» استفاده کن.")
        return

    ticket = get_support_ticket(ticket_id)
    if not ticket or not support_ticket_is_open(ticket_id):
        await state.clear()
        await message.answer("⚠️ این تیکت پیدا نشد یا قبلاً بسته شده است.")
        return

    user_id, admin_id = ticket
    if admin_id and admin_id != message.from_user.id:
        await state.clear()
        await message.answer("⚠️ این تیکت در حال حاضر به ادمین دیگری اختصاص داده شده است.")
        return

    reply_text = (message.text or "").strip()
    if not reply_text:
        await message.answer("⚠️ متن پاسخ نمی‌تواند خالی باشد.")
        return

    try:
        await bot.send_message(
            user_id,
            "🎧 <b>پاسخ پشتیبانی</b> 💙\n"
            "━━━━━━━━━━━━━━\n"
            f"🎟 تیکت: <code>{ticket_id}</code>\n\n"
            f"{escape(reply_text)}\n\n"
            "اگر سؤال دیگری داری، همینجا پیام بده تا گفت‌وگو ادامه پیدا کنه.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        await message.answer(
            f"✅ پاسخ تیکت <code>{ticket_id}</code> برای کاربر ارسال شد.",
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )
    except Exception:
        await message.answer("❌ ارسال پاسخ انجام نشد؛ ممکن است کاربر دسترسی ربات را بسته باشد.")


@dp.message(Command("reply"))
async def admin_reply_handler(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "📝 <b>فرمت پاسخ</b>\n\n"
            "<code>/reply شماره_تیکت متن پاسخ</code>\n\n"
            "مثال:\n<code>/reply 123456 سلام، درخواست شما بررسی شد.</code>",
            parse_mode="HTML"
        )
        return

    ticket_id, reply_text = parts[1], parts[2].strip()
    ticket = get_support_ticket(ticket_id)
    if not ticket or not support_ticket_is_open(ticket_id):
        await message.answer("⚠️ این تیکت پیدا نشد یا قبلاً بسته شده است.")
        return

    user_id, admin_id = ticket
    if admin_id and admin_id != message.from_user.id:
        await message.answer("⚠️ این تیکت در حال حاضر به ادمین دیگری اختصاص داده شده است.")
        return

    try:
        await bot.send_message(
            user_id,
            "🎧 <b>پاسخ پشتیبانی</b> 💙\n"
            "━━━━━━━━━━━━━━\n"
            f"🎟 تیکت: <code>{ticket_id}</code>\n\n"
            f"{escape(reply_text)}\n\n"
            "اگر سؤال دیگری داری، همینجا پیام بده تا گفت‌وگو ادامه پیدا کنه.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        await message.answer(f"✅ پاسخ تیکت <code>{ticket_id}</code> برای کاربر ارسال شد.", parse_mode="HTML")
    except Exception:
        await message.answer("❌ ارسال پاسخ انجام نشد؛ ممکن است کاربر دسترسی ربات را بسته باشد.")


@dp.message(Command("close"))
async def admin_close_support(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("فرمت صحیح: <code>/close شماره_تیکت</code>", parse_mode="HTML")
        return
    ticket_id = parts[1]
    ticket = get_support_ticket(ticket_id)
    if not ticket or not support_ticket_is_open(ticket_id):
        await message.answer("⚠️ این تیکت پیدا نشد یا قبلاً بسته شده است.")
        return
    user_id, admin_id = ticket
    if admin_id and admin_id != message.from_user.id:
        await message.answer("⚠️ این تیکت به ادمین دیگری اختصاص داده شده است.")
        return
    close_support_ticket(ticket_id)
    try:
        await bot.send_message(
            user_id,
            "🔒 <b>گفت‌وگوی پشتیبانی بسته شد.</b>\n\n"
            f"🎟 تیکت: <code>{ticket_id}</code>\n\n"
            "اگر دوباره به کمکی نیاز داشتی، از بخش «🎧 پشتیبانی» یک گفت‌وگوی جدید باز کن.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await message.answer(f"🔒 تیکت <code>{ticket_id}</code> بسته شد.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("support_close:"))
async def support_close_button(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    ticket_id = callback.data.split(":", 1)[1]
    ticket = get_support_ticket(ticket_id)
    if not ticket or not support_ticket_is_open(ticket_id):
        await callback.answer("⚠️ این تیکت بسته یا نامعتبر است.", show_alert=True)
        return
    user_id, admin_id = ticket
    if admin_id and admin_id != callback.from_user.id:
        await callback.answer("⚠️ این تیکت به ادمین دیگری اختصاص داده شده است.", show_alert=True)
        return
    close_support_ticket(ticket_id)
    try:
        await bot.send_message(
            user_id,
            "🔒 <b>گفت‌وگوی پشتیبانی بسته شد.</b>\n\n"
            f"🎟 تیکت: <code>{ticket_id}</code>\n\n"
            "هر زمان دوباره نیاز به کمک داشتی، از بخش «🎧 پشتیبانی» استفاده کن.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("🔒 تیکت بسته شد.")


@dp.message(Command("cancel"))
async def cancel_support_reply(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    if data.get("reply_ticket_id"):
        await state.clear()
        await message.answer("❌ پاسخ لغو شد.", reply_markup=admin_keyboard())

#=========================================================
# SAVE SUPPORT USER
#=========================================================

def remember_support_user(admin_id, user_id):
    set_setting(
        f"support_last_user_{admin_id}",
        str(user_id)
    )

#=========================================================
# ADMIN NEW ORDERS
#=========================================================

@dp.message(F.text == "📦 سفارش‌های جدید")
async def admin_new_orders(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    orders = get_all_orders()

    waiting = [
    order
    for order in orders
    if order["status"] == "waiting"
    ]

    if not waiting:
        await message.answer(
        "📦 <b>سفارش جدیدی در صف نیست.</b>\n\n"
        "فعلاً موردی برای بررسی وجود نداره. ✨",
        parse_mode="HTML"
        )
        return

    for order in waiting[:30]:
        kind = "🔄 تمدید" if order["renewal_for_order_code"] else "🛒 خرید جدید"
        text = (
        "📦 <b>سفارش جدید</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 شماره: <code>{order['order_code']}</code>\n"
        f"👤 کاربر: <code>{order['user_id']}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n"
        f"🏷 نوع: <b>{kind}</b>\n"
        f"💰 مبلغ: <b>{format_money(get_order_final_price(order))} تومان</b>"
        )

        await message.answer(text, parse_mode="HTML")

#=========================================================
# ADMIN PENDING PAYMENTS
#=========================================================

@dp.message(F.text == "💳 پرداخت‌های در انتظار")
async def admin_pending_payments(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    orders = get_payment_pending_orders()

    if not orders:
        await message.answer(
        "💳 <b>پرداخت معوقی وجود ندارد.</b>\n\n"
        "در حال حاضر رسیدی برای بررسی منتظر نمانده است. ✨",
        parse_mode="HTML"
        )
        return

    for order in orders[:30]:
        kind = "🔄 تمدید" if order["renewal_for_order_code"] else "🛒 خرید جدید"
        text = (
        "💳 <b>پرداخت در انتظار بررسی</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🧾 سفارش: <code>{order['order_code']}</code>\n"
        f"👤 کاربر: <code>{order['user_id']}</code>\n"
        f"📦 سرویس: <b>{order['service_name']}</b>\n"
        f"📊 حجم: <b>{order['volume']}</b>\n"
        f"🏷 نوع: <b>{kind}</b>\n"
        f"💰 مبلغ: <b>{format_money(get_order_final_price(order))} تومان</b>"
        )

        if order["receipt_file_id"]:
            try:
                await bot.send_photo(
                message.from_user.id,
                order["receipt_file_id"],
                caption=text,
                reply_markup=admin_payment_keyboard(
                order["order_code"]
                ),
                parse_mode="HTML"
                )
            except Exception:
                await message.answer(
                text,
                reply_markup=admin_payment_keyboard(
                order["order_code"]
                ),
                parse_mode="HTML"
                )
            else:
                await message.answer(
                text,
                reply_markup=admin_payment_keyboard(
                order["order_code"]
                ),
                parse_mode="HTML"
                )

#=========================================================
# ADMIN USERS
#=========================================================

USERS_PAGE_SIZE = 10

def admin_users_page_keyboard(page: int, total_pages: int):
    rows = []

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"users:page:{page - 1}"
            )
        )
    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                text="بعدی ➡️",
                callback_data=f"users:page:{page + 1}"
            )
        )
    if navigation:
        rows.append(navigation)

    rows.append([
        InlineKeyboardButton(
            text="🔄 بروزرسانی",
            callback_data=f"users:page:{page}"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_admin_users_text(users, page: int):
    total = len(users)
    total_pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * USERS_PAGE_SIZE
    page_users = users[start:start + USERS_PAGE_SIZE]

    text = (
        "👥 <b>مدیریت کاربران</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 تعداد کل: <b>{total}</b> نفر\n"
        f"📄 صفحه: <b>{page + 1}</b> از <b>{total_pages}</b>\n\n"
    )

    if not page_users:
        text += "هنوز کاربری ثبت نشده است."
        return text, total_pages

    for index, user in enumerate(page_users, start=start + 1):
        first_name = escape(str(user["first_name"] or "-"))
        username = (
            f"@{escape(str(user['username']))}"
            if user["username"]
            else "ندارد"
        )
        user_id = escape(str(user["user_id"]))
        created_at = escape(str(user["created_at"] or "-"))

        text += (
            f"<b>{index}.</b> 👤 {first_name}\n"
            f"🆔 آیدی: <code>{user_id}</code>\n"
            f"🔗 یوزرنیم: {username}\n"
            f"📅 ثبت‌نام: {created_at}\n"
            "──────────────\n"
        )

    return text, total_pages


@dp.message(F.text == "👥 کاربران")
async def admin_users(message: Message):
    if not db_is_admin(message.from_user.id):
        return

    users = get_all_users() or []
    text, total_pages = build_admin_users_text(users, 0)

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=admin_users_page_keyboard(0, total_pages)
    )


@dp.callback_query(F.data.startswith("users:page:"))
async def admin_users_page(callback: CallbackQuery):
    if not db_is_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return

    try:
        page = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback.answer("❌ صفحه نامعتبر است.", show_alert=True)
        return

    users = get_all_users() or []
    text, total_pages = build_admin_users_text(users, page)

    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_users_page_keyboard(page, total_pages)
        )
    except TelegramBadRequest:
        pass

    await callback.answer()

#=========================================================
# ADMIN STATS
#=========================================================

@dp.message(F.text == "📊 آمار فروش")
async def admin_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    total_orders = get_order_count()
    users = get_user_count()
    waiting = get_order_count_by_status("waiting")
    delivered = get_order_count_by_status("delivered")
    expired = get_order_count_by_status("expired")
    rejected = get_order_count_by_status("rejected")
    sales = get_total_sales()

    text = (
    "📊 <b>گزارش فروش</b>\n"
    "━━━━━━━━━━━━━━\n"
    f"👥 کاربران: <b>{users}</b>\n"
    f"🧾 کل سفارش‌ها: <b>{total_orders}</b>\n"
    f"⏳ در انتظار پرداخت: <b>{waiting}</b>\n"
    f"📡 تحویل‌شده: <b>{delivered}</b>\n"
    f"🔴 منقضی‌شده: <b>{expired}</b>\n"
    f"❌ ردشده: <b>{rejected}</b>\n"
    f"💰 مجموع فروش: <b>{format_money(sales)} تومان</b>"
    )

    await message.answer(text, parse_mode="HTML")

#=========================================================
# ADMIN SEARCH
#=========================================================

@dp.message(F.text == "🔍 جستجوی سفارش")
async def admin_search_start(
    message: Message,
    state: FSMContext
):
    if message.from_user.id not in ADMIN_IDS:
        return

    # هر جستجوی قبلی را کامل می‌بندیم تا state قدیمی باعث گیر کردن منوی ادمین نشود.
    await state.clear()
    await state.set_state(AdminSearchState.waiting_for_query)

    await message.answer(
        "🔍 <b>جستجوی سفارش</b>\n\n"
        "شماره سفارش، آیدی کاربر یا نام سرویس را ارسال کنید.\n\n"
        "برای خروج، دکمه «❌ لغو جستجو» را بزنید یا یکی از گزینه‌های منوی ادمین را انتخاب کنید.",
        reply_markup=admin_search_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_search:cancel")
async def admin_search_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return

    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        "✅ جستجوی سفارش لغو شد.",
        reply_markup=admin_keyboard()
    )
    await callback.answer("جستجو لغو شد.")


def _normalize_search_text(value):
    """Normalize Persian/Arabic text and digits for reliable order searching."""
    text = str(value or "").strip().casefold()
    replacements = str.maketrans({
        "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    })
    return text.translate(replacements)


def _search_orders_fallback(query):
    """Use database search first, then a safe in-Python fallback if it returns nothing."""
    query = _normalize_search_text(query)
    if not query:
        return []

    try:
        orders = search_orders(query) or []
    except Exception:
        logger.exception("search_orders failed for query %r", query)
        orders = []

    if orders:
        return orders

    # This fallback makes the admin search independent from SQL LIKE/collation
    # differences, especially for Persian service names and Telegram user IDs.
    try:
        all_orders = get_all_orders() or []
    except Exception:
        logger.exception("Could not load orders for search fallback")
        return []

    matched = []
    for order in all_orders:
        values = (
            order["order_code"],
            order["user_id"],
            order["service_name"],
            order["volume"],
            order["status"],
        )
        if any(query in _normalize_search_text(value) for value in values):
            matched.append(order)

    return matched


@dp.message(AdminSearchState.waiting_for_query)
async def admin_search_result(
    message: Message,
    state: FSMContext
):
    if message.from_user.id not in ADMIN_IDS:
        return

    # دکمه‌های منوی ادمین در حالت جستجو نباید به عنوان عبارت جستجو مصرف شوند.
    # مخصوصاً «مدیریت سرویس‌ها» باید بلافاصله قابل انتخاب باشد.
    navigation_handlers = {
        "📡 مدیریت سرویس‌ها": admin_services,
        "📦 سفارش‌های جدید": admin_new_orders,
        "💳 پرداخت‌های در انتظار": admin_pending_payments,
    }
    menu_text = (message.text or "").strip()
    if menu_text in navigation_handlers:
        await state.clear()
        await navigation_handlers[menu_text](message)
        return

    query = (message.text or "").strip()
    if not query:
        await message.answer(
            "❌ عبارت جستجو خالی است. یک شماره سفارش، آیدی کاربر یا نام سرویس ارسال کنید.",
            reply_markup=admin_search_keyboard()
        )
        return

    try:
        orders = _search_orders_fallback(query)
    except Exception:
        logger.exception("Admin order search failed for query %r", query)
        await message.answer(
            "❌ هنگام جستجوی سفارش خطایی رخ داد. لطفاً دوباره تلاش کنید.",
            reply_markup=admin_search_keyboard()
        )
        return

    if not orders:
        await message.answer(
            "🔎 <b>نتیجه‌ای پیدا نشد</b>\n\n"
            "شماره سفارش، آیدی کاربر یا نام سرویس را بررسی کنید و دوباره بفرستید.",
            reply_markup=admin_search_keyboard(),
            parse_mode="HTML"
        )
        # جستجو را باز نگه می‌داریم تا ادمین مجبور نباشد دوباره وارد بخش جستجو شود.
        return

    await message.answer(
        f"🔎 <b>{len(orders[:30])} نتیجه پیدا شد</b>",
        parse_mode="HTML"
    )

    for order in orders[:30]:
        await message.answer(
            "🔎 <b>نتیجه سفارش</b>\n"
            f"🧾 <code>{order['order_code']}</code>\n"
            f"👤 کاربر: <code>{order['user_id']}</code>\n"
            f"📦 <b>{escape(str(order['service_name']))}</b>\n"
            f"💰 <b>{format_money(get_order_final_price(order))} تومان</b>\n"
            f"📌 وضعیت: <b>{order['status']}</b>",
            parse_mode="HTML"
        )

    # بعد از یک جستجوی موفق هم state را می‌بندیم تا منوی ادمین آزاد شود.
    await state.clear()
    await message.answer(
        "✅ جستجو تمام شد.",
        reply_markup=admin_keyboard()
    )

#=========================================================
# JALALI DATE HELPERS FOR DISCOUNTS
#=========================================================

def _gregorian_to_jalali(gy, gm, gd):
    """Convert Gregorian date to Jalali date without external dependencies."""
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += g_days_in_month[i]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        g_day_no += 1
    g_day_no += gd2
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm = 1
    while jm <= 11 and j_day_no >= j_days_in_month[jm - 1]:
        j_day_no -= j_days_in_month[jm - 1]
        jm += 1
    jd = j_day_no + 1
    return jy, jm, jd

def _jalali_to_gregorian(jy, jm, jd):
    """Convert Jalali date to Gregorian date without external dependencies."""
    if not (1 <= jm <= 12 and 1 <= jd <= 31):
        raise ValueError("تاریخ شمسی نامعتبر است.")
    jy2 = jy - 979
    j_day_no = 365 * jy2 + (jy2 // 33) * 8 + ((jy2 % 33) + 3) // 4
    for i in range(jm - 1):
        j_day_no += 31 if i < 6 else 30
    j_day_no += jd - 1
    g_day_no = j_day_no + 79
    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no %= 146097
    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False
    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no %= 365
    gm = 1
    g_month_days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    while gm <= 12 and g_day_no >= g_month_days[gm - 1]:
        g_day_no -= g_month_days[gm - 1]
        gm += 1
    gd = g_day_no + 1
    return gy, gm, gd

def format_jalali_datetime(value):
    if not value:
        return "بدون انقضا"
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        jy, jm, jd = _gregorian_to_jalali(dt.year, dt.month, dt.day)
        return f"{jy:04d}/{jm:02d}/{jd:02d} {dt:%H:%M:%S}"
    except (TypeError, ValueError):
        return str(value)

def parse_jalali_datetime(value):
    value = value.strip()
    if value == "0":
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            parts = datetime.strptime(value, fmt)
            jy, jm, jd = parts.year, parts.month, parts.day
            if jy < 1700:
                gy, gm, gd = _jalali_to_gregorian(jy, jm, jd)
                return datetime(gy, gm, gd, parts.hour, parts.minute, parts.second).strftime("%Y-%m-%d %H:%M:%S")
            return parts.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError("فرمت تاریخ نامعتبر است.")

#=========================================================
# ADMIN DISCOUNTS
#=========================================================

def discount_admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ساخت کد تخفیف", callback_data="discount_admin:create")],
        [InlineKeyboardButton(text="📋 لیست کدها", callback_data="discount_admin:list")],
    ])


def discount_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="٪ درصدی", callback_data="discount_create:type:percent"),
            InlineKeyboardButton(text="💰 مبلغ ثابت", callback_data="discount_create:type:fixed"),
        ],
        [InlineKeyboardButton(text="❌ لغو", callback_data="discount_create:cancel")],
    ])


def discount_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ ساخت کد", callback_data="discount_create:confirm"),
            InlineKeyboardButton(text="❌ لغو", callback_data="discount_create:cancel"),
        ]
    ])


def discount_edit_fields_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 مقدار تخفیف", callback_data=f"discount_edit:value:{code}")],
        [InlineKeyboardButton(text="👥 ظرفیت استفاده", callback_data=f"discount_edit:max:{code}")],
        [InlineKeyboardButton(text="⏳ تاریخ انقضا", callback_data=f"discount_edit:expiry:{code}")],
        [InlineKeyboardButton(text="🔄 تغییر وضعیت", callback_data=f"discount_toggle:{code}")],
        [InlineKeyboardButton(text="🗑 حذف کد", callback_data=f"discount_delete:{code}")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="discount_admin:list")],
    ])


def _discount_admin_card(code):
    status = "🟢 فعال" if int(code["active"]) == 1 else "🔴 غیرفعال"
    value = (
        f"{code['discount_value']}٪"
        if code["discount_type"] == "percent"
        else f"{format_money(code['discount_value'])} تومان"
    )
    max_uses = int(code["max_uses"])
    used = int(code["used_count"])
    capacity = f"{used}/{max_uses}" if max_uses > 0 else f"{used}/∞"
    expiry = format_jalali_datetime(code["expires_at"])
    return (
        f"🎟 <code>{escape(str(code['code']))}</code>\n"
        f"💸 تخفیف: <b>{value}</b>\n"
        f"👥 استفاده: <b>{capacity}</b>\n"
        f"⏳ انقضا: <b>{escape(str(expiry))}</b>\n"
        f"📌 وضعیت: <b>{status}</b>"
    )


async def _show_discount_list(message_or_callback):
    codes = get_all_discount_codes()
    if not codes:
        text = "🎟 <b>کدهای تخفیف</b>\n\nهنوز هیچ کد تخفیفی ساخته نشده است."
        markup = discount_admin_menu_keyboard()
        if isinstance(message_or_callback, CallbackQuery):
            try:
                await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            except TelegramBadRequest:
                await message_or_callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
        else:
            await message_or_callback.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.answer("📋 <b>لیست کدهای تخفیف</b>", parse_mode="HTML")
        target = message_or_callback.message
    else:
        await message_or_callback.answer("📋 <b>لیست کدهای تخفیف</b>", parse_mode="HTML")
        target = message_or_callback

    for code in codes:
        action = "🔴 غیرفعال کردن" if int(code["active"]) == 1 else "🟢 فعال کردن"
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"discount_edit:{code['code']}")],
            [InlineKeyboardButton(text="📊 آمار", callback_data=f"discount_stats:{code['code']}"),
             InlineKeyboardButton(text=action, callback_data=f"discount_toggle:{code['code']}")],
        ])
        await target.answer(_discount_admin_card(code), parse_mode="HTML", reply_markup=markup)

    await target.answer("🛠 برای ساخت کد جدید از گزینه زیر استفاده کنید.", reply_markup=discount_admin_menu_keyboard())


@dp.message(F.text == "🎟 کدهای تخفیف")
async def admin_discounts(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "🎟 <b>مدیریت کدهای تخفیف</b>\n\n"
        "از منوی زیر عملیات موردنظر را انتخاب کنید.",
        parse_mode="HTML",
        reply_markup=discount_admin_menu_keyboard(),
    )


@dp.callback_query(F.data == "discount_admin:list")
async def discount_admin_list_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await callback.answer()
    await _show_discount_list(callback)


@dp.callback_query(F.data == "discount_admin:create")
async def discount_admin_create_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await state.set_state(DiscountState.waiting_for_code)
    await state.update_data(discount_admin_action="create")
    await callback.message.answer(
        "➕ <b>ساخت کد تخفیف</b>\n\n"
        "کد موردنظر را ارسال کنید.\n"
        "مثال: <code>AMIR20</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "discount_create:cancel")
async def discount_create_cancel_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer("❌ ساخت کد لغو شد.", reply_markup=discount_admin_menu_keyboard())
    await callback.answer()


@dp.message(DiscountState.waiting_for_code, F.text)
async def discount_admin_code_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    code = _normalize_discount_input(message.text).upper()
    if not code or any(ch.isspace() for ch in code):
        await message.answer("❌ کد نامعتبر است. کد را بدون فاصله ارسال کنید.")
        return
    if get_discount_code(code):
        await message.answer("❌ این کد قبلاً وجود دارد. یک کد دیگر وارد کنید.")
        return
    await state.update_data(new_code=code)
    await state.set_state(DiscountState.waiting_for_type)
    await message.answer("نوع تخفیف را انتخاب کنید:", reply_markup=discount_type_keyboard())


@dp.callback_query(F.data.startswith("discount_create:type:"))
async def discount_create_type_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    discount_type = callback.data.rsplit(":", 1)[1]
    await state.update_data(discount_type=discount_type)
    await state.set_state(DiscountState.waiting_for_value)
    label = "درصد تخفیف را" if discount_type == "percent" else "مبلغ تخفیف را"
    await callback.message.answer(
        f"💸 {label} به صورت عددی ارسال کنید.\n"
        + ("مثال: <code>20</code>" if discount_type == "percent" else "مثال: <code>50000</code>"),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(DiscountState.waiting_for_type, F.text)
async def discount_admin_type_text_handler(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer("لطفاً نوع تخفیف را از دکمه‌های بالا انتخاب کنید.")


@dp.message(DiscountState.waiting_for_value, F.text)
async def discount_admin_value_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("❌ مقدار باید عددی باشد.")
        return
    data = await state.get_data()
    if data.get("discount_admin_action") == "edit_value":
        discount = get_discount_code(data.get("edit_code"))
        if not discount:
            await state.clear()
            await message.answer("❌ کد پیدا نشد.", reply_markup=discount_admin_menu_keyboard())
            return
        if value <= 0 or (discount["discount_type"] == "percent" and value > 100):
            await message.answer("❌ مقدار تخفیف نامعتبر است.")
            return
        update_discount_code(data["edit_code"], discount_value=value)
        await state.clear()
        await message.answer("✅ مقدار تخفیف ویرایش شد.", reply_markup=discount_admin_menu_keyboard())
        return
    if value <= 0 or (data.get("discount_type") == "percent" and value > 100):
        await message.answer("❌ مقدار تخفیف نامعتبر است.")
        return
    await state.update_data(discount_value=value)
    await state.set_state(DiscountState.waiting_for_max_uses)
    await message.answer(
        "👥 حداکثر تعداد کاربران مختلف را ارسال کنید.\n"
        "برای نامحدود، <code>0</code> بفرستید.",
        parse_mode="HTML",
    )


@dp.message(DiscountState.waiting_for_max_uses, F.text)
async def discount_admin_max_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        max_uses = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ظرفیت باید عددی باشد.")
        return
    if max_uses < 0:
        await message.answer("❌ ظرفیت نمی‌تواند منفی باشد.")
        return
    data = await state.get_data()
    if data.get("discount_admin_action") == "edit_max":
        discount = get_discount_code(data.get("edit_code"))
        if not discount:
            await state.clear()
            await message.answer("❌ کد پیدا نشد.", reply_markup=discount_admin_menu_keyboard())
            return
        update_discount_code(data["edit_code"], max_uses=max_uses)
        await state.clear()
        await message.answer("✅ ظرفیت کد ویرایش شد.", reply_markup=discount_admin_menu_keyboard())
        return
    await state.update_data(max_uses=max_uses)
    await state.set_state(DiscountState.waiting_for_expiry)
    await message.answer(
        "⏳ تاریخ انقضا را ارسال کنید.\n"
        "فرمت شمسی: <code>1405/07/08 23:59:59</code>\n"
        "برای بدون انقضا، <code>0</code> بفرستید.",
        parse_mode="HTML",
    )


@dp.message(DiscountState.waiting_for_expiry, F.text)
async def discount_admin_expiry_handler(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        expiry = parse_jalali_datetime(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ فرمت تاریخ اشتباه است.\n"
            "مثال: <code>1405/07/08 23:59:59</code>\n"
            "برای بدون انقضا <code>0</code> بفرستید.",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    if data.get("discount_admin_action") == "edit_expiry":
        discount = get_discount_code(data.get("edit_code"))
        if not discount:
            await state.clear()
            await message.answer("❌ کد پیدا نشد.", reply_markup=discount_admin_menu_keyboard())
            return
        update_discount_code(data["edit_code"], expires_at=expiry)
        await state.clear()
        await message.answer("✅ تاریخ انقضای کد ویرایش شد.", reply_markup=discount_admin_menu_keyboard())
        return

    await state.update_data(expires_at=expiry)
    display_type = f"{data['discount_value']}٪" if data["discount_type"] == "percent" else f"{format_money(data['discount_value'])} تومان"
    display_max = data["max_uses"] if data["max_uses"] > 0 else "نامحدود"
    display_expiry = format_jalali_datetime(expiry)
    await message.answer(
        "🔎 <b>بررسی اطلاعات کد</b>\n\n"
        f"🎟 کد: <code>{escape(data['new_code'])}</code>\n"
        f"💸 تخفیف: <b>{display_type}</b>\n"
        f"👥 ظرفیت: <b>{display_max}</b>\n"
        f"⏳ انقضا: <b>{escape(display_expiry)}</b>\n\n"
        "اگر اطلاعات درست است، ساخت کد را تأیید کنید.",
        parse_mode="HTML",
        reply_markup=discount_confirm_keyboard(),
    )


@dp.callback_query(F.data == "discount_create:confirm")
async def discount_create_confirm_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    required = ("new_code", "discount_type", "discount_value", "max_uses")
    if any(key not in data for key in required):
        await callback.answer("❌ اطلاعات ساخت کد ناقص است.", show_alert=True)
        return
    try:
        create_discount_code(
            data["new_code"], data["discount_type"], data["discount_value"],
            data["max_uses"], data.get("expires_at")
        )
    except sqlite3.IntegrityError:
        await callback.answer("❌ این کد قبلاً وجود دارد.", show_alert=True)
        return
    except Exception:
        logger.exception("Admin discount creation failed")
        await callback.answer("❌ ساخت کد ناموفق بود.", show_alert=True)
        return

    code = data["new_code"]
    value = data["discount_value"]
    discount_type = data["discount_type"]
    max_uses = data["max_uses"]
    expiry = data.get("expires_at")
    users = get_all_users() or []
    success = failed = 0
    discount_display = f"{value}%" if discount_type == "percent" else f"{format_money(value)} تومان"
    expiry_display = format_jalali_datetime(expiry)
    capacity_display = str(max_uses) if max_uses > 0 else "نامحدود"
    broadcast_text = (
        "🎁 <b>پیشنهاد ویژه برای شما</b>\n\n"
        f"🎟 کد تخفیف: <code>{code}</code>\n"
        f"💸 مقدار تخفیف: <b>{discount_display}</b>\n"
        f"👥 ظرفیت: <b>{capacity_display} کاربر</b>\n"
        f"⏳ اعتبار: <b>{expiry_display}</b>\n\n"
        "هر کاربر فقط یک‌بار می‌تواند از این کد استفاده کند.\n"
        "برای استفاده، هنگام خرید گزینه «🎟 کد تخفیف دارم» را انتخاب کنید و کد را ارسال کنید.\n\n"
        "✨ فرصت را از دست ندهید."
    )
    for user in users:
        try:
            await bot.send_message(user["user_id"], broadcast_text, parse_mode="HTML")
            success += 1
        except Exception:
            failed += 1
    await state.clear()
    await callback.message.answer(
        "🎉 <b>کد تخفیف با موفقیت ساخته شد.</b>\n\n"
        f"📢 اطلاع‌رسانی: {success} موفق / {failed} ناموفق",
        parse_mode="HTML",
        reply_markup=discount_admin_menu_keyboard(),
    )
    await callback.answer("✅ ساخته شد.")


@dp.callback_query(F.data.startswith("discount_toggle:"))
async def discount_toggle_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1].upper()
    discount = get_discount_code(code)
    if not discount:
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    new_active = not bool(int(discount["active"]))
    set_discount_active(code, new_active)
    await callback.answer("🟢 کد فعال شد." if new_active else "🔴 کد غیرفعال شد.")
    await callback.message.answer(
        _discount_admin_card(get_discount_code(code)),
        parse_mode="HTML",
        reply_markup=discount_edit_fields_keyboard(code),
    )


@dp.callback_query(F.data.startswith("discount_stats:"))
async def discount_stats_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1].upper()
    stats = get_discount_usage_stats(code)
    if not stats:
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    remaining = (int(stats["max_uses"]) - int(stats["used_count"])) if int(stats["max_uses"]) > 0 else None
    await callback.message.answer(
        "📊 <b>آمار کد تخفیف</b>\n\n"
        f"🎟 کد: <code>{escape(str(stats['code']))}</code>\n"
        f"👥 استفاده موفق: <b>{stats['used_count']}</b>\n"
        f"👤 کاربران یکتا: <b>{stats['unique_users']}</b>\n"
        f"📦 ظرفیت: <b>{stats['max_uses'] if int(stats['max_uses']) > 0 else 'نامحدود'}</b>\n"
        + (f"🔢 باقی‌مانده: <b>{remaining}</b>\n" if remaining is not None else "")
        + f"📌 وضعیت: <b>{'فعال' if int(stats['active']) else 'غیرفعال'}</b>",
        parse_mode="HTML",
        reply_markup=discount_edit_fields_keyboard(code),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("discount_edit:") & ~F.data.startswith("discount_edit:value:") & ~F.data.startswith("discount_edit:max:") & ~F.data.startswith("discount_edit:expiry:"))
async def discount_edit_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    await callback.message.answer(
        f"✏️ <b>ویرایش کد {escape(code)}</b>\n\nفیلد موردنظر را انتخاب کنید.",
        parse_mode="HTML",
        reply_markup=discount_edit_fields_keyboard(code),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("discount_edit:value:"))
async def discount_edit_value_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 2)[2].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    await state.set_state(DiscountState.waiting_for_value)
    await state.update_data(discount_admin_action="edit_value", edit_code=code)
    await callback.message.answer("💸 مقدار جدید تخفیف را ارسال کنید.")
    await callback.answer()


@dp.callback_query(F.data.startswith("discount_edit:max:"))
async def discount_edit_max_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 2)[2].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    await state.set_state(DiscountState.waiting_for_max_uses)
    await state.update_data(discount_admin_action="edit_max", edit_code=code)
    await callback.message.answer("👥 ظرفیت جدید را ارسال کنید. برای نامحدود <code>0</code>.", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("discount_edit:expiry:"))
async def discount_edit_expiry_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 2)[2].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    await state.set_state(DiscountState.waiting_for_expiry)
    await state.update_data(discount_admin_action="edit_expiry", edit_code=code)
    await callback.message.answer(
        "⏳ تاریخ انقضای جدید را ارسال کنید.\n"
        "فرمت شمسی: <code>1405/07/08 23:59:59</code>\n"
        "برای بدون انقضا <code>0</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("discount_delete:"))
async def discount_delete_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"discount_delete_confirm:{code}"),
            InlineKeyboardButton(text="❌ لغو", callback_data=f"discount_edit:{code}"),
        ]
    ])
    await callback.message.answer(
        f"⚠️ <b>حذف کد تخفیف</b>\n\nآیا مطمئنی کد <code>{escape(code)}</code> حذف شود؟\n\n"
        "سوابق استفاده این کد نیز حذف خواهد شد.",
        parse_mode="HTML", reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("discount_delete_confirm:"))
async def discount_delete_confirm_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1].upper()
    if delete_discount_code(code):
        await callback.message.edit_text(
            f"🗑 <b>کد تخفیف {escape(code)} با موفقیت حذف شد.</b>",
            parse_mode="HTML",
        )
        await callback.answer("✅ کد حذف شد.")
    else:
        await callback.answer("❌ کد پیدا نشد.", show_alert=True)



@dp.callback_query(F.data.startswith("discount:expire:"))
async def discount_expire_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return

    code = callback.data.split(":", 2)[2].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد تخفیف پیدا نشد.", show_alert=True)
        return

    set_discount_active(code, False)
    await callback.answer("🔴 کد منقضی شد.")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🟢 فعال کردن مجدد",
                    callback_data=f"discount:activate:{code}",
                )
            ]])
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("discount:activate:"))
async def discount_activate_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return

    code = callback.data.split(":", 2)[2].upper()
    if not get_discount_code(code):
        await callback.answer("❌ کد تخفیف پیدا نشد.", show_alert=True)
        return

    set_discount_active(code, True)
    await callback.answer("🟢 کد دوباره فعال شد.")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔴 منقضی کردن کد",
                    callback_data=f"discount:expire:{code}",
                )
            ]])
        )
    except TelegramBadRequest:
        pass


@dp.message(Command("discount"))
async def create_discount_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()

    if len(parts) < 5:
        await message.answer(
            "فرمت:\n"
            "/discount CODE TYPE VALUE MAX_USES EXPIRY\n\n"
            "TYPE: percent یا fixed\n"
            "MAX_USES: تعداد کاربران مختلف؛ برای نامحدود 0\n"
            "EXPIRY: برای بدون انقضا 0"
        )
        return

    code = parts[1].upper()
    discount_type = parts[2].lower()

    try:
        value = int(parts[3])
        max_uses = int(parts[4])
    except ValueError:
        await message.answer("❌ مقدار عددی نامعتبر است.")
        return

    if max_uses < 0:
        await message.answer("❌ تعداد استفاده نمی‌تواند منفی باشد.")
        return

    expiry = None
    if len(parts) >= 6 and parts[5] != "0":
        expiry = parts[5]
        if len(parts) >= 7:
            expiry += " " + parts[6]

    if discount_type not in ("percent", "fixed"):
        await message.answer("❌ نوع تخفیف باید percent یا fixed باشد.")
        return

    if value <= 0:
        await message.answer("❌ مقدار تخفیف باید بیشتر از صفر باشد.")
        return

    if discount_type == "percent" and value > 100:
        await message.answer("❌ درصد تخفیف نمی‌تواند بیشتر از ۱۰۰ باشد.")
        return

    try:
        if expiry:
            datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        await message.answer(
            "❌ فرمت تاریخ اشتباه است.\nفرمت صحیح: YYYY-MM-DD HH:MM:SS"
        )
        return

    try:
        create_discount_code(code, discount_type, value, max_uses, expiry)
    except sqlite3.IntegrityError:
        await message.answer("❌ این کد تخفیف قبلاً وجود دارد.")
        return

    await message.answer(
        f"🎉 <b>کد تخفیف ساخته شد</b>\n\n"
        f"🎟 کد: <code>{code}</code>\n"
        f"💸 مقدار تخفیف: <b>{value}{'٪' if discount_type == 'percent' else ' تومان'}</b>\n"
        f"👥 ظرفیت: <b>{max_uses if max_uses > 0 else 'نامحدود'} کاربر مختلف</b>\n"
        "📢 در حال اطلاع‌رسانی به کاربران...",
        parse_mode="HTML"
    )

    users = get_all_users() or []
    success = 0
    failed = 0

    discount_display = f"{value}%" if discount_type == "percent" else f"{format_money(value)} تومان"
    expiry_display = expiry if expiry else "بدون انقضا"
    capacity_display = str(max_uses) if max_uses > 0 else "نامحدود"

    broadcast_text = (
        "🎁 <b>پیشنهاد ویژه برای شما</b>\n\n"
        f"🎟 کد تخفیف: <code>{code}</code>\n"
        f"💸 مقدار تخفیف: <b>{discount_display}</b>\n"
        f"👥 ظرفیت: <b>{capacity_display} کاربر</b>\n"
        f"⏳ اعتبار: <b>{expiry_display}</b>\n\n"
        "هر کاربر فقط یک‌بار می‌تواند از این کد استفاده کند.\n"
        "برای استفاده، هنگام خرید گزینه «🎟 کد تخفیف دارم» را انتخاب کنید و کد را ارسال کنید.\n\n"
        "✨ فرصت را از دست ندهید."
    )

    for user in users:
        try:
            await bot.send_message(user["user_id"], broadcast_text, parse_mode="HTML")
            success += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1
            logger.exception("Discount broadcast failed for user %s", user["user_id"])

    await message.answer(
        "📢 <b>گزارش اطلاع‌رسانی کد تخفیف</b>\n\n"
        f"👥 کل کاربران: {len(users)}\n"
        f"✅ ارسال موفق: {success}\n"
        f"❌ ناموفق: {failed}",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Command("discount_off"))
async def discount_off_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("/discount_off CODE")
        return

    code = parts[1].upper()
    if not get_discount_code(code):
        await message.answer("❌ کد پیدا نشد.")
        return

    set_discount_active(code, False)
    await message.answer(f"🔴 کد {code} غیرفعال شد.")


#=========================================================
# ADMIN SERVICES
#=========================================================

SERVICE_PAGE_SIZE = 5


def _service_admin_db_init():
    """Create a lightweight audit table without changing database.py."""
    conn = wallet_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS service_admin_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                service_key TEXT NOT NULL,
                action TEXT NOT NULL,
                field TEXT,
                old_value TEXT,
                new_value TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _service_log(admin_id, service_key, action, field=None, old_value=None, new_value=None):
    try:
        _service_admin_db_init()
        conn = wallet_db()
        try:
            conn.execute(
                "INSERT INTO service_admin_history(admin_id,service_key,action,field,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?)",
                (int(admin_id), str(service_key), action, field,
                 None if old_value is None else str(old_value),
                 None if new_value is None else str(new_value),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not write service audit log for %s", service_key)


def _service_numeric_volume(value):
    text = str(value or "").lower().replace("٬", "").replace(",", "")
    if "نامحدود" in text or "unlimited" in text:
        return 10**12
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else -1


def _service_sort_services(services, sort_key):
    if sort_key == "price":
        return sorted(services, key=lambda x: int(x["price"]), reverse=True)
    if sort_key == "volume":
        return sorted(services, key=lambda x: _service_numeric_volume(x["volume"]), reverse=True)
    if sort_key == "duration":
        return sorted(services, key=lambda x: int(x["duration_days"]), reverse=True)
    if sort_key == "active":
        return sorted(services, key=lambda x: int(x["active"]), reverse=True)
    return sorted(services, key=lambda x: str(x["name"]).lower())


def _service_matches(service, query):
    q = str(query or "").strip().lower()
    if not q:
        return True
    values = (service["service_key"], service["name"], service["volume"])
    return any(q in str(value or "").lower() for value in values)


def service_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن سرویس", callback_data="service:add")],
            [InlineKeyboardButton(text="📋 لیست سرویس‌ها", callback_data="service:list")],
            [
                InlineKeyboardButton(text="🔎 جستجوی سرویس", callback_data="service:search"),
                InlineKeyboardButton(text="📊 آمار سرویس‌ها", callback_data="service:stats")
            ],
            [InlineKeyboardButton(text="🕘 تاریخچه تغییرات", callback_data="service:history")]
        ]
    )


def _service_list_keyboard(page, total_pages, sort_key, query=""):
    rows = []
    sort_labels = {
        "name": "🔤 نام",
        "price": "💰 قیمت",
        "volume": "📊 حجم",
        "duration": "⏳ مدت",
        "active": "🟢 وضعیت",
    }
    rows.append([
        InlineKeyboardButton(text=f"مرتب‌سازی: {sort_labels.get(sort_key, 'نام')}", callback_data="service:sort_menu")
    ])
    if query:
        rows.append([InlineKeyboardButton(text=f"🔎 جستجو: {query[:25]}", callback_data="service:search")])
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"service:page:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="service:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"service:page:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 مدیریت سرویس‌ها", callback_data="service:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _service_actions_keyboard(service):
    key = service["service_key"]
    active = int(service["active"]) == 1
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"service:edit:{key}"),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"service:delete:{key}")
        ],
        [
            InlineKeyboardButton(
                text="🔴 غیرفعال کردن" if active else "🟢 فعال کردن",
                callback_data=f"service:toggle:{key}"
            ),
            InlineKeyboardButton(text="📋 کپی", callback_data=f"service:copy:{key}")
        ],
        [InlineKeyboardButton(text="📈 آمار این سرویس", callback_data=f"service:stats:{key}")]
    ])


async def _show_service_list(target, page=0, sort_key="name", query=""):
    services = [s for s in get_all_services() if _service_matches(s, query)]
    services = _service_sort_services(services, sort_key)
    total_pages = max(1, (len(services) + SERVICE_PAGE_SIZE - 1) // SERVICE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = services[page * SERVICE_PAGE_SIZE:(page + 1) * SERVICE_PAGE_SIZE]

    if not services:
        await target.answer("❌ سرویسی مطابق جستجوی شما پیدا نشد.", reply_markup=service_admin_keyboard())
        return

    await target.answer(
        f"📋 <b>لیست سرویس‌ها</b>\n\nتعداد: <b>{len(services)}</b>\n"
        f"صفحه <b>{page+1}</b> از <b>{total_pages}</b>",
        reply_markup=_service_list_keyboard(page, total_pages, sort_key, query),
        parse_mode="HTML"
    )
    for service in chunk:
        status = "🟢 فعال" if int(service["active"]) == 1 else "🔴 غیرفعال"
        await target.answer(
            f"{status}\n"
            f"🔑 <code>{service['service_key']}</code>\n"
            f"📦 <b>{escape(str(service['name']))}</b>\n"
            f"🏷 دسته‌بندی: <b>{_service_category_label(_get_service_category(service['service_key']))}</b>\n"
            f"📊 حجم: {escape(str(service['volume']))}\n"
            f"💰 قیمت: {format_money(service['price'])} تومان\n"
            f"⏳ اعتبار: {service['duration_days']} روز",
            reply_markup=_service_actions_keyboard(service),
            parse_mode="HTML"
        )


@dp.message(F.text == "📡 مدیریت سرویس‌ها")
async def admin_services(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await message.answer("📡 <b>مدیریت سرویس‌ها</b>", reply_markup=service_admin_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "service:menu")
async def service_menu_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer("📡 <b>مدیریت سرویس‌ها</b>", reply_markup=service_admin_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "service:list")
async def service_list_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.update_data(service_list_page=0, service_list_sort="name", service_list_query="")
    await _show_service_list(callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("service:page:"))
async def service_page_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    page = int(callback.data.split(":")[2])
    data = await state.get_data()
    sort_key = data.get("service_list_sort", "name")
    query = data.get("service_list_query", "")
    await state.update_data(service_list_page=page)
    await _show_service_list(callback.message, page, sort_key, query)
    await callback.answer()


@dp.callback_query(F.data == "service:sort_menu")
async def service_sort_menu_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 نام", callback_data="service:sort:name")],
        [InlineKeyboardButton(text="💰 قیمت", callback_data="service:sort:price")],
        [InlineKeyboardButton(text="📊 حجم", callback_data="service:sort:volume")],
        [InlineKeyboardButton(text="⏳ مدت", callback_data="service:sort:duration")],
        [InlineKeyboardButton(text="🟢 وضعیت", callback_data="service:sort:active")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="service:sort_cancel")]
    ])
    await callback.message.answer("📋 مرتب‌سازی را انتخاب کنید:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("service:sort:"))
async def service_sort_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    sort_key = callback.data.split(":")[2]
    if sort_key == "cancel":
        await callback.answer()
        return
    data = await state.get_data()
    query = data.get("service_list_query", "")
    await state.update_data(service_list_page=0, service_list_sort=sort_key)
    await _show_service_list(callback.message, 0, sort_key, query)
    await callback.answer()


@dp.callback_query(F.data == "service:sort_cancel")
async def service_sort_cancel_callback(callback: CallbackQuery):
    await callback.answer("لغو شد.")


@dp.callback_query(F.data == "service:search")
async def service_search_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(ServiceSearchState.waiting_for_query)
    await callback.message.answer("🔎 نام، حجم یا service_key را برای جستجو ارسال کنید.\nبرای نمایش همه سرویس‌ها: /allservices")
    await callback.answer()


@dp.message(ServiceSearchState.waiting_for_query)
async def service_search_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    query = (message.text or "").strip()
    if query.lower() == "/allservices":
        query = ""
    data = await state.get_data()
    sort_key = data.get("service_list_sort", "name")
    await state.update_data(service_list_page=0, service_list_sort=sort_key, service_list_query=query)
    await state.set_state(None)
    await _show_service_list(message, 0, sort_key, query)


@dp.callback_query(F.data == "service:toggle_all")
async def service_toggle_all_callback(callback: CallbackQuery):
    await callback.answer("برای تغییر وضعیت، از دکمه هر سرویس استفاده کنید.", show_alert=True)


@dp.callback_query(F.data.startswith("service:toggle:"))
async def service_toggle_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    service = get_service(key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    old = int(service["active"]) == 1
    set_service_active(key, not old)
    _service_log(callback.from_user.id, key, "toggle", "active", old, not old)
    await callback.message.answer("🟢 سرویس فعال شد." if not old else "🔴 سرویس غیرفعال شد.")
    await callback.answer()


@dp.callback_query(F.data.startswith("service:stats:"))
async def service_stats_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    service = get_service(key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    orders = [o for o in get_all_orders() if (o["service_key"] == key if o["service_key"] else o["service_name"] == service["name"])]
    delivered = [o for o in orders if o["status"] == "delivered"]
    users = {o["user_id"] for o in delivered}
    revenue = sum(get_order_final_price(o) for o in delivered)
    await callback.message.answer(
        "📈 <b>آمار سرویس</b>\n\n"
        f"📦 {escape(str(service['name']))}\n"
        f"🧾 کل سفارش‌ها: <b>{len(orders)}</b>\n"
        f"✅ فروش تحویل‌شده: <b>{len(delivered)}</b>\n"
        f"👥 کاربران یکتا: <b>{len(users)}</b>\n"
        f"💰 درآمد ثبت‌شده: <b>{format_money(revenue)} تومان</b>\n"
        f"🟢 وضعیت: <b>{'فعال' if int(service['active']) else 'غیرفعال'}</b>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "service:stats")
async def service_stats_all_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    services = get_all_services()
    orders = get_all_orders()
    total_delivered = [o for o in orders if o["status"] == "delivered"]
    revenue = sum(get_order_final_price(o) for o in total_delivered)
    active = sum(1 for s in services if int(s["active"]) == 1)
    await callback.message.answer(
        "📊 <b>آمار کلی سرویس‌ها</b>\n\n"
        f"📦 تعداد سرویس‌ها: <b>{len(services)}</b>\n"
        f"🟢 فعال: <b>{active}</b>\n"
        f"🔴 غیرفعال: <b>{len(services)-active}</b>\n"
        f"🧾 فروش تحویل‌شده: <b>{len(total_delivered)}</b>\n"
        f"💰 درآمد ثبت‌شده: <b>{format_money(revenue)} تومان</b>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "service:history")
async def service_history_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    _service_admin_db_init()
    conn = wallet_db()
    try:
        rows = conn.execute("SELECT * FROM service_admin_history ORDER BY id DESC LIMIT 20").fetchall()
    finally:
        conn.close()
    if not rows:
        await callback.message.answer("🕘 هنوز تغییری در سرویس‌ها ثبت نشده است.")
        await callback.answer()
        return
    lines = ["🕘 <b>۲۰ تغییر اخیر سرویس‌ها</b>\n"]
    for row in rows:
        detail = f"{row['field']}: {row['old_value']} → {row['new_value']}" if row['field'] else ""
        lines.append(f"• <code>{row['created_at']}</code> | {row['action']} | <code>{row['service_key']}</code> {escape(detail)}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "service:noop")
async def service_noop_callback(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("service:edit:"))
async def service_edit_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    service_key = callback.data.split(":", 2)[2]
    service = get_service(service_key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    edit_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ویرایش نام", callback_data=f"service:edit_name:{service_key}")],
        [InlineKeyboardButton(text="📊 ویرایش حجم", callback_data=f"service:edit_volume:{service_key}")],
        [InlineKeyboardButton(text="💰 ویرایش قیمت", callback_data=f"service:edit_price:{service_key}")],
        [InlineKeyboardButton(text="⏳ ویرایش مدت", callback_data=f"service:edit_duration:{service_key}")],
        [InlineKeyboardButton(text="🏷 ویرایش دسته‌بندی", callback_data=f"service:edit_category:{service_key}")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="service:edit_cancel")]
    ])
    await callback.message.answer(
        "✏️ <b>ویرایش سرویس</b>\n\n"
        f"📦 سرویس: <b>{escape(str(service['name']))}</b>\n"
        f"🔑 کلید: <code>{service_key}</code>\n\nکدام مورد را می‌خواهید تغییر دهید؟",
        reply_markup=edit_keyboard, parse_mode="HTML"
    )
    await callback.answer()


async def _start_service_edit(callback: CallbackQuery, state: FSMContext, field: str, title: str):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    service_key = callback.data.split(":", 2)[2]
    service = get_service(service_key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    await state.update_data(service_key=service_key, edit_field=field)
    await state.set_state(ServiceEditState.waiting_for_value)
    current = service[field]
    current_text = f"{format_money(current)} تومان" if field == "price" else str(current)
    await callback.message.answer(
        f"✏️ <b>{title}</b>\n\nمقدار فعلی: <b>{escape(current_text)}</b>\n\nمقدار جدید را ارسال کنید.\nبرای لغو، /cancel را ارسال کنید.",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("service:edit_name:"))
async def service_edit_name_callback(callback: CallbackQuery, state: FSMContext):
    await _start_service_edit(callback, state, "name", "ویرایش نام سرویس")


@dp.callback_query(F.data.startswith("service:edit_volume:"))
async def service_edit_volume_callback(callback: CallbackQuery, state: FSMContext):
    await _start_service_edit(callback, state, "volume", "ویرایش حجم سرویس")


@dp.callback_query(F.data.startswith("service:edit_price:"))
async def service_edit_price_callback(callback: CallbackQuery, state: FSMContext):
    await _start_service_edit(callback, state, "price", "ویرایش قیمت سرویس")


@dp.callback_query(F.data.startswith("service:edit_duration:"))
async def service_edit_duration_callback(callback: CallbackQuery, state: FSMContext):
    await _start_service_edit(callback, state, "duration_days", "ویرایش مدت سرویس")


@dp.callback_query(F.data.startswith("service:edit_category:"))
async def service_edit_category_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    service_key = callback.data.split(":", 2)[2]
    service = get_service(service_key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾️ نامحدود", callback_data=f"service:set_category:{service_key}:unlimited")],
        [InlineKeyboardButton(text="🌐 آی‌پی ثابت", callback_data=f"service:set_category:{service_key}:fixed_ip")],
        [InlineKeyboardButton(text="🌍 مولتی‌لوکیشن", callback_data=f"service:set_category:{service_key}:multi_location")],
        [InlineKeyboardButton(text="📦 سایر سرویس‌ها", callback_data=f"service:set_category:{service_key}:other")],
        [InlineKeyboardButton(text="🎮 گیمینگ", callback_data=f"service:set_category:{service_key}:gaming")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="service:edit_cancel")],
    ])
    await callback.message.answer(
        f"🏷 <b>دسته‌بندی سرویس</b>\n\nدسته فعلی: <b>{_service_category_label(_get_service_category(service_key))}</b>\nدسته جدید را انتخاب کنید.",
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("service:set_category:"))
async def service_set_category_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 3)
    if len(parts) != 4 or parts[3] not in {"unlimited", "fixed_ip", "multi_location", "other", "gaming"}:
        await callback.answer("❌ دسته‌بندی نامعتبر است.", show_alert=True)
        return
    service_key, category = parts[2], parts[3]
    service = get_service(service_key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    old = _get_service_category(service_key)
    try:
        if not _set_service_category_compat(service_key, category):
            await callback.answer("❌ تغییر دسته‌بندی انجام نشد.", show_alert=True)
            return
    except Exception:
        logger.exception("Could not update service category %s", service_key)
        await callback.answer("❌ تغییر دسته‌بندی انجام نشد.", show_alert=True)
        return
    _service_log(callback.from_user.id, service_key, "update", "category", old, category)
    await callback.message.answer(
        f"✅ دسته‌بندی سرویس تغییر کرد.\n\n📦 {escape(str(service['name']))}\n🏷 {_service_category_label(category)}",
        parse_mode="HTML"
    )
    await callback.answer("دسته‌بندی ذخیره شد.")


@dp.message(ServiceEditState.waiting_for_value)
async def service_edit_value_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ ویرایش لغو شد.")
        return
    data = await state.get_data()
    service_key = data.get("service_key")
    field = data.get("edit_field")
    if not service_key or field not in {"name", "volume", "price", "duration_days"}:
        await state.clear()
        await message.answer("❌ اطلاعات ویرایش معتبر نیست.")
        return
    service = get_service(service_key)
    if not service:
        await state.clear()
        await message.answer("❌ سرویس پیدا نشد.")
        return
    if not value:
        await message.answer("❌ مقدار جدید نمی‌تواند خالی باشد.")
        return
    if field in {"price", "duration_days"}:
        try:
            new_value = int(value.replace(",", "").replace("٬", "").replace(" ", ""))
        except ValueError:
            await message.answer("❌ مقدار باید عدد باشد.")
            return
        if new_value <= 0:
            await message.answer("❌ مقدار باید بیشتر از صفر باشد.")
            return
    else:
        new_value = value
    old_value = service[field]
    try:
        update_service(service_key, **{field: new_value})
    except Exception:
        logger.exception("Could not update service %s field %s", service_key, field)
        await message.answer("❌ ویرایش سرویس انجام نشد.")
        return
    _service_log(message.from_user.id, service_key, "update", field, old_value, new_value)
    await state.clear()
    updated = get_service(service_key)
    await message.answer(
        "✅ <b>سرویس با موفقیت ویرایش شد.</b>\n\n"
        f"🔑 {service_key}\n📦 {escape(str(updated['name']))}\n📊 حجم: {escape(str(updated['volume']))}\n"
        f"💰 قیمت: {format_money(updated['price'])} تومان\n⏳ اعتبار: {updated['duration_days']} روز",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "service:edit_cancel")
async def service_edit_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("ویرایش لغو شد.")


@dp.callback_query(F.data.startswith("service:copy:"))
async def service_copy_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    service = get_service(key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    await state.update_data(copy_source_key=key)
    await state.set_state(ServiceCopyState.waiting_for_key)
    await callback.message.answer(
        "📋 <b>کپی سرویس</b>\n\n"
        f"سرویس <b>{escape(str(service['name']))}</b> آماده کپی است.\n"
        "service_key جدید را ارسال کنید.\nمثال: <code>20gb_new</code>\nبرای لغو: /cancel",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(ServiceCopyState.waiting_for_key)
async def service_copy_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    key = (message.text or "").strip()
    if key.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ کپی لغو شد.")
        return
    if not key or any(ch in key for ch in " |\n\t"):
        await message.answer("❌ service_key نامعتبر است.")
        return
    data = await state.get_data()
    source_key = data.get("copy_source_key")
    source = get_service(source_key) if source_key else None
    if not source:
        await state.clear()
        await message.answer("❌ سرویس اصلی پیدا نشد.")
        return
    if get_service(key):
        await message.answer("❌ این service_key قبلاً وجود دارد. یک کلید دیگر وارد کنید.")
        return
    try:
        create_service(
            service_key=key,
            name=source["name"],
            volume=source["volume"],
            price=int(source["price"]),
            duration_days=int(source["duration_days"]),
            active=bool(source["active"]),
            category=_get_service_category(source_key)
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ این service_key قبلاً وجود دارد.")
        return
    _service_log(message.from_user.id, key, "copy", "source_key", source_key, key)
    await state.clear()
    await message.answer(f"✅ سرویس کپی شد.\n\n🔑 کلید جدید: <code>{key}</code>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("service:delete:"))
async def service_delete_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    service_key = callback.data.split(":", 2)[2]
    service = get_service(service_key)
    if not service:
        await callback.answer("❌ این سرویس دیگر وجود ندارد.", show_alert=True)
        return
    orders = [o for o in get_all_orders() if (o["service_key"] == service_key if o["service_key"] else o["service_name"] == service["name"])]
    if orders:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 غیرفعال کردن سرویس", callback_data=f"service:toggle:{service_key}")],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="service:delete_cancel")]
        ])
        await callback.message.answer(
            "⚠️ <b>حذف امن سرویس</b>\n\n"
            f"📦 {escape(str(service['name']))}\n"
            f"🧾 این سرویس در <b>{len(orders)}</b> سفارش سابقه دارد.\n\n"
            "برای حفظ سابقه سفارش‌ها، حذف واقعی مجاز نیست؛ می‌توانید سرویس را غیرفعال کنید.",
            reply_markup=kb, parse_mode="HTML"
        )
        await callback.answer()
        return
    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"service:delete_confirm:{service_key}"),
         InlineKeyboardButton(text="❌ انصراف", callback_data="service:delete_cancel")]
    ])
    await callback.message.answer(
        "⚠️ <b>تأیید حذف سرویس</b>\n\n"
        f"📦 {escape(str(service['name']))}\n🔑 <code>{service_key}</code>\n\nآیا مطمئن هستید؟",
        reply_markup=confirm_keyboard, parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("service:delete_confirm:"))
async def service_delete_confirm_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    service_key = callback.data.split(":", 2)[2]
    service = get_service(service_key)
    if not service:
        await callback.answer("❌ سرویس پیدا نشد.", show_alert=True)
        return
    orders = [o for o in get_all_orders() if (o["service_key"] == service_key if o["service_key"] else o["service_name"] == service["name"])]
    if orders:
        await callback.answer("⚠️ این سرویس سابقه سفارش دارد و حذف نشد.", show_alert=True)
        return
    try:
        delete_service(service_key)
    except Exception:
        logger.exception("Could not delete service %s", service_key)
        await callback.answer("❌ حذف سرویس انجام نشد.", show_alert=True)
        return
    _service_log(callback.from_user.id, service_key, "delete", None, service["name"], None)
    await callback.message.answer("✅ <b>سرویس حذف شد</b>\n\n" f"📦 {escape(str(service['name']))}\n🔑 <code>{service_key}</code>", parse_mode="HTML")
    await callback.answer("✅ سرویس حذف شد.")


@dp.callback_query(F.data == "service:delete_cancel")
async def service_delete_cancel_callback(callback: CallbackQuery):
    await callback.answer("حذف لغو شد.")


async def _notify_new_service(service):
    """Create and send a persistent new-service notification."""
    category = _service_category_label(_get_service_category(service["service_key"]))
    text = (
        "✨ <b>سرویس جدید اضافه شد!</b>\n\n"
        "یک سرویس جدید به لیست خدمات اضافه شده است.\n\n"
        f"📦 حجم: <b>{escape(str(service['volume']))}</b>\n"
        f"🏷 نوع: <b>{escape(category)}</b>\n"
        f"💰 قیمت: <b>{format_money(service['price'])} تومان</b>\n"
        f"⏳ مدت: <b>{int(service['duration_days'])} روز</b>\n\n"
        "🛒 برای مشاهده و خرید، وارد بخش «خرید سرویس» شوید."
    )
    try:
        return await _create_and_send_notification("service", text)
    except Exception:
        logger.exception("Could not notify users about new service %s", service["service_key"])
        return None


@dp.callback_query(F.data == "service:add")
async def service_add_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await state.set_state(ServiceAdminState.waiting_for_category)
    await callback.message.answer(
        "➕ <b>افزودن سرویس</b>\n\n"
        "اول دسته‌بندی سرویس را انتخاب کنید:",
        reply_markup=service_category_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("service_add_category:"))
async def service_add_category_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    category = callback.data.split(":", 1)[1]
    if category not in {"unlimited", "fixed_ip", "multi_location", "other", "gaming"}:
        await callback.answer("❌ دسته‌بندی نامعتبر است.", show_alert=True)
        return
    await state.update_data(service_category=category)
    await state.set_state(ServiceAdminState.waiting_for_key)
    await callback.message.answer(
        "🔑 <b>service_key را ارسال کنید</b>\n\n"
        "مثال: <code>50gb_fixed</code>\n"
        "برای لغو: /cancel",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "service_add_cancel")
async def service_add_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("❌ افزودن سرویس لغو شد.")
    await callback.message.answer("افزودن سرویس لغو شد.", reply_markup=service_admin_keyboard())


@dp.message(ServiceAdminState.waiting_for_key)
async def service_add_key_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    key = (message.text or "").strip()
    if key.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ افزودن سرویس لغو شد.", reply_markup=service_admin_keyboard())
        return
    if not key or any(ch in key for ch in " |\n\t"):
        await message.answer("❌ service_key نامعتبر است.")
        return
    if get_service(key):
        await message.answer("❌ این service_key قبلاً وجود دارد. یک کلید دیگر ارسال کنید.")
        return
    await state.update_data(service_key=key)
    await state.set_state(ServiceAdminState.waiting_for_name)
    await message.answer("📦 نام سرویس را ارسال کنید:")


@dp.message(ServiceAdminState.waiting_for_name)
async def service_add_name_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ افزودن سرویس لغو شد.", reply_markup=service_admin_keyboard())
        return
    if not value:
        await message.answer("❌ نام سرویس نمی‌تواند خالی باشد.")
        return
    await state.update_data(service_name=value)
    await state.set_state(ServiceAdminState.waiting_for_volume)
    await message.answer("📊 حجم سرویس را ارسال کنید (مثلاً ۱۰ گیگ یا نامحدود):")


@dp.message(ServiceAdminState.waiting_for_volume)
async def service_add_volume_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ افزودن سرویس لغو شد.", reply_markup=service_admin_keyboard())
        return
    if not value:
        await message.answer("❌ حجم سرویس نمی‌تواند خالی باشد.")
        return
    await state.update_data(service_volume=value)
    await state.set_state(ServiceAdminState.waiting_for_price)
    await message.answer("💰 قیمت سرویس را به تومان ارسال کنید:")


@dp.message(ServiceAdminState.waiting_for_price)
async def service_add_price_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ افزودن سرویس لغو شد.", reply_markup=service_admin_keyboard())
        return
    try:
        price = int(value.replace(",", "").replace("٬", "").replace(" ", ""))
    except ValueError:
        await message.answer("❌ قیمت باید عدد باشد.")
        return
    if price <= 0:
        await message.answer("❌ قیمت باید بیشتر از صفر باشد.")
        return
    await state.update_data(service_price=price)
    await state.set_state(ServiceAdminState.waiting_for_duration)
    await message.answer("⏳ مدت اعتبار سرویس را به روز ارسال کنید (مثلاً ۳۰):")


@dp.message(ServiceAdminState.waiting_for_duration)
async def service_add_duration_message(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ افزودن سرویس لغو شد.", reply_markup=service_admin_keyboard())
        return
    try:
        days = int(value.replace(" ", ""))
    except ValueError:
        await message.answer("❌ مدت باید عدد باشد.")
        return
    if days <= 0:
        await message.answer("❌ مدت باید بیشتر از صفر باشد.")
        return
    data = await state.get_data()
    try:
        selected_category = data["service_category"]
        # Older database.py versions do not yet accept the gaming category.
        # Create it as a normal record first, then assign gaming directly.
        create_service(
            service_key=data["service_key"],
            name=data["service_name"],
            volume=data["service_volume"],
            price=int(data["service_price"]),
            duration_days=days,
            active=True,
            category="other" if selected_category == "gaming" else selected_category,
        )
        if selected_category == "gaming" and not _set_service_category_compat(data["service_key"], "gaming"):
            delete_service(data["service_key"])
            raise RuntimeError("Could not save gaming category")
    except sqlite3.IntegrityError:
        await message.answer("❌ این service_key قبلاً وجود دارد.")
        return
    except Exception:
        logger.exception("Could not create service %s", data.get("service_key"))
        await message.answer("❌ افزودن سرویس انجام نشد.")
        return
    category_label = _service_category_label(data["service_category"])
    created_service = get_service(data["service_key"])
    notification_stats = await _notify_new_service(created_service) if created_service else None
    await state.clear()
    await message.answer(
        "✅ <b>سرویس با موفقیت اضافه شد.</b>\n\n"
        f"📦 نام: <b>{escape(data['service_name'])}</b>\n"
        f"🔑 کلید: <code>{escape(data['service_key'])}</code>\n"
        f"🏷 دسته‌بندی: <b>{category_label}</b>\n"
        f"📊 حجم: <b>{escape(data['service_volume'])}</b>\n"
        f"💰 قیمت: <b>{format_money(data['service_price'])} تومان</b>\n"
        f"⏳ اعتبار: <b>{days} روز</b>",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Command("addservice"))
async def add_service_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    raw = message.text[len("/addservice"):].strip()
    parts = [item.strip() for item in raw.split("|")]
    if len(parts) not in (5, 6):
        await message.answer(
            "❌ فرمت اشتباه است.\n\n"
            "/addservice KEY | NAME | VOLUME | PRICE | DAYS | CATEGORY\n"
            "CATEGORY: unlimited / fixed_ip / multi_location / other / gaming"
        )
        return
    service_key, name, volume, price_text, days_text = parts[:5]
    category = parts[5].strip().lower() if len(parts) == 6 else "other"
    if category not in {"unlimited", "fixed_ip", "multi_location", "other", "gaming"}:
        await message.answer("❌ دسته‌بندی نامعتبر است. از unlimited / fixed_ip / multi_location / other / gaming استفاده کنید.")
        return
    try:
        price = int(price_text.replace(",", "").replace("٬", "").replace(" ", ""))
        days = int(days_text)
    except ValueError:
        await message.answer("❌ قیمت و تعداد روز باید عدد باشند.")
        return
    if price <= 0 or days <= 0 or not service_key or not name or not volume:
        await message.answer("❌ اطلاعات سرویس معتبر نیستند. قیمت و مدت باید بیشتر از صفر باشند.")
        return
    try:
        create_service(
            service_key=service_key,
            name=name,
            volume=volume,
            price=price,
            duration_days=days,
            active=True,
            category="other" if category == "gaming" else category,
        )
        if category == "gaming" and not _set_service_category_compat(service_key, "gaming"):
            delete_service(service_key)
            raise RuntimeError("Could not save gaming category")
    except sqlite3.IntegrityError:
        await message.answer("❌ این service_key قبلاً وجود دارد.")
        return
    _service_log(message.from_user.id, service_key, "create", "category", None, category)
    created_service = get_service(service_key)
    await _notify_new_service(created_service) if created_service else None
    await message.answer(
        f"✅ سرویس {escape(name)} اضافه شد.\n🏷 دسته‌بندی: <b>{_service_category_label(category)}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("service_on"))
async def service_on_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("/service_on KEY")
        return
    key = parts[1]
    service = get_service(key)
    if not service:
        await message.answer("❌ سرویس پیدا نشد.")
        return
    set_service_active(key, True)
    _service_log(message.from_user.id, key, "toggle", "active", service["active"], True)
    await message.answer("🟢 سرویس فعال شد.")


@dp.message(Command("service_off"))
async def service_off_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("/service_off KEY")
        return
    key = parts[1]
    service = get_service(key)
    if not service:
        await message.answer("❌ سرویس پیدا نشد.")
        return
    set_service_active(key, False)
    _service_log(message.from_user.id, key, "toggle", "active", service["active"], False)
    await message.answer("🔴 سرویس غیرفعال شد.")


@dp.message(Command("service_price"))
async def service_price_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("/service_price KEY PRICE")
        return
    key = parts[1]
    try:
        price = int(parts[2].replace(",", "").replace("٬", ""))
    except ValueError:
        await message.answer("❌ قیمت باید عدد باشد.")
        return
    service = get_service(key)
    if not service:
        await message.answer("❌ سرویس پیدا نشد.")
        return
    if price <= 0:
        await message.answer("❌ قیمت باید بیشتر از صفر باشد.")
        return
    update_service(key, price=price)
    _service_log(message.from_user.id, key, "update", "price", service["price"], price)
    await message.answer("✅ قیمت سرویس تغییر کرد.")

#=========================================================
# UPDATE NOTIFICATION
#=========================================================

@dp.message(F.text == "🔔 اطلاع‌رسانی بروزرسانی")
async def update_notification_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ فعال‌سازی و ارسال", callback_data="update_notice:confirm")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="update_notice:cancel")],
    ])
    await message.answer(
        "🔔 <b>اطلاع‌رسانی بروزرسانی</b>\n\n"
        "با تأیید، یک اطلاع‌رسانی جدید در دیتابیس ثبت می‌شود و برای هر کاربر فعلی فقط یک بار ارسال خواهد شد.\n\n"
        "⚠️ Restart یا اجرای دوباره ربات این پیام را دوباره ارسال نمی‌کند.\n\n"
        f"<blockquote>{UPDATE_NOTIFICATION_TEXT}</blockquote>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "update_notice:confirm")
async def update_notification_confirm(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    try:
        notification_id, stats = await _create_and_send_notification("update", UPDATE_NOTIFICATION_TEXT)
        total, success, failed = stats
        await callback.message.answer(
            "✅ <b>اطلاع‌رسانی بروزرسانی فعال شد.</b>\n\n"
            f"🆔 شناسه اطلاع‌رسانی: <code>{notification_id}</code>\n"
            f"👥 کاربران بررسی‌شده: {total}\n"
            f"✅ ارسال‌شده: {success}\n"
            f"❌ ناموفق: {failed}",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )
        await callback.answer("اطلاع‌رسانی ارسال شد.")
    except Exception:
        logger.exception("Could not activate update notification")
        await callback.answer("❌ فعال‌سازی انجام نشد.", show_alert=True)


@dp.callback_query(F.data == "update_notice:cancel")
async def update_notification_cancel(callback: CallbackQuery):
    await callback.answer("لغو شد.")

#=========================================================
# BROADCAST
#=========================================================

@dp.message(F.text == "📢 پیام همگانی")
async def broadcast_start(
message: Message,
state: FSMContext
):
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(
    BroadcastState.waiting_for_message
    )

    await message.answer(
    "📢 <b>پیام همگانی</b>\n\n"
        "متنی که می‌خوای برای کاربران ارسال بشه رو همینجا بفرست. ✍️",
        parse_mode="HTML"
    )

@dp.message(BroadcastState.waiting_for_message)
async def broadcast_send(
    message: Message,
    state: FSMContext
):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ متن پیام خالیه.\n\nلطفاً متن اطلاعیه رو ارسال کن تا برای کاربران فرستاده بشه.")
        return

    users = get_all_users()
    success = 0
    failed = 0

    for user in users:
        try:
            broadcast_text = (
                "📢 <b>اطلاعیه جدید</b>\n"
                "━━━━━━━━━━━━━━\n\n"
                f"{escape(text)}\n\n"
                "━━━━━━━━━━━━━━\n"
                "🌐 ممنون که همراه ما هستید.\n"
                "🎧 برای پشتیبانی، از بخش «پشتیبانی» استفاده کنید."
            )
            await bot.send_message(user["user_id"], broadcast_text, parse_mode="HTML", reply_markup=main_keyboard())
            success += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1
            logger.exception("Broadcast failed for user %s", user["user_id"])

    await message.answer(
        "📢 <b>ارسال پیام به پایان رسید</b>\n\n"
        f"👥 کل کاربران: {len(users)}\n"
        f"✅ موفق: {success}\n"
        f"❌ ناموفق: {failed}",
        reply_markup=admin_keyboard()
    )
    await state.clear()

#=========================================================
# EXPIRY LOOP
#=========================================================

async def expiry_loop():
    while True:
        try:
            now = datetime.now()
            services = get_services_for_expiry_check()

            for service in services:
                expiry_text = service["expiry_date"]

                try:
                    expiry = datetime.strptime(
                        expiry_text,
                        "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    continue

                remaining = expiry - now

                # سه روز مانده
                if (
                    remaining > timedelta(days=1)
                    and remaining <= timedelta(days=3)
                    and not service["reminder_3_sent"]
                ):
                    if not claim_reminder(service["order_code"], 3):
                        continue
                    try:
                        await bot.send_message(
                            service["user_id"],
                            "⏰ <b>یک یادآوری دوستانه درباره سرویس شما</b> 💙\n\n"
                            f"📦 سرویس: {service['service_name']}\n"
                            f"⏳ تاریخ انقضا: {service['expiry_date']}\n\n"
                            "حدود ۳ روز تا پایان اعتبار سرویس شما باقی مانده است."
                        )
                    except Exception as e:
                        reset_reminder_claim(service["order_code"], 3)
                        logger.warning(
                            "3-day reminder failed: %s",
                            e
                        )

                # کمتر از 24 ساعت
                elif (
                    remaining > timedelta(0)
                    and remaining <= timedelta(days=1)
                    and not service["reminder_1_sent"]
                ):
                    if not claim_reminder(service["order_code"], 1):
                        continue
                    try:
                        await bot.send_message(
                            service["user_id"],
                            "⏳ <b>سرویس شما به پایان اعتبار نزدیک شده</b>\n\n"
                            f"📦 سرویس: {service['service_name']}\n"
                            f"⏳ تاریخ انقضا: {service['expiry_date']}\n\n"
                            "کمتر از ۲۴ ساعت تا پایان اعتبار سرویس شما باقی مانده است."
                        )
                    except Exception as e:
                        reset_reminder_claim(service["order_code"], 1)
                        logger.warning(
                            "24-hour reminder failed: %s",
                            e
                        )

                # منقضی شده
                elif remaining <= timedelta(0):
                    if service["expired_notified"]:
                        continue

                    now_text = now.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                    scheduled_renewal = has_scheduled_renewal(
                        service["order_code"],
                        service["expiry_date"],
                        now_text
                    )

                    if scheduled_renewal:
                        mark_expired(service["order_code"])
                        claim_expired_notification(service["order_code"])
                        continue

                    if not claim_expired_notification(service["order_code"]):
                        continue
                    try:
                        await bot.send_message(
                            service["user_id"],
                            "🔴 <b>اعتبار سرویس شما به پایان رسید</b>\n\n"
                            f"📦 سرویس: {service['service_name']}\n"
                            f"⏳ تاریخ انقضا: {service['expiry_date']}\n\n"
                            "برای استفاده مجدد می‌توانید سرویس را تمدید کنید."
                        )

                        mark_expired(service["order_code"])
                    except Exception as e:
                        reset_expired_notification_claim(service["order_code"])
                        logger.warning(
                            "Expiry notification failed: %s",
                            e
                        )

        except Exception as e:
            logger.exception(
                "Expiry loop error: %s",
                e
            )

        await asyncio.sleep(900)


class ReferralSettingState(StatesGroup):
    waiting_for_invite = State()
    waiting_for_purchase = State()


def _referral_settings_keyboard(enabled: bool):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تغییر پاداش هر دعوت", callback_data="refset:invite")],
        [InlineKeyboardButton(text="✏️ تغییر پاداش هر خرید موفق", callback_data="refset:purchase")],
        [InlineKeyboardButton(
            text="🔴 غیرفعال کردن سیستم" if enabled else "🟢 فعال کردن سیستم",
            callback_data="refset:toggle",
        )],
    ])


def _referral_settings_text():
    settings = db_get_referral_settings() or {}
    invite = int(settings.get("invite_reward", 0) or 0)
    purchase = int(settings.get("purchase_reward", 0) or 0)
    enabled = str(settings.get("enabled", "1")).strip() != "0"
    text = (
        "🎁 <b>تنظیمات دعوت دوستان</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 پاداش هر دعوت جدید: <b>{format_money(invite)} تومان</b>\n"
        f"🛒 پاداش هر خرید موفق: <b>{format_money(purchase)} تومان</b>\n"
        f"📌 وضعیت سیستم: <b>{'🟢 فعال' if enabled else '🔴 غیرفعال'}</b>\n\n"
        "ℹ️ پاداش دعوت و پاداش خرید به <b>کیف پول دعوت‌کننده</b> واریز می‌شود.\n"
        "پاداش دعوت فقط برای ورود اولیه کاربر از لینک دعوت ثبت می‌شود و پاداش خرید برای هر سفارش موفقِ دعوت‌شده، فقط یک‌بار پرداخت می‌شود."
    )
    return text, enabled


@dp.callback_query(F.data.startswith("refset:"))
async def referral_setting_action(callback: CallbackQuery, state: FSMContext):
    if not db_is_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    try:
        if action == "toggle":
            current = str(db_get_referral_settings().get("enabled", "1")).strip() != "0"
            db_set_referral_setting("enabled", "0" if current else "1")
            text, enabled = _referral_settings_text()
            try:
                await callback.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=_referral_settings_keyboard(enabled),
                )
            except TelegramBadRequest:
                pass
            await callback.answer("🟢 سیستم دعوت فعال شد." if enabled else "🔴 سیستم دعوت غیرفعال شد.")
            return

        if action not in {"invite", "purchase"}:
            await callback.answer("❌ گزینه نامعتبر است.", show_alert=True)
            return

        # Always replace a previous state with the requested Referral state.
        await state.clear()
        if action == "invite":
            await state.set_state(ReferralSettingState.waiting_for_invite)
            prompt = (
                "👥 <b>مبلغ پاداش هر دعوت</b> را به تومان ارسال کنید.\n\n"
                "مثال: <code>50000</code>"
            )
        else:
            await state.set_state(ReferralSettingState.waiting_for_purchase)
            prompt = (
                "🛒 <b>مبلغ پاداش هر خرید موفق</b> را به تومان ارسال کنید.\n\n"
                "مثال: <code>100000</code>"
            )
        await callback.message.answer(prompt, parse_mode="HTML")
        await callback.answer()
    except Exception:
        logger.exception("Referral setting action failed for admin %s", callback.from_user.id)
        await callback.answer("❌ انجام عملیات ناموفق بود. خطا در لاگ ثبت شد.", show_alert=True)


def _parse_referral_amount(value):
    text = str(value or "").strip().translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )
    text = text.replace(",", "").replace("٬", "").replace("،", "").replace(" ", "")
    for unit in ("تومان", "تومن"):
        if text.endswith(unit):
            text = text[:-len(unit)]
            break
    amount = int(text)
    if amount < 0:
        raise ValueError
    return amount


@dp.message(ReferralSettingState.waiting_for_invite, F.text)
async def referral_invite_amount(message: Message, state: FSMContext):
    if not db_is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        value = _parse_referral_amount(message.text)
        db_set_referral_setting("invite_reward", str(value))
        saved = int((db_get_referral_settings() or {}).get("invite_reward", -1))
        if saved != value:
            raise RuntimeError("Referral invite reward was not persisted")
        await state.clear()
        await message.answer(
            f"✅ پاداش هر دعوت روی <b>{format_money(value)} تومان</b> تنظیم شد.\n\n"
            "💳 این مبلغ به کیف پول <b>دعوت‌کننده</b> واریز می‌شود.",
            parse_mode="HTML",
            reply_markup=admin_keyboard(is_main_admin=_is_main_admin(message.from_user.id)),
        )
    except (TypeError, ValueError):
        await message.answer("❌ مبلغ نامعتبر است. فقط عدد صفر یا بیشتر ارسال کنید.")
    except Exception:
        logger.exception("Could not save referral invite reward for admin %s", message.from_user.id)
        await message.answer("❌ ذخیره مبلغ پاداش دعوت انجام نشد؛ خطا در لاگ ثبت شد.")


@dp.message(ReferralSettingState.waiting_for_purchase, F.text)
async def referral_purchase_amount(message: Message, state: FSMContext):
    if not db_is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        value = _parse_referral_amount(message.text)
        db_set_referral_setting("purchase_reward", str(value))
        saved = int((db_get_referral_settings() or {}).get("purchase_reward", -1))
        if saved != value:
            raise RuntimeError("Referral purchase reward was not persisted")
        await state.clear()
        await message.answer(
            f"✅ پاداش هر خرید موفق روی <b>{format_money(value)} تومان</b> تنظیم شد.\n\n"
            "💳 این مبلغ به کیف پول <b>دعوت‌کننده</b> واریز می‌شود.",
            parse_mode="HTML",
            reply_markup=admin_keyboard(is_main_admin=_is_main_admin(message.from_user.id)),
        )
    except (TypeError, ValueError):
        await message.answer("❌ مبلغ نامعتبر است. فقط عدد صفر یا بیشتر ارسال کنید.")
    except Exception:
        logger.exception("Could not save referral purchase reward for admin %s", message.from_user.id)
        await message.answer("❌ ذخیره مبلغ پاداش خرید انجام نشد؛ خطا در لاگ ثبت شد.")



#=========================================================
# FALLBACK TEXT
#=========================================================

@dp.message(
F.text,
~F.text.startswith("/"),
~F.text.in_([
"🛒 خرید سرویس",
"🛒 سبد خرید",
"📡 سرویس‌های من",
"📋 سفارش‌های من",
"💰 کیف پول",
"👤 حساب کاربری",
"🎧 پشتیبانی",
"📚 راهنما",
"💡 پیشنهادات",
"📦 سفارش‌های جدید",
"💳 پرداخت‌های در انتظار",
"👥 کاربران",
"📡 مدیریت سرویس‌ها",
"📊 آمار فروش",
"🔍 جستجوی سفارش",
"🎟 کدهای تخفیف",
"📢 پیام همگانی",
"🔔 اطلاع‌رسانی بروزرسانی",
"🎁 دعوت دوستان",
"💰 کنترل کیف پول",
"📡 مانیتور اتصال",
"💾 Backup / Restore",
"📊 داشبورد مدیریتی",
])
)
async def fallback_handler(
message: Message,
state: FSMContext
):
    # Secondary admins are stored in the database and are intentionally not
    # necessarily present in the legacy ADMIN_IDS constant. Never let the
    # generic user fallback consume their messages, especially a config that
    # is being entered from the receipt group.
    if db_is_admin(int(message.from_user.id)):
        return

    await message.answer(
    "🌟 <b>بزن بریم!</b> 👋\n\n"
    "برای ادامه، یکی از گزینه‌های منوی اصلی رو انتخاب کن.\n"
    "من اینجام تا خرید، پیگیری سفارش و دریافت پشتیبانی رو برات راحت‌تر کنم. ✨",
    parse_mode="HTML",
    reply_markup=main_keyboard()
    )

#=========================================================
# ERRORS
#=========================================================

@dp.errors()
async def error_handler(event):
    logger.exception(
    "Unhandled error: %s",
    event.exception
    )


# Referral settings state handlers are registered before broad/generic FSM handlers.
# This prevents numeric amount messages from being consumed by another active state.
# User-only referral menu. It is intentionally excluded from the generic fallback above.
@dp.message(F.text == "🎁 دعوت دوستان")
async def user_referral_menu(message: Message):
    settings = db_get_referral_settings()
    if str(settings.get("enabled", "1")).strip() == "0":
        await message.answer(
            "🎁 <b>دعوت دوستان</b>\n\n"
            "سیستم دعوت دوستان موقتاً غیرفعال است.",
            reply_markup=main_keyboard(),
            parse_mode="HTML",
        )
        return

    me = await bot.get_me()
    if not me.username:
        await message.answer(
            "⚠️ لینک دعوت اختصاصی فعلاً قابل ساخت نیست.\n"
            "لطفاً کمی بعد دوباره تلاش کنید.",
            reply_markup=main_keyboard(),
        )
        return

    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    stats = db_get_referral_stats(message.from_user.id)

    invite_reward = int(settings.get("invite_reward", 0) or 0)
    purchase_reward = int(settings.get("purchase_reward", 0) or 0)

    text = (
        "🎁 <b>دعوت دوستان</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "👥 دوستات رو با لینک اختصاصی خودت به ربات دعوت کن.\n"
        "هر کاربری که برای اولین بار از لینک تو وارد ربات بشه، "
        "پاداش دعوت طبق تنظیمات مدیریت به کیف پولت اضافه می‌شه.\n"
        "اگر کاربر دعوت‌شده خرید موفق انجام بده، پاداش خرید هم جداگانه به کیف پولت اضافه می‌شه.\n\n"
        "🔗 <b>لینک دعوت شما:</b>\n"
        f'<a href="{escape(link, quote=True)}">{escape(link)}</a>\n\n'
        f"👥 تعداد دعوت‌ها: <b>{stats['invited']}</b>\n"
        f"🛒 خریدهای موفق دعوت‌شده‌ها: <b>{stats['purchases']}</b>\n"
        f"💰 مجموع پاداش دریافت‌شده: <b>{format_money(stats['rewards'])} تومان</b>\n\n"
        f"💵 پاداش هر دعوت: <b>{format_money(invite_reward)} تومان</b>\n"
        f"🛍 پاداش هر خرید موفق: <b>{format_money(purchase_reward)} تومان</b>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 باز کردن لینک دعوت", url=link)],
        ]
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

#=========================================================
# NEW ADMIN / REFERRAL / PAYMENT FEATURES
#=========================================================

PERMISSIONS = {
    "users": "👥 کاربران",
    "receipts": "🧾 رسیدها",
    "orders": "📦 سفارش‌ها",
    "payments": "💳 پرداخت‌ها",
    "services": "📡 سرویس‌ها",
    "stats": "📊 آمار",
    "discounts": "🎟 تخفیف‌ها",
    "broadcast": "📢 پیام همگانی",
    "groups": "👥 گروه‌ها",
    "support": "🎧 پشتیبانی",
    "suggestions": "💡 پیشنهادات",
    "settings": "⚙️ تنظیمات ربات",
}

def _has_perm(user_id, permission):
    if _is_main_admin(user_id):
        return True
    return db_has_admin_permission(int(user_id), permission)

def _has_config_perm(user_id):
    """Allow secondary admins with receipt OR payment access to deliver configs."""
    if _is_main_admin(user_id):
        return True
    uid = int(user_id)
    return (
        db_has_admin_permission(uid, "receipts")
        or db_has_admin_permission(uid, "payments")
    )

def _audit(admin_id, action, target=None, details=None):
    try:
        db_log_admin_action(int(admin_id), action, target, details)
    except Exception:
        logger.exception("Admin audit failed")

def _permission_keyboard(admin_id):
    perms = db_get_admin_permissions(int(admin_id))
    rows=[]
    for key,label in PERMISSIONS.items():
        enabled = bool(perms.get(key, False))
        rows.append([InlineKeyboardButton(text=f"{'🟢' if enabled else '🔴'} {label}", callback_data=f"perm:{admin_id}:{key}")])
    rows.append([InlineKeyboardButton(text="🟢 فعال کردن همه", callback_data=f"perm_all:{admin_id}:1")])
    rows.append([InlineKeyboardButton(text="🔴 غیرفعال کردن همه", callback_data=f"perm_all:{admin_id}:0")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admins:permissions")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "admins:permissions")
async def admin_permissions_list(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    rows=get_admins(); buttons=[]
    main_id = int(_main_admin_id())
    for row in rows:
        admin_id = int(row["admin_id"])
        name = escape(str(row["admin_name"] or admin_id))
        role = "👑 مدیر اصلی" if admin_id == main_id else "👤 ادمین"
        buttons.append([InlineKeyboardButton(text=f"{role} | {name}", callback_data=f"perm_user:{admin_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admins:cancel")])
    await callback.message.edit_text("🔐 <b>مدیریت دسترسی ادمین‌ها</b>\n\nادمین موردنظر را انتخاب کنید:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("perm_user:"))
async def admin_permissions_user(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True); return
    admin_id=int(callback.data.split(":",1)[1])
    await callback.message.edit_text(f"🔐 <b>دسترسی‌های ادمین {admin_id}</b>", parse_mode="HTML", reply_markup=_permission_keyboard(admin_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("perm:"))
async def admin_permission_toggle(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True); return
    _,admin_id,permission=callback.data.split(":",2)
    current=bool(db_get_admin_permissions(int(admin_id)).get(permission,False))
    db_set_admin_permission(int(admin_id),permission,not current)
    _audit(callback.from_user.id,"permission_changed",admin_id,f"{permission}={'on' if not current else 'off'}")
    await callback.message.edit_reply_markup(reply_markup=_permission_keyboard(int(admin_id)))
    await callback.answer("🟢 فعال شد." if not current else "🔴 غیرفعال شد.")

@dp.callback_query(F.data.startswith("perm_all:"))
async def admin_permission_all(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True); return
    _,admin_id,value=callback.data.split(":",2)
    enabled=bool(int(value))
    for permission in PERMISSIONS:
        db_set_admin_permission(int(admin_id),permission,enabled)
    _audit(callback.from_user.id,"permissions_bulk_changed",admin_id,"all_on" if enabled else "all_off")
    await callback.message.edit_reply_markup(reply_markup=_permission_keyboard(int(admin_id)))
    await callback.answer("همه دسترسی‌ها فعال شد." if enabled else "همه دسترسی‌ها غیرفعال شد.")

def _dashboard_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="dashboard:refresh"),
            InlineKeyboardButton(text="📈 جزئیات فروش", callback_data="dashboard:sales"),
        ],
        [
            InlineKeyboardButton(text="📡 مانیتور اتصال", callback_data="dashboard:monitor"),
        ],
    ])


def _format_uptime(delta_seconds):
    total = max(0, int(delta_seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    if not parts:
        parts.append(f"{seconds} ثانیه")
    return " و ".join(parts)


def _render_dashboard_text(stats):
    return (
        "📊 <b>داشبورد مدیریتی</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 کاربران: <b>{stats['users']:,}</b>\n"
        f"🧾 کل سفارش‌ها: <b>{stats['orders']:,}</b>\n"
        f"✅ سفارش‌های تحویل‌شده: <b>{stats['delivered']:,}</b>\n"
        f"⏳ سفارش‌های در انتظار: <b>{stats['pending']:,}</b>\n"
        f"💰 کل فروش موفق: <b>{format_money(stats['revenue'])} تومان</b>\n"
        f"📅 فروش امروز: <b>{format_money(stats['today_revenue'])} تومان</b>\n"
        f"📡 سرویس‌های فعال: <b>{stats['active_services']:,}</b>\n"
        f"🎧 تیکت‌های باز: <b>{stats['open_support']:,}</b>\n"
        f"💡 پیشنهادات باز: <b>{stats['open_suggestions']:,}</b>\n"
        f"👨‍💼 ادمین‌ها: <b>{stats['admins']:,}</b>\n"
        f"🎁 ثبت‌نام‌های Referral: <b>{stats['referrals']:,}</b>"
    )


async def _send_dashboard(target, edit=False):
    stats = get_admin_dashboard_stats()
    text = _render_dashboard_text(stats)
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=_dashboard_keyboard())
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise
    await target.answer(text, parse_mode="HTML", reply_markup=_dashboard_keyboard())


@dp.message(F.text == "📊 داشبورد مدیریتی")
async def admin_dashboard(message: Message):
    if not _has_perm(message.from_user.id, "stats"):
        return
    await _send_dashboard(message)


@dp.callback_query(F.data == "dashboard:refresh")
async def dashboard_refresh(callback: CallbackQuery):
    if not _has_perm(callback.from_user.id, "stats"):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    await _send_dashboard(callback.message, edit=True)
    await callback.answer("🔄 داشبورد بروزرسانی شد.")


@dp.callback_query(F.data == "dashboard:sales")
async def dashboard_sales(callback: CallbackQuery):
    if not _has_perm(callback.from_user.id, "stats"):
        await callback.answer("❌ دسترسی ندارید.", show_alert=True)
        return
    stats = get_admin_dashboard_stats()
    text = (
        "📈 <b>جزئیات فروش</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 کل فروش موفق: <b>{format_money(stats['revenue'])} تومان</b>\n"
        f"📅 فروش امروز: <b>{format_money(stats['today_revenue'])} تومان</b>\n"
        f"✅ تعداد فروش موفق: <b>{stats['delivered']:,}</b>\n"
        f"⏳ سفارش‌های در انتظار: <b>{stats['pending']:,}</b>\n"
        f"📦 کل سفارش‌ها: <b>{stats['orders']:,}</b>\n\n"
        "ℹ️ فروش بر اساس سفارش‌های تحویل‌شده و منقضی‌شده محاسبه می‌شود."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="dashboard:sales")],
        [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="dashboard:refresh")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as exc:
        # Telegram returns this when refresh produces exactly the same message.
        # That means the refresh itself is fine; suppress only this specific error.
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer("🔄 بروزرسانی انجام شد.")


def _mask_proxy(proxy):
    if not proxy or proxy == "ندارد":
        return "ندارد"
    try:
        parsed = urlparse(str(proxy))
        host = parsed.hostname or "نامشخص"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except Exception:
        return "تنظیم‌شده"


def _monitor_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 تست مجدد", callback_data="monitor:refresh"),
            InlineKeyboardButton(text="🩺 سلامت دیتابیس", callback_data="monitor:db"),
        ],
        [InlineKeyboardButton(text="📊 داشبورد", callback_data="dashboard:refresh")],
    ])


async def _connection_monitor_text():
    mode = _active_connection_mode or "نامشخص"
    proxy = _active_proxy_url or None
    started = time.monotonic()
    telegram_ok = False
    telegram_latency = None
    telegram_error = None
    try:
        await asyncio.wait_for(bot.get_me(), timeout=8)
        telegram_latency = (time.monotonic() - started) * 1000.0
        telegram_ok = True
    except Exception as exc:
        telegram_error = str(exc)

    vpn = _vpn_interface_present()
    uptime = _format_uptime((datetime.now() - BOT_STARTED_AT).total_seconds())

    try:
        db_health = get_database_health()
    except Exception as exc:
        db_health = {
            "ok": False,
            "integrity": f"خطا: {exc}",
            "journal_mode": "نامشخص",
            "size_mb": 0,
        }

    status = "🟢 سالم" if telegram_ok else "🔴 قطع/خطادار"
    db_status = "🟢 سالم" if db_health["ok"] else "🔴 مشکل دارد"

    text = (
        "📡 <b>مانیتور اتصال و سلامت ربات</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Telegram API: <b>{status}</b>\n"
        f"⚡ Latency: <b>{('%.0f ms' % telegram_latency) if telegram_latency is not None else 'نامشخص'}</b>\n"
        f"🌐 مسیر اتصال: <b>{escape(str(mode))}</b>\n"
        f"🔗 Proxy: <code>{escape(_mask_proxy(proxy))}</code>\n"
        f"🛡️ VPN: <b>{'🟢 شناسایی شد' if vpn else '🔴 شناسایی نشد'}</b>\n"
        f"🗄 دیتابیس: <b>{db_status}</b>\n"
        f"💽 حجم دیتابیس: <b>{db_health['size_mb']:.2f} MB</b>\n"
        f"📚 Journal: <b>{escape(str(db_health['journal_mode']))}</b>\n"
        f"⏱ زمان اجرای ربات: <b>{escape(uptime)}</b>"
    )
    if telegram_error:
        text += f"\n\n⚠️ خطای Telegram: <code>{escape(telegram_error[:500])}</code>"
    return text


@dp.message(F.text == "📡 مانیتور اتصال")
async def connection_monitor(message: Message):
    if not _is_main_admin(message.from_user.id):
        return
    await message.answer(await _connection_monitor_text(), parse_mode="HTML", reply_markup=_monitor_keyboard())


@dp.callback_query(F.data == "monitor:refresh")
async def connection_monitor_refresh(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            await _connection_monitor_text(),
            parse_mode="HTML",
            reply_markup=_monitor_keyboard(),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer("🔄 وضعیت دوباره بررسی شد.")


@dp.callback_query(F.data == "dashboard:monitor")
async def dashboard_monitor(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    await callback.message.edit_text(
        await _connection_monitor_text(),
        parse_mode="HTML",
        reply_markup=_monitor_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor:db")
async def connection_monitor_db(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    health = get_database_health()
    text = (
        "🩺 <b>سلامت دیتابیس</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"وضعیت: <b>{'🟢 سالم' if health['ok'] else '🔴 مشکل دارد'}</b>\n"
        f"Integrity Check: <code>{escape(str(health['integrity']))}</code>\n"
        f"Journal Mode: <b>{escape(str(health['journal_mode']))}</b>\n"
        f"حجم فایل: <b>{health['size_mb']:.2f} MB</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بررسی مجدد", callback_data="monitor:db")],
        [InlineKeyboardButton(text="🔙 مانیتور اتصال", callback_data="monitor:refresh")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as exc:
        # Telegram returns this when refresh produces exactly the same message.
        # That means the refresh itself is fine; suppress only this specific error.
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer("🔄 بررسی مجدد انجام شد.")


#=========================================================
@dp.callback_query(F.data.startswith("invoice:"))
async def invoice_callback(callback:CallbackQuery):
    order=get_order(callback.data.split(":",1)[1])
    if not order or int(order["user_id"])!=int(callback.from_user.id): await callback.answer("❌ فاکتور پیدا نشد.",show_alert=True); return
    await callback.message.answer(f"🧾 <b>فاکتور خرید</b>\n━━━━━━━━━━━━━━\n🧾 سفارش: <code>{order['order_code']}</code>\n📦 سرویس: <b>{escape(str(order['service_name']))}</b>\n💰 قیمت: <b>{format_money(order['price'])} تومان</b>\n🎟 تخفیف: <b>{format_money(order['discount_amount'])} تومان</b>\n💵 مبلغ نهایی: <b>{format_money(get_order_final_price(order))} تومان</b>\n📅 خرید: {order['purchase_date'] or '-'}\n⏳ انقضا: {order['expiry_date'] or '-'}",parse_mode="HTML")
    await callback.answer()

def _backup_files():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(
        [
            p for p in BACKUP_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in {".zip", ".db"}
            and (p.name.startswith("bot_backup_") or p.name.startswith("bot_backup"))
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _backup_label(path):
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return f"{path.name} | {size_mb:.2f} MB | {stamp}"
    except OSError:
        return path.name


def _backup_keyboard():
    auto_enabled = str(get_setting("backup_auto_enabled", "1")).strip().lower() not in {"0", "false", "off", "no"}
    auto_text = "🟢 بکاپ خودکار: روشن" if auto_enabled else "🔴 بکاپ خودکار: خاموش"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 ساخت Backup", callback_data="backup:create")],
        [InlineKeyboardButton(text="📋 لیست Backupها", callback_data="backup:list")],
        [InlineKeyboardButton(text="📥 Restore", callback_data="backup:restore")],
        [InlineKeyboardButton(text=auto_text, callback_data="backup:auto")],
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="backup:menu")],
    ])


async def _create_database_backup():
    """Create a complete backup of both the main DB and the separate wallet DB."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = BACKUP_DIR / f"bot_backup_{stamp}.zip"
    source_path = Path(str(DB_NAME))
    wallet_path = Path(str(WALLET_DB))
    temp_dir = BACKUP_DIR / f".backup_{stamp}_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    main_copy = temp_dir / "bot_database.db"
    wallet_copy = temp_dir / "wallet.db"

    def _sqlite_backup(source_path, destination):
        if not source_path.exists():
            raise FileNotFoundError(f"Database not found: {source_path}")
        src = sqlite3.connect(str(source_path), timeout=30)
        dest = sqlite3.connect(str(destination), timeout=30)
        try:
            src.backup(dest)
            integrity = dest.execute("PRAGMA integrity_check").fetchone()[0]
            if str(integrity).lower() != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
            dest.commit()
        finally:
            dest.close()
            src.close()

    try:
        await asyncio.to_thread(_sqlite_backup, source_path, main_copy)
        if wallet_path.exists():
            await asyncio.to_thread(_sqlite_backup, wallet_path, wallet_copy)
        else:
            # Keep the archive structurally complete even before wallet.db exists.
            sqlite3.connect(str(wallet_copy)).close()

        def _make_zip():
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(main_copy, "bot_database.db")
                zf.write(wallet_copy, "wallet.db")
                zf.writestr("backup_format.txt", "AurthorVless complete backup v2\\n")

        await asyncio.to_thread(_make_zip)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    files = _backup_files()
    for old in files[BACKUP_KEEP_COUNT:]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Could not remove old backup %s", old)

    return destination


def _restore_database_sync(source_path):
    """Restore both databases from a new ZIP backup, while retaining legacy .db restore."""
    source_path = Path(source_path)
    target_path = Path(str(DB_NAME))
    wallet_path = Path(str(WALLET_DB))
    if not source_path.exists():
        raise FileNotFoundError("Backup file not found.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safety_main = target_path.with_name(target_path.name + f".before_restore_{stamp}")
    safety_wallet = wallet_path.with_name(wallet_path.name + f".before_restore_{stamp}")

    if source_path.suffix.lower() == ".zip":
        temp_dir = BACKUP_DIR / f".restore_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(source_path, "r") as zf:
                names = set(zf.namelist())
                if "bot_database.db" not in names or "wallet.db" not in names:
                    raise RuntimeError("این فایل Backup کامل ربات نیست.")
                zf.extract("bot_database.db", temp_dir)
                zf.extract("wallet.db", temp_dir)

            extracted_main = temp_dir / "bot_database.db"
            extracted_wallet = temp_dir / "wallet.db"

            def _check(path, required_tables=()):
                conn = sqlite3.connect(str(path), timeout=30)
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if str(integrity).lower() != "ok":
                        raise RuntimeError(f"Backup integrity check failed: {integrity}")
                    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    missing = set(required_tables) - tables
                    if missing:
                        raise RuntimeError(f"Backup missing required tables: {', '.join(sorted(missing))}")
                finally:
                    conn.close()

            _check(extracted_main, ("users", "orders", "settings", "admins"))
            _check(extracted_wallet, ("wallets", "wallet_transactions", "wallet_topups"))

            if target_path.exists():
                shutil.copy2(target_path, safety_main)
            if wallet_path.exists():
                shutil.copy2(wallet_path, safety_wallet)

            temp_main = target_path.with_name(target_path.name + ".restore_tmp")
            temp_wallet = wallet_path.with_name(wallet_path.name + ".restore_tmp")
            shutil.copy2(extracted_main, temp_main)
            shutil.copy2(extracted_wallet, temp_wallet)
            os.replace(temp_main, target_path)
            os.replace(temp_wallet, wallet_path)
            return {"main": safety_main if safety_main.exists() else None, "wallet": safety_wallet if safety_wallet.exists() else None}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # Backward-compatible restore of older main-only .db backups.
    source = sqlite3.connect(str(source_path), timeout=30)
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"users", "orders", "settings", "admins"}
        missing = required - tables
        if missing:
            raise RuntimeError("این فایل Backup مربوط به دیتابیس ربات نیست.")
        temp_target = target_path.with_name(target_path.name + ".restore_tmp")
        if temp_target.exists():
            temp_target.unlink()
        target = sqlite3.connect(str(temp_target), timeout=30)
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if str(check).lower() != "ok":
                raise RuntimeError(f"Restore integrity check failed: {check}")
            target.commit()
        finally:
            target.close()
    finally:
        source.close()
    if target_path.exists():
        shutil.copy2(target_path, safety_main)
    os.replace(temp_target, target_path)
    return {"main": safety_main if safety_main.exists() else None, "wallet": None}


def _backup_menu_text():
    files = _backup_files()
    auto_enabled = str(get_setting("backup_auto_enabled", "1")).strip().lower() not in {"0", "false", "off", "no"}
    if files:
        last = datetime.fromtimestamp(files[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        last_text = f"{last}\n<code>{escape(files[0].name)}</code>"
    else:
        last_text = "هنوز بکاپی ساخته نشده است."
    return (
        "💾 <b>مدیریت Backup / Restore</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🗄 دیتابیس: <code>{escape(str(DB_NAME))}</code>\n"
        f"📦 تعداد Backupها: <b>{len(files)}</b>\n"
        f"🕒 آخرین Backup:\n{last_text}\n"
        f"🤖 بکاپ خودکار: <b>{'روشن' if auto_enabled else 'خاموش'}</b>\n\n"
        "💡 Backupهای ساخته‌شده در پوشه پشتیبان نگهداری می‌شوند و هنگام ساخت Backup جدید، "
        f"حداکثر {BACKUP_KEEP_COUNT} نسخه آخر حفظ می‌شود."
    )


@dp.message(F.text == "💾 Backup / Restore")
async def backup_menu(message: Message):
    if not _is_main_admin(message.from_user.id):
        return
    await message.answer(_backup_menu_text(), parse_mode="HTML", reply_markup=_backup_keyboard())


@dp.callback_query(F.data == "backup:menu")
async def backup_menu_callback(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            _backup_menu_text(),
            parse_mode="HTML",
            reply_markup=_backup_keyboard(),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@dp.callback_query(F.data == "backup:create")
async def backup_create(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    await callback.answer("⏳ در حال ساخت Backup...")
    try:
        destination = await _create_database_backup()
        await bot.send_document(
            callback.from_user.id,
            FSInputFile(destination),
            caption=(
                "💾 <b>Backup با موفقیت ساخته شد</b>\n"
                f"📁 <code>{escape(destination.name)}</code>"
            ),
            parse_mode="HTML",
        )
        _audit(callback.from_user.id, "database_backup", str(destination.name))
        await callback.message.edit_text(
            _backup_menu_text(),
            parse_mode="HTML",
            reply_markup=_backup_keyboard(),
        )
    except Exception as exc:
        logger.exception("Database backup failed")
        await callback.message.answer(
            f"❌ ساخت Backup ناموفق بود.\n<code>{escape(str(exc)[:700])}</code>",
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "backup:list")
async def backup_list(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    files = _backup_files()
    if not files:
        text = "📋 <b>لیست Backupها</b>\n\nهنوز Backupای وجود ندارد."
    else:
        rows = ["📋 <b>Backupهای موجود</b>", "━━━━━━━━━━━━━━━━━━"]
        for index, path in enumerate(files[:BACKUP_KEEP_COUNT], 1):
            rows.append(f"{index}. <code>{escape(_backup_label(path))}</code>")
        text = "\n".join(rows)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 ساخت Backup جدید", callback_data="backup:create")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="backup:menu")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "backup:auto")
async def backup_auto_toggle(callback: CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    current = str(get_setting("backup_auto_enabled", "1")).strip().lower() not in {"0", "false", "off", "no"}
    set_setting("backup_auto_enabled", "0" if current else "1")
    _audit(callback.from_user.id, "backup_auto_toggle", details="enabled" if not current else "disabled")
    await callback.message.edit_text(
        _backup_menu_text(),
        parse_mode="HTML",
        reply_markup=_backup_keyboard(),
    )
    await callback.answer("🟢 بکاپ خودکار روشن شد." if not current else "🔴 بکاپ خودکار خاموش شد.")


@dp.callback_query(F.data == "backup:restore")
async def backup_restore_start(callback: CallbackQuery, state: FSMContext):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    await state.clear()
    await state.update_data(backup_restore=True)
    await callback.message.answer(
        "📥 <b>Restore</b>\n\n"
        "فایل Backup دیتابیس را به‌صورت Document ارسال کنید.\n"
        "قبل از جایگزینی، سلامت و ساختار فایل بررسی می‌شود و از دیتابیس فعلی نیز یک نسخه ایمنی ساخته خواهد شد.\n\n"
        "برای لغو <code>/cancel</code> را بفرستید.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(F.document)
async def backup_restore_document(message: Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id):
        return
    data = await state.get_data()
    if not data.get("backup_restore"):
        return

    filename = str(message.document.file_name or "").lower()
    if not filename.endswith((".db", ".sqlite", ".sqlite3", ".zip")):
        await message.answer("❌ فایل Backup باید SQLite یا ZIP بکاپ کامل ربات باشد.")
        return

    suffix = ".zip" if filename.endswith(".zip") else ".db"
    temp = Path(tempfile.gettempdir()) / f"restore_{uuid.uuid4().hex}{suffix}"
    try:
        await bot.download(message.document, temp)

        def _validate_restore(path):
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path, "r") as zf:
                    names = set(zf.namelist())
                    if "bot_database.db" not in names or "wallet.db" not in names:
                        return False, "zip_missing_databases"
                    return zf.testzip() is None, "complete_zip"
            conn = sqlite3.connect(str(path), timeout=30)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                return str(integrity).lower() == "ok", tables
            finally:
                conn.close()

        ok, info = await asyncio.to_thread(_validate_restore, temp)
        required = {"users", "orders", "settings", "admins"}
        if not ok:
            await message.answer("❌ Backup خراب است یا فایل ZIP ناقص است.")
            return
        if info != "complete_zip":
            missing = required - set(info)
            if missing:
                await message.answer("❌ این فایل ساختار دیتابیس ربات را ندارد.")
                return

        await state.update_data(backup_restore_path=str(temp))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ بله، Restore کن", callback_data="backup:restore_confirm")],
            [InlineKeyboardButton(text="❌ لغو", callback_data="backup:restore_cancel")],
        ])
        await message.answer(
            "⚠️ <b>تأیید Restore</b>\n\n"
            "دیتابیس فعلی با این Backup جایگزین می‌شود.\n"
            "قبل از جایگزینی، یک نسخه ایمنی از دیتابیس فعلی ساخته خواهد شد.\n\n"
            "آیا ادامه می‌دهی؟",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as exc:
        logger.exception("Restore validation failed")
        await message.answer(
            f"❌ بررسی Backup ناموفق بود: <code>{escape(str(exc)[:700])}</code>",
            parse_mode="HTML",
        )
        try:
            temp.unlink()
        except OSError:
            pass


@dp.callback_query(F.data == "backup:restore_cancel")
async def backup_restore_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    data = await state.get_data()
    path = data.get("backup_restore_path")
    if path:
        try:
            Path(path).unlink()
        except OSError:
            pass
    await state.clear()
    await callback.message.edit_text("❌ Restore لغو شد.")
    await callback.answer()


@dp.callback_query(F.data == "backup:restore_confirm")
async def backup_restore_confirm(callback: CallbackQuery, state: FSMContext):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("❌ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    data = await state.get_data()
    path = data.get("backup_restore_path")
    if not path or not Path(path).exists():
        await state.clear()
        await callback.answer("❌ فایل Restore پیدا نشد؛ دوباره ارسالش کن.", show_alert=True)
        return

    try:
        safety = await asyncio.to_thread(_restore_database_sync, path)
        await state.clear()
        safety_main = safety.get("main") if isinstance(safety, dict) else safety
        safety_wallet = safety.get("wallet") if isinstance(safety, dict) else None
        _audit(
            callback.from_user.id,
            "database_restore",
            details=(
                f"source={Path(path).name}; "
                f"safety_main={safety_main.name if safety_main else 'none'}; "
                f"safety_wallet={safety_wallet.name if safety_wallet else 'none'}"
            ),
        )
        await callback.message.edit_text(
            "✅ <b>Restore با موفقیت انجام شد.</b>\n\n"
            "🔐 قبل از جایگزینی، نسخه ایمنی دیتابیس فعلی نگهداری شد.\n"
            "🔄 برای اطمینان از بارگذاری کامل تنظیمات و کش‌ها، ربات را یک‌بار Restart کن.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Database restore failed")
        await callback.message.edit_text(
            f"❌ <b>Restore انجام نشد.</b>\n\n<code>{escape(str(exc)[:1000])}</code>",
            parse_mode="HTML",
        )
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass
    await callback.answer()


# گروه ثابت دریافت بکاپ‌های روزانه.
# ربات باید داخل این گروه عضو باشد و اجازه ارسال فایل داشته باشد.
DAILY_BACKUP_GROUP_ID = -1004458952313

async def _automatic_backup_loop():
    while True:
        try:
            await asyncio.sleep(300)
            enabled = str(get_setting("backup_auto_enabled", "1")).strip().lower() not in {"0", "false", "off", "no"}
            if not enabled:
                continue

            files = _backup_files()
            latest_mtime = files[0].stat().st_mtime if files else 0

            if not latest_mtime or (time.time() - latest_mtime) >= BACKUP_INTERVAL_SECONDS:
                destination = await _create_database_backup()

                now = datetime.now()
                caption = (
                    "💾 <b>بکاپ روزانه ربات</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📅 تاریخ: <b>{now.strftime('%Y-%m-%d')}</b>\n"
                    f"⏰ ساعت: <b>{now.strftime('%H:%M:%S')}</b>\n"
                    f"📁 فایل: <code>{escape(destination.name)}</code>"
                )

                try:
                    await bot.send_document(
                        DAILY_BACKUP_GROUP_ID,
                        FSInputFile(destination),
                        caption=caption,
                        parse_mode="HTML",
                    )
                except Exception:
                    # اگر ارسال ناموفق باشد، فایل محلی حذف نمی‌شود.
                    logger.exception(
                        "Daily backup was created but could not be sent to group %s",
                        DAILY_BACKUP_GROUP_ID,
                    )
                    raise
                else:
                    admin_id = _main_admin_id()
                    if admin_id:
                        _audit(
                            admin_id,
                            "automatic_database_backup_sent",
                            details=f"group={DAILY_BACKUP_GROUP_ID}; file={destination.name}",
                        )
                    logger.info(
                        "Daily database backup created and sent to group %s: %s",
                        DAILY_BACKUP_GROUP_ID,
                        destination,
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic database backup failed")


# Lightweight spam protection: per-user request window.
_SPAM_WINDOW={}
_SPAM_LIMIT=25
@dp.message()
async def _anti_spam_fallback(message:Message):
    # This handler is intentionally last and only records abuse; existing handlers run first.
    uid=int(message.from_user.id); now=time.monotonic(); bucket=_SPAM_WINDOW.setdefault(uid,[]); bucket[:]=[t for t in bucket if now-t<10]
    bucket.append(now)
    if len(bucket)>_SPAM_LIMIT: logger.warning("Rate-limit warning for user %s",uid)

#=========================================================
# MAIN
#=========================================================

async def main():
    init_db()
    _load_admins_from_database(seed=True)
    if not ADMIN_IDS:
        raise RuntimeError(
            "No admins found. Add the first admin to the database or set INITIAL_ADMIN_IDS."
        )
    init_wallet_db()
    init_connected_groups_table()

    seed_services(DEFAULT_SERVICES)
    _init_service_categories()
    for _default_service in DEFAULT_SERVICES:
        _default_category = _default_service.get("category")
        if _default_category:
            try:
                if _get_service_category(_default_service["service_key"]) == "other" and _default_category != "other":
                    _set_service_category_compat(_default_service["service_key"], _default_category)
            except Exception:
                logger.exception("Could not migrate default category for %s", _default_service["service_key"])

    asyncio.create_task(expiry_loop())
    asyncio.create_task(_automatic_backup_loop())

    global bot
    startup_update_notified = False
    while True:
        current_session = None
        watchdog_task = None
        try:
            bot, current_session = await _build_bot_with_working_proxy()
            if bot is None:
                logger.warning("No working Telegram connection. Retrying in 3 seconds...")
                await asyncio.sleep(3)
                continue

            # This is the only place where Telegram commands are registered,
            # after a working network path has already been established.
            await setup_bot_commands()

            # Register/synchronize the three group IDs already configured in
            # this bot. This also repairs older databases where those groups
            # were never inserted into connected_groups.
            try:
                synced, deactivated, failed = await _sync_all_connected_groups()
                logger.info(
                    "Connected groups startup sync: synced=%s deactivated=%s failed=%s",
                    synced, deactivated, failed,
                )
            except Exception:
                logger.exception("Connected groups startup sync failed.")

            logger.info("Bot started.")

            # This notification is sent once per Python process start, only to admins.
            # Polling reconnects inside the same process do not send it again.
            if not startup_update_notified:
                startup_update_notified = True
                update_text = (
                    "🔄 <b>ربات بروزرسانی و اجرا شد</b>\n\n"
                    "✅ نسخه جدید با موفقیت فعال شد.\n"
                    "👤 این پیام فقط برای ادمین‌ها ارسال شده است."
                )
                for admin_id in list(ADMIN_IDS):
                    try:
                        await bot.send_message(admin_id, update_text, parse_mode="HTML")
                    except Exception:
                        logger.exception("Could not send startup update notification to admin %s", admin_id)

            watchdog_task = asyncio.create_task(_network_watchdog())
            await dp.start_polling(bot)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Telegram connection/polling failed: %s", exc)
        finally:
            if watchdog_task is not None:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("Watchdog shutdown error.", exc_info=True)

            if current_session is not None:
                try:
                    await current_session.close()
                except Exception:
                    pass

        # Retry forever. The bot process is not terminated by a temporary
        # Telegram/Internet outage; it keeps rebuilding the connection until
        # Telegram becomes reachable again.
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())