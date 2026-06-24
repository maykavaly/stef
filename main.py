import asyncio
from io import BytesIO
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("client_template_bot")

BASE_DIR = Path(__file__).resolve().parent
APP_TZ = ZoneInfo("America/Mexico_City")
PORT = int(os.getenv("PORT", "8080"))

router = Router()
app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@dataclass(frozen=True)
class Settings:
    bot_token: str
    supabase_url: str
    supabase_key: str
    admin_chat_id: int
    admin_user_ids: set[int]
    admin_password: str
    content_channel_id: int
    auto_remove_expired: bool
    renewal_notice_days: list[int]


def parse_int(value: str | None, name: str) -> int:
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return int(value)


def parse_admin_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def load_settings() -> Settings:
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        admin_chat_id=parse_int(os.getenv("ADMIN_CHAT_ID"), "ADMIN_CHAT_ID"),
        admin_user_ids=parse_admin_ids(os.getenv("ADMIN_USER_IDS")),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        content_channel_id=parse_int(os.getenv("CONTENT_CHANNEL_ID"), "CONTENT_CHANNEL_ID"),
        auto_remove_expired=os.getenv("AUTO_REMOVE_EXPIRED", "false").lower() == "true",
        renewal_notice_days=[int(day.strip()) for day in os.getenv("RENEWAL_NOTICE_DAYS", "7,3,1").split(",") if day.strip()],
    )


settings = load_settings()
if not settings.bot_token or not settings.supabase_url or not settings.supabase_key or not settings.admin_password:
    raise RuntimeError("BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and ADMIN_PASSWORD are required.")

supabase: Client = create_client(settings.supabase_url, settings.supabase_key)
bot = Bot(settings.bot_token)
dp = Dispatcher()

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", settings.admin_password))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def today_local() -> date:
    return datetime.now(APP_TZ).date()


def is_admin_id(telegram_id: int | None) -> bool:
    return telegram_id is not None and telegram_id in settings.admin_user_ids


def is_admin_message(message: Message) -> bool:
    return bool(message.from_user and is_admin_id(message.from_user.id))


async def admin_only_message(message: Message) -> bool:
    if is_admin_message(message):
        return True
    await message.reply("Admin only.")
    return False


async def admin_only_callback(callback: CallbackQuery) -> bool:
    if callback.from_user and is_admin_id(callback.from_user.id):
        return True
    await callback.answer("Admin only.", show_alert=True)
    return False


def db_execute(label: str, query: Any) -> Any:
    try:
        return query.execute()
    except Exception:
        logger.exception("Supabase query failed: %s", label)
        raise


def parse_db_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def days_remaining(value: Any) -> int | None:
    expiry = parse_db_date(value)
    if not expiry:
        return None
    return (expiry - today_local()).days


def is_blacklisted(db: Client, telegram_id: int) -> bool:
    if is_admin_id(telegram_id):
        return False
    result = db.table("blacklist").select("telegram_id").eq("telegram_id", telegram_id).limit(1).execute()
    return bool(result.data)


def user_payload(user: Any) -> dict[str, Any]:
    return {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "last_seen_at": now_iso(),
        "updated_at": now_iso(),
    }


def get_user(telegram_id: int) -> dict[str, Any] | None:
    result = supabase.table("telegram_users").select("*").eq("telegram_id", telegram_id).limit(1).execute()
    return result.data[0] if result.data else None


def upsert_user(data: dict[str, Any]) -> None:
    supabase.table("telegram_users").upsert(data, on_conflict="telegram_id").execute()


def get_access_channels() -> list[dict[str, Any]]:
    result = supabase.table("access_channels").select("*").eq("is_active", True).order("sort_order").execute()
    return result.data or []


def channel_code(channel: dict[str, Any]) -> str:
    return str(channel.get("code") or channel.get("channel_key") or "").strip()


def channel_label(channel: dict[str, Any]) -> str:
    return str(channel.get("title") or channel.get("label") or channel_code(channel)).strip()


def channel_telegram_chat_id(channel: dict[str, Any]) -> int:
    raw = channel.get("telegram_chat_id") or channel.get("chat_id")
    if raw is None:
        raise ValueError(f"Missing telegram_chat_id for channel {channel}")
    return int(str(raw))


def get_access_channel_by_code(requested_code: str) -> dict[str, Any] | None:
    code = requested_code.strip()
    result = (
        supabase.table("access_channels")
        .select("*")
        .eq("code", code)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    for channel in get_access_channels():
        if channel_code(channel) == code:
            return channel
    return None


def available_channel_codes() -> str:
    codes = [channel_code(channel) for channel in get_access_channels()]
    return ", ".join(code for code in codes if code) or "none"


async def create_one_use_invite_link_for_chat(chat_id: int, name: str) -> str:
    link = await bot.create_chat_invite_link(
        chat_id=chat_id,
        member_limit=1,
        expire_date=now_utc() + timedelta(hours=1),
        name=name[:32],
    )
    return link.invite_link


def save_user_channel_access(
    telegram_id: int,
    channel: dict[str, Any],
    invite_link: str,
    invite_name: str,
    expires_at: date | None,
) -> None:
    supabase.table("user_channel_access").upsert(
        {
            "telegram_id": telegram_id,
            "channel_code": channel_code(channel),
            "channel_title": channel_label(channel),
            "telegram_chat_id": str(channel_telegram_chat_id(channel)),
            "invite_link": invite_link,
            "invite_link_name": invite_name,
            "invite_link_created_at": now_iso(),
            "invite_link_revoked": False,
            "invite_link_used": False,
            "status": "active",
            "granted_at": now_iso(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "updated_at": now_iso(),
        },
        on_conflict="telegram_id,channel_code",
    ).execute()


def insert_payment_history(data: dict[str, Any]) -> None:
    try:
        supabase.table("payment_history").insert(data).execute()
    except Exception:
        logger.warning("Payment history insert failed", exc_info=True)


def pending_payment_keyboard(telegram_id: int, selected_codes: set[str] | None = None) -> InlineKeyboardMarkup:
    selected_codes = selected_codes or set()
    rows: list[list[InlineKeyboardButton]] = []
    channel_buttons: list[InlineKeyboardButton] = []
    channels = get_access_channels()
    logger.info("Loaded approval channels: %s", channels)
    for channel in channels:
        code = channel_code(channel)
        prefix = "✅" if code in selected_codes else "⬜"
        channel_buttons.append(
            InlineKeyboardButton(text=f"{prefix} {channel_label(channel)}", callback_data=f"payment:toggle:{telegram_id}:{code}")
        )
    for index in range(0, len(channel_buttons), 3):
        rows.append(channel_buttons[index : index + 3])
    rows.append([InlineKeyboardButton(text="Aprobar seleccionados ✅", callback_data=f"payment:approve:{telegram_id}")])
    rows.append(
        [
            InlineKeyboardButton(text="Rechazar ❌", callback_data=f"payment:reject:{telegram_id}"),
            InlineKeyboardButton(text="Pedir otro comprobante 🔁", callback_data=f"payment:ask:{telegram_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def selected_codes_from_message(callback: CallbackQuery) -> set[str]:
    selected: set[str] = set()
    markup = callback.message.reply_markup if callback.message else None
    if not markup:
        return selected
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.startswith("payment:toggle:") and button.text.startswith("✅"):
                selected.add(button.callback_data.split(":", 3)[3])
    return selected


async def send_invite_links_to_user(telegram_id: int, channel_links: list[tuple[str, str]]) -> bool:
    body = ["Pago aprobado ✅\nAquí está tu link de acceso:", ""]
    for label, invite_link in channel_links:
        body.append(f"{label}:")
        body.append(invite_link)
        body.append("")
    try:
        await bot.send_message(telegram_id, "\n".join(body).strip())
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.warning("Could not DM invite links to %s", telegram_id, exc_info=True)
        return False


async def remove_user_from_chat(chat_id: int, telegram_id: int) -> None:
    await bot.ban_chat_member(chat_id=chat_id, user_id=telegram_id)
    await bot.unban_chat_member(chat_id=chat_id, user_id=telegram_id, only_if_banned=True)


def auth_required(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def dashboard_notice(message: str) -> RedirectResponse:
    return redirect(f"/dashboard?notice={quote(message)}")


def receipt_media_type(file_type: str | None) -> str:
    if file_type == "photo":
        return "image/jpeg"
    return "application/octet-stream"


async def telegram_file_response(file_id: str | None, file_type: str | None, filename_prefix: str) -> Response:
    if not file_id:
        raise HTTPException(status_code=404, detail="Receipt not found")
    try:
        telegram_file = await bot.get_file(file_id)
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
    except Exception as exc:
        logger.warning("Could not download Telegram receipt file", exc_info=True)
        raise HTTPException(status_code=502, detail="Could not download receipt") from exc
    extension = "jpg" if file_type == "photo" else "bin"
    return Response(
        content=buffer.getvalue(),
        media_type=receipt_media_type(file_type),
        headers={"Content-Disposition": f'inline; filename="{filename_prefix}.{extension}"'},
    )


@app.get("/", response_class=HTMLResponse, response_model=None)
async def root(request: Request):
    if auth_required(request):
        return redirect("/dashboard")
    return redirect("/login")


@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(request: Request, password: str = Form(...)):
    if password == settings.admin_password:
        request.session["authenticated"] = True
        return redirect("/dashboard")
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid password"})


@app.post("/logout", response_model=None)
async def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/dashboard", response_class=HTMLResponse, response_model=None)
async def dashboard(request: Request, status: str = "", payment_status: str = "", search: str = "", notice: str = ""):
    if not auth_required(request):
        return redirect("/login")
    query = supabase.table("telegram_users").select("*").order("registered_at", desc=True).limit(100)
    if status:
        query = query.eq("status", status)
    if payment_status:
        query = query.eq("payment_status", payment_status)
    result = query.execute()
    users = result.data or []
    if search:
        needle = search.lower()
        users = [
            user
            for user in users
            if needle in str(user.get("telegram_id", "")).lower()
            or needle in str(user.get("username", "")).lower()
            or needle in str(user.get("first_name", "")).lower()
            or needle in str(user.get("last_name", "")).lower()
        ]
    for user in users:
        user["days_remaining"] = days_remaining(user.get("expiry_date"))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"users": users, "status": status, "payment_status": payment_status, "search": search, "notice": notice},
    )


@app.post("/dashboard/users/{telegram_id}/renew-today", response_model=None)
async def dashboard_renew_today(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    start = today_local()
    expiry = start + timedelta(days=30)
    supabase.table("telegram_users").update(
        {
            "status": "active",
            "membership_start_date": start.isoformat(),
            "expiry_date": expiry.isoformat(),
            "updated_at": now_iso(),
        }
    ).eq("telegram_id", telegram_id).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/renew-from-expiry", response_model=None)
async def dashboard_renew_from_expiry(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    user = get_user(telegram_id)
    start = parse_db_date(user.get("expiry_date")) if user else None
    if not start:
        start = today_local()
    expiry = start + timedelta(days=30)
    supabase.table("telegram_users").update(
        {
            "status": "active",
            "membership_start_date": start.isoformat(),
            "expiry_date": expiry.isoformat(),
            "updated_at": now_iso(),
        }
    ).eq("telegram_id", telegram_id).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/mark-inactive", response_model=None)
async def dashboard_mark_inactive(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    supabase.table("telegram_users").update({"status": "inactive", "updated_at": now_iso()}).eq("telegram_id", telegram_id).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/mark-paid", response_model=None)
async def dashboard_mark_paid(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    supabase.table("telegram_users").update(
        {"payment_status": "paid", "last_payment_at": now_iso(), "updated_at": now_iso()}
    ).eq("telegram_id", telegram_id).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/mark-pending", response_model=None)
async def dashboard_mark_pending(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    supabase.table("telegram_users").update({"payment_status": "pending_review", "updated_at": now_iso()}).eq(
        "telegram_id", telegram_id
    ).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/needs-new-receipt", response_model=None)
async def dashboard_needs_new_receipt(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    supabase.table("telegram_users").update(
        {
            "payment_status": "needs_new_receipt",
            "needs_new_receipt_at": now_iso(),
            "notes": "Admin requested another receipt from dashboard",
            "updated_at": now_iso(),
        }
    ).eq("telegram_id", telegram_id).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/reject-payment", response_model=None)
async def dashboard_reject_payment(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    supabase.table("telegram_users").update(
        {
            "payment_status": "rejected",
            "rejected_at": now_iso(),
            "notes": "Payment rejected from dashboard",
            "updated_at": now_iso(),
        }
    ).eq("telegram_id", telegram_id).execute()
    return redirect("/dashboard")


@app.post("/dashboard/users/{telegram_id}/remove-access", response_model=None)
async def dashboard_remove_access(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    active_access = (
        supabase.table("user_channel_access")
        .select("*")
        .eq("telegram_id", telegram_id)
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    if not active_access:
        return dashboard_notice("No tenía accesos activos")

    removed_at = now_iso()
    successful_removals = 0
    failed_removals = 0
    for access in active_access:
        try:
            chat_id = int(str(access.get("telegram_chat_id")))
            await remove_user_from_chat(chat_id, telegram_id)
        except Exception:
            failed_removals += 1
            logger.warning(
                "Dashboard manual removal failed for telegram_id=%s channel_code=%s chat_id=%s",
                telegram_id,
                access.get("channel_code"),
                access.get("telegram_chat_id"),
                exc_info=True,
            )
            continue

        successful_removals += 1
        supabase.table("user_channel_access").update(
            {
                "status": "inactive",
                "removed_at": removed_at,
                "removal_reason": "dashboard_manual_remove",
                "updated_at": removed_at,
            }
        ).eq("id", access.get("id")).execute()

    user = get_user(telegram_id)
    existing_notes = str(user.get("notes") or "").strip() if user else ""
    removal_note = f"Removido desde dashboard en {removed_at}"
    notes = f"{existing_notes}\n{removal_note}" if existing_notes else removal_note
    supabase.table("telegram_users").update(
        {
            "status": "inactive",
            "left_channel_at": removed_at,
            "updated_at": removed_at,
            "notes": notes,
        }
    ).eq("telegram_id", telegram_id).execute()

    if failed_removals:
        return dashboard_notice("Usuario removido parcialmente")
    if successful_removals:
        return dashboard_notice("Usuario removido correctamente")
    return dashboard_notice("Usuario removido parcialmente")


@app.get("/dashboard/users/{telegram_id}/receipt", response_model=None)
async def dashboard_user_receipt(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    user = get_user(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    file_id = user.get("pending_payment_file_id")
    file_type = user.get("pending_payment_file_type")
    if not file_id:
        history = (
            supabase.table("payment_history")
            .select("*")
            .eq("telegram_id", telegram_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
        history = [row for row in history if row.get("receipt_file_id")]
        if history:
            file_id = history[0].get("receipt_file_id")
            file_type = history[0].get("receipt_file_type")
    return await telegram_file_response(file_id, file_type, f"receipt-{telegram_id}")


@app.get("/dashboard/payments/{payment_id}/receipt", response_model=None)
async def dashboard_payment_receipt(request: Request, payment_id: int):
    if not auth_required(request):
        return redirect("/login")
    result = supabase.table("payment_history").select("*").eq("id", payment_id).limit(1).execute()
    payment = result.data[0] if result.data else None
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return await telegram_file_response(payment.get("receipt_file_id"), payment.get("receipt_file_type"), f"payment-{payment_id}-receipt")


@app.get("/dashboard/users/{telegram_id}/history", response_class=HTMLResponse, response_model=None)
async def dashboard_payment_history(request: Request, telegram_id: int):
    if not auth_required(request):
        return redirect("/login")
    user = get_user(telegram_id)
    history = (
        supabase.table("payment_history")
        .select("*")
        .eq("telegram_id", telegram_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    return templates.TemplateResponse(request, "payment_history.html", {"user": user, "history": history})


@router.message(Command("chat_id"))
async def chat_id_command(message: Message) -> None:
    if message.chat.type == "private" and not await admin_only_message(message):
        return
    chat = message.chat
    text = f"Chat ID: {chat.id}\nTitle: {chat.title}\nUsername: {chat.username}\nType: {chat.type}"
    logger.info(text)
    await bot.send_message(settings.admin_chat_id, text)
    if message.chat.type != "channel":
        await message.reply("Chat info sent to admin.")


@router.channel_post(Command("chat_id"))
async def channel_chat_id_command(message: Message) -> None:
    chat = message.chat
    text = f"Chat ID: {chat.id}\nTitle: {chat.title}\nUsername: {chat.username}\nType: {chat.type}"
    logger.info(text)
    await bot.send_message(settings.admin_chat_id, text)


@router.message(Command("users"))
async def users_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    users = supabase.table("telegram_users").select("*").order("registered_at", desc=True).limit(10).execute().data or []
    total = supabase.table("telegram_users").select("telegram_id", count="exact").execute().count or 0
    lines = [f"Total registered users: {total}", "", "Latest 10:"]
    for user in users:
        lines.append(f"- {user.get('telegram_id')} @{user.get('username') or '-'} {user.get('first_name') or ''}")
    await message.reply("\n".join(lines))


@router.message(Command("manual_open_link"))
async def manual_open_link_command(message: Message) -> None:
    logger.info("/manual_open_link received from %s", message.from_user.id if message.from_user else None)
    if not await admin_only_message(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        await message.reply("Usage: /manual_open_link grupo")
        return
    channel = get_access_channel_by_code(args[1])
    if not channel:
        await message.reply(f"Channel not found. Available: {available_channel_codes()}")
        return
    timestamp = int(now_utc().timestamp())
    code = channel_code(channel)
    invite_name = f"manual-open-{code}-{timestamp}"[:32]
    invite_link = await create_one_use_invite_link_for_chat(channel_telegram_chat_id(channel), invite_name)
    supabase.table("manual_invite_links").insert(
        {
            "channel_code": code,
            "telegram_chat_id": str(channel_telegram_chat_id(channel)),
            "invite_link": invite_link,
            "invite_link_name": invite_name,
            "created_by_admin_id": message.from_user.id,
            "expires_at": (now_utc() + timedelta(hours=1)).isoformat(),
        }
    ).execute()
    await message.reply(f"{channel_label(channel)}\n{invite_link}")


@router.message(Command("blacklist"))
async def blacklist_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: /blacklist <telegram_id>")
        return
    telegram_id = int(parts[1])
    supabase.table("blacklist").upsert({"telegram_id": telegram_id, "blocked_at": now_iso()}).execute()
    await message.reply(f"Blacklisted {telegram_id}.")


@router.message(Command("unblacklist"))
async def unblacklist_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: /unblacklist <telegram_id>")
        return
    telegram_id = int(parts[1])
    supabase.table("blacklist").delete().eq("telegram_id", telegram_id).execute()
    await message.reply(f"Removed {telegram_id} from blacklist.")


@router.message(Command("check_blacklist"))
async def check_blacklist_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: /check_blacklist <telegram_id>")
        return
    telegram_id = int(parts[1])
    await message.reply("Blocked." if is_blacklisted(supabase, telegram_id) else "Not blocked.")


@router.message(Command("pending_payments"))
async def pending_payments_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    users = supabase.table("telegram_users").select("*").eq("payment_status", "pending_review").execute().data or []
    if not users:
        await message.reply("No pending payments.")
        return
    lines = ["Pending payments:"]
    for user in users[:40]:
        lines.append(f"- {user.get('telegram_id')} @{user.get('username') or '-'} {user.get('first_name') or ''}")
    await message.reply("\n".join(lines))


@router.message(Command("renewal_preview"))
async def renewal_preview_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    today = today_local()
    end = today + timedelta(days=7)
    users = (
        supabase.table("telegram_users")
        .select("*")
        .eq("status", "active")
        .gte("expiry_date", today.isoformat())
        .lte("expiry_date", end.isoformat())
        .order("expiry_date")
        .execute()
        .data
        or []
    )
    if not users:
        await message.reply("No active users expiring in the next 7 days.")
        return
    lines = ["Renewal preview:"]
    for user in users:
        lines.append(
            f"- telegram_id: {user.get('telegram_id')}\n"
            f"  username: @{user.get('username') or '-'}\n"
            f"  first_name: {user.get('first_name') or '-'}\n"
            f"  expiry_date: {user.get('expiry_date')}\n"
            f"  days_remaining: {days_remaining(user.get('expiry_date'))}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("expired"))
async def expired_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    users = (
        supabase.table("telegram_users")
        .select("*")
        .eq("status", "active")
        .lt("expiry_date", today_local().isoformat())
        .order("expiry_date")
        .execute()
        .data
        or []
    )
    if not users:
        await message.reply("No expired active users.")
        return
    lines = ["Expired active users:"]
    for user in users[:50]:
        lines.append(f"- {user.get('telegram_id')} @{user.get('username') or '-'} expiry_date={user.get('expiry_date')}")
    await message.reply("\n".join(lines))


@router.message(Command("remove_expired_preview"))
async def remove_expired_preview_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    rows = (
        supabase.table("user_channel_access")
        .select("*")
        .eq("status", "active")
        .lt("expires_at", today_local().isoformat())
        .execute()
        .data
        or []
    )
    if not rows:
        await message.reply("No expired channel access records.")
        return
    lines = ["Would remove:"]
    for row in rows[:50]:
        lines.append(f"- {row.get('telegram_id')} from {row.get('channel_title') or row.get('channel_code')} expires_at={row.get('expires_at')}")
    await message.reply("\n".join(lines))


@router.message(Command("remove_expired_confirm"))
async def remove_expired_confirm_command(message: Message) -> None:
    if not await admin_only_message(message):
        return
    rows = (
        supabase.table("user_channel_access")
        .select("*")
        .eq("status", "active")
        .lt("expires_at", today_local().isoformat())
        .execute()
        .data
        or []
    )
    removed = 0
    failed: list[int] = []
    for row in rows:
        telegram_id = int(row["telegram_id"])
        try:
            await remove_user_from_chat(int(row["telegram_chat_id"]), telegram_id)
            removed += 1
            supabase.table("user_channel_access").update(
                {"status": "inactive", "removed_at": now_iso(), "removal_reason": "expired_manual_confirm", "updated_at": now_iso()}
            ).eq("id", row["id"]).execute()
        except Exception:
            logger.warning("Failed to remove expired user %s", telegram_id, exc_info=True)
            failed.append(telegram_id)
    await message.reply(f"Removed: {removed}\nFailed: {failed or '-'}")


@router.message(F.chat.type == "private", (F.photo | F.document))
async def payment_receipt_handler(message: Message) -> None:
    if not message.from_user:
        return
    telegram_id = message.from_user.id
    if is_blacklisted(supabase, telegram_id):
        return
    if is_admin_id(telegram_id):
        await message.reply("Receipt flow is for non-admin users.")
        return
    existing = get_user(telegram_id)
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    file_type = "photo" if message.photo else "document"
    payload = user_payload(message.from_user)
    payload.update(
        {
            "payment_status": "pending_review",
            "pending_payment_file_id": file_id,
            "pending_payment_file_type": file_type,
            "pending_payment_at": now_iso(),
            "source": "payment_receipt_private_bot",
            "registered_at": existing.get("registered_at") if existing else now_iso(),
        }
    )
    upsert_user(payload)
    if existing and existing.get("payment_status") == "pending_review":
        await message.reply("Tu comprobante anterior fue actualizado ✅\nLo revisaremos y te enviaremos tu acceso en cuanto sea aprobado.")
        return
    await message.reply("Comprobante recibido ✅ Lo revisaremos y te enviaremos tu acceso en cuanto sea aprobado.")
    caption = (
        "Nuevo comprobante pendiente\n"
        f"telegram_id: {telegram_id}\n"
        f"username: @{message.from_user.username or '-'}\n"
        f"first_name: {message.from_user.first_name or '-'}\n"
        f"pending_payment_at: {payload['pending_payment_at']}"
    )
    if message.photo:
        await bot.send_photo(settings.admin_chat_id, file_id, caption=caption, reply_markup=pending_payment_keyboard(telegram_id))
    else:
        await bot.send_document(settings.admin_chat_id, file_id, caption=caption, reply_markup=pending_payment_keyboard(telegram_id))


@router.callback_query(F.data.startswith("payment:"))
async def payment_callback(callback: CallbackQuery) -> None:
    if not await admin_only_callback(callback):
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Invalid action.", show_alert=True)
        return
    action = parts[1]
    telegram_id = int(parts[2])
    if action == "toggle" and len(parts) == 4:
        selected = selected_codes_from_message(callback)
        code = parts[3]
        if code in selected:
            selected.remove(code)
        else:
            selected.add(code)
        await callback.message.edit_reply_markup(reply_markup=pending_payment_keyboard(telegram_id, selected))
        await callback.answer()
        return
    user = get_user(telegram_id)
    if not user:
        await callback.answer("User not found.", show_alert=True)
        return
    if action == "approve":
        selected = selected_codes_from_message(callback)
        if not selected:
            await callback.answer("Select at least one channel before approving.", show_alert=True)
            return
        start = today_local()
        expiry = start + timedelta(days=30)
        channel_links: list[tuple[str, str]] = []
        any_expiring_channel = False
        for code in selected:
            channel = get_access_channel_by_code(code)
            if not channel:
                logger.warning("Selected channel not found: %s", code)
                continue
            chat_id = channel_telegram_chat_id(channel)
            invite_name = f"approved-{telegram_id}-{code}-{int(now_utc().timestamp())}"[:32]
            invite_link = await create_one_use_invite_link_for_chat(chat_id, invite_name)
            expires_at = expiry if channel.get("has_expiry", True) else None
            any_expiring_channel = any_expiring_channel or bool(expires_at)
            save_user_channel_access(telegram_id, channel, invite_link, invite_name, expires_at)
            channel_links.append((channel_label(channel), invite_link))
        if not channel_links:
            await callback.answer("No invite links were created.", show_alert=True)
            return
        update_payload = {
            "status": "active",
            "payment_status": "paid",
            "approved_by_admin_id": callback.from_user.id,
            "approved_at": now_iso(),
            "last_payment_at": now_iso(),
            "invite_link": channel_links[0][1],
            "invite_link_created_at": now_iso(),
            "updated_at": now_iso(),
        }
        if any_expiring_channel:
            update_payload["membership_start_date"] = start.isoformat()
            update_payload["expiry_date"] = expiry.isoformat()
        supabase.table("telegram_users").update(update_payload).eq("telegram_id", telegram_id).execute()
        insert_payment_history(
            {
                "telegram_id": telegram_id,
                "username": user.get("username"),
                "first_name": user.get("first_name"),
                "admin_id": callback.from_user.id,
                "payment_status": "paid",
                "receipt_file_id": user.get("pending_payment_file_id"),
                "receipt_file_type": user.get("pending_payment_file_type"),
                "invite_link": "\n".join(link for _, link in channel_links),
                "membership_start_date": start.isoformat() if any_expiring_channel else None,
                "expiry_date": expiry.isoformat() if any_expiring_channel else None,
                "verified": True,
            }
        )
        sent = await send_invite_links_to_user(telegram_id, channel_links)
        await bot.send_message(settings.admin_chat_id, f"Pago aprobado para {telegram_id}. Link enviado: {'sí' if sent else 'no'}")
        await callback.answer("Approved ✅")
        return
    if action == "reject":
        supabase.table("telegram_users").update(
            {"payment_status": "rejected", "rejected_at": now_iso(), "notes": "Payment rejected by admin", "updated_at": now_iso()}
        ).eq("telegram_id", telegram_id).execute()
        try:
            await bot.send_message(telegram_id, "No pudimos validar tu comprobante. Por favor revísalo y envíalo nuevamente.")
        except Exception:
            logger.warning("Could not DM rejection to %s", telegram_id, exc_info=True)
        await callback.answer("Rejected.")
        return
    if action == "ask":
        supabase.table("telegram_users").update(
            {
                "payment_status": "needs_new_receipt",
                "needs_new_receipt_at": now_iso(),
                "notes": "Admin requested another receipt",
                "updated_at": now_iso(),
            }
        ).eq("telegram_id", telegram_id).execute()
        try:
            await bot.send_message(telegram_id, "Por favor envía nuevamente tu comprobante en una captura más clara.")
        except Exception:
            logger.warning("Could not DM receipt request to %s", telegram_id, exc_info=True)
        await callback.answer("Requested another receipt.")


@router.chat_member()
async def chat_member_handler(update: ChatMemberUpdated) -> None:
    user = update.new_chat_member.user
    if not user or is_admin_id(user.id):
        return
    channels = get_access_channels()
    channel = next((item for item in channels if str(channel_telegram_chat_id(item)) == str(update.chat.id)), None)
    if not channel:
        return
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    if is_blacklisted(supabase, user.id):
        try:
            await remove_user_from_chat(update.chat.id, user.id)
            await bot.send_message(settings.admin_chat_id, f"Blacklisted user removed: {user.id}")
        except Exception:
            logger.warning("Failed removing blacklisted user %s", user.id, exc_info=True)
        return
    if old_status in {"left", "kicked"} and new_status in {"member", "administrator", "creator"}:
        upsert_user({**user_payload(user), "status": "active", "joined_channel_at": now_iso(), "source": "channel_join"})
        supabase.table("user_channel_access").upsert(
            {
                "telegram_id": user.id,
                "channel_code": channel_code(channel),
                "channel_title": channel_label(channel),
                "telegram_chat_id": str(update.chat.id),
                "status": "active",
                "joined_channel_at": now_iso(),
                "updated_at": now_iso(),
            },
            on_conflict="telegram_id,channel_code",
        ).execute()
        invite_link = getattr(update, "invite_link", None)
        if invite_link:
            link_value = invite_link.invite_link
            supabase.table("manual_invite_links").update(
                {"used_by_telegram_id": user.id, "used_at": now_iso()}
            ).eq("invite_link", link_value).execute()
            await bot.send_message(settings.admin_chat_id, f"Manual invite used by @{user.username or '-'} ({user.id}) in {channel_label(channel)}")
    elif new_status in {"left", "kicked"}:
        supabase.table("telegram_users").update({"left_channel_at": now_iso(), "updated_at": now_iso()}).eq("telegram_id", user.id).execute()
        supabase.table("user_channel_access").update({"left_channel_at": now_iso(), "status": "inactive", "updated_at": now_iso()}).eq(
            "telegram_id", user.id
        ).eq("channel_code", channel_code(channel)).execute()


async def renewal_reminder_job() -> None:
    logger.info("Renewal reminder job started")
    today = today_local()
    for notice_day in settings.renewal_notice_days:
        column = f"renewal_notice_{notice_day}d_sent_at"
        expiry = today + timedelta(days=notice_day)
        users = (
            supabase.table("telegram_users")
            .select("*")
            .eq("status", "active")
            .eq("expiry_date", expiry.isoformat())
            .is_(column, "null")
            .execute()
            .data
            or []
        )
        if not users:
            continue
        lines = [f"Users expiring in {notice_day} day(s):"]
        for user in users:
            lines.append(f"- {user.get('telegram_id')} @{user.get('username') or '-'} {user.get('first_name') or ''}")
        await bot.send_message(settings.admin_chat_id, "\n".join(lines))
        for user in users:
            supabase.table("telegram_users").update({column: now_iso(), "updated_at": now_iso()}).eq("telegram_id", user["telegram_id"]).execute()
    expired = (
        supabase.table("telegram_users")
        .select("*")
        .eq("status", "active")
        .lt("expiry_date", today.isoformat())
        .execute()
        .data
        or []
    )
    if expired:
        await bot.send_message(settings.admin_chat_id, f"Expired active users: {len(expired)}. Use /expired or /remove_expired_preview.")


async def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=APP_TZ)
    scheduler.add_job(renewal_reminder_job, CronTrigger(hour=9, minute=0, timezone=APP_TZ), id="renewal_reminders", replace_existing=True)
    scheduler.start()
    logger.info("Renewal reminder job registered")
    return scheduler


async def main() -> None:
    dp.include_router(router)
    await start_scheduler()
    logger.info("Starting web dashboard on port %s", PORT)
    logger.info("Starting Telegram bot polling")
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
