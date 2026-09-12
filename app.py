#!/usr/bin/env python3
import cgi
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import secrets
import shutil
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, time as dt_time, timedelta
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
BROADCAST_UPLOAD_DIR = DATA_DIR / "broadcast_uploads"
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "realty_bot.sqlite3"))
WATERMARK_LOGO_PATH = APP_DIR / "assets" / "watermark-logo.png"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_BASE_URL = os.environ.get("ADMIN_BASE_URL", PUBLIC_BASE_URL).rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "4"))
PERF_LOG_ENABLED = os.environ.get("PERF_LOG_ENABLED", "1") != "0"
PERF_SLOW_MS = int(os.environ.get("PERF_SLOW_MS", "800"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "belowmarketdubaibot")
YANDEX_METRIKA_ID = os.environ.get("YANDEX_METRIKA_ID", "112517412").strip()
YANDEX_METRIKA_TOKEN = os.environ.get("YANDEX_METRIKA_TOKEN", "").strip()

SEND_WINDOW_START = dt_time(9, 0)
SEND_WINDOW_END = dt_time(21, 0)
DAILY_SEND_SLOTS = [("morning", dt_time(11, 0)), ("evening", dt_time(17, 0))]
METRIKA_DAILY_REPORT_TIME = dt_time(11, 0)
CATALOG_FOLLOWUP_DELAY_MINUTES = 2
CATALOG_CLOSE_FOLLOWUP_DELAY_SECONDS = 5
CATALOG_STALE_SESSION_MINUTES = 5
LEAD_STATUSES = [
    ("new", "Новая"),
    ("contacted", "Связались"),
    ("qualified", "Квалифицирована"),
    ("viewing_scheduled", "Показ назначен"),
    ("offer", "Оффер / переговоры"),
    ("closed", "Сделка закрыта"),
    ("lost", "Неактуальна"),
]
METRIKA_GOALS = [
    ("lot_view", "Просмотр лота"),
    ("lot_share", "Поделиться лотом"),
    ("lead_submit", "Хочу узнать подробнее"),
]
LEAD_STATUS_LABELS = dict(LEAD_STATUSES)
SUBSCRIBER_STATUSES = [
    ("active", "Активен"),
    ("blocked", "Заблокировал бота"),
    ("deactivated", "Аккаунт удалён"),
    ("unreachable", "Недоступен"),
]
SUBSCRIBER_STATUS_LABELS = dict(SUBSCRIBER_STATUSES)
SUBSCRIBER_TAGS = [
    ("", "Без тега"),
    ("realtor", "Риелтор"),
    ("end_user", "Конечник"),
]
SUBSCRIBER_TAG_LABELS = dict(SUBSCRIBER_TAGS)
DEFAULT_CRM_STATUSES = [
    "Новый запрос",
    "Связаться",
    "Квалификация",
    "Подбор объектов",
    "Показ / встреча",
    "Переговоры",
    "Сделка",
    "Неактуально",
]
EVENT_LABELS = {
    "click_start_view": "Начал смотреть лоты",
    "click_next": "Смотреть ещё",
    "click_interest": "Хочу узнать подробнее",
    "click_contact_tg": "Выбрал связь в Telegram",
    "click_contact_phone": "Выбрал оставить телефон",
    "click_contact_whatsapp": "Выбрал оставить WhatsApp",
    "click_custom_interest": "Интерес к свободной рассылке",
    "click_custom_tg": "Свободная рассылка: Telegram",
    "click_custom_phone": "Свободная рассылка: телефон",
    "click_custom_whatsapp": "Свободная рассылка: WhatsApp",
    "click_personal_interest": "Заявка на персональный подбор",
    "click_personal_tg": "Персональный подбор: Telegram",
    "click_personal_phone": "Персональный подбор: телефон",
    "click_personal_whatsapp": "Персональный подбор: WhatsApp",
    "click_language": "Выбор языка",
    "click_filter_start": "Искать по фильтрам",
    "click_filter_rooms": "Фильтр: выбрал комнаты",
    "click_filter_district": "Фильтр: выбрал район",
    "click_filter_reset": "Сбросил фильтры",
    "click_filter_view_all": "Фильтр: смотреть без фильтров",
    "click_filter_show_seen": "Фильтр: показать просмотренные",
    "catalog_open": "Открыл каталог",
    "catalog_lot_view": "Каталог: просмотр лота",
    "catalog_lead": "Каталог: оставил заявку",
    "catalog_heartbeat": "Каталог: активная сессия",
    "catalog_close": "Закрыл каталог",
    "catalog_followup_yes": "Каталог follow-up: Да",
    "catalog_followup_no": "Каталог follow-up: Нет",
    "start_shared_lot": "Открыл лот по deep link",
}


def now_local():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)


def iso_now():
    return now_local().replace(microsecond=0).isoformat()


def iso_start_of_day(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def iso_next_day(moment):
    return (moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()


def log_perf(label, started_at, **fields):
    if not PERF_LOG_ENABLED:
        return
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    if elapsed_ms < PERF_SLOW_MS:
        return
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    print(f"PERF {label} elapsed_ms={elapsed_ms} {details}".rstrip(), file=sys.stderr)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    BROADCAST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            create table if not exists projects (
              id integer primary key autoincrement,
              title text not null,
              category text not null,
              district text not null,
              building text not null,
              rooms text not null,
              bathrooms text,
              floor_level text not null,
              parking text,
              availability text not null,
              furnishing text,
              balcony text,
              area text not null,
              price real not null,
              market_price real,
              distress integer not null default 0,
              original_price real,
              source_from text,
              description text,
              cover_media_id integer,
              tg_media_file_id text,
              tg_media_kind text,
              tg_media_signature text,
              status text not null default 'active',
              created_at text not null,
              updated_at text not null
            );

            create table if not exists media (
              id integer primary key autoincrement,
              project_id integer not null,
              file_name text not null,
              original_name text not null,
              mime_type text not null,
              created_at text not null,
              foreign key(project_id) references projects(id) on delete cascade
            );

            create table if not exists subscribers (
              chat_id integer primary key,
              username text,
              first_name text,
              last_name text,
              language text,
              state text,
              state_project_id integer,
              filter_rooms text,
              filter_district text,
              subscribed_at text not null,
              last_seen_at text,
              last_daily_sent_at text,
              daily_sent_slots text,
              delivery_status text not null default 'active',
              last_delivery_at text,
              last_delivery_error_at text,
              last_delivery_error text,
              subscriber_tag text,
              chat_read_at text
            );

            create table if not exists seen_projects (
              chat_id integer not null,
              project_id integer not null,
              seen_at text not null,
              primary key(chat_id, project_id)
            );

            create table if not exists leads (
              id integer primary key autoincrement,
              project_id integer not null,
              chat_id integer not null,
              username text,
              name text,
              contact_method text,
              contact_value text,
              message text,
              status text not null default 'new',
              created_at text not null,
              foreign key(project_id) references projects(id)
            );

            create table if not exists broadcasts (
              id integer primary key autoincrement,
              project_id integer not null,
              send_at text not null,
              status text not null default 'scheduled',
              created_at text not null,
              sent_at text,
              sent_count integer not null default 0,
              foreign key(project_id) references projects(id)
            );

            create table if not exists custom_broadcasts (
              id integer primary key autoincrement,
              text text not null,
              send_at text,
              status text not null default 'sent',
              created_at text not null,
              sent_at text,
              sent_count integer not null default 0
            );

            create table if not exists custom_broadcast_media (
              id integer primary key autoincrement,
              broadcast_id integer not null,
              file_name text not null,
              original_name text not null,
              mime_type text not null,
              created_at text not null,
              foreign key(broadcast_id) references custom_broadcasts(id) on delete cascade
            );

            create table if not exists custom_broadcast_leads (
              id integer primary key autoincrement,
              broadcast_id integer not null,
              chat_id integer not null,
              username text,
              name text,
              contact_method text,
              contact_value text,
              status text not null default 'new',
              created_at text not null,
              foreign key(broadcast_id) references custom_broadcasts(id) on delete cascade
            );

            create table if not exists personal_leads (
              id integer primary key autoincrement,
              chat_id integer not null,
              username text,
              name text,
              contact_method text,
              contact_value text,
              status text not null default 'new',
              created_at text not null
            );

            create table if not exists web_leads (
              id integer primary key autoincrement,
              project_id integer,
              name text,
              contact_value text not null,
              message text,
              status text not null default 'new',
              created_at text not null,
              foreign key(project_id) references projects(id)
            );

            create table if not exists catalog_events (
              id integer primary key autoincrement,
              event_type text not null,
              project_id integer,
              chat_id integer,
              username text,
              name text,
              payload text,
              created_at text not null,
              foreign key(project_id) references projects(id)
            );

            create table if not exists catalog_sessions (
              session_id text primary key,
              chat_id integer,
              username text,
              name text,
              opened_at text not null,
              last_seen_at text not null,
              closed_at text,
              duration_seconds integer,
              had_lead integer not null default 0,
              followup_sent_at text,
              followup_answer text,
              followup_answered_at text
            );

            create table if not exists bot_events (
              id integer primary key autoincrement,
              chat_id integer not null,
              username text,
              name text,
              event_type text not null,
              project_id integer,
              broadcast_id integer,
              payload text,
              created_at text not null
            );

            create table if not exists chat_messages (
              id integer primary key autoincrement,
              chat_id integer not null,
              username text,
              name text,
              direction text not null,
              text text not null,
              created_at text not null
            );

            create table if not exists crm_notes (
              id integer primary key autoincrement,
              chat_id integer,
              card_id integer,
              text text not null,
              created_at text not null
            );

            create table if not exists crm_reminders (
              id integer primary key autoincrement,
              chat_id integer,
              card_id integer,
              client_name text,
              contact_value text,
              text text not null,
              remind_at text not null,
              status text not null default 'scheduled',
              created_at text not null,
              sent_at text
            );

            create table if not exists crm_statuses (
              id integer primary key autoincrement,
              name text not null,
              position integer not null default 0,
              created_at text not null
            );

            create table if not exists crm_cards (
              id integer primary key autoincrement,
              status_id integer not null,
              chat_id integer,
              title text not null,
              client_name text,
              contact_value text,
              budget text,
              request text,
              source text,
              created_at text not null,
              updated_at text not null,
              foreign key(status_id) references crm_statuses(id)
            );

            create table if not exists report_runs (
              report_key text primary key,
              sent_at text not null
            );
            """
        )
        ensure_column(conn, "projects", "tg_media_file_id", "text")
        ensure_column(conn, "projects", "tg_media_kind", "text")
        ensure_column(conn, "projects", "tg_media_signature", "text")
        ensure_column(conn, "projects", "source_from", "text")
        ensure_column(conn, "projects", "cover_media_id", "integer")
        ensure_column(conn, "subscribers", "language", "text")
        ensure_column(conn, "subscribers", "filter_rooms", "text")
        ensure_column(conn, "subscribers", "filter_district", "text")
        ensure_column(conn, "subscribers", "chat_read_at", "text")
        ensure_column(conn, "subscribers", "daily_sent_slots", "text")
        ensure_column(conn, "subscribers", "delivery_status", "text not null default 'active'")
        ensure_column(conn, "subscribers", "last_delivery_at", "text")
        ensure_column(conn, "subscribers", "last_delivery_error_at", "text")
        ensure_column(conn, "subscribers", "last_delivery_error", "text")
        ensure_column(conn, "subscribers", "subscriber_tag", "text")
        ensure_column(conn, "custom_broadcasts", "send_at", "text")
        ensure_column(conn, "custom_broadcast_leads", "status", "text not null default 'new'")
        ensure_column(conn, "web_leads", "status", "text not null default 'new'")
        ensure_column(conn, "catalog_sessions", "followup_variant", "text")
        ensure_column(conn, "crm_notes", "chat_id", "integer")
        ensure_column(conn, "crm_notes", "card_id", "integer")
        ensure_column(conn, "crm_reminders", "chat_id", "integer")
        ensure_column(conn, "crm_reminders", "card_id", "integer")
        ensure_column(conn, "crm_reminders", "client_name", "text")
        ensure_column(conn, "crm_reminders", "contact_value", "text")
        ensure_column(conn, "crm_cards", "chat_id", "integer")
        ensure_default_crm_statuses(conn)
        ensure_initial_crm_cards(conn)


def ensure_column(conn, table_name, column_name, column_type):
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table_name})").fetchall()}
    if column_name not in columns:
        conn.execute(f"alter table {table_name} add column {column_name} {column_type}")


def ensure_default_crm_statuses(conn):
    existing = conn.execute("select count(*) c from crm_statuses").fetchone()["c"]
    if existing:
        return
    now = iso_now()
    for index, name in enumerate(DEFAULT_CRM_STATUSES, start=1):
        conn.execute(
            "insert into crm_statuses(name, position, created_at) values (?, ?, ?)",
            (name, index, now),
        )


def default_crm_status_id(conn):
    row = conn.execute("select id from crm_statuses order by position, id limit 1").fetchone()
    return row["id"] if row else None


def ensure_initial_crm_cards(conn):
    if conn.execute("select count(*) c from crm_cards").fetchone()["c"]:
        return
    status_id = default_crm_status_id(conn)
    if not status_id:
        return
    now = iso_now()
    for row in conn.execute(
        """
        select l.chat_id, l.name, l.contact_value, p.title project_title, p.district
        from leads l
        left join projects p on p.id = l.project_id
        order by l.created_at asc
        """
    ).fetchall():
        conn.execute(
            """
            insert into crm_cards(status_id, chat_id, title, client_name, contact_value, request, source, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                status_id,
                row["chat_id"],
                f"Заявка по лоту: {row['project_title'] or 'лот'}",
                row["name"],
                row["contact_value"],
                row["district"] or "",
                "Заявка по лоту",
                now,
                now,
            ),
        )
    for row in conn.execute("select chat_id, name, contact_value from personal_leads order by created_at asc").fetchall():
        conn.execute(
            """
            insert into crm_cards(status_id, chat_id, title, client_name, contact_value, request, source, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (status_id, row["chat_id"], "Персональный подбор", row["name"], row["contact_value"], "Подобрать лоты под запрос", "Персональный подбор", now, now),
        )


def create_crm_card(title, chat_id=None, client_name="", contact_value="", request="", source=""):
    with db() as conn:
        status_id = default_crm_status_id(conn)
        if not status_id:
            return None
        now = iso_now()
        cur = conn.execute(
            """
            insert into crm_cards(status_id, chat_id, title, client_name, contact_value, request, source, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (status_id, chat_id, title, client_name, contact_value, request, source, now, now),
        )
        return cur.lastrowid


def money(value):
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def pct_below(price, reference):
    try:
        price_f = float(price)
        ref_f = float(reference)
    except (TypeError, ValueError):
        return None
    if ref_f <= 0:
        return None
    return round((ref_f - price_f) / ref_f * 100)


def project_caption(project):
    rows = [
        ("🏷", "Категория", project["category"]),
        ("📍", "Район", project["district"]),
        ("🏢", "Здание", project["building"]),
        ("🛏", "Комнаты", project["rooms"]),
        ("🛁", "Санузлы", project["bathrooms"]),
        ("🪜", "Этаж", project["floor_level"]),
        ("🚗", "Парковка", project["parking"]),
        ("🔑", "Статус", project["availability"]),
        ("🛋", "Меблировка", project["furnishing"]),
        ("🌿", "Балкон", project["balcony"]),
        ("📐", "Площадь", project["area"]),
        ("💰", "Цена", f"{money(project['price'])} AED"),
    ]
    if project["market_price"]:
        rows.append(("📊", "Средняя цена рынка", f"{money(project['market_price'])} AED"))
        discount = pct_below(project["price"], project["market_price"])
        if discount is not None and discount > 0:
            rows.append(("📉", "Ниже рынка", f"на {discount}%"))
    if project["distress"]:
        rows.append(("🔥", "Special deal", "distress opportunity"))
    if project["original_price"]:
        rows.append(("🧾", "Original price", f"{money(project['original_price'])} AED"))
        discount = pct_below(project["price"], project["original_price"])
        if discount is not None and discount > 0:
            rows.append(("🎯", "Выгода от original price", f"на {discount}%"))

    lines = [f"<b>Лот: {html.escape(project['title'])}</b>", ""]
    for icon, label, value in rows:
        if value not in (None, ""):
            lines.append(f"{icon} <b>{label}:</b> {html.escape(str(value))}")
    if project["description"]:
        lines.extend(["", html.escape(project["description"])])
    return "\n".join(lines)


def sign(value):
    digest = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{digest}"


def verify_signature(signed):
    if not signed or "." not in signed:
        return None
    value, digest = signed.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(digest, expected):
        return value
    return None


def telegram_api(method, payload):
    if not BOT_TOKEN:
        return None
    started_at = time.perf_counter()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            result = json.loads(res.read().decode())
            if method != "getUpdates":
                log_perf("telegram_api", started_at, method=method, ok=result.get("ok"))
            return result
    except urllib.error.HTTPError as err:
        log_perf("telegram_api_http_error", started_at, method=method, status=err.code)
        try:
            return json.loads(err.read().decode())
        except Exception:
            return {"ok": False, "error_code": err.code, "description": str(err)}
    except Exception:
        log_perf("telegram_api_error", started_at, method=method)
        traceback.print_exc()
        return None


def telegram_api_multipart(method, fields, file_field, file_path, mime_type):
    return telegram_api_multipart_files(
        method,
        fields,
        [(file_field, file_path, Path(file_path).name, mime_type)],
    )


def telegram_api_multipart_files(method, fields, files):
    if not BOT_TOKEN:
        return None
    started_at = time.perf_counter()
    boundary = "----tgRealtyBoundary" + secrets.token_hex(12)
    body = bytearray()

    def add_field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")

    for key, value in fields.items():
        add_field(key, value)

    for field_name, file_path, filename, mime_type in files:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        )
        body.extend(f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n".encode())
        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, _BytearrayWriter(body))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            result = json.loads(res.read().decode())
            log_perf("telegram_api_multipart", started_at, method=method, ok=result.get("ok"), files=len(files), bytes=len(body))
            return result
    except urllib.error.HTTPError as err:
        log_perf("telegram_api_multipart_http_error", started_at, method=method, files=len(files), bytes=len(body))
        try:
            body = err.read().decode()
            print(body, file=sys.stderr)
            return json.loads(body)
        except Exception:
            pass
        traceback.print_exc()
        return {"ok": False, "error_code": err.code, "description": str(err)}
    except Exception:
        log_perf("telegram_api_multipart_error", started_at, method=method, files=len(files), bytes=len(body))
        traceback.print_exc()
        return None


class _BytearrayWriter:
    def __init__(self, body):
        self.body = body

    def write(self, data):
        self.body.extend(data)
        return len(data)


def inline_keyboard(buttons):
    return json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)


def reply_keyboard(buttons, one_time=True):
    return json.dumps(
        {"keyboard": buttons, "resize_keyboard": True, "one_time_keyboard": one_time},
        ensure_ascii=False,
    )


def media_url(file_name):
    if not PUBLIC_BASE_URL:
        return None
    return f"{PUBLIC_BASE_URL}/media/{urllib.parse.quote(file_name)}"


def app_media_path(file_name):
    return f"/media/{urllib.parse.quote(file_name)}"


def send_message(chat_id, text, keyboard=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if keyboard:
        payload["reply_markup"] = keyboard
    result = telegram_api("sendMessage", payload)
    update_delivery_status(chat_id, result)
    return result


def classify_delivery_error(result):
    description = (result or {}).get("description", "")
    lowered = description.lower()
    if "blocked by the user" in lowered or "bot was blocked" in lowered:
        return "blocked"
    if "user is deactivated" in lowered:
        return "deactivated"
    if "chat not found" in lowered or "forbidden" in lowered:
        return "unreachable"
    return "unreachable"


def update_delivery_status(chat_id, result):
    if not chat_id or result is None:
        return
    now = iso_now()
    if result.get("ok"):
        with db() as conn:
            conn.execute(
                """
                update subscribers
                set delivery_status='active',
                    last_delivery_at=?,
                    last_delivery_error=null
                where chat_id=?
                """,
                (now, chat_id),
            )
        return
    status = classify_delivery_error(result)
    description = result.get("description") or json.dumps(result, ensure_ascii=False)
    with db() as conn:
        conn.execute(
            """
            update subscribers
            set delivery_status=?,
                last_delivery_error_at=?,
                last_delivery_error=?
            where chat_id=?
            """,
            (status, now, description, chat_id),
        )


def configure_bot_menu_button():
    if not PUBLIC_BASE_URL:
        return
    telegram_api(
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "web_app",
                "text": "Каталог",
                "web_app": {"url": f"{PUBLIC_BASE_URL}/app"},
            }
        },
    )


def send_media(chat_id, media_row):
    path = UPLOAD_DIR / media_row["file_name"]
    if not path.exists():
        return None
    method = "sendVideo" if media_row["mime_type"].startswith("video/") else "sendPhoto"
    field = "video" if method == "sendVideo" else "photo"
    return telegram_api_multipart(method, {"chat_id": chat_id}, field, path, media_row["mime_type"])


def send_media_path(chat_id, file_path, mime_type, caption=None):
    if not file_path.exists():
        return None
    method = "sendVideo" if mime_type.startswith("video/") else "sendPhoto"
    field = "video" if method == "sendVideo" else "photo"
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = media_caption(caption)
        fields["parse_mode"] = "HTML"
    return telegram_api_multipart(method, fields, field, file_path, mime_type)


def send_media_with_caption(chat_id, media_row, caption, keyboard):
    path = UPLOAD_DIR / media_row["file_name"]
    if not path.exists():
        return None
    method = "sendVideo" if media_row["mime_type"].startswith("video/") else "sendPhoto"
    field = "video" if method == "sendVideo" else "photo"
    return telegram_api_multipart(
        method,
        {
            "chat_id": chat_id,
            "caption": media_caption(caption),
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        },
        field,
        path,
        media_row["mime_type"],
    )


def send_media_album_paths(chat_id, media_items, caption=None):
    media = []
    files = []
    for index, item in enumerate(media_items[:10]):
        path = item["path"]
        if not path.exists():
            continue
        field = f"file{index}"
        item_type = "video" if item["mime_type"].startswith("video/") else "photo"
        media_item = {"type": item_type, "media": f"attach://{field}"}
        if index == 0 and caption:
            media_item["caption"] = media_caption(caption)
            media_item["parse_mode"] = "HTML"
        media.append(media_item)
        files.append((field, path, path.name, item["mime_type"]))
    if not media:
        return None
    return telegram_api_multipart_files(
        "sendMediaGroup",
        {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
        files,
    )


def send_media_album(chat_id, media_rows, caption):
    media = []
    files = []
    for index, row in enumerate(media_rows[:10]):
        path = UPLOAD_DIR / row["file_name"]
        if not path.exists():
            continue
        field = f"file{index}"
        item_type = "video" if row["mime_type"].startswith("video/") else "photo"
        item = {"type": item_type, "media": f"attach://{field}"}
        if index == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
        files.append((field, path, row["file_name"], row["mime_type"]))
    if not media:
        return None
    return telegram_api_multipart_files(
        "sendMediaGroup",
        {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
        files,
    )


def media_caption(caption):
    if len(caption) <= 1024:
        return caption
    return caption[:1000].rstrip() + "\n..."


def is_supported_collage_image(row):
    mime_type = (row["mime_type"] or "").lower()
    suffix = Path(row["file_name"]).suffix.lower()
    return mime_type in {"image/jpeg", "image/jpg", "image/png", "image/webp"} or suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }


def build_photo_collage(project_id, media_rows):
    started_at = time.perf_counter()
    image_rows = [
        row
        for row in media_rows
        if row["mime_type"].startswith("image/") and is_supported_collage_image(row)
    ]
    if not image_rows:
        return None
    if len(image_rows) == 1:
        return UPLOAD_DIR / image_rows[0]["file_name"]

    selected = image_rows[:9]
    output_dir = DATA_DIR / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"project_{project_id}_collage.jpg"
    source_paths = [UPLOAD_DIR / row["file_name"] for row in selected]
    source_mtimes = [p.stat().st_mtime for p in source_paths if p.exists()]
    if WATERMARK_LOGO_PATH.exists():
        source_mtimes.append(WATERMARK_LOGO_PATH.stat().st_mtime)
    if output_path.exists() and source_mtimes and output_path.stat().st_mtime >= max(source_mtimes):
        return output_path

    from PIL import Image, ImageDraw, ImageFont, ImageOps

    count = len(selected)
    cols = 3 if count > 4 else 2
    rows = (count + cols - 1) // cols
    cell = 480
    gap = 8
    width = cols * cell + (cols - 1) * gap
    height = rows * cell + (rows - 1) * gap
    canvas = Image.new("RGB", (width, height), "white")

    for index, row in enumerate(selected):
        path = UPLOAD_DIR / row["file_name"]
        try:
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img = ImageOps.fit(img, (cell, cell), method=Image.Resampling.LANCZOS)
                x = (index % cols) * (cell + gap)
                y = (index // cols) * (cell + gap)
                canvas.paste(img, (x, y))
        except Exception:
            traceback.print_exc()
            continue

    add_collage_watermark(canvas, Image, ImageDraw, ImageFont)

    canvas.save(output_path, "JPEG", quality=82, optimize=True)
    log_perf("build_photo_collage", started_at, project_id=project_id, images=len(selected), bytes=output_path.stat().st_size)
    return output_path


def add_collage_watermark(canvas, image, image_draw, image_font):
    font = None
    for font_path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ):
        try:
            font = image_font.truetype(font_path, max(24, int(canvas.size[0] * 0.035)))
            break
        except Exception:
            continue
    if font is None:
        font = image_font.load_default()

    link_text = "t.me/belowmarketdubaibot"
    link_layer = image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = image_draw.Draw(link_layer)
    text_bbox = draw.textbbox((0, 0), link_text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    margin = max(18, int(canvas.size[0] * 0.018))
    text_x = canvas.size[0] - text_width - margin
    text_y = canvas.size[1] - text_height - margin - text_bbox[1]
    base = canvas.convert("RGBA")
    shadow_offset = max(2, int(canvas.size[0] * 0.003))
    draw.text((text_x + shadow_offset, text_y + shadow_offset), link_text, fill=(0, 0, 0, 190), font=font)
    draw.text((text_x, text_y), link_text, fill=(255, 255, 255, 235), font=font)
    base = image.alpha_composite(base, link_layer)
    canvas.paste(base.convert("RGB"))


def send_project_card(chat_id, project, media_rows, caption, keyboard):
    started_at = time.perf_counter()
    project_data = dict(project)
    signature = project_media_signature(media_rows)
    cached_file_id = project_data.get("tg_media_file_id")
    cached_kind = project_data.get("tg_media_kind")
    cached_signature = project_data.get("tg_media_signature")
    if signature and cached_file_id and cached_kind and cached_signature == signature:
        field = "video" if cached_kind == "video" else "photo"
        result = telegram_api(
            "sendVideo" if cached_kind == "video" else "sendPhoto",
            {
                "chat_id": chat_id,
                field: cached_file_id,
                "caption": media_caption(caption),
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            },
        )
        if result and result.get("ok"):
            log_perf("send_project_card", started_at, project_id=project["id"], mode=f"cached_{cached_kind}", media=len(media_rows))
            return result

    image_rows = [
        row
        for row in media_rows
        if row["mime_type"].startswith("image/") and is_supported_collage_image(row)
    ]
    if len(image_rows) > 1:
        collage_path = build_photo_collage(project["id"], image_rows)
        if collage_path:
            result = telegram_api_multipart(
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "caption": media_caption(caption),
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
                "photo",
                collage_path,
                "image/jpeg",
            )
            cache_project_media_file_id(project["id"], signature, "photo", result)
            log_perf("send_project_card", started_at, project_id=project["id"], mode="collage", media=len(media_rows), ok=result.get("ok") if result else False)
            return result
    if media_rows:
        first_supported = next(
            (
                row
                for row in media_rows
                if row["mime_type"].startswith("video/")
                or (row["mime_type"].startswith("image/") and is_supported_collage_image(row))
            ),
            None,
        )
        if first_supported:
            result = send_media_with_caption(chat_id, first_supported, caption, keyboard)
            kind = "video" if first_supported["mime_type"].startswith("video/") else "photo"
            cache_project_media_file_id(project["id"], signature, kind, result)
            log_perf("send_project_card", started_at, project_id=project["id"], mode=f"single_{kind}", media=len(media_rows), ok=result.get("ok") if result else False)
            return result
    result = send_message(chat_id, caption, keyboard=keyboard)
    log_perf("send_project_card", started_at, project_id=project["id"], mode="text", media=len(media_rows), ok=result.get("ok") if result else False)
    return result


def project_media_signature(media_rows):
    if not media_rows:
        return ""
    image_rows = [
        row
        for row in media_rows
        if row["mime_type"].startswith("image/") and is_supported_collage_image(row)
    ]
    selected = image_rows[:9] if len(image_rows) > 1 else media_rows[:1]
    parts = []
    for row in selected:
        path = UPLOAD_DIR / row["file_name"]
        if path.exists():
            stat = path.stat()
            parts.append(f"{row['file_name']}:{row['mime_type']}:{stat.st_size}:{int(stat.st_mtime)}")
        else:
            parts.append(f"{row['file_name']}:{row['mime_type']}:missing")
    if len(image_rows) > 1 and WATERMARK_LOGO_PATH.exists():
        stat = WATERMARK_LOGO_PATH.stat()
        parts.append(f"watermark:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def extract_telegram_file_id(result, kind):
    if not result or not result.get("ok"):
        return None
    payload = result.get("result") or {}
    if kind == "video" and payload.get("video"):
        return payload["video"].get("file_id")
    photos = payload.get("photo") or []
    if photos:
        return photos[-1].get("file_id")
    return None


def cache_project_media_file_id(project_id, signature, kind, result):
    file_id = extract_telegram_file_id(result, kind)
    if not file_id or not signature:
        return
    with db() as conn:
        conn.execute(
            """
            update projects
            set tg_media_file_id = ?, tg_media_kind = ?, tg_media_signature = ?
            where id = ?
            """,
            (file_id, kind, signature, project_id),
        )


def mark_seen(chat_id, project_id):
    with db() as conn:
        conn.execute(
            "insert or ignore into seen_projects(chat_id, project_id, seen_at) values (?, ?, ?)",
            (chat_id, project_id, iso_now()),
        )


def log_event(chat_id, user, event_type, project_id=None, broadcast_id=None, payload=None):
    with db() as conn:
        conn.execute(
            """
            insert into bot_events(chat_id, username, name, event_type, project_id, broadcast_id, payload, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user.get("username") if user else None,
                lead_name(user) if user else str(chat_id),
                event_type,
                project_id,
                broadcast_id,
                payload,
                iso_now(),
            ),
        )


def get_project(project_id):
    with db() as conn:
        return conn.execute("select * from projects where id = ?", (project_id,)).fetchone()


def get_project_media(project_id):
    with db() as conn:
        return conn.execute("select * from media where project_id = ? order by id", (project_id,)).fetchall()


def first_active_project():
    with db() as conn:
        return conn.execute(
            "select * from projects where status = 'active' order by created_at asc, id asc limit 1"
        ).fetchone()


def project_order_clause(random_order=False):
    return "random()" if random_order else "p.created_at asc, p.id asc"


def next_project_for(chat_id, exclude_id=None, include_seen_filtered=False, random_order=False):
    with db() as conn:
        sub = conn.execute("select filter_rooms, filter_district from subscribers where chat_id = ?", (chat_id,)).fetchone()
        has_filters = bool(sub and (sub["filter_rooms"] or sub["filter_district"]))
        params = [chat_id]
        extra = ""
        if exclude_id:
            extra = "and p.id != ?"
            params.append(exclude_id)
        if sub and sub["filter_rooms"]:
            extra += " and p.rooms = ?"
            params.append(sub["filter_rooms"])
        if sub and sub["filter_district"]:
            extra += " and p.district = ?"
            params.append(sub["filter_district"])
        project = conn.execute(
            f"""
            select p.* from projects p
            left join seen_projects s on s.project_id = p.id and s.chat_id = ?
            where p.status = 'active' and s.project_id is null {extra}
            order by {project_order_clause(random_order)}
            limit 1
            """,
            params,
        ).fetchone()
        if project or not has_filters or not include_seen_filtered:
            return project

        params = []
        extra = ""
        if exclude_id:
            extra = "and p.id != ?"
            params.append(exclude_id)
        if sub and sub["filter_rooms"]:
            extra += " and p.rooms = ?"
            params.append(sub["filter_rooms"])
        if sub and sub["filter_district"]:
            extra += " and p.district = ?"
            params.append(sub["filter_district"])
        project = conn.execute(
            f"""
            select p.* from projects p
            where p.status = 'active' {extra}
            order by {project_order_clause(random_order)}
            limit 1
            """,
            params,
        ).fetchone()
        if project or not exclude_id:
            return project

        params = []
        extra = ""
        if sub and sub["filter_rooms"]:
            extra += " and p.rooms = ?"
            params.append(sub["filter_rooms"])
        if sub and sub["filter_district"]:
            extra += " and p.district = ?"
            params.append(sub["filter_district"])
        return conn.execute(
            f"""
            select p.* from projects p
            where p.status = 'active' {extra}
            order by {project_order_clause(random_order)}
            limit 1
            """,
            params,
        ).fetchone()


def filtered_projects_counts(chat_id):
    with db() as conn:
        sub = conn.execute("select filter_rooms, filter_district from subscribers where chat_id = ?", (chat_id,)).fetchone()
        params = []
        where = "where status = 'active'"
        if sub and sub["filter_rooms"]:
            where += " and rooms = ?"
            params.append(sub["filter_rooms"])
        if sub and sub["filter_district"]:
            where += " and district = ?"
            params.append(sub["filter_district"])
        total = conn.execute(f"select count(*) c from projects {where}", params).fetchone()["c"]
        unseen = conn.execute(
            f"""
            select count(*) c from projects p
            left join seen_projects s on s.project_id = p.id and s.chat_id = ?
            {where.replace('status', 'p.status').replace('rooms', 'p.rooms').replace('district', 'p.district')}
              and s.project_id is null
            """,
            [chat_id] + params,
        ).fetchone()["c"]
        return total, unseen


def offer_filter_change(chat_id, total):
    keyboard = inline_keyboard(
        [
            [{"text": "🔎 Изменить фильтры", "callback_data": "filter_start"}],
            [{"text": "👀 Показать просмотренные", "callback_data": "filter_show_seen"}],
            [{"text": "♻️ Сбросить фильтры", "callback_data": "filter_reset"}],
        ]
    )
    send_message(
        chat_id,
        f"По выбранным фильтрам все {total} лотов уже просмотрены. Можем изменить фильтры или показать эти лоты повторно.",
        keyboard=keyboard,
    )


def offer_after_filter_reset(chat_id):
    keyboard = inline_keyboard(
        [
            [{"text": "👀 Смотреть без фильтров", "callback_data": "filter_view_all"}],
            [{"text": "🔎 Применить другие фильтры", "callback_data": "filter_start"}],
        ]
    )
    send_message(
        chat_id,
        "Фильтры сброшены. Как продолжим?",
        keyboard=keyboard,
    )


def subscriber_filters_active(chat_id):
    with db() as conn:
        sub = conn.execute("select filter_rooms, filter_district from subscribers where chat_id = ?", (chat_id,)).fetchone()
    return bool(sub and (sub["filter_rooms"] or sub["filter_district"]))


def subscriber_repeats_seen_filters(chat_id):
    with db() as conn:
        sub = conn.execute("select state from subscribers where chat_id = ?", (chat_id,)).fetchone()
    return bool(sub and sub["state"] == "filter_show_seen")


def active_rooms_options():
    with db() as conn:
        return [
            row["rooms"]
            for row in conn.execute(
                "select distinct rooms from projects where status='active' and rooms != '' order by rooms"
            ).fetchall()
        ]


def active_district_options(rooms=None):
    with db() as conn:
        params = []
        where = "where status='active' and district != ''"
        if rooms:
            where += " and rooms = ?"
            params.append(rooms)
        return [
            row["district"]
            for row in conn.execute(f"select distinct district from projects {where} order by district", params).fetchall()
        ]


def send_project(chat_id, project, user=None):
    started_at = time.perf_counter()
    if not project:
        send_no_projects_message(chat_id)
        log_perf("send_project", started_at, chat_id=chat_id, project_id="none")
        return False
    caption = project_caption(project)
    media = get_project_media(project["id"])
    result = send_project_card(chat_id, project, media, caption, project_actions_keyboard(project["id"], subscriber_filters_active(chat_id)))
    update_delivery_status(chat_id, result)
    if not result or not result.get("ok"):
        log_perf("send_project", started_at, chat_id=chat_id, project_id=project["id"], ok=False)
        return False
    mark_seen(chat_id, project["id"])
    log_event(chat_id, user or {}, "project_sent", project_id=project["id"])
    log_perf("send_project", started_at, chat_id=chat_id, project_id=project["id"], media=len(media))
    return True


def project_share_url(project_id):
    bot_link = f"https://t.me/{BOT_USERNAME}?start=lot_{project_id}"
    text = (
        "Посмотрите этот лот недвижимости в Дубае ниже рынка в Below Market Dubai:\n"
        f"{bot_link}"
    )
    return "https://t.me/share/url?" + urllib.parse.urlencode({"url": bot_link, "text": text})


def project_actions_keyboard(project_id, filters_active=False):
    filter_button = (
        {"text": "♻️ Сбросить фильтры", "callback_data": "filter_reset"}
        if filters_active
        else {"text": "🔎 Искать по фильтрам", "callback_data": "filter_start"}
    )
    rows = [
        [{"text": "💬 Хочу узнать подробнее", "callback_data": f"interest:{project_id}"}],
        [{"text": "👀 Смотреть еще", "callback_data": f"next:{project_id}"}],
        [{"text": "↗️ Поделиться лотом", "url": project_share_url(project_id)}],
        [filter_button],
    ]
    if PUBLIC_BASE_URL:
        rows.append([{"text": "🏙 Открыть каталог", "web_app": {"url": f"{PUBLIC_BASE_URL}/app"}}])
    return inline_keyboard(
        rows
    )


def welcome_keyboard():
    rows = []
    if PUBLIC_BASE_URL:
        rows.append([{"text": "🏙 Открыть каталог", "web_app": {"url": f"{PUBLIC_BASE_URL}/app"}}])
    rows.append([{"text": "🎯 Искать по фильтрам", "callback_data": "filter_start"}])
    return inline_keyboard(rows)


def send_welcome_message(chat_id):
    text = (
        "Добро пожаловать в Below Market Dubai 🏙\n\n"
        "Я Александр, лицензированный брокер в Дубае.\n\n"
        "В этом боте я собираю объекты недвижимости, которые можно купить ниже рынка: "
        "distress deals, срочные продажи и варианты, которых часто нет в открытых листингах.\n\n"
        "Здесь можно:\n"
        "🔥 смотреть актуальные лоты ниже рынка\n"
        "📊 быстро понимать выгоду по цене\n"
        "🏡 открыть каталог и изучить варианты самостоятельно\n"
        "🎯 подобрать объекты по району и количеству комнат\n"
        "📩 получать новые предложения 2 раза в день, без ночных уведомлений\n\n"
        "Если хотите просто посмотреть рынок — откройте каталог.\n"
        "Если уже есть запрос по бюджету, району или цели покупки — начните с фильтров."
    )
    send_message(chat_id, text, keyboard=welcome_keyboard())


def send_no_projects_message(chat_id):
    text = (
        "Сейчас новых актуальных лотов нет.\n\n"
        "Но мы можем сделать для вас персональный подбор недвижимости ниже рынка в Дубае: "
        "подберём варианты под бюджет, район, цель покупки и желаемую доходность.\n\n"
        "Оставьте заявку, и <a href=\"https://t.me/roi_counter\">@roi_counter</a> с вами свяжется."
    )
    keyboard = inline_keyboard(
        [[{"text": "📝 Оставить заявку на подбор", "callback_data": "personal_interest"}]]
    )
    send_message(chat_id, text, keyboard=keyboard)


def catalog_followup_variant(chat_id):
    digest = hashlib.sha256(str(chat_id).encode()).hexdigest()
    return "a" if int(digest[:8], 16) % 2 == 0 else "b"


def followup_variant_label(variant):
    return {
        "a": "A — подбор 3-5 лотов",
        "b": "B — консультация / актуальна покупка",
    }.get(variant or "", "Не задан")


def send_catalog_followup(chat_id, name, variant="a"):
    first_name = (name or "").strip()
    greeting = f"Здравствуйте, {html.escape(first_name)}!" if first_name else "Здравствуйте!"
    if variant == "b":
        text = (
            f"{greeting}\n"
            "Это Александр.\n"
            "Спасибо за интерес к моему боту.\n"
            "Буду рад проконсультировать вас по вопросам приобретения недвижимости в Дубае.\n"
            "Подскажите, актуальна сейчас покупка?"
        )
        buttons = [
            {"text": "✅ Да, актуальна", "callback_data": "catalog_followup_yes"},
            {"text": "👀 Пока нет", "callback_data": "catalog_followup_no"},
        ]
    else:
        text = (
            f"{greeting}\n"
            "Это Александр.\n"
            "Спасибо за интерес к моему боту.\n"
            "Вижу, вы посмотрели каталог. Хотите, я подберу 3-5 лотов ниже рынка под ваш бюджет и цель покупки?"
        )
        buttons = [
            {"text": "✅ Да, подобрать", "callback_data": "catalog_followup_yes"},
            {"text": "👀 Пока просто смотрю", "callback_data": "catalog_followup_no"},
        ]
    keyboard = inline_keyboard(
        [
            buttons
        ]
    )
    return send_message(chat_id, text, keyboard=keyboard)


def send_catalog_positive_reply(chat_id, user):
    text = (
        "Спасибо за ответ.\n"
        "В скором времени я свяжусь с вами по поводу консультации в личных сообщениях.\n\n"
        "@roi_counter"
    )
    keyboard = inline_keyboard(
        [[{"text": "💬 Открыть диалог с @roi_counter", "url": "https://t.me/roi_counter"}]]
    )
    result = send_message(chat_id, text, keyboard=keyboard)
    create_personal_lead_async(
        chat_id,
        user,
        method="catalog_followup",
        value=f"@{user.get('username')}" if user.get("username") else str(chat_id),
    )
    return result


def send_filter_rooms_prompt(chat_id):
    rooms = active_rooms_options()
    if not rooms:
        send_no_projects_message(chat_id)
        return
    buttons = [[{"text": room, "callback_data": f"filter_rooms:{room}"}] for room in rooms[:20]]
    buttons.append([{"text": "♻️ Сбросить фильтры", "callback_data": "filter_reset"}])
    send_message(chat_id, "Выберите количество комнат:", keyboard=inline_keyboard(buttons))


def send_filter_district_prompt(chat_id, rooms):
    districts = active_district_options(rooms)
    if not districts:
        send_message(chat_id, "По выбранному количеству комнат сейчас нет активных районов.", keyboard=inline_keyboard([[{"text": "♻️ Сбросить фильтры", "callback_data": "filter_reset"}]]))
        return
    buttons = [[{"text": district, "callback_data": f"filter_district:{urllib.parse.quote(district)}"}] for district in districts[:30]]
    buttons.append([{"text": "♻️ Сбросить фильтры", "callback_data": "filter_reset"}])
    send_message(chat_id, "Теперь выберите район:", keyboard=inline_keyboard(buttons))


def send_project_actions(chat_id, project_id):
    send_message(chat_id, ".", keyboard=project_actions_keyboard(project_id), parse_mode=None)


def notify_admin(text):
    started_at = time.perf_counter()
    count = 0
    for chat_id in [item.strip() for item in ADMIN_CHAT_ID.split(",") if item.strip()]:
        send_message(chat_id, text)
        count += 1
    log_perf("notify_admin", started_at, admins=count)


def save_chat_message(chat_id, user, direction, text):
    with db() as conn:
        conn.execute(
            """
            insert into chat_messages(chat_id, username, name, direction, text, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user.get("username") if user else None,
                lead_name(user) if user else str(chat_id),
                direction,
                text,
                iso_now(),
            ),
        )


def handle_incoming_chat_message(chat_id, user, text):
    save_chat_message(chat_id, user, "user", text)
    notify_admin(
        "\n".join(
            [
                "Новое сообщение от пользователя",
                f"Клиент: {lead_name(user)}",
                f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                f"Сообщение: {html.escape(text)}",
            ]
        )
    )
    send_message(chat_id, "Получили сообщение. Менеджер ответит вам здесь.")


def send_admin_message(chat_id, text):
    result = send_message(chat_id, text, parse_mode=None)
    if result and result.get("ok"):
        with db() as conn:
            sub = conn.execute("select * from subscribers where chat_id = ?", (chat_id,)).fetchone()
        user = (
            {
                "id": chat_id,
                "username": sub["username"],
                "first_name": sub["first_name"],
                "last_name": sub["last_name"],
            }
            if sub
            else {"id": chat_id}
        )
        save_chat_message(chat_id, user, "admin", text)
        return True
    return False


def segment_subscribers(segment):
    with db() as conn:
        if segment == "realtors":
            return conn.execute(
                """
                select * from subscribers
                where subscriber_tag='realtor' and coalesce(delivery_status, 'active') = 'active'
                order by last_seen_at desc
                """
            ).fetchall()
        if segment == "end_users":
            return conn.execute(
                """
                select * from subscribers
                where subscriber_tag='end_user' and coalesce(delivery_status, 'active') = 'active'
                order by last_seen_at desc
                """
            ).fetchall()
        if segment == "active_7d":
            return conn.execute(
                "select * from subscribers where last_seen_at >= ? and coalesce(delivery_status, 'active') = 'active' order by last_seen_at desc",
                ((now_local() - timedelta(days=7)).replace(microsecond=0).isoformat(),),
            ).fetchall()
        if segment == "has_lot_leads":
            return conn.execute(
                """
                select distinct s.* from subscribers s
                join leads l on l.chat_id = s.chat_id
                order by s.last_seen_at desc
                """
            ).fetchall()
        if segment == "has_personal_leads":
            return conn.execute(
                """
                select distinct s.* from subscribers s
                join personal_leads l on l.chat_id = s.chat_id
                order by s.last_seen_at desc
                """
            ).fetchall()
        if segment == "clicked_interest":
            return conn.execute(
                """
                select distinct s.* from subscribers s
                join bot_events e on e.chat_id = s.chat_id
                where e.event_type in ('click_interest', 'click_personal_interest', 'click_custom_interest')
                order by s.last_seen_at desc
                """
            ).fetchall()
        return conn.execute(
            "select * from subscribers where coalesce(delivery_status, 'active') = 'active' order by subscribed_at desc"
        ).fetchall()


def send_segment_message(segment, text):
    subscribers = segment_subscribers(segment)
    count = 0
    for sub in subscribers:
        if send_admin_message(sub["chat_id"], text):
            count += 1
        time.sleep(0.05)
    return count, len(subscribers)


def upsert_subscriber(user, chat_id):
    with db() as conn:
        existing = conn.execute("select chat_id from subscribers where chat_id = ?", (chat_id,)).fetchone()
        conn.execute(
            """
            insert into subscribers(chat_id, username, first_name, last_name, subscribed_at, last_seen_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(chat_id) do update set
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name,
              last_seen_at=excluded.last_seen_at,
              delivery_status='active'
            """,
            (
                chat_id,
                user.get("username"),
                user.get("first_name"),
                user.get("last_name"),
                iso_now(),
                iso_now(),
            ),
        )
    if not existing:
        notify_admin(
            "\n".join(
                [
                    "Новый подписчик",
                    f"Клиент: {lead_name(user)}",
                    f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                ]
            )
        )


def lead_name(user):
    parts = [user.get("first_name"), user.get("last_name")]
    return " ".join([p for p in parts if p]) or user.get("username") or str(user.get("id"))


def lead_thank_you_text(user):
    name = user.get("first_name") or user.get("username")
    greeting = f"Спасибо, {name}!" if name else "Спасибо!"
    return f"{greeting} Я получил ваш запрос. Напишу вам в Telegram и подскажу детали по объекту."


def create_lead(project_id, chat_id, user, method=None, value=None, message=None):
    with db() as conn:
        cur = conn.execute(
            """
            insert into leads(project_id, chat_id, username, name, contact_method, contact_value, message, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                chat_id,
                user.get("username"),
                lead_name(user),
                method,
                value,
                message,
                iso_now(),
            ),
        )
        lead_id = cur.lastrowid
    project = get_project(project_id)
    notify_admin(
        "\n".join(
            [
                "Новая заявка",
                f"Объект: {project['title'] if project else project_id}",
                f"Клиент: {lead_name(user)}",
                f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                f"Контакт: {method or 'не выбран'} {value or ''}".strip(),
            ]
        )
    )
    create_crm_card(
        f"Заявка по лоту: {project['title'] if project else project_id}",
        chat_id=chat_id,
        client_name=lead_name(user),
        contact_value=value or "",
        request=message or (project["district"] if project else ""),
        source="Заявка по лоту",
    )
    return lead_id


def create_custom_broadcast_lead(broadcast_id, chat_id, user, method=None, value=None):
    with db() as conn:
        broadcast = conn.execute(
            "select * from custom_broadcasts where id = ?", (broadcast_id,)
        ).fetchone()
        cur = conn.execute(
            """
            insert into custom_broadcast_leads(broadcast_id, chat_id, username, name, contact_method, contact_value, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                broadcast_id,
                chat_id,
                user.get("username"),
                lead_name(user),
                method,
                value,
                iso_now(),
            ),
        )
        lead_id = cur.lastrowid
    text_preview = broadcast["text"][:120] + ("..." if broadcast and len(broadcast["text"]) > 120 else "") if broadcast else str(broadcast_id)
    notify_admin(
        "\n".join(
            [
                "Новая заявка по свободной рассылке",
                f"Рассылка: {text_preview}",
                f"Клиент: {lead_name(user)}",
                f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                f"Контакт: {method or 'не выбран'} {value or ''}".strip(),
            ]
        )
    )
    create_crm_card(
        "Интерес к свободной рассылке",
        chat_id=chat_id,
        client_name=lead_name(user),
        contact_value=value or "",
        request=text_preview,
        source="Свободная рассылка",
    )
    return lead_id


def create_personal_lead(chat_id, user, method=None, value=None):
    with db() as conn:
        cur = conn.execute(
            """
            insert into personal_leads(chat_id, username, name, contact_method, contact_value, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user.get("username"),
                lead_name(user),
                method,
                value,
                iso_now(),
            ),
        )
        lead_id = cur.lastrowid
    notify_admin(
        "\n".join(
            [
                "Новая заявка на персональный подбор",
                f"Клиент: {lead_name(user)}",
                f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                f"Контакт: {method or 'не выбран'} {value or ''}".strip(),
            ]
        )
    )
    create_crm_card(
        "Персональный подбор",
        chat_id=chat_id,
        client_name=lead_name(user),
        contact_value=value or "",
        request="Подобрать лоты под запрос",
        source="Персональный подбор",
    )
    return lead_id


def create_personal_lead_async(chat_id, user, method=None, value=None):
    threading.Thread(
        target=create_personal_lead,
        args=(chat_id, dict(user or {})),
        kwargs={"method": method, "value": value},
        daemon=True,
    ).start()


def handle_start(chat_id, user, payload=""):
    upsert_subscriber(user, chat_id)
    if payload.startswith("lot_"):
        try:
            project_id = int(payload.split("_", 1)[1])
        except (TypeError, ValueError):
            project_id = None
        project = get_project(project_id) if project_id else None
        if project and project["status"] == "active":
            log_event(chat_id, user, "start_shared_lot", project_id=project["id"], payload=payload)
            send_project(chat_id, project, user=user)
            return
        send_no_projects_message(chat_id)
        return
    send_welcome_message(chat_id)


def handle_text(message):
    started_at = time.perf_counter()
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    text = message.get("text", "")
    contact = message.get("contact")
    try:
        upsert_subscriber(user, chat_id)
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            handle_start(chat_id, user, parts[1].strip() if len(parts) > 1 else "")
            return

        with db() as conn:
            sub = conn.execute("select * from subscribers where chat_id = ?", (chat_id,)).fetchone()
        if not sub:
            send_message(chat_id, "Нажмите «Смотреть объекты», чтобы получить актуальный лот.")
            return

        project_id = sub["state_project_id"]
        if contact:
            value = contact.get("phone_number")
            if sub["state"] == "awaiting_custom_phone" and project_id:
                create_custom_broadcast_lead(project_id, chat_id, user, method="phone", value=value)
            elif sub["state"] == "awaiting_personal_phone":
                create_personal_lead(chat_id, user, method="phone", value=value)
            elif sub["state"] == "awaiting_phone" and project_id:
                create_lead(project_id, chat_id, user, method="phone", value=value)
            else:
                send_message(chat_id, "Выберите кнопку заявки под лотом или запросите персональный подбор.")
                return
            with db() as conn:
                conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
            send_message(chat_id, lead_thank_you_text(user), keyboard=json.dumps({"remove_keyboard": True}))
            return

        if sub["state"] == "awaiting_personal_whatsapp":
            create_personal_lead(chat_id, user, method="whatsapp", value=text)
            with db() as conn:
                conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
            send_message(chat_id, lead_thank_you_text(user), keyboard=json.dumps({"remove_keyboard": True}))
            return

        if not sub["state_project_id"]:
            handle_incoming_chat_message(chat_id, user, text)
            return

        if sub["state"] == "awaiting_whatsapp":
            create_lead(project_id, chat_id, user, method="whatsapp", value=text)
            with db() as conn:
                conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
            send_message(chat_id, lead_thank_you_text(user), keyboard=json.dumps({"remove_keyboard": True}))
            return

        if sub["state"] == "awaiting_custom_whatsapp":
            create_custom_broadcast_lead(project_id, chat_id, user, method="whatsapp", value=text)
            with db() as conn:
                conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
            send_message(chat_id, lead_thank_you_text(user), keyboard=json.dumps({"remove_keyboard": True}))
            return

        handle_incoming_chat_message(chat_id, user, text)
    finally:
        log_perf("handle_text", started_at, chat_id=chat_id, has_contact=bool(contact), command=text[:24] if text.startswith("/") else "")


def handle_callback(callback):
    started_at = time.perf_counter()
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    try:
        return handle_callback_inner(callback)
    finally:
        log_perf("handle_callback", started_at, chat_id=chat_id, data=data[:80])


def handle_callback_inner(callback):
    query_id = callback["id"]
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user = callback.get("from", {})
    if not chat_id:
        return
    upsert_subscriber(user, chat_id)
    telegram_api("answerCallbackQuery", {"callback_query_id": query_id})

    if data == "start_view":
        log_event(chat_id, user, "click_start_view", payload=data)
        send_project(chat_id, next_project_for(chat_id), user=user)
        return

    if data == "filter_start":
        log_event(chat_id, user, "click_filter_start", payload=data)
        with db() as conn:
            conn.execute("update subscribers set state=null, filter_rooms=null, filter_district=null where chat_id = ?", (chat_id,))
        send_filter_rooms_prompt(chat_id)
        return

    if data == "filter_reset":
        log_event(chat_id, user, "click_filter_reset", payload=data)
        with db() as conn:
            conn.execute("update subscribers set state=null, filter_rooms=null, filter_district=null where chat_id = ?", (chat_id,))
        offer_after_filter_reset(chat_id)
        return

    if data == "filter_view_all":
        log_event(chat_id, user, "click_filter_view_all", payload=data)
        with db() as conn:
            conn.execute("update subscribers set state=null, filter_rooms=null, filter_district=null where chat_id = ?", (chat_id,))
        send_message(chat_id, "Показываю актуальные лоты без фильтров.")
        send_project(chat_id, next_project_for(chat_id), user=user)
        return

    if data == "filter_show_seen":
        log_event(chat_id, user, "click_filter_show_seen", payload=data)
        with db() as conn:
            conn.execute("update subscribers set state='filter_show_seen' where chat_id = ?", (chat_id,))
        send_project(chat_id, next_project_for(chat_id, include_seen_filtered=True), user=user)
        return

    if data.startswith("filter_rooms:"):
        rooms = data.split(":", 1)[1]
        log_event(chat_id, user, "click_filter_rooms", payload=rooms)
        with db() as conn:
            conn.execute("update subscribers set state=null, filter_rooms=?, filter_district=null where chat_id = ?", (rooms, chat_id))
        send_filter_district_prompt(chat_id, rooms)
        return

    if data.startswith("filter_district:"):
        district = urllib.parse.unquote(data.split(":", 1)[1])
        log_event(chat_id, user, "click_filter_district", payload=district)
        with db() as conn:
            conn.execute("update subscribers set state=null, filter_district=? where chat_id = ?", (district, chat_id))
        total, unseen = filtered_projects_counts(chat_id)
        if total and not unseen:
            offer_filter_change(chat_id, total)
        else:
            send_message(chat_id, f"Нашла подходящих вариантов: {total}. Новых для просмотра: {unseen}. Буду отправлять их по очереди.")
            send_project(chat_id, next_project_for(chat_id), user=user)
        return

    if data == "personal_interest":
        log_event(chat_id, user, "click_personal_interest", payload=data)
        keyboard = inline_keyboard(
            [
                [{"text": "✈️ Написать в Telegram", "callback_data": "personal_tg"}],
                [{"text": "📞 Оставить телефон", "callback_data": "personal_phone"}],
                [{"text": "🟢 Оставить WhatsApp", "callback_data": "personal_whatsapp"}],
            ]
        )
        send_message(chat_id, "Как вам удобнее, чтобы @roi_counter связался с вами для персонального подбора?", keyboard=keyboard)
        return

    if data == "personal_tg":
        log_event(chat_id, user, "click_personal_tg", payload=data)
        create_personal_lead(chat_id, user, method="telegram", value=f"@{user.get('username')}" if user.get("username") else str(chat_id))
        send_message(chat_id, lead_thank_you_text(user))
        return

    if data == "personal_phone":
        log_event(chat_id, user, "click_personal_phone", payload=data)
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_personal_phone', state_project_id = null where chat_id = ?",
                (chat_id,),
            )
        keyboard = reply_keyboard([[{"text": "📞 Отправить телефон", "request_contact": True}]])
        send_message(chat_id, "Нажмите кнопку ниже, чтобы поделиться номером телефона.", keyboard=keyboard)
        return

    if data == "personal_whatsapp":
        log_event(chat_id, user, "click_personal_whatsapp", payload=data)
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_personal_whatsapp', state_project_id = null where chat_id = ?",
                (chat_id,),
            )
        send_message(chat_id, "Напишите номер WhatsApp одним сообщением.")
        return

    if data == "catalog_followup_yes":
        log_event(chat_id, user, "catalog_followup_yes", payload=data)
        with db() as conn:
            conn.execute(
                """
                update catalog_sessions
                set followup_answer='yes', followup_answered_at=?
                where chat_id=? and followup_sent_at is not null and followup_answer is null
                """,
                (iso_now(), chat_id),
            )
        send_catalog_positive_reply(chat_id, user)
        return

    if data == "catalog_followup_no":
        log_event(chat_id, user, "catalog_followup_no", payload=data)
        with db() as conn:
            conn.execute(
                """
                update catalog_sessions
                set followup_answer='no', followup_answered_at=?
                where chat_id=? and followup_sent_at is not null and followup_answer is null
                """,
                (iso_now(), chat_id),
            )
        send_message(chat_id, "Понял, спасибо. Каталог всегда доступен здесь, а когда покупка станет актуальна, я помогу подобрать варианты ниже рынка.")
        return

    action, _, raw_project_id = data.partition(":")
    if action == "lang" and raw_project_id in ("ru", "en"):
        log_event(chat_id, user, "click_language", payload=data)
        with db() as conn:
            conn.execute("update subscribers set language = ? where chat_id = ?", (raw_project_id, chat_id))
        send_project(chat_id, first_active_project(), user=user)
        return

    try:
        project_id = int(raw_project_id)
    except ValueError:
        project_id = None

    if action == "next":
        log_event(chat_id, user, "click_next", project_id=project_id, payload=data)
        repeat_seen_filters = subscriber_repeats_seen_filters(chat_id)
        if subscriber_filters_active(chat_id):
            total, unseen = filtered_projects_counts(chat_id)
            if total and not unseen and not repeat_seen_filters:
                notify_admin(
                    "\n".join(
                        [
                            "Клик «Смотреть еще»",
                            f"Клиент: {lead_name(user)}",
                            f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                            "По фильтрам новых лотов нет, предложено изменить фильтры",
                        ]
                    )
                )
                offer_filter_change(chat_id, total)
                return
        next_project = next_project_for(chat_id, exclude_id=project_id, include_seen_filtered=repeat_seen_filters)
        notify_admin(
            "\n".join(
                [
                    "Клик «Смотреть еще»",
                    f"Клиент: {lead_name(user)}",
                    f"Telegram: @{user.get('username')}" if user.get("username") else f"Chat ID: {chat_id}",
                    f"Следующий лот: {next_project['title'] if next_project else 'актуальных лотов больше нет'}",
                ]
            )
        )
        send_project(chat_id, next_project, user=user)
    elif action == "custom_interest" and project_id:
        log_event(chat_id, user, "click_custom_interest", broadcast_id=project_id, payload=data)
        keyboard = inline_keyboard(
            [
                [{"text": "✈️ Написать в Telegram", "callback_data": f"custom_tg:{project_id}"}],
                [{"text": "📞 Оставить телефон", "callback_data": f"custom_phone:{project_id}"}],
                [{"text": "🟢 Оставить WhatsApp", "callback_data": f"custom_whatsapp:{project_id}"}],
            ]
        )
        send_message(chat_id, "Куда отправить подробную информацию?", keyboard=keyboard)
    elif action == "custom_tg" and project_id:
        log_event(chat_id, user, "click_custom_tg", broadcast_id=project_id, payload=data)
        create_custom_broadcast_lead(project_id, chat_id, user, method="telegram", value=f"@{user.get('username')}" if user.get("username") else str(chat_id))
        send_message(chat_id, lead_thank_you_text(user))
    elif action == "custom_phone" and project_id:
        log_event(chat_id, user, "click_custom_phone", broadcast_id=project_id, payload=data)
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_custom_phone', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        keyboard = reply_keyboard([[{"text": "📞 Отправить телефон", "request_contact": True}]])
        send_message(chat_id, "Нажмите кнопку ниже, чтобы поделиться номером телефона.", keyboard=keyboard)
    elif action == "custom_whatsapp" and project_id:
        log_event(chat_id, user, "click_custom_whatsapp", broadcast_id=project_id, payload=data)
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_custom_whatsapp', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        send_message(chat_id, "Напишите номер WhatsApp одним сообщением.")
    elif action in ("interest", "contact") and project_id:
        log_event(chat_id, user, "click_interest", project_id=project_id, payload=data)
        keyboard = inline_keyboard(
            [
                [{"text": "✈️ Написать в Telegram", "callback_data": f"contact_tg:{project_id}"}],
                [{"text": "📞 Оставить телефон", "callback_data": f"contact_phone:{project_id}"}],
                [{"text": "🟢 Оставить WhatsApp", "callback_data": f"contact_whatsapp:{project_id}"}],
            ]
        )
        send_message(chat_id, "Куда отправить подробную информацию по этому лоту?", keyboard=keyboard)
    elif action == "contact_tg" and project_id:
        log_event(chat_id, user, "click_contact_tg", project_id=project_id, payload=data)
        create_lead(project_id, chat_id, user, method="telegram", value=f"@{user.get('username')}" if user.get("username") else str(chat_id))
        send_message(chat_id, lead_thank_you_text(user))
    elif action == "contact_phone" and project_id:
        log_event(chat_id, user, "click_contact_phone", project_id=project_id, payload=data)
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_phone', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        keyboard = reply_keyboard([[{"text": "Отправить телефон", "request_contact": True}]])
        send_message(chat_id, "Нажмите кнопку ниже, чтобы поделиться номером телефона.", keyboard=keyboard)
    elif action == "contact_whatsapp" and project_id:
        log_event(chat_id, user, "click_contact_whatsapp", project_id=project_id, payload=data)
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_whatsapp', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        send_message(chat_id, "Напишите номер WhatsApp одним сообщением.")


def bot_loop(stop_event):
    if not BOT_TOKEN:
        return
    offset = None
    while not stop_event.is_set():
        payload = {"timeout": 25}
        if offset:
            payload["offset"] = offset
        result = telegram_api("getUpdates", payload)
        if not result or not result.get("ok"):
            time.sleep(5)
            continue
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_text(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception:
                traceback.print_exc()


def send_to_all(project_id):
    started_at = time.perf_counter()
    project = get_project(project_id)
    if not project:
        return 0
    count = 0
    with db() as conn:
        subscribers = conn.execute("select chat_id from subscribers where coalesce(delivery_status, 'active') = 'active'").fetchall()
    for sub in subscribers:
        if send_project(sub["chat_id"], project):
            count += 1
            time.sleep(0.05)
    log_perf("send_to_all", started_at, project_id=project_id, subscribers=len(subscribers), sent=count)
    return count


def custom_broadcast_keyboard(broadcast_id):
    return inline_keyboard(
        [[{"text": "💬 Хочу узнать подробнее", "callback_data": f"custom_interest:{broadcast_id}"}]]
    )


def send_custom_to_all(broadcast_id, text, media_items):
    started_at = time.perf_counter()
    count = 0
    safe_text = html.escape(text)
    with db() as conn:
        subscribers = conn.execute("select chat_id from subscribers where coalesce(delivery_status, 'active') = 'active'").fetchall()
    for sub in subscribers:
        chat_id = sub["chat_id"]
        sent = False
        delivery_result = None
        keyboard = custom_broadcast_keyboard(broadcast_id)
        if media_items:
            caption = safe_text if len(safe_text) <= 1024 else None
            if len(safe_text) > 1024:
                delivery_result = send_message(chat_id, safe_text, keyboard=keyboard)
                sent = bool(delivery_result and delivery_result.get("ok"))
            if len(media_items) == 1:
                if caption:
                    delivery_result = telegram_api_multipart(
                        "sendVideo" if media_items[0]["mime_type"].startswith("video/") else "sendPhoto",
                        {
                            "chat_id": chat_id,
                            "caption": media_caption(caption),
                            "parse_mode": "HTML",
                            "reply_markup": keyboard,
                        },
                        "video" if media_items[0]["mime_type"].startswith("video/") else "photo",
                        media_items[0]["path"],
                        media_items[0]["mime_type"],
                    )
                    update_delivery_status(chat_id, delivery_result)
                    sent = bool(delivery_result and delivery_result.get("ok")) or sent
                else:
                    delivery_result = send_media_path(chat_id, media_items[0]["path"], media_items[0]["mime_type"])
                    update_delivery_status(chat_id, delivery_result)
                    sent = bool(delivery_result and delivery_result.get("ok")) or sent
            else:
                delivery_result = send_media_album_paths(chat_id, media_items, caption=caption)
                update_delivery_status(chat_id, delivery_result)
                sent = bool(delivery_result and delivery_result.get("ok")) or sent
                if caption:
                    send_message(chat_id, "Подробнее:", keyboard=keyboard, parse_mode=None)
        else:
            delivery_result = send_message(chat_id, safe_text, keyboard=keyboard)
            sent = bool(delivery_result and delivery_result.get("ok"))
        if sent:
            count += 1
        time.sleep(0.05)
    log_perf("send_custom_to_all", started_at, broadcast_id=broadcast_id, subscribers=len(subscribers), sent=count, media=len(media_items))
    return count


def get_custom_broadcast_media(broadcast_id):
    with db() as conn:
        rows = conn.execute(
            "select * from custom_broadcast_media where broadcast_id = ? order by id",
            (broadcast_id,),
        ).fetchall()
    return [
        {
            "file_name": row["file_name"],
            "original_name": row["original_name"],
            "mime_type": row["mime_type"],
            "path": BROADCAST_UPLOAD_DIR / row["file_name"],
        }
        for row in rows
    ]


def process_catalog_followups(moment):
    stale_cutoff = (moment - timedelta(minutes=CATALOG_STALE_SESSION_MINUTES)).replace(microsecond=0).isoformat()
    with db() as conn:
        stale_sessions = conn.execute(
            """
            select session_id, opened_at, last_seen_at
            from catalog_sessions
            where closed_at is null and last_seen_at <= ?
            limit 100
            """,
            (stale_cutoff,),
        ).fetchall()
        for session in stale_sessions:
            opened_at = parse_iso(session["opened_at"]) or moment
            last_seen_at = parse_iso(session["last_seen_at"]) or moment
            duration = max(0, int((last_seen_at - opened_at).total_seconds()))
            conn.execute(
                """
                update catalog_sessions
                set closed_at=?, duration_seconds=?
                where session_id=?
                """,
                (session["last_seen_at"], duration, session["session_id"]),
            )
    cutoff = (moment - timedelta(minutes=CATALOG_FOLLOWUP_DELAY_MINUTES)).replace(microsecond=0).isoformat()
    with db() as conn:
        sessions = conn.execute(
            """
            select cs.*, s.delivery_status
            from catalog_sessions cs
            left join subscribers s on s.chat_id = cs.chat_id
            where cs.chat_id is not null
              and cs.closed_at is not null
              and cs.closed_at <= ?
              and cs.had_lead = 0
              and cs.followup_sent_at is null
              and coalesce(s.delivery_status, 'active') = 'active'
              and not exists (
                select 1
                from catalog_sessions prev
                where prev.chat_id = cs.chat_id
                  and prev.session_id != cs.session_id
                  and prev.followup_sent_at is not null
              )
            order by cs.closed_at asc
            limit 20
            """,
            (cutoff,),
        ).fetchall()
    for session in sessions:
        variant = catalog_followup_variant(session["chat_id"])
        result = send_catalog_followup(session["chat_id"], session["name"], variant=variant)
        if result and result.get("ok"):
            with db() as conn:
                conn.execute(
                    "update catalog_sessions set followup_sent_at=?, followup_variant=? where session_id=?",
                    (iso_now(), variant, session["session_id"]),
                )
        time.sleep(0.05)


def send_catalog_followup_for_session(session_id):
    with db() as conn:
        session = conn.execute(
            """
            select cs.*, s.delivery_status
            from catalog_sessions cs
            left join subscribers s on s.chat_id = cs.chat_id
            where cs.session_id = ?
              and cs.chat_id is not null
              and cs.closed_at is not null
              and cs.had_lead = 0
              and cs.followup_sent_at is null
              and coalesce(s.delivery_status, 'active') = 'active'
              and not exists (
                select 1
                from catalog_sessions prev
                where prev.chat_id = cs.chat_id
                  and prev.session_id != cs.session_id
                  and prev.followup_sent_at is not null
              )
            """,
            (session_id,),
        ).fetchone()
    if not session:
        return
    variant = catalog_followup_variant(session["chat_id"])
    result = send_catalog_followup(session["chat_id"], session["name"], variant=variant)
    if result and result.get("ok"):
        with db() as conn:
            conn.execute(
                """
                update catalog_sessions
                set followup_sent_at=?, followup_variant=?
                where session_id=? and followup_sent_at is null
                """,
                (iso_now(), variant, session_id),
            )


def send_catalog_followup_for_session_async(session_id, delay_seconds=CATALOG_CLOSE_FOLLOWUP_DELAY_SECONDS):
    if not session_id:
        return

    def worker():
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        send_catalog_followup_for_session(session_id)

    threading.Thread(target=worker, daemon=True).start()


def process_crm_reminders(moment):
    cutoff = moment.replace(microsecond=0).isoformat()
    with db() as conn:
        reminders = conn.execute(
            """
            select r.*, s.username, s.first_name, s.last_name,
              c.title card_title, c.client_name card_client_name, c.contact_value card_contact_value,
              trim(
                coalesce((select group_concat(contact_value, ' | ') from leads where chat_id = r.chat_id and contact_value is not null and contact_value != ''), '') || ' | ' ||
                coalesce((select group_concat(contact_value, ' | ') from custom_broadcast_leads where chat_id = r.chat_id and contact_value is not null and contact_value != ''), '') || ' | ' ||
                coalesce((select group_concat(contact_value, ' | ') from personal_leads where chat_id = r.chat_id and contact_value is not null and contact_value != ''), '')
              ) contact_values
            from crm_reminders r
            left join subscribers s on s.chat_id = r.chat_id
            left join crm_cards c on c.id = r.card_id
            where r.status = 'scheduled' and r.remind_at <= ?
            order by r.remind_at asc
            limit 20
            """,
            (cutoff,),
        ).fetchall()
    for reminder in reminders:
        name = reminder["client_name"] or reminder["card_client_name"] or reminder["card_title"] or crm_client_name(reminder)
        contact = reminder["contact_value"] or reminder["card_contact_value"] or reminder["contact_values"].strip(" |") or (f"@{reminder['username']}" if reminder["username"] else f"Chat ID: {reminder['chat_id']}")
        url = crm_card_url(reminder["card_id"]) if reminder["card_id"] else crm_client_url(reminder["chat_id"])
        text = (
            "⏰ CRM-напоминание\n\n"
            f"Клиент: {html.escape(name)}\n"
            f"Контакт: {html.escape(contact)}\n"
            f"Задача: {html.escape(reminder['text'])}\n"
            f"Карточка: <a href=\"{html.escape(url)}\">открыть клиента</a>"
        )
        for admin_chat_id in [item.strip() for item in ADMIN_CHAT_ID.split(",") if item.strip()]:
            send_message(admin_chat_id, text)
            time.sleep(0.05)
        with db() as conn:
            conn.execute(
                "update crm_reminders set status='sent', sent_at=? where id=? and status='scheduled'",
                (iso_now(), reminder["id"]),
            )


def metrika_api_get(path, params=None):
    if not YANDEX_METRIKA_TOKEN:
        raise RuntimeError("YANDEX_METRIKA_TOKEN is not configured")
    query = urllib.parse.urlencode(params or {})
    url = f"https://api-metrika.yandex.net{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {YANDEX_METRIKA_TOKEN}"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())


def metrika_goal_ids():
    data = metrika_api_get(f"/management/v1/counter/{YANDEX_METRIKA_ID}/goals")
    result = {}
    for goal in data.get("goals", []):
        goal_id = goal.get("id")
        goal_name = str(goal.get("name") or "")
        conditions = goal.get("conditions") or []
        condition_values = {str(item.get("url") or item.get("action") or "") for item in conditions}
        for key, label in METRIKA_GOALS:
            if goal_id and (key == goal_name or label == goal_name or key in condition_values):
                result[key] = goal_id
    return result


def metrika_daily_summary(report_date):
    metrics = ["ym:s:visits", "ym:s:users"]
    goal_ids = {}
    try:
        goal_ids = metrika_goal_ids()
    except Exception:
        traceback.print_exc()
    for key, _ in METRIKA_GOALS:
        goal_id = goal_ids.get(key)
        if goal_id:
            metrics.append(f"ym:s:goal{goal_id}reaches")
    data = metrika_api_get(
        "/stat/v1/data",
        {
            "ids": YANDEX_METRIKA_ID,
            "date1": report_date.isoformat(),
            "date2": report_date.isoformat(),
            "metrics": ",".join(metrics),
            "accuracy": "full",
        },
    )
    totals = data.get("totals") or []
    summary = {
        "visits": int(totals[0] or 0) if len(totals) > 0 else 0,
        "users": int(totals[1] or 0) if len(totals) > 1 else 0,
        "goals": {},
        "missing_goals": [],
    }
    metric_index = 2
    for key, label in METRIKA_GOALS:
        if key in goal_ids:
            summary["goals"][key] = int(totals[metric_index] or 0) if len(totals) > metric_index else 0
            metric_index += 1
        else:
            summary["goals"][key] = None
            summary["missing_goals"].append(label)
    return summary


def internal_lot_activity_summary(start_at, end_at):
    with db() as conn:
        views = conn.execute(
            """
            select p.id, p.title, count(*) c
            from catalog_events ce
            left join projects p on p.id = ce.project_id
            where ce.event_type='catalog_lot_view' and ce.created_at >= ? and ce.created_at < ?
            group by p.id, p.title
            order by c desc
            limit 5
            """,
            (start_at, end_at),
        ).fetchall()
        shares = conn.execute(
            """
            select p.id, p.title, count(*) c
            from catalog_events ce
            left join projects p on p.id = ce.project_id
            where ce.event_type='catalog_share' and ce.created_at >= ? and ce.created_at < ?
            group by p.id, p.title
            order by c desc
            limit 5
            """,
            (start_at, end_at),
        ).fetchall()
        leads = conn.execute(
            """
            select p.id, p.title, count(*) c
            from web_leads wl
            left join projects p on p.id = wl.project_id
            where wl.created_at >= ? and wl.created_at < ?
            group by p.id, p.title
            order by c desc
            limit 5
            """,
            (start_at, end_at),
        ).fetchall()
    return {"views": views, "shares": shares, "leads": leads}


def format_lot_rows(rows):
    if not rows:
        return "нет"
    parts = []
    for row in rows:
        title = row["title"] or "Лот удален"
        lot_id = row["id"] or "?"
        parts.append(f"• #{lot_id} {title}: {row['c']}")
    return "\n".join(parts)


def build_metrika_daily_report(report_date):
    start_at = datetime.combine(report_date, dt_time(0, 0)).isoformat()
    end_at = datetime.combine(report_date + timedelta(days=1), dt_time(0, 0)).isoformat()
    metrika = metrika_daily_summary(report_date)
    internal = internal_lot_activity_summary(start_at, end_at)
    goal_lines = []
    for key, label in METRIKA_GOALS:
        value = metrika["goals"].get(key)
        goal_lines.append(f"• {label}: {'не настроена в Метрике' if value is None else value}")
    missing = ""
    if metrika["missing_goals"]:
        missing = "\n\n⚠️ В Метрике не найдены цели: " + ", ".join(metrika["missing_goals"])
    return (
        f"📊 Ежедневная сводка Below Market UAE\n"
        f"Период: {report_date.strftime('%d.%m.%Y')} по Дубаю\n\n"
        f"Метрика:\n"
        f"• Визиты: {metrika['visits']}\n"
        f"• Посетители: {metrika['users']}\n\n"
        f"Цели:\n" + "\n".join(goal_lines) +
        f"\n\nТоп просмотров лотов:\n{format_lot_rows(internal['views'])}\n\n"
        f"Топ share лотов:\n{format_lot_rows(internal['shares'])}\n\n"
        f"Топ заявок по лотам:\n{format_lot_rows(internal['leads'])}"
        f"{missing}"
    )


def process_metrika_daily_report(moment):
    if not YANDEX_METRIKA_TOKEN or not YANDEX_METRIKA_ID:
        return
    if moment.time() < METRIKA_DAILY_REPORT_TIME:
        return
    report_date = moment.date() - timedelta(days=1)
    report_key = f"metrika_daily:{report_date.isoformat()}"
    with db() as conn:
        already_sent = conn.execute("select 1 from report_runs where report_key = ?", (report_key,)).fetchone()
    if already_sent:
        return
    try:
        text = build_metrika_daily_report(report_date)
    except Exception:
        traceback.print_exc()
        return
    notify_admin(text)
    with db() as conn:
        conn.execute(
            "insert or replace into report_runs(report_key, sent_at) values (?, ?)",
            (report_key, iso_now()),
        )


def in_send_window(moment):
    t = moment.time()
    return SEND_WINDOW_START <= t <= SEND_WINDOW_END


def sent_daily_slots(value, today):
    if not value:
        return set()
    date_part, _, slots_part = value.partition(":")
    if date_part != today:
        return set()
    return {slot for slot in slots_part.split(",") if slot}


def encode_daily_slots(today, slots):
    return f"{today}:{','.join(sorted(slots))}"


def due_daily_slot(moment, sent_slots):
    current = moment.time()
    slots = [
        slot_key
        for slot_key, slot_time in DAILY_SEND_SLOTS
        if current >= slot_time and slot_key not in sent_slots
    ]
    if not slots:
        return None, set()
    return slots[-1], set(slots[:-1])


def scheduler_loop(stop_event):
    while not stop_event.is_set():
        try:
            moment = now_local()
            process_crm_reminders(moment)
            process_catalog_followups(moment)
            process_metrika_daily_report(moment)
            if in_send_window(moment):
                with db() as conn:
                    pending = conn.execute(
                        "select * from broadcasts where status = 'scheduled' and send_at <= ? order by send_at limit 5",
                        (moment.replace(microsecond=0).isoformat(),),
                    ).fetchall()
                for item in pending:
                    count = send_to_all(item["project_id"])
                    with db() as conn:
                        conn.execute(
                            "update broadcasts set status='sent', sent_at=?, sent_count=? where id=?",
                            (iso_now(), count, item["id"]),
                        )

                with db() as conn:
                    custom_pending = conn.execute(
                        "select * from custom_broadcasts where status = 'scheduled' and send_at <= ? order by send_at limit 5",
                        (moment.replace(microsecond=0).isoformat(),),
                    ).fetchall()
                for item in custom_pending:
                    media_items = get_custom_broadcast_media(item["id"])
                    count = send_custom_to_all(item["id"], item["text"], media_items)
                    with db() as conn:
                        conn.execute(
                            "update custom_broadcasts set status='sent', sent_at=?, sent_count=? where id=?",
                            (iso_now(), count, item["id"]),
                        )

                with db() as conn:
                    subscribers = conn.execute("select * from subscribers where coalesce(delivery_status, 'active') = 'active'").fetchall()
                today = moment.date().isoformat()
                for sub in subscribers:
                    sent_slots = sent_daily_slots(sub["daily_sent_slots"], today)
                    slot_key, skipped_slots = due_daily_slot(moment, sent_slots)
                    if not slot_key:
                        continue
                    project = next_project_for(sub["chat_id"], random_order=True)
                    if project and send_project(sub["chat_id"], project):
                        sent_slots.add(slot_key)
                        sent_slots.update(skipped_slots)
                        with db() as conn:
                            conn.execute(
                                "update subscribers set last_daily_sent_at = ?, daily_sent_slots = ? where chat_id = ?",
                                (iso_now(), encode_daily_slots(today, sent_slots), sub["chat_id"]),
                            )
                        time.sleep(0.05)
        except Exception:
            traceback.print_exc()
        stop_event.wait(60)


def parse_form(handler):
    content_type = handler.headers.get("Content-Type", "")
    if content_type.startswith("multipart/form-data"):
        form = cgi.FieldStorage(
            fp=handler.rfile,
            headers=handler.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
            },
        )
        return form
    length = int(handler.headers.get("Content-Length", "0"))
    data = handler.rfile.read(length).decode()
    return urllib.parse.parse_qs(data)


def form_value(form, key, default=""):
    if isinstance(form, cgi.FieldStorage):
        item = form[key] if key in form else None
        if item is None:
            return default
        if isinstance(item, list):
            item = item[0]
        return item.value if item.value is not None else default
    return form.get(key, [default])[0]


def save_uploaded_files(form, field_name, target_dir, prefix):
    saved = []
    if not isinstance(form, cgi.FieldStorage) or field_name not in form:
        return saved
    files = form[field_name]
    if not isinstance(files, list):
        files = [files]
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in files:
        if not item.filename:
            continue
        ext = Path(item.filename).suffix.lower()
        safe_name = f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
        target = target_dir / safe_name
        with target.open("wb") as out:
            shutil.copyfileobj(item.file, out)
        saved.append(
            {
                "file_name": safe_name,
                "original_name": item.filename,
                "mime_type": item.type or mimetypes.guess_type(item.filename)[0] or "application/octet-stream",
                "path": target,
            }
        )
    return saved


def escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def xlsx_cell_ref(row_index, col_index):
    letters = ""
    col = col_index
    while col:
        col, remainder = divmod(col - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_index}"


def xlsx_sheet_data(rows):
    xml_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            cell_ref = xlsx_cell_ref(row_index, col_index)
            text = html.escape("" if value is None else str(value), quote=False)
            cells.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return "".join(xml_rows)


def build_xlsx_package(sheets):
    sheet_parts = []
    workbook_sheets = []
    workbook_relationships = []
    content_overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    ]
    for index, (sheet_name, rows) in enumerate(sheets, start=1):
        col_count = max((len(row) for row in rows), default=1)
        safe_sheet_name = html.escape(sheet_name[:31], quote=True)
        sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <cols>{''.join(f'<col min="{i}" max="{i}" width="22" customWidth="1"/>' for i in range(1, col_count + 1))}</cols>
  <sheetData>{xlsx_sheet_data(rows)}</sheetData>
</worksheet>"""
        sheet_parts.append((f"xl/worksheets/sheet{index}.xml", sheet_xml))
        workbook_sheets.append(f'<sheet name="{safe_sheet_name}" sheetId="{index}" r:id="rId{index}"/>')
        workbook_relationships.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{''.join(workbook_sheets)}</sheets>
</workbook>"""
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {''.join(workbook_relationships)}
</Relationships>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  {''.join(content_overrides)}
</Types>"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for path, xml in sheet_parts:
            archive.writestr(path, xml)
    return buffer.getvalue()


def build_active_projects_xlsx():
    columns = [
        ("id", "ID"),
        ("title", "Лот"),
        ("category", "Категория"),
        ("district", "Район"),
        ("building", "Название здания"),
        ("rooms", "Комнаты"),
        ("bathrooms", "Санузлы"),
        ("floor_level", "Этаж"),
        ("parking", "Парковка"),
        ("availability", "Статус"),
        ("furnishing", "Меблировка"),
        ("balcony", "Балкон"),
        ("area", "Площадь"),
        ("price", "Цена, AED"),
        ("market_price", "Средняя цена рынка, AED"),
        ("distress", "Distress"),
        ("original_price", "Original price, AED"),
        ("description", "Дополнительное описание"),
        ("created_at", "Создан"),
        ("updated_at", "Обновлён"),
    ]
    with db() as conn:
        projects = conn.execute(
            "select * from projects where status = 'active' order by created_at asc, id asc"
        ).fetchall()

    rows = [[label for _, label in columns]]
    for project in projects:
        row = []
        for key, _ in columns:
            value = project[key]
            if key == "distress":
                value = "Да" if value else "Нет"
            row.append(value)
        rows.append(row)

    return build_xlsx_package([("Актуальные лоты", rows)])


def metrika_head():
    if not YANDEX_METRIKA_ID:
        return ""
    counter_id = json.dumps(YANDEX_METRIKA_ID)
    return f"""
  <!-- Yandex.Metrika counter -->
  <script>
  (function(m,e,t,r,i,k,a){{m[i]=m[i]||function(){{(m[i].a=m[i].a||[]).push(arguments)}};m[i].l=1*new Date();
    for (var j = 0; j < document.scripts.length; j++) {{if (document.scripts[j].src === r) {{ return; }}}}
    k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)
  }})(window, document, 'script', 'https://mc.yandex.ru/metrika/tag.js', 'ym');
  ym({counter_id}, 'init', {{clickmap:true, trackLinks:true, accurateTrackBounce:true, webvisor:true}});
  </script>
  <noscript><div><img src="https://mc.yandex.ru/watch/{escape(YANDEX_METRIKA_ID)}" style="position:absolute; left:-9999px;" alt=""></div></noscript>
  <!-- /Yandex.Metrika counter -->"""


def public_url(path="/"):
    if not PUBLIC_BASE_URL:
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{PUBLIC_BASE_URL}{path}"


def seo_description_for_project(project):
    parts = [
        f"{project['rooms']} в {project['building']}, {project['district']}",
        f"цена {money(project['price'])} AED",
    ]
    if project["market_price"]:
        discount = pct_below(project["price"], project["market_price"])
        if discount and discount > 0:
            parts.append(f"ниже рынка на {discount}%")
    if project["distress"]:
        parts.append("distress deal")
    return "Лот недвижимости в Дубае ниже рынка: " + ", ".join(parts) + ". Оставьте заявку, и @roi_counter свяжется с вами."


def app_layout(title, content, message="", catalog_event="", project_id=None, project=None, description="", canonical_url="", og_image="", noindex=False):
    metrika_project = {}
    if project:
        metrika_project = {
            "lot_id": project["id"],
            "title": project["title"],
            "district": project["district"],
            "building": project["building"],
            "rooms": project["rooms"],
            "price": project["price"],
        }
    description = description or "Актуальные лоты недвижимости в Дубае ниже рынка: distress deals, срочные продажи, квартиры и инвестиционные объекты Below Market UAE."
    canonical_url = canonical_url or public_url("/")
    og_image_tag = f'<meta property="og:image" content="{escape(og_image)}">' if og_image else ""
    event_script = ""
    if catalog_event:
        event_key = f"{catalog_event}:{project_id or ''}"
        once_guard = "sessionStorage.getItem(eventKey)" if catalog_event == "catalog_open" else "false"
        once_mark = "sessionStorage.setItem(eventKey, '1');" if catalog_event == "catalog_open" else ""
        event_script = f"""
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script>
  (function() {{
    var eventKey = {json.dumps(event_key, ensure_ascii=False)};
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.ready) tg.ready();
    var user = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;
    var metrikaCounterId = {json.dumps(YANDEX_METRIKA_ID)};
    var metrikaProject = {json.dumps(metrika_project, ensure_ascii=False)};
    var sessionId = sessionStorage.getItem('catalog_session_id');
    if (!sessionId) {{
      sessionId = String(Date.now()) + '-' + Math.random().toString(16).slice(2);
      sessionStorage.setItem('catalog_session_id', sessionId);
    }}
    if (user) {{
      document.querySelectorAll('input[name="tg_user_json"]').forEach(function(input) {{
        input.value = JSON.stringify(user);
      }});
    }}
    document.querySelectorAll('input[name="catalog_session_id"]').forEach(function(input) {{
      input.value = sessionId;
    }});
    function payload(eventType) {{
      return {{
        event_type: eventType,
        project_id: {json.dumps(project_id)},
        tg_user: user || null,
        path: window.location.pathname + window.location.search,
        session_id: sessionId,
        visible: document.visibilityState || ''
      }};
    }}
    function sendEvent(eventType, beacon) {{
      var body = JSON.stringify(payload(eventType));
      if (beacon && navigator.sendBeacon) {{
        navigator.sendBeacon('/app/event', new Blob([body], {{type: 'application/json'}}));
        return;
      }}
      fetch('/app/event', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: body,
        keepalive: true
      }}).catch(function() {{}});
    }}
    function metrikaGoal(goal, params) {{
      if (!metrikaCounterId || typeof window.ym !== 'function') return;
      try {{
        window.ym(Number(metrikaCounterId), 'reachGoal', goal, params || {{}});
      }} catch (e) {{}}
    }}
    if ({json.dumps(catalog_event)} === 'catalog_lot_view') {{
      metrikaGoal('lot_view', metrikaProject);
    }}
    if (!({once_guard})) {{
      sendEvent({json.dumps(catalog_event, ensure_ascii=False)}, false);
      {once_mark}
    }}
    var heartbeat = setInterval(function() {{
      sendEvent('catalog_heartbeat', false);
    }}, 30000);
    var closed = false;
    var internalNavigation = false;
    document.addEventListener('click', function(event) {{
      var link = event.target && event.target.closest ? event.target.closest('a[href]') : null;
      if (link) {{
        try {{
          var url = new URL(link.getAttribute('href'), window.location.origin);
          if (url.origin === window.location.origin && (url.pathname === '/' || url.pathname === '/lot' || url.pathname.indexOf('/app') === 0)) {{
            internalNavigation = true;
          }}
        }} catch (e) {{}}
      }}
    }});
    document.addEventListener('submit', function(event) {{
      var form = event.target;
      if (form && form.action) {{
        try {{
          var url = new URL(form.action, window.location.origin);
          if (url.origin === window.location.origin && (url.pathname === '/lead' || url.pathname.indexOf('/app') === 0)) {{
            internalNavigation = true;
          }}
        }} catch (e) {{}}
      }}
      if (form && form.classList && form.classList.contains('lead-form')) {{
        metrikaGoal('lead_submit', metrikaProject);
      }}
    }});
    document.addEventListener('click', function(event) {{
      var button = event.target && event.target.closest ? event.target.closest('[data-share-lot]') : null;
      if (!button) return;
      var shareUrl = button.getAttribute('data-share-url') || window.location.href;
      var shareText = button.getAttribute('data-share-text') || document.title;
      sendEvent('catalog_share', false);
      metrikaGoal('lot_share', metrikaProject);
      if (navigator.share) {{
        navigator.share({{title: shareText, text: shareText, url: shareUrl}}).catch(function() {{}});
      }} else if (tg && tg.openTelegramLink) {{
        tg.openTelegramLink('https://t.me/share/url?' + new URLSearchParams({{url: shareUrl, text: shareText}}).toString());
      }} else {{
        window.open('https://t.me/share/url?' + new URLSearchParams({{url: shareUrl, text: shareText}}).toString(), '_blank', 'noopener');
      }}
    }});
    function closeCatalog() {{
      if (internalNavigation) return;
      if (closed) return;
      closed = true;
      clearInterval(heartbeat);
      sendEvent('catalog_close', true);
    }}
    window.addEventListener('pagehide', closeCatalog);
    document.addEventListener('visibilitychange', function() {{
      if (document.visibilityState === 'hidden') closeCatalog();
    }});
  }})();
  </script>"""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {('<meta name="robots" content="noindex, nofollow">' if noindex else '')}
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(description)}">
  <link rel="canonical" href="{escape(canonical_url)}">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{escape(title)}">
  <meta property="og:description" content="{escape(description)}">
  <meta property="og:url" content="{escape(canonical_url)}">
  {og_image_tag}
  {metrika_head()}
  <style>{APP_CSS}</style>
</head>
<body>
  <main class="app-shell">
    {f'<div class="notice">{escape(message)}</div>' if message else ''}
    {content}
  </main>
  {event_script}
</body>
</html>"""


def project_cover(project_id):
    with db() as conn:
        media = conn.execute(
            """
            select m.* from projects p
            join media m on m.id = p.cover_media_id and m.project_id = p.id
            where p.id = ? and m.mime_type like 'image/%'
            limit 1
            """,
            (project_id,),
        ).fetchone()
        if not media:
            media = conn.execute(
                """
                select * from media
                where project_id = ? and mime_type like 'image/%'
                order by id asc
                limit 1
                """,
                (project_id,),
            ).fetchone()
    return app_media_path(media["file_name"]) if media else ""


def app_filter_options():
    with db() as conn:
        districts = conn.execute(
            "select distinct district from projects where status='active' and district != '' order by district"
        ).fetchall()
        rooms = conn.execute(
            "select distinct rooms from projects where status='active' and rooms != '' order by rooms"
        ).fetchall()
    return [row["district"] for row in districts], [row["rooms"] for row in rooms]


def app_projects_page(query=None, message="", base_path="/app"):
    query = query or {}
    search = (query.get("q", [""])[0] or "").strip()
    district = (query.get("district", [""])[0] or "").strip()
    rooms = (query.get("rooms", [""])[0] or "").strip()
    max_price = (query.get("max_price", [""])[0] or "").strip()
    where = ["status = 'active'"]
    params = []
    if search:
        where.append("(title like ? or building like ? or district like ? or category like ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if district:
        where.append("district = ?")
        params.append(district)
    if rooms:
        where.append("rooms = ?")
        params.append(rooms)
    if max_price:
        try:
            max_price_value = float(max_price.replace(" ", "").replace(",", "."))
            where.append("price <= ?")
            params.append(max_price_value)
        except ValueError:
            pass
    with db() as conn:
        projects = conn.execute(
            f"""
            select * from projects
            where {' and '.join(where)}
            order by created_at desc, id desc
            """,
            params,
        ).fetchall()
    districts, rooms_options = app_filter_options()
    district_options = '<option value="">Все районы</option>' + "".join(
        f'<option value="{escape(item)}"{" selected" if item == district else ""}>{escape(item)}</option>'
        for item in districts
    )
    rooms_select = '<option value="">Любые комнаты</option>' + "".join(
        f'<option value="{escape(item)}"{" selected" if item == rooms else ""}>{escape(item)}</option>'
        for item in rooms_options
    )
    root_path = base_path or "/"
    lot_path = f"{base_path}/lot" if base_path else "/lot"
    lead_path = f"{base_path}/lead" if base_path else "/lead"
    cards = "".join(app_project_card(project, lot_path) for project in projects)
    content = f"""
    <section class="app-hero">
      <div class="app-brand">Below Market Dubai</div>
      <h1>Лоты недвижимости в Дубае ниже рынка</h1>
      <p>Выберите район, бюджет и формат объекта. Оставьте заявку по понравившемуся лоту, и @roi_counter свяжется с вами.</p>
    </section>
    <form class="app-filters" method="get" action="{escape(root_path)}">
      <input name="q" value="{escape(search)}" placeholder="Район, здание или название">
      <select name="district">{district_options}</select>
      <select name="rooms">{rooms_select}</select>
      <input name="max_price" value="{escape(max_price)}" inputmode="numeric" placeholder="Цена до, AED">
      <button>Найти</button>
      <a class="app-button secondary" href="{escape(root_path)}">Сбросить</a>
    </form>
    {f'<section class="lot-grid">{cards}</section>' if cards else '<div class="empty-state">По выбранным параметрам активных лотов нет. Попробуйте изменить фильтры или оставьте заявку на персональный подбор.</div>'}
    """
    return app_layout(
        "Недвижимость в Дубае ниже рынка | Below Market UAE",
        content,
        message,
        catalog_event="catalog_open",
        description="Каталог Below Market UAE: актуальные лоты недвижимости в Дубае ниже рынка, distress deals, срочные продажи и инвестиционные объекты.",
        canonical_url=public_url("/"),
        noindex=bool(base_path),
    )


def app_project_card(project, lot_path="/app/lot"):
    cover = project_cover(project["id"])
    deal_tags = app_deal_tags(project)
    deal_labels = "".join(f'<span class="{escape(css_class)}">{escape(label)}</span>' for label, css_class in deal_tags)
    return f"""
    <a class="lot-card" href="{escape(lot_path)}?id={project['id']}">
      <div class="lot-cover">{f'<img src="{escape(cover)}" alt="">' if cover else 'Фото скоро появятся'}</div>
      <div class="lot-body">
        <div class="lot-title"><span>{escape(project['title'])}</span><span class="lot-price">{money(project['price'])} AED</span></div>
        <div>{escape(project['building'])}<br><span class="muted">{escape(project['district'])}</span></div>
        <div class="lot-meta">
          <span>{escape(project['category'])}</span>
          <span>{escape(project['rooms'])}</span>
          <span>{escape(project['area'])}</span>
          {deal_labels}
        </div>
      </div>
    </a>
    """


def app_deal_tags(project):
    tags = []
    market_discount = pct_below(project["price"], project["market_price"]) if project["market_price"] else None
    original_discount = pct_below(project["price"], project["original_price"]) if project["original_price"] else None
    if market_discount and market_discount > 0:
        tags.append((f"Ниже рынка на {market_discount}%", "deal-tag"))
    elif project["distress"] and original_discount and original_discount > 0:
        tags.append((f"Ниже original price на {original_discount}%", "deal-tag"))
    if project["distress"]:
        tags.append(("Дистресс", "distress-tag"))
    return tags


def app_project_page(project_id, message="", base_path="/app"):
    project = get_project(project_id)
    if not project or project["status"] != "active":
        return app_projects_page(message="Лот не найден или больше не актуален.", base_path=base_path)
    root_path = base_path or "/"
    lead_path = f"{base_path}/lead" if base_path else "/lead"
    lot_path = f"{base_path}/lot" if base_path else "/lot"
    share_url = f"{PUBLIC_BASE_URL}{lot_path}?id={project['id']}" if PUBLIC_BASE_URL else f"{lot_path}?id={project['id']}"
    share_text = f"Лот недвижимости в Дубае ниже рынка: {project['title']}"
    media = get_project_media(project_id)
    gallery_items = []
    for item in media:
        src = app_media_path(item["file_name"])
        if item["mime_type"].startswith("video/"):
            gallery_items.append(f'<div class="gallery-item"><video src="{escape(src)}" controls playsinline></video></div>')
        else:
            gallery_items.append(f'<div class="gallery-item"><img src="{escape(src)}" alt=""></div>')
    facts = [
        ("Категория", project["category"]),
        ("Район", project["district"]),
        ("Здание", project["building"]),
        ("Комнаты", project["rooms"]),
        ("Санузлы", project["bathrooms"]),
        ("Этаж", project["floor_level"]),
        ("Парковка", project["parking"]),
        ("Статус", project["availability"]),
        ("Меблировка", project["furnishing"]),
        ("Балкон", project["balcony"]),
        ("Площадь", project["area"]),
    ]
    fact_html = "".join(
        f'<div class="fact"><small>{escape(label)}</small>{escape(value)}</div>'
        for label, value in facts
        if value not in (None, "")
    )
    if project["market_price"]:
        discount = pct_below(project["price"], project["market_price"])
        fact_html += f'<div class="fact"><small>Средняя цена рынка</small>{money(project["market_price"])} AED</div>'
        if discount and discount > 0:
            fact_html += f'<div class="fact deal-tag"><small>Ниже рынка</small>на {discount}%</div>'
    if project["distress"]:
        fact_html += '<div class="fact distress-tag"><small>Дистресс</small>Да, специальное предложение</div>'
        if project["original_price"]:
            original_discount = pct_below(project["price"], project["original_price"])
            fact_html += f'<div class="fact"><small>Original price</small>{money(project["original_price"])} AED</div>'
            if original_discount and original_discount > 0:
                fact_html += f'<div class="fact deal-tag"><small>Ниже original price</small>на {original_discount}%</div>'
    content = f"""
    <a class="back-link" href="{escape(root_path)}">← Все лоты</a>
    <section class="lot-detail">
      <div class="gallery">{''.join(gallery_items) or '<div class="gallery-item">Фото скоро появятся</div>'}</div>
      <aside class="detail-panel">
        <div>
          <div class="app-brand">Лот #{project['id']}</div>
          <h1>{escape(project['title'])}</h1>
        </div>
        <div class="detail-price">{money(project['price'])} AED</div>
        <div class="facts">{fact_html}</div>
        {f'<p>{escape(project["description"])}</p>' if project["description"] else ''}
        <button class="app-button secondary" type="button" data-share-lot="1" data-share-url="{escape(share_url)}" data-share-text="{escape(share_text)}">↗️ Поделиться лотом</button>
        <form class="lead-form" method="post" action="{escape(lead_path)}">
          <input type="hidden" name="project_id" value="{project['id']}">
          <input type="hidden" name="tg_user_json" value="">
          <input type="hidden" name="catalog_session_id" value="">
          <input name="name" placeholder="Ваше имя">
          <input name="contact" required placeholder="Телефон, WhatsApp или Telegram">
          <textarea name="message" placeholder="Комментарий"></textarea>
          <button>💬 Хочу узнать подробнее</button>
        </form>
      </aside>
    </section>
    """
    cover = project_cover(project["id"])
    title = f"{project['title']} - недвижимость в Дубае ниже рынка | Below Market UAE"
    return app_layout(
        title,
        content,
        message,
        catalog_event="catalog_lot_view",
        project_id=project["id"],
        project=project,
        description=seo_description_for_project(project),
        canonical_url=public_url(f"/lot?id={project['id']}"),
        og_image=cover,
        noindex=bool(base_path),
    )


def robots_txt():
    sitemap_url = public_url("/sitemap.xml")
    return "\n".join(
        [
            "User-agent: *",
            "Allow: /$",
            "Allow: /lot",
            "Allow: /media/",
            "Disallow: /app",
            "Disallow: /login",
            "Disallow: /logout",
            "Disallow: /admin",
            "Disallow: /project",
            "Disallow: /projects",
            "Disallow: /crm",
            "Disallow: /leads",
            "Disallow: /chats",
            "Disallow: /stats",
            "Disallow: /broadcasts",
            "Disallow: /subscribers",
            f"Sitemap: {sitemap_url}",
            "",
        ]
    )


def sitemap_xml():
    with db() as conn:
        projects = conn.execute(
            """
            select id, updated_at, created_at
            from projects
            where status='active'
            order by updated_at desc, id desc
            """
        ).fetchall()
    urls = [
        (public_url("/"), now_local().date().isoformat(), "daily", "1.0"),
    ]
    for project in projects:
        lastmod = (project["updated_at"] or project["created_at"] or now_local().date().isoformat())[:10]
        urls.append((public_url(f"/lot?id={project['id']}"), lastmod, "daily", "0.8"))
    items = "\n".join(
        "  <url>\n"
        f"    <loc>{html.escape(loc)}</loc>\n"
        f"    <lastmod>{html.escape(lastmod)}</lastmod>\n"
        f"    <changefreq>{changefreq}</changefreq>\n"
        f"    <priority>{priority}</priority>\n"
        "  </url>"
        for loc, lastmod, changefreq, priority in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>\n"
    )


def catalog_user_name(tg_user):
    if not isinstance(tg_user, dict):
        return ""
    parts = [tg_user.get("first_name"), tg_user.get("last_name")]
    name = " ".join(str(part).strip() for part in parts if part).strip()
    return name or tg_user.get("username") or ""


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def catalog_session_id(payload):
    if not isinstance(payload, dict):
        return None
    value = str(payload.get("session_id") or "").strip()
    return value[:80] or None


def upsert_catalog_session(session_id, event_type, chat_id=None, username=None, name="", had_lead=False):
    if not session_id:
        return
    now = iso_now()
    with db() as conn:
        existing = conn.execute("select * from catalog_sessions where session_id = ?", (session_id,)).fetchone()
        if not existing:
            conn.execute(
                """
                insert into catalog_sessions(session_id, chat_id, username, name, opened_at, last_seen_at, had_lead)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, chat_id, username, name, now, now, 1 if had_lead else 0),
            )
            existing = conn.execute("select * from catalog_sessions where session_id = ?", (session_id,)).fetchone()
        if event_type == "catalog_close":
            opened_at = parse_iso(existing["opened_at"]) or now_local()
            closed_at = parse_iso(now) or now_local()
            duration = max(0, int((closed_at - opened_at).total_seconds()))
            conn.execute(
                """
                update catalog_sessions
                set chat_id=coalesce(?, chat_id),
                    username=coalesce(?, username),
                    name=coalesce(nullif(?, ''), name),
                    last_seen_at=?,
                    closed_at=?,
                    duration_seconds=?,
                    had_lead=max(had_lead, ?)
                where session_id=?
                """,
                (chat_id, username, name, now, now, duration, 1 if had_lead else 0, session_id),
            )
        else:
            conn.execute(
                """
                update catalog_sessions
                set chat_id=coalesce(?, chat_id),
                    username=coalesce(?, username),
                    name=coalesce(nullif(?, ''), name),
                    last_seen_at=?,
                    had_lead=max(had_lead, ?)
                where session_id=?
                """,
                (chat_id, username, name, now, 1 if had_lead else 0, session_id),
            )


def record_catalog_event(event_type, project_id=None, tg_user=None, payload=None):
    if event_type not in {"catalog_open", "catalog_lot_view", "catalog_lead", "catalog_heartbeat", "catalog_close", "catalog_share"}:
        return None
    project = get_project(project_id) if project_id else None
    chat_id = None
    username = None
    name = ""
    if isinstance(tg_user, dict):
        try:
            chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
        except (TypeError, ValueError):
            chat_id = None
        username = tg_user.get("username")
        name = catalog_user_name(tg_user)
    session_id = catalog_session_id(payload)
    upsert_catalog_session(session_id, event_type, chat_id, username, name, had_lead=event_type == "catalog_lead")
    if event_type == "catalog_close":
        send_catalog_followup_for_session_async(session_id)
    with db() as conn:
        cur = conn.execute(
            """
            insert into catalog_events(event_type, project_id, chat_id, username, name, payload, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                project["id"] if project else None,
                chat_id,
                username,
                name,
                json.dumps(payload or {}, ensure_ascii=False),
                iso_now(),
            ),
        )
        event_id = cur.lastrowid
    if event_type == "catalog_open":
        notify_admin(
            "\n".join(
                [
                    "Пользователь открыл каталог",
                    f"Клиент: {name or 'не определён'}",
                    f"Telegram: @{username}" if username else f"Telegram ID: {chat_id}" if chat_id else "Источник: мини-апп / браузер",
                ]
            )
        )
    return event_id


def create_web_lead(project_id, name, contact, message, tg_user=None, session_id=None):
    project = get_project(project_id) if project_id else None
    with db() as conn:
        cur = conn.execute(
            """
            insert into web_leads(project_id, name, contact_value, message, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (project_id if project else None, name, contact, message, iso_now()),
        )
        lead_id = cur.lastrowid
    record_catalog_event(
        "catalog_lead",
        project_id=project_id,
        tg_user=tg_user,
        payload={"lead_id": lead_id, "name": name, "contact": contact, "message": message, "session_id": session_id},
    )
    notify_admin(
        "\n".join(
            [
                "Новая заявка из мини-приложения",
                f"Лот: {project['title'] if project else 'не выбран'}",
                f"Клиент: {name or 'не указано'}",
                f"Контакт: {contact}",
                f"Комментарий: {message}" if message else "",
            ]
        ).strip()
    )
    chat_id = None
    if isinstance(tg_user, dict):
        try:
            chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
        except (TypeError, ValueError):
            chat_id = None
    create_crm_card(
        f"Заявка из каталога: {project['title'] if project else 'без лота'}",
        chat_id=chat_id,
        client_name=name or "",
        contact_value=contact,
        request=message or (project["district"] if project else ""),
        source="Мини-апп каталог",
    )
    return lead_id


def layout(title, content, active="projects", message=""):
    nav = [
        ("projects", "/admin", "Объекты"),
        ("new", "/project/new", "Добавить"),
        ("crm", "/crm", "CRM"),
        ("leads", "/leads", "Заявки"),
        ("chats", "/chats", "Чаты"),
        ("stats", "/stats", "Статистика"),
        ("subscriber_stats", "/subscribers/stats", "Стата подписчиков"),
        ("broadcasts", "/broadcasts", "Рассылки"),
        ("subscribers", "/subscribers", "Подписчики"),
    ]
    links = "".join(
        f'<a class="{ "active" if active == key else "" }" href="{href}">{label}</a>'
        for key, href, label in nav
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>{escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
  <aside>
    <div class="brand">Realty Bot</div>
    <nav>{links}</nav>
    <a class="logout" href="/logout">Выйти</a>
  </aside>
  <main>
    <header>
      <div>
        <p class="eyebrow">Telegram CRM</p>
        <h1>{escape(title)}</h1>
      </div>
    </header>
    {f'<div class="notice">{escape(message)}</div>' if message else ''}
    {content}
  </main>
</body>
</html>"""


CSS = """
:root { --bg:#f5f6f4; --panel:#fff; --line:#dfe3df; --text:#1f2923; --muted:#68746c; --accent:#245a49; --accent2:#b8792a; --danger:#a64242; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:grid; grid-template-columns:240px 1fr; color:var(--text); background:var(--bg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }
aside { background:#17221d; color:#fff; padding:24px 18px; display:flex; flex-direction:column; gap:28px; }
.brand { font-size:20px; font-weight:700; letter-spacing:.2px; }
nav { display:grid; gap:6px; }
nav a, .logout { color:#d9e1dc; text-decoration:none; padding:10px 12px; border-radius:7px; }
nav a.active, nav a:hover { background:#24352e; color:#fff; }
.logout { margin-top:auto; color:#f0c5c5; }
main { padding:28px 34px 56px; overflow:auto; }
header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:22px; }
h1 { margin:0; font-size:28px; letter-spacing:0; }
.eyebrow { margin:0 0 4px; color:var(--muted); text-transform:uppercase; font-size:11px; font-weight:700; letter-spacing:.8px; }
.grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:22px; }
.metric, .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
.metric { padding:16px; }
.metric strong { display:block; font-size:24px; }
.metric span { color:var(--muted); }
.panel { padding:18px; margin-bottom:18px; }
table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.table-scroll { width:100%; overflow:auto; }
.table-scroll table { min-width:760px; }
th,td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-size:12px; font-weight:700; background:#fafbf9; }
tr:last-child td { border-bottom:0; }
.status { display:inline-flex; align-items:center; padding:3px 8px; border-radius:999px; background:#e8f1ed; color:var(--accent); font-size:12px; font-weight:700; }
.status.archived { background:#eee; color:#777; }
.actions { display:flex; gap:8px; flex-wrap:wrap; }
button, .button { border:0; background:var(--accent); color:#fff; padding:9px 12px; border-radius:7px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-block; }
button.icon-button, .button.icon-button { width:40px; height:40px; padding:0; display:inline-flex; align-items:center; justify-content:center; font-size:22px; line-height:1; }
button.secondary, .button.secondary { background:#eef2ef; color:var(--text); border:1px solid var(--line); }
button.danger, .button.danger { background:var(--danger); color:#fff; }
form.inline { display:inline; }
.form-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }
label { display:grid; gap:6px; color:var(--muted); font-weight:700; font-size:12px; }
input, select, textarea { width:100%; border:1px solid var(--line); border-radius:7px; padding:10px 11px; background:#fff; color:var(--text); font:inherit; }
textarea { min-height:110px; resize:vertical; }
.chat-layout { display:grid; grid-template-columns:320px minmax(0,1fr); gap:16px; height:calc(100vh - 150px); min-height:520px; }
.chat-sidebar { padding:0; overflow:hidden; display:flex; flex-direction:column; min-height:0; }
.chat-sidebar h2 { margin:0; padding:18px 18px 12px; }
.chat-search { display:flex; gap:8px; padding:0 14px 14px; border-bottom:1px solid var(--line); }
.chat-search input { min-width:0; }
.chat-list { flex:1; min-height:0; overflow:auto; border-top:1px solid var(--line); }
.chat-list a { display:block; padding:12px 14px; border-bottom:1px solid var(--line); color:var(--text); text-decoration:none; }
.chat-list a.active, .chat-list a:hover { background:#eef4f0; }
.chat-list strong { display:block; font-size:14px; margin-bottom:2px; }
.chat-list-title { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.chat-preview { color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.unread-badge { flex:0 0 auto; min-width:22px; height:22px; padding:0 7px; display:inline-flex; align-items:center; justify-content:center; border-radius:999px; background:var(--danger); color:#fff; font-size:12px; font-weight:800; }
.chat-shell { padding:0; display:grid; grid-template-rows:auto minmax(0,1fr) auto; overflow:hidden; min-height:0; }
.chat-header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; padding:18px; border-bottom:1px solid var(--line); margin:0; }
.chat-header h2 { margin:0 0 2px; }
.chat-thread { display:grid; align-content:start; gap:10px; min-height:0; overflow:auto; padding:18px; background:#f8faf8; }
.chat-bubble { max-width:min(680px,78%); padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#fff; white-space:pre-wrap; box-shadow:0 1px 1px rgba(0,0,0,.02); }
.chat-bubble.admin { justify-self:end; background:#e1eee8; border-color:#c4d9ce; }
.chat-bubble.user { justify-self:start; }
.chat-meta { margin-bottom:4px; color:var(--muted); font-size:12px; font-weight:700; }
.chat-compose { border-top:1px solid var(--line); padding:14px 18px 18px; background:#fff; }
.chat-compose label { color:var(--text); font-size:13px; }
.chat-compose textarea { min-height:118px; font-size:15px; }
.chat-compose p { margin:10px 0 0; }
.crm-header { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:16px; align-items:start; }
.crm-card-title { margin:0 0 4px; font-size:20px; }
.crm-contact { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
.crm-pill { display:inline-flex; align-items:center; gap:6px; padding:6px 9px; border-radius:999px; background:#eef4f0; border:1px solid var(--line); color:var(--text); font-size:13px; text-decoration:none; }
.crm-timeline { display:grid; gap:10px; }
.crm-timeline-item { border:1px solid var(--line); border-radius:8px; padding:10px 12px; background:#fff; }
.crm-timeline-item strong { display:block; margin-bottom:3px; }
.crm-two-col { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.crm-toolbar { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; margin-bottom:16px; }
.crm-toolbar form { margin:0; }
.crm-toolbar-search { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:8px; }
.crm-toolbar-actions { display:flex; gap:8px; justify-content:flex-end; align-items:center; }
.modal { width:min(860px,calc(100vw - 32px)); border:1px solid var(--line); border-radius:10px; padding:0; box-shadow:0 24px 70px rgba(0,0,0,.22); }
.modal::backdrop { background:rgba(23,34,29,.38); }
.modal-panel { padding:22px; }
.modal-header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:16px; }
.modal-header h2 { margin:0; }
.modal-close { background:#eef2ef; color:var(--text); border:1px solid var(--line); width:36px; height:36px; padding:0; border-radius:999px; font-size:20px; line-height:1; }
.crm-board { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(280px,320px); gap:14px; overflow:auto; padding-bottom:10px; }
.crm-column { background:#eef2ef; border:1px solid var(--line); border-radius:8px; padding:12px; min-height:420px; }
.crm-column h3 { margin:0 0 10px; display:flex; justify-content:space-between; gap:8px; font-size:15px; }
.crm-column h3 span { color:var(--muted); font-weight:700; }
.crm-column-list { display:grid; gap:10px; }
.crm-board-card { background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px; display:grid; gap:8px; }
.crm-board-card a { color:var(--text); text-decoration:none; }
.crm-board-card p { margin:0; }
.crm-board-card select { padding:8px; }
.media-picker { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:12px; margin-top:12px; }
.media-option { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fafbf9; display:grid; gap:8px; }
.media-option img { width:100%; aspect-ratio:4/3; object-fit:cover; border-radius:6px; background:#e7ebe7; }
.media-option label { display:flex; align-items:center; gap:8px; color:var(--text); font-size:13px; }
.media-name { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.wide { grid-column:1 / -1; }
.notice { background:#fff7e8; border:1px solid #ead6b5; color:#614111; padding:12px 14px; border-radius:8px; margin-bottom:16px; }
.login { min-height:100vh; display:grid; grid-template-columns:1fr; place-items:center; background:var(--bg); }
.login form { width:min(420px,calc(100vw - 32px)); background:#fff; border:1px solid var(--line); border-radius:8px; padding:26px; display:grid; gap:14px; }
.login h1 { font-size:24px; }
.muted { color:var(--muted); }
@media (max-width:900px) { body { grid-template-columns:1fr; } aside { position:static; } .grid,.form-grid,.chat-layout,.crm-two-col,.crm-header { grid-template-columns:1fr; } main { padding:22px 16px 44px; } .chat-layout { height:auto; min-height:0; } .chat-layout.active-chat .chat-shell { order:-1; } .chat-shell { height:calc(100vh - 190px); min-height:520px; } .chat-sidebar { max-height:360px; } }
"""

APP_CSS = """
:root { --bg:#f4f5f2; --card:#fff; --text:#17221d; --muted:#66736b; --line:#dfe4df; --accent:#245a49; --gold:#b8792a; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; background:var(--bg); color:var(--text); font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }
a { color:inherit; }
.app-shell { max-width:1180px; margin:0 auto; padding:18px 18px 48px; }
.app-hero { display:grid; gap:8px; padding:18px 0 16px; }
.app-brand { font-size:13px; color:var(--gold); font-weight:800; text-transform:uppercase; letter-spacing:.8px; }
.app-hero h1 { margin:0; font-size:clamp(28px,5vw,44px); line-height:1.06; letter-spacing:0; max-width:780px; }
.app-hero p { margin:0; max-width:680px; color:var(--muted); font-size:16px; }
.app-filters { position:sticky; top:0; z-index:2; display:grid; grid-template-columns:1.2fr 1fr 1fr auto auto; gap:10px; padding:12px; margin:10px 0 18px; background:rgba(244,245,242,.92); backdrop-filter:blur(10px); border:1px solid var(--line); border-radius:8px; }
input, select, textarea { width:100%; border:1px solid var(--line); border-radius:7px; padding:11px 12px; background:#fff; color:var(--text); font:inherit; }
button, .app-button { border:0; border-radius:7px; padding:11px 14px; background:var(--accent); color:#fff; font-weight:800; text-decoration:none; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:6px; }
.app-button.secondary { background:#fff; color:var(--text); border:1px solid var(--line); }
.lot-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }
.lot-card { display:grid; overflow:hidden; background:var(--card); border:1px solid var(--line); border-radius:8px; text-decoration:none; min-height:100%; }
.lot-cover { aspect-ratio:4/3; background:#e7ebe7; overflow:hidden; display:grid; place-items:center; color:var(--muted); }
.lot-cover img { width:100%; height:100%; object-fit:cover; display:block; }
.lot-body { padding:13px; display:grid; gap:8px; }
.lot-title { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; font-weight:850; font-size:16px; }
.lot-price { color:var(--accent); white-space:nowrap; }
.lot-meta { display:flex; flex-wrap:wrap; gap:6px; color:var(--muted); font-size:13px; }
.lot-meta span { padding:4px 8px; background:#f4f7f5; border:1px solid var(--line); border-radius:999px; }
.lot-meta .deal-tag, .deal-tag { background:#e7f2ec; border-color:#bcd8c9; color:#1f6b4d; font-weight:850; }
.lot-meta .distress-tag, .distress-tag { background:#fff1df; border-color:#e6bd80; color:#915515; font-weight:900; }
.lot-detail { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr); gap:18px; align-items:start; }
.gallery { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.gallery-item { border-radius:8px; overflow:hidden; background:#e7ebe7; aspect-ratio:4/3; }
.gallery-item img, .gallery-item video { width:100%; height:100%; object-fit:cover; display:block; }
.detail-panel { background:#fff; border:1px solid var(--line); border-radius:8px; padding:16px; display:grid; gap:14px; position:sticky; top:14px; }
.detail-panel h1 { margin:0; font-size:26px; line-height:1.12; }
.detail-price { font-size:24px; color:var(--accent); font-weight:900; }
.facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.fact { padding:10px; background:#f8faf8; border:1px solid var(--line); border-radius:7px; }
.fact.deal-tag { background:#e7f2ec; border-color:#bcd8c9; color:#1f6b4d; }
.fact.distress-tag { background:#fff1df; border-color:#e6bd80; color:#915515; }
.fact small { display:block; color:var(--muted); font-weight:700; margin-bottom:2px; }
.lead-form { display:grid; gap:10px; }
.lead-form textarea { min-height:92px; resize:vertical; }
.empty-state { padding:24px; background:#fff; border:1px solid var(--line); border-radius:8px; color:var(--muted); }
.back-link { display:inline-flex; margin:0 0 14px; color:var(--muted); text-decoration:none; font-weight:800; }
.notice { background:#fff7e8; border:1px solid #ead6b5; color:#614111; padding:12px 14px; border-radius:8px; margin-bottom:16px; }
.muted { color:var(--muted); }
@media (max-width:900px) { .app-shell { padding:14px 12px 36px; } .app-filters { position:static; grid-template-columns:1fr 1fr; } .app-filters input:first-child { grid-column:1 / -1; } .lot-grid { grid-template-columns:1fr; } .lot-detail { grid-template-columns:1fr; } .detail-panel { position:static; } }
@media (max-width:520px) { .app-filters { grid-template-columns:1fr; } .facts { grid-template-columns:1fr; } .gallery { grid-template-columns:1fr; } }
"""


def stats():
    with db() as conn:
        lot_leads = conn.execute("select count(*) c from leads").fetchone()["c"]
        custom_leads = conn.execute("select count(*) c from custom_broadcast_leads").fetchone()["c"]
        personal_leads = conn.execute("select count(*) c from personal_leads").fetchone()["c"]
        return {
            "active": conn.execute("select count(*) c from projects where status='active'").fetchone()["c"],
            "leads": lot_leads + custom_leads + personal_leads,
            "subs": conn.execute("select count(*) c from subscribers").fetchone()["c"],
            "scheduled": conn.execute("select count(*) c from broadcasts where status='scheduled'").fetchone()["c"],
        }


def dashboard(message=""):
    s = stats()
    with db() as conn:
        projects = conn.execute("select * from projects order by created_at desc").fetchall()
    rows = "".join(
        f"""
        <tr>
          <td><strong>{escape(p['title'])}</strong><br><span class="muted">{escape(p['district'])}, {escape(p['building'])}</span>{f'<br><span class="muted">От кого: {escape(p["source_from"])}</span>' if p['source_from'] else ''}</td>
          <td>{escape(p['rooms'])}</td>
          <td>{money(p['price'])} AED</td>
          <td><span class="status {escape(p['status'])}">{escape(p['status'])}</span></td>
          <td class="actions">
            <a class="button secondary" href="/project/edit?id={p['id']}">Редактировать</a>
            <form class="inline" method="post" action="/project/send"><input type="hidden" name="id" value="{p['id']}"><button>Отправить всем</button></form>
            <form class="inline" method="post" action="/project/archive"><input type="hidden" name="id" value="{p['id']}"><button class="danger">Архив</button></form>
            <form class="inline" method="post" action="/project/delete"><input type="hidden" name="id" value="{p['id']}"><button class="danger">Удалить</button></form>
          </td>
        </tr>
        """
        for p in projects
    )
    content = f"""
    <section class="grid">
      <div class="metric"><strong>{s['active']}</strong><span>актуальных объектов</span></div>
      <div class="metric"><strong>{s['leads']}</strong><span>заявок</span></div>
      <div class="metric"><strong>{s['subs']}</strong><span>подписчиков</span></div>
      <div class="metric"><strong>{s['scheduled']}</strong><span>запланировано</span></div>
    </section>
    <p class="actions"><a class="button secondary" href="/projects/export.xlsx">Выгрузить актуальные лоты в Excel</a></p>
    <table>
      <thead><tr><th>Объект</th><th>Комнаты</th><th>Цена</th><th>Статус</th><th>Действия</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="5" class="muted">Пока нет объектов.</td></tr>'}</tbody>
    </table>
    """
    return layout("Объекты", content, "projects", message)


def conversion(numerator, denominator):
    if not denominator:
        return "0%"
    return f"{round(numerator / denominator * 100, 1)}%"


def format_duration(seconds):
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м {secs}с"
    return f"{secs}с"


def event_label(event_type):
    return EVENT_LABELS.get(event_type, event_type)


def period_condition(column, start_at=None, end_at=None):
    clauses = []
    params = []
    if start_at:
        clauses.append(f"{column} >= ?")
        params.append(start_at)
    if end_at:
        clauses.append(f"{column} < ?")
        params.append(end_at)
    return (" and " + " and ".join(clauses) if clauses else "", params)


def parse_stats_period(query):
    period = query.get("period", ["all"])[0] or "all"
    today = now_local()
    date_from = query.get("date_from", [""])[0]
    date_to = query.get("date_to", [""])[0]
    start_at = None
    end_at = None
    label = "За всё время"

    if period == "today":
        start_at = iso_start_of_day(today)
        end_at = iso_next_day(today)
        label = "Сегодня"
    elif period == "7d":
        start_at = (today - timedelta(days=7)).replace(microsecond=0).isoformat()
        end_at = today.replace(microsecond=0).isoformat()
        label = "Последние 7 дней"
    elif period == "30d":
        start_at = (today - timedelta(days=30)).replace(microsecond=0).isoformat()
        end_at = today.replace(microsecond=0).isoformat()
        label = "Последние 30 дней"
    elif period == "custom":
        try:
            if date_from:
                start_at = datetime.fromisoformat(date_from).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            if date_to:
                end_at = (datetime.fromisoformat(date_to).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
            if date_from and date_to:
                label = f"{date_from} — {date_to}"
            elif date_from:
                label = f"С {date_from}"
            elif date_to:
                label = f"До {date_to}"
            else:
                label = "Произвольный период"
        except ValueError:
            period = "all"
            date_from = ""
            date_to = ""
            label = "За всё время"

    return {
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "start_at": start_at,
        "end_at": end_at,
        "label": label,
    }


def period_query(period):
    params = {"period": period["period"]}
    if period["date_from"]:
        params["date_from"] = period["date_from"]
    if period["date_to"]:
        params["date_to"] = period["date_to"]
    return urllib.parse.urlencode(params)


def get_statistics_data(period):
    start_at = period["start_at"]
    end_at = period["end_at"]
    seen_filter, seen_params = period_condition("seen_at", start_at, end_at)
    event_filter, event_params = period_condition("created_at", start_at, end_at)
    catalog_filter, catalog_params = period_condition("created_at", start_at, end_at)
    lead_filter, lead_params = period_condition("created_at", start_at, end_at)
    subscriber_filter, subscriber_params = period_condition("subscribed_at", start_at, end_at)
    seen_join, seen_join_params = period_condition("s.seen_at", start_at, end_at)
    event_join, event_join_params = period_condition("e.created_at", start_at, end_at)
    catalog_join, catalog_join_params = period_condition("ce.created_at", start_at, end_at)
    session_filter, session_params = period_condition("opened_at", start_at, end_at)
    lead_join, lead_join_params = period_condition("l.created_at", start_at, end_at)
    web_lead_join, web_lead_join_params = period_condition("wl.created_at", start_at, end_at)

    with db() as conn:
        overview = {
            "subscribers": conn.execute("select count(*) c from subscribers").fetchone()["c"],
            "new_subscribers": conn.execute(f"select count(*) c from subscribers where 1=1{subscriber_filter}", subscriber_params).fetchone()["c"],
            "shows": conn.execute(f"select count(*) c from seen_projects where 1=1{seen_filter}", seen_params).fetchone()["c"],
            "clicks": conn.execute(f"select count(*) c from bot_events where event_type like 'click_%'{event_filter}", event_params).fetchone()["c"],
            "deep_link_opens": conn.execute(f"select count(*) c from bot_events where event_type = 'start_shared_lot'{event_filter}", event_params).fetchone()["c"],
            "deep_link_users": conn.execute(f"select count(distinct chat_id) c from bot_events where event_type = 'start_shared_lot'{event_filter}", event_params).fetchone()["c"],
            "deep_link_leads": conn.execute(
                f"""
                select count(distinct l.id) c
                from leads l
                join bot_events e on e.chat_id = l.chat_id
                 and e.project_id = l.project_id
                 and e.event_type = 'start_shared_lot'
                 and l.created_at >= e.created_at
                where 1=1{lead_filter}
                """,
                lead_params,
            ).fetchone()["c"],
            "catalog_opens": conn.execute(f"select count(*) c from catalog_events where event_type = 'catalog_open'{catalog_filter}", catalog_params).fetchone()["c"],
            "catalog_lot_views": conn.execute(f"select count(*) c from catalog_events where event_type = 'catalog_lot_view'{catalog_filter}", catalog_params).fetchone()["c"],
            "catalog_closes": conn.execute(f"select count(*) c from catalog_events where event_type = 'catalog_close'{catalog_filter}", catalog_params).fetchone()["c"],
            "catalog_sessions": conn.execute(f"select count(*) c from catalog_sessions where 1=1{session_filter}", session_params).fetchone()["c"],
            "catalog_avg_duration": conn.execute(f"select avg(duration_seconds) c from catalog_sessions where duration_seconds is not null{session_filter}", session_params).fetchone()["c"] or 0,
            "catalog_followups": conn.execute(f"select count(*) c from catalog_sessions where followup_sent_at is not null{session_filter}", session_params).fetchone()["c"],
            "catalog_followup_yes": conn.execute(f"select count(*) c from catalog_sessions where followup_answer='yes'{session_filter}", session_params).fetchone()["c"],
            "catalog_followup_no": conn.execute(f"select count(*) c from catalog_sessions where followup_answer='no'{session_filter}", session_params).fetchone()["c"],
            "lot_leads": conn.execute(f"select count(*) c from leads where 1=1{lead_filter}", lead_params).fetchone()["c"],
            "web_leads": conn.execute(f"select count(*) c from web_leads where 1=1{lead_filter}", lead_params).fetchone()["c"],
            "personal_leads": conn.execute(f"select count(*) c from personal_leads where 1=1{lead_filter}", lead_params).fetchone()["c"],
            "custom_leads": conn.execute(f"select count(*) c from custom_broadcast_leads where 1=1{lead_filter}", lead_params).fetchone()["c"],
        }
        project_rows_data = conn.execute(
            f"""
            select
              p.id,
              p.title,
              p.district,
              p.building,
              p.status,
              count(distinct s.chat_id) shows,
              count(distinct case when e.event_type like 'click_%' then e.id end) clicks,
              count(distinct case when e.event_type = 'click_interest' then e.id end) interests,
              count(distinct case when e.event_type = 'start_shared_lot' then e.id end) deep_link_opens,
              count(distinct case when e.event_type = 'start_shared_lot' then e.chat_id end) deep_link_users,
              count(distinct l.id) leads,
              count(distinct case when ce.event_type = 'catalog_lot_view' then ce.id end) catalog_views,
              count(distinct wl.id) web_leads
            from projects p
            left join seen_projects s on s.project_id = p.id{seen_join}
            left join bot_events e on e.project_id = p.id{event_join}
            left join leads l on l.project_id = p.id{lead_join}
            left join catalog_events ce on ce.project_id = p.id{catalog_join}
            left join web_leads wl on wl.project_id = p.id{web_lead_join}
            group by p.id
            order by p.status = 'active' desc, catalog_views desc, shows desc, web_leads desc, leads desc, p.created_at desc
            """,
            seen_join_params + event_join_params + lead_join_params + catalog_join_params + web_lead_join_params,
        ).fetchall()
        catalog_rows_data = conn.execute(
            f"""
            select
              ce.created_at,
              ce.event_type,
              p.title,
              p.district,
              coalesce(nullif(ce.name, ''), ce.username, ce.chat_id, 'Не определён') client_name,
              ce.username,
              ce.chat_id
            from catalog_events ce
            left join projects p on p.id = ce.project_id
            where ce.event_type != 'catalog_heartbeat'{catalog_filter}
            order by ce.created_at desc
            limit 120
            """,
            catalog_params,
        ).fetchall()
        catalog_session_rows_data = conn.execute(
            f"""
            select
              session_id,
              opened_at,
              closed_at,
              duration_seconds,
              had_lead,
              followup_sent_at,
              followup_variant,
              followup_answer,
              followup_answered_at,
              coalesce(nullif(name, ''), username, chat_id, 'Не определён') client_name,
              username,
              chat_id
            from catalog_sessions
            where 1=1{session_filter}
            order by opened_at desc
            limit 120
            """,
            session_params,
        ).fetchall()
        catalog_followup_ab_rows_data = conn.execute(
            f"""
            select
              coalesce(followup_variant, 'unknown') variant,
              count(*) sent,
              sum(case when followup_answer = 'yes' then 1 else 0 end) yes_count,
              sum(case when followup_answer = 'no' then 1 else 0 end) no_count
            from catalog_sessions
            where followup_sent_at is not null{session_filter}
            group by coalesce(followup_variant, 'unknown')
            order by variant
            """,
            session_params,
        ).fetchall()
        deep_link_rows_data = conn.execute(
            f"""
            select
              e.created_at,
              p.title,
              p.district,
              coalesce(nullif(e.name, ''), e.username, e.chat_id, 'Не определён') client_name,
              e.username,
              e.chat_id,
              count(distinct l.id) leads_after_open
            from bot_events e
            left join projects p on p.id = e.project_id
            left join leads l on l.project_id = e.project_id
             and l.chat_id = e.chat_id
             and l.created_at >= e.created_at
            where e.event_type = 'start_shared_lot'{event_filter}
            group by e.id
            order by e.created_at desc
            limit 120
            """,
            event_params,
        ).fetchall()
        seen_rows_data = conn.execute(
            f"""
            select
              s.seen_at,
              p.title,
              p.district,
              coalesce(nullif(trim(coalesce(sub.first_name, '') || ' ' || coalesce(sub.last_name, '')), ''), sub.username, s.chat_id) client_name,
              sub.username,
              s.chat_id,
              count(distinct e.id) clicks,
              count(distinct l.id) leads
            from seen_projects s
            left join projects p on p.id = s.project_id
            left join subscribers sub on sub.chat_id = s.chat_id
            left join bot_events e on e.project_id = s.project_id and e.chat_id = s.chat_id and e.event_type like 'click_%'{event_join}
            left join leads l on l.project_id = s.project_id and l.chat_id = s.chat_id{lead_join}
            where 1=1{seen_filter}
            group by s.chat_id, s.project_id
            order by s.seen_at desc
            limit 80
            """,
            event_join_params + lead_join_params + seen_params,
        ).fetchall()
        event_rows_data = conn.execute(
            f"""
            select event_type, count(*) c
            from bot_events
            where event_type like 'click_%'{event_filter}
            group by event_type
            order by c desc, event_type
            """,
            event_params,
        ).fetchall()

    return {
        "overview": overview,
        "project_rows": project_rows_data,
        "catalog_rows": catalog_rows_data,
        "catalog_session_rows": catalog_session_rows_data,
        "catalog_followup_ab_rows": catalog_followup_ab_rows_data,
        "deep_link_rows": deep_link_rows_data,
        "seen_rows": seen_rows_data,
        "event_rows": event_rows_data,
    }


def statistics_page(query=None):
    period = parse_stats_period(query or {})
    data = get_statistics_data(period)
    overview = data["overview"]
    total_leads = overview["lot_leads"] + overview["web_leads"] + overview["personal_leads"] + overview["custom_leads"]
    project_rows = "".join(
        f"""
        <tr>
          <td><strong>{escape(row['title'])}</strong><br><span class="muted">{escape(row['district'])}, {escape(row['building'])}</span></td>
          <td>{escape(row['status'])}</td>
          <td>{row['shows']}</td>
          <td>{row['clicks']}</td>
          <td>{row['interests']}</td>
          <td>{row['leads']}</td>
          <td>{row['deep_link_opens']}</td>
          <td>{row['deep_link_users']}</td>
          <td>{row['catalog_views']}</td>
          <td>{row['web_leads']}</td>
          <td>{conversion(row['web_leads'], row['catalog_views'])}</td>
        </tr>
        """
        for row in data["project_rows"]
    )
    catalog_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['created_at'])}</td>
          <td>{escape(event_label(row['event_type']))}</td>
          <td><strong>{escape(row['client_name'])}</strong><br><span class="muted">{'@' + escape(row['username']) if row['username'] else 'Telegram ID: ' + escape(row['chat_id']) if row['chat_id'] else 'Без Telegram data'}</span></td>
          <td>{escape(row['title'] or 'Без лота')}<br><span class="muted">{escape(row['district'])}</span></td>
        </tr>
        """
        for row in data["catalog_rows"]
    )
    catalog_session_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['opened_at'])}<br><span class="muted">Закрыт: {escape(row['closed_at'] or '')}</span></td>
          <td><strong>{escape(row['client_name'])}</strong><br><span class="muted">{'@' + escape(row['username']) if row['username'] else 'Telegram ID: ' + escape(row['chat_id']) if row['chat_id'] else 'Без Telegram data'}</span></td>
          <td>{format_duration(row['duration_seconds'])}</td>
          <td>{'Да' if row['had_lead'] else 'Нет'}</td>
          <td>{escape(row['followup_sent_at'] or '')}<br><span class="muted">{escape(followup_variant_label(row['followup_variant']))}</span></td>
          <td>{escape(row['followup_answer'] or '')}<br><span class="muted">{escape(row['followup_answered_at'] or '')}</span></td>
        </tr>
        """
        for row in data["catalog_session_rows"]
    )
    catalog_followup_ab_rows = "".join(
        f"""
        <tr>
          <td><strong>{escape(followup_variant_label(row['variant']))}</strong><br><span class="muted">{escape(row['variant'])}</span></td>
          <td>{row['sent']}</td>
          <td>{row['yes_count'] or 0}</td>
          <td>{row['no_count'] or 0}</td>
          <td>{conversion(row['yes_count'] or 0, row['sent'])}</td>
        </tr>
        """
        for row in data["catalog_followup_ab_rows"]
    )
    deep_link_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['created_at'])}</td>
          <td><strong>{escape(row['client_name'])}</strong><br><span class="muted">{'@' + escape(row['username']) if row['username'] else 'Telegram ID: ' + escape(row['chat_id']) if row['chat_id'] else 'Без Telegram data'}</span></td>
          <td>{escape(row['title'] or 'Лот удалён')}<br><span class="muted">{escape(row['district'] or '')}</span></td>
          <td>{row['leads_after_open']}</td>
        </tr>
        """
        for row in data["deep_link_rows"]
    )
    seen_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['seen_at'])}</td>
          <td><strong>{escape(row['client_name'])}</strong><br><span class="muted">{'@' + escape(row['username']) if row['username'] else 'Chat ID: ' + escape(row['chat_id'])}</span></td>
          <td>{escape(row['title'] or 'Лот удалён')}<br><span class="muted">{escape(row['district'])}</span></td>
          <td>{row['clicks']}</td>
          <td>{row['leads']}</td>
        </tr>
        """
        for row in data["seen_rows"]
    )
    event_rows = "".join(
        f"<tr><td>{escape(event_label(row['event_type']))}<br><span class='muted'>{escape(row['event_type'])}</span></td><td>{row['c']}</td></tr>"
        for row in data["event_rows"]
    )
    export_query = period_query(period)
    content = f"""
    <form class="panel" method="get" action="/stats">
      <h2>Период</h2>
      <div class="form-grid">
        <label>Период<select name="period">
          <option value="all" {selected(period['period'], 'all')}>За всё время</option>
          <option value="today" {selected(period['period'], 'today')}>Сегодня</option>
          <option value="7d" {selected(period['period'], '7d')}>Последние 7 дней</option>
          <option value="30d" {selected(period['period'], '30d')}>Последние 30 дней</option>
          <option value="custom" {selected(period['period'], 'custom')}>Произвольный период</option>
        </select></label>
        <label>С даты<input type="date" name="date_from" value="{escape(period['date_from'])}"></label>
        <label>По дату<input type="date" name="date_to" value="{escape(period['date_to'])}"></label>
      </div>
      <p class="actions">
        <button>Показать</button>
        <a class="button secondary" href="/stats/export.xlsx?{escape(export_query)}">Выгрузить статистику в Excel</a>
      </p>
      <p class="muted">Текущий период: {escape(period['label'])}</p>
    </form>
    <section class="grid">
      <div class="metric"><strong>{overview['subscribers']}</strong><span>подписчиков</span></div>
      <div class="metric"><strong>{overview['new_subscribers']}</strong><span>новых за период</span></div>
      <div class="metric"><strong>{overview['shows']}</strong><span>показов лотов</span></div>
      <div class="metric"><strong>{overview['clicks']}</strong><span>кликов по кнопкам</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{overview['catalog_opens']}</strong><span>открытий каталога</span></div>
      <div class="metric"><strong>{overview['catalog_lot_views']}</strong><span>просмотров лотов в каталоге</span></div>
      <div class="metric"><strong>{overview['web_leads']}</strong><span>заявок из каталога</span></div>
      <div class="metric"><strong>{conversion(overview['web_leads'], overview['catalog_lot_views'])}</strong><span>конверсия каталога</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{overview['catalog_closes']}</strong><span>закрытий каталога</span></div>
      <div class="metric"><strong>{format_duration(overview['catalog_avg_duration'])}</strong><span>среднее время в каталоге</span></div>
      <div class="metric"><strong>{overview['catalog_followups']}</strong><span>follow-up отправлено</span></div>
      <div class="metric"><strong>{overview['catalog_followup_yes']} / {overview['catalog_followup_no']}</strong><span>ответы Да / Нет</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{total_leads}</strong><span>заявок всего</span></div>
      <div class="metric"><strong>{overview['lot_leads']}</strong><span>заявок по лотам</span></div>
      <div class="metric"><strong>{overview['personal_leads']}</strong><span>персональный подбор</span></div>
      <div class="metric"><strong>{overview['custom_leads']}</strong><span>заявок по рассылкам</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{conversion(overview['lot_leads'], overview['shows'])}</strong><span>конверсия показов в заявки</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{overview['deep_link_opens']}</strong><span>deep-link переходов</span></div>
      <div class="metric"><strong>{overview['deep_link_users']}</strong><span>уникальных пользователей по deep link</span></div>
      <div class="metric"><strong>{overview['deep_link_leads']}</strong><span>заявок после deep link</span></div>
      <div class="metric"><strong>{conversion(overview['deep_link_leads'], overview['deep_link_opens'])}</strong><span>конверсия deep link</span></div>
    </section>
    <div class="panel">
      <h2>A/B тест follow-up</h2>
      <div class="table-scroll"><table><thead><tr><th>Вариант</th><th>Отправлено</th><th>Да</th><th>Нет</th><th>Конверсия в Да</th></tr></thead><tbody>{catalog_followup_ab_rows or '<tr><td colspan="5" class="muted">A/B данных пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Deep links</h2>
      <div class="table-scroll"><table><thead><tr><th>Дата</th><th>Пользователь</th><th>Лот</th><th>Заявки после перехода</th></tr></thead><tbody>{deep_link_rows or '<tr><td colspan="4" class="muted">Переходов по deep link пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Статистика по лотам</h2>
      <div class="table-scroll"><table><thead><tr><th>Лот</th><th>Статус</th><th>TG-показы</th><th>TG-клики</th><th>TG-интерес</th><th>TG-заявки</th><th>Deep link переходы</th><th>Deep link пользователи</th><th>Каталог просмотры</th><th>Каталог заявки</th><th>Конверсия каталога</th></tr></thead><tbody>{project_rows or '<tr><td colspan="11" class="muted">Данных пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>События каталога</h2>
      <div class="table-scroll"><table><thead><tr><th>Дата</th><th>Событие</th><th>Пользователь</th><th>Лот</th></tr></thead><tbody>{catalog_rows or '<tr><td colspan="4" class="muted">Событий каталога пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Сессии каталога</h2>
      <div class="table-scroll"><table><thead><tr><th>Открытие / закрытие</th><th>Пользователь</th><th>Время</th><th>Заявка</th><th>Follow-up</th><th>Ответ</th></tr></thead><tbody>{catalog_session_rows or '<tr><td colspan="6" class="muted">Сессий каталога пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Кто видел лоты</h2>
      <div class="table-scroll"><table><thead><tr><th>Дата показа</th><th>Пользователь</th><th>Лот</th><th>Клики</th><th>Заявки</th></tr></thead><tbody>{seen_rows or '<tr><td colspan="5" class="muted">Показов пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Клики по действиям</h2>
      <div class="table-scroll"><table><thead><tr><th>Действие</th><th>Клики</th></tr></thead><tbody>{event_rows or '<tr><td colspan="2" class="muted">Кликов пока нет.</td></tr>'}</tbody></table></div>
    </div>
    """
    return layout("Статистика", content, "stats")


def build_statistics_xlsx(period):
    data = get_statistics_data(period)
    overview = data["overview"]
    total_leads = overview["lot_leads"] + overview["web_leads"] + overview["personal_leads"] + overview["custom_leads"]
    overview_rows = [
        ["Период", period["label"]],
        ["Подписчиков всего", overview["subscribers"]],
        ["Новых подписчиков за период", overview["new_subscribers"]],
        ["Показов лотов", overview["shows"]],
        ["Кликов по кнопкам", overview["clicks"]],
        ["Открытий каталога", overview["catalog_opens"]],
        ["Просмотров лотов в каталоге", overview["catalog_lot_views"]],
        ["Закрытий каталога", overview["catalog_closes"]],
        ["Сессий каталога", overview["catalog_sessions"]],
        ["Среднее время в каталоге", format_duration(overview["catalog_avg_duration"])],
        ["Follow-up отправлено", overview["catalog_followups"]],
        ["Follow-up Да", overview["catalog_followup_yes"]],
        ["Follow-up Нет", overview["catalog_followup_no"]],
        ["Заявок из каталога", overview["web_leads"]],
        ["Конверсия каталога", conversion(overview["web_leads"], overview["catalog_lot_views"])],
        ["Заявок всего", total_leads],
        ["Заявок по лотам", overview["lot_leads"]],
        ["Заявок на персональный подбор", overview["personal_leads"]],
        ["Заявок по свободным рассылкам", overview["custom_leads"]],
        ["Конверсия показов в заявки", conversion(overview["lot_leads"], overview["shows"])],
        ["Deep-link переходов", overview["deep_link_opens"]],
        ["Уникальных пользователей по deep link", overview["deep_link_users"]],
        ["Заявок после deep link", overview["deep_link_leads"]],
        ["Конверсия deep link", conversion(overview["deep_link_leads"], overview["deep_link_opens"])],
    ]
    ab_rows = [["Вариант", "Код", "Отправлено", "Да", "Нет", "Конверсия в Да"]]
    for row in data["catalog_followup_ab_rows"]:
        ab_rows.append(
            [
                followup_variant_label(row["variant"]),
                row["variant"],
                row["sent"],
                row["yes_count"] or 0,
                row["no_count"] or 0,
                conversion(row["yes_count"] or 0, row["sent"]),
            ]
        )
    project_rows = [["Лот", "Район", "Здание", "Статус", "TG-показы", "TG-клики", "TG-интерес", "TG-заявки", "Deep link переходы", "Deep link пользователи", "Каталог просмотры", "Каталог заявки", "Конверсия каталога"]]
    for row in data["project_rows"]:
        project_rows.append(
            [
                row["title"],
                row["district"],
                row["building"],
                row["status"],
                row["shows"],
                row["clicks"],
                row["interests"],
                row["leads"],
                row["deep_link_opens"],
                row["deep_link_users"],
                row["catalog_views"],
                row["web_leads"],
                conversion(row["web_leads"], row["catalog_views"]),
            ]
        )
    catalog_rows = [["Дата", "Событие", "Пользователь", "Username", "Telegram ID", "Лот", "Район"]]
    for row in data["catalog_rows"]:
        catalog_rows.append(
            [
                row["created_at"],
                event_label(row["event_type"]),
                row["client_name"],
                f"@{row['username']}" if row["username"] else "",
                row["chat_id"] or "",
                row["title"] or "Без лота",
                row["district"] or "",
            ]
        )
    catalog_session_rows = [["Открытие", "Закрытие", "Длительность", "Пользователь", "Username", "Telegram ID", "Была заявка", "Follow-up отправлен", "A/B вариант", "Ответ", "Дата ответа"]]
    for row in data["catalog_session_rows"]:
        catalog_session_rows.append(
            [
                row["opened_at"],
                row["closed_at"],
                format_duration(row["duration_seconds"]),
                row["client_name"],
                f"@{row['username']}" if row["username"] else "",
                row["chat_id"] or "",
                "Да" if row["had_lead"] else "Нет",
                row["followup_sent_at"] or "",
                followup_variant_label(row["followup_variant"]),
                row["followup_answer"] or "",
                row["followup_answered_at"] or "",
            ]
        )
    deep_link_rows = [["Дата", "Пользователь", "Username", "Telegram ID", "Лот", "Район", "Заявки после перехода"]]
    for row in data["deep_link_rows"]:
        deep_link_rows.append(
            [
                row["created_at"],
                row["client_name"],
                f"@{row['username']}" if row["username"] else "",
                row["chat_id"] or "",
                row["title"] or "Лот удалён",
                row["district"] or "",
                row["leads_after_open"],
            ]
        )
    seen_rows = [["Дата показа", "Пользователь", "Username", "Chat ID", "Лот", "Район", "Клики", "Заявки"]]
    for row in data["seen_rows"]:
        seen_rows.append(
            [
                row["seen_at"],
                row["client_name"],
                f"@{row['username']}" if row["username"] else "",
                row["chat_id"],
                row["title"] or "Лот удалён",
                row["district"],
                row["clicks"],
                row["leads"],
            ]
        )
    event_rows = [["Действие", "Техническое событие", "Клики"]]
    for row in data["event_rows"]:
        event_rows.append([event_label(row["event_type"]), row["event_type"], row["c"]])
    return build_xlsx_package(
        [
            ("Обзор", overview_rows),
            ("AB Follow-up", ab_rows),
            ("Deep links", deep_link_rows),
            ("Лоты", project_rows),
            ("Каталог", catalog_rows),
            ("Сессии каталога", catalog_session_rows),
            ("Кто видел", seen_rows),
            ("Клики", event_rows),
        ]
    )


def subscriber_status_label(status):
    return SUBSCRIBER_STATUS_LABELS.get(status or "active", status or "Активен")


def subscriber_tag_label(tag):
    return SUBSCRIBER_TAG_LABELS.get(tag or "", tag or "Без тега")


def subscriber_tag_select(current):
    current = current or ""
    return "".join(
        f'<option value="{escape(value)}" {selected(current, value)}>{escape(label)}</option>'
        for value, label in SUBSCRIBER_TAGS
    )


def subscriber_identity(row):
    name = " ".join(part for part in [row["first_name"], row["last_name"]] if part).strip()
    return name or row["username"] or str(row["chat_id"])


def has_leads_sql(alias="s"):
    return f"""
        exists(select 1 from leads l where l.chat_id = {alias}.chat_id)
        or exists(select 1 from personal_leads pl where pl.chat_id = {alias}.chat_id)
        or exists(select 1 from custom_broadcast_leads cl where cl.chat_id = {alias}.chat_id)
        or exists(select 1 from catalog_events cel where cel.chat_id = {alias}.chat_id and cel.event_type = 'catalog_lead')
    """


def get_subscriber_statistics_data(period):
    start_at = period["start_at"]
    end_at = period["end_at"]
    subscriber_filter, subscriber_params = period_condition("subscribed_at", start_at, end_at)
    seen_filter, seen_params = period_condition("seen_at", start_at, end_at)
    catalog_filter, catalog_params = period_condition("created_at", start_at, end_at)
    bot_event_filter, bot_event_params = period_condition("created_at", start_at, end_at)
    message_filter, message_params = period_condition("created_at", start_at, end_at)
    lead_filter, lead_params = period_condition("created_at", start_at, end_at)
    inactive_before = (now_local() - timedelta(days=30)).replace(microsecond=0).isoformat()
    lead_union = f"""
        select chat_id from leads where 1=1{lead_filter}
        union all select chat_id from personal_leads where 1=1{lead_filter}
        union all select chat_id from custom_broadcast_leads where 1=1{lead_filter}
        union all select chat_id from catalog_events where event_type='catalog_lead' and chat_id is not null{catalog_filter}
    """
    with db() as conn:
        status_counts = {
            row["delivery_status"] or "active": row["c"]
            for row in conn.execute(
                "select coalesce(delivery_status, 'active') delivery_status, count(*) c from subscribers group by coalesce(delivery_status, 'active')"
            ).fetchall()
        }
        overview = {
            "total": conn.execute("select count(*) c from subscribers").fetchone()["c"],
            "active": status_counts.get("active", 0),
            "blocked": status_counts.get("blocked", 0),
            "deactivated": status_counts.get("deactivated", 0),
            "unreachable": status_counts.get("unreachable", 0),
            "new": conn.execute(f"select count(*) c from subscribers where 1=1{subscriber_filter}", subscriber_params).fetchone()["c"],
            "catalog_users": conn.execute(
                f"select count(distinct chat_id) c from catalog_events where chat_id is not null and event_type='catalog_open'{catalog_filter}",
                catalog_params,
            ).fetchone()["c"],
            "lead_users": conn.execute(f"select count(distinct chat_id) c from ({lead_union})", lead_params * 3 + catalog_params).fetchone()["c"],
            "chat_users": conn.execute(
                f"select count(distinct chat_id) c from chat_messages where direction='user'{message_filter}",
                message_params,
            ).fetchone()["c"],
            "avg_seen": conn.execute(
                f"select avg(c) c from (select count(*) c from seen_projects where 1=1{seen_filter} group by chat_id)",
                seen_params,
            ).fetchone()["c"] or 0,
        }
        segments = {
            "active_no_leads": conn.execute(
                f"select count(*) c from subscribers s where coalesce(s.delivery_status, 'active')='active' and not ({has_leads_sql('s')})"
            ).fetchone()["c"],
            "viewed_no_leads": conn.execute(
                f"""
                select count(distinct s.chat_id) c
                from subscribers s
                join seen_projects sp on sp.chat_id = s.chat_id
                where not ({has_leads_sql('s')}){seen_filter.replace('seen_at', 'sp.seen_at')}
                """,
                seen_params,
            ).fetchone()["c"],
            "catalog_no_leads": conn.execute(
                f"""
                select count(distinct s.chat_id) c
                from subscribers s
                join catalog_events ce on ce.chat_id = s.chat_id and ce.event_type = 'catalog_open'
                where not ({has_leads_sql('s')}){catalog_filter.replace('created_at', 'ce.created_at')}
                """,
                catalog_params,
            ).fetchone()["c"],
            "blocked": overview["blocked"],
            "inactive_30d": conn.execute(
                "select count(*) c from subscribers where last_seen_at is null or last_seen_at < ?",
                (inactive_before,),
            ).fetchone()["c"],
        }
        rows_raw = conn.execute(
            f"""
            select
              s.*,
              (select count(*) from seen_projects sp where sp.chat_id = s.chat_id{seen_filter.replace('seen_at', 'sp.seen_at')}) tg_views,
              (select count(*) from catalog_events ce where ce.chat_id = s.chat_id and ce.event_type='catalog_open'{catalog_filter.replace('created_at', 'ce.created_at')}) catalog_opens,
              (select count(*) from catalog_events ce where ce.chat_id = s.chat_id and ce.event_type='catalog_lot_view'{catalog_filter.replace('created_at', 'ce.created_at')}) catalog_views,
              (select count(*) from leads l where l.chat_id = s.chat_id{lead_filter.replace('created_at', 'l.created_at')}) lot_leads,
              (select count(*) from personal_leads pl where pl.chat_id = s.chat_id{lead_filter.replace('created_at', 'pl.created_at')}) personal_leads,
              (select count(*) from custom_broadcast_leads cl where cl.chat_id = s.chat_id{lead_filter.replace('created_at', 'cl.created_at')}) custom_leads,
              (select count(*) from catalog_events cel where cel.chat_id = s.chat_id and cel.event_type='catalog_lead'{catalog_filter.replace('created_at', 'cel.created_at')}) catalog_leads,
              (select count(*) from chat_messages cm where cm.chat_id = s.chat_id and cm.direction='user'{message_filter.replace('created_at', 'cm.created_at')}) user_messages,
              (select created_at from bot_events be where be.chat_id = s.chat_id order by created_at desc limit 1) last_bot_event_at,
              (select event_type from bot_events be where be.chat_id = s.chat_id order by created_at desc limit 1) last_bot_event_type,
              (select created_at from catalog_events ce where ce.chat_id = s.chat_id order by created_at desc limit 1) last_catalog_event_at,
              (select event_type from catalog_events ce where ce.chat_id = s.chat_id order by created_at desc limit 1) last_catalog_event_type,
              (select created_at from chat_messages cm where cm.chat_id = s.chat_id order by created_at desc limit 1) last_message_at
            from subscribers s
            order by coalesce(s.last_seen_at, s.subscribed_at) desc
            """,
            seen_params + catalog_params + catalog_params + lead_params + lead_params + lead_params + catalog_params + message_params,
        ).fetchall()

    rows = []
    for row in rows_raw:
        item = dict(row)
        item["lead_count"] = item["lot_leads"] + item["personal_leads"] + item["custom_leads"] + item["catalog_leads"]
        actions = [
            (item.get("last_seen_at"), "Активность в боте"),
            (item.get("last_bot_event_at"), event_label(item.get("last_bot_event_type"))),
            (item.get("last_catalog_event_at"), event_label(item.get("last_catalog_event_type"))),
            (item.get("last_message_at"), "Сообщение в чат"),
        ]
        actions = [(date, label) for date, label in actions if date]
        if actions:
            item["last_action_at"], item["last_action"] = max(actions, key=lambda pair: pair[0])
        else:
            item["last_action_at"], item["last_action"] = "", ""
        rows.append(item)
    return {"overview": overview, "segments": segments, "rows": rows}


def subscriber_statistics_page(query=None):
    period = parse_stats_period(query or {})
    data = get_subscriber_statistics_data(period)
    overview = data["overview"]
    segments = data["segments"]
    export_query = period_query(period)
    rows = "".join(
        f"""
        <tr>
          <td>{escape(row['subscribed_at'])}</td>
          <td><strong>{escape(subscriber_identity(row))}</strong><br><span class="muted">{'@' + escape(row['username']) if row['username'] else 'Chat ID: ' + escape(row['chat_id'])}</span></td>
          <td>{escape(subscriber_tag_label(row['subscriber_tag']))}</td>
          <td>{escape(subscriber_status_label(row['delivery_status']))}<br><span class="muted">{escape(row['last_delivery_error'] or '')}</span></td>
          <td>{escape(row['last_seen_at'])}</td>
          <td>{escape(row['last_delivery_at'])}</td>
          <td>{escape(row['last_delivery_error_at'])}</td>
          <td>{row['tg_views']}</td>
          <td>{row['catalog_opens']} / {row['catalog_views']}</td>
          <td>{row['lead_count']}</td>
          <td>{row['user_messages']}</td>
          <td>{escape(row['filter_rooms'] or '')}<br><span class="muted">{escape(row['filter_district'] or '')}</span></td>
          <td>{escape(row['last_action'])}<br><span class="muted">{escape(row['last_action_at'])}</span></td>
        </tr>
        """
        for row in data["rows"]
    )
    content = f"""
    <form class="panel" method="get" action="/subscribers/stats">
      <h2>Период</h2>
      <div class="form-grid">
        <label>Период<select name="period">
          <option value="all" {selected(period['period'], 'all')}>За всё время</option>
          <option value="today" {selected(period['period'], 'today')}>Сегодня</option>
          <option value="7d" {selected(period['period'], '7d')}>Последние 7 дней</option>
          <option value="30d" {selected(period['period'], '30d')}>Последние 30 дней</option>
          <option value="custom" {selected(period['period'], 'custom')}>Произвольный период</option>
        </select></label>
        <label>С даты<input type="date" name="date_from" value="{escape(period['date_from'])}"></label>
        <label>По дату<input type="date" name="date_to" value="{escape(period['date_to'])}"></label>
      </div>
      <p class="actions">
        <button>Показать</button>
        <a class="button secondary" href="/subscribers/stats/export.xlsx?{escape(export_query)}">Выгрузить в Excel</a>
      </p>
      <p class="muted">Текущий период: {escape(period['label'])}</p>
    </form>
    <section class="grid">
      <div class="metric"><strong>{overview['total']}</strong><span>подписчиков всего</span></div>
      <div class="metric"><strong>{overview['active']}</strong><span>активные</span></div>
      <div class="metric"><strong>{overview['blocked']}</strong><span>заблокировали бота</span></div>
      <div class="metric"><strong>{overview['unreachable'] + overview['deactivated']}</strong><span>недоступные</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{overview['new']}</strong><span>новые за период</span></div>
      <div class="metric"><strong>{overview['catalog_users']}</strong><span>открывали каталог</span></div>
      <div class="metric"><strong>{overview['lead_users']}</strong><span>оставляли заявки</span></div>
      <div class="metric"><strong>{overview['chat_users']}</strong><span>писали в чат</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{round(overview['avg_seen'], 1)}</strong><span>средне просмотров TG-лотов</span></div>
      <div class="metric"><strong>{segments['active_no_leads']}</strong><span>активные без заявок</span></div>
      <div class="metric"><strong>{segments['viewed_no_leads']}</strong><span>смотрели без заявки</span></div>
      <div class="metric"><strong>{segments['catalog_no_leads']}</strong><span>каталог без заявки</span></div>
    </section>
    <section class="grid">
      <div class="metric"><strong>{segments['inactive_30d']}</strong><span>неактивны 30+ дней</span></div>
      <div class="metric"><strong>{segments['blocked']}</strong><span>blocked-сегмент</span></div>
    </section>
    <div class="panel">
      <h2>Подписчики</h2>
      <div class="table-scroll"><table><thead><tr><th>Дата подписки</th><th>Контакт</th><th>Тег</th><th>Статус</th><th>Последняя активность</th><th>Доставка OK</th><th>Ошибка доставки</th><th>TG-лоты</th><th>Каталог</th><th>Заявки</th><th>Чат</th><th>Фильтры</th><th>Последнее действие</th></tr></thead><tbody>{rows or '<tr><td colspan="13" class="muted">Подписчиков пока нет.</td></tr>'}</tbody></table></div>
    </div>
    """
    return layout("Статистика подписчиков", content, "subscriber_stats")


def build_subscriber_statistics_xlsx(period):
    data = get_subscriber_statistics_data(period)
    overview = data["overview"]
    segments = data["segments"]
    overview_rows = [
        ["Период", period["label"]],
        ["Подписчиков всего", overview["total"]],
        ["Активные", overview["active"]],
        ["Заблокировали бота", overview["blocked"]],
        ["Аккаунт удалён", overview["deactivated"]],
        ["Недоступные", overview["unreachable"]],
        ["Новые за период", overview["new"]],
        ["Открывали каталог", overview["catalog_users"]],
        ["Оставляли заявки", overview["lead_users"]],
        ["Писали в чат", overview["chat_users"]],
        ["Среднее количество просмотренных TG-лотов", round(overview["avg_seen"], 1)],
    ]
    segment_rows = [
        ["Сегмент", "Количество"],
        ["Активные без заявок", segments["active_no_leads"]],
        ["Смотрели лоты, но не оставили заявку", segments["viewed_no_leads"]],
        ["Открывали каталог, но не оставили заявку", segments["catalog_no_leads"]],
        ["Заблокировали бота", segments["blocked"]],
        ["Давно не взаимодействовали", segments["inactive_30d"]],
    ]
    subscriber_rows = [[
        "Дата подписки", "Имя", "Username", "Telegram ID", "Тег", "Статус", "Последняя активность",
        "Последняя успешная доставка", "Дата ошибки доставки", "Ошибка доставки",
        "TG-лоты", "Открытий каталога", "Просмотров в каталоге", "Заявки", "Сообщения в чат",
        "Фильтр комнат", "Фильтр района", "Последнее действие", "Дата последнего действия",
    ]]
    for row in data["rows"]:
        subscriber_rows.append([
            row["subscribed_at"],
            subscriber_identity(row),
            f"@{row['username']}" if row["username"] else "",
            row["chat_id"],
            subscriber_tag_label(row["subscriber_tag"]),
            subscriber_status_label(row["delivery_status"]),
            row["last_seen_at"],
            row["last_delivery_at"],
            row["last_delivery_error_at"],
            row["last_delivery_error"],
            row["tg_views"],
            row["catalog_opens"],
            row["catalog_views"],
            row["lead_count"],
            row["user_messages"],
            row["filter_rooms"],
            row["filter_district"],
            row["last_action"],
            row["last_action_at"],
        ])
    return build_xlsx_package(
        [
            ("Обзор", overview_rows),
            ("Сегменты", segment_rows),
            ("Подписчики", subscriber_rows),
        ]
    )


def project_form(project=None, message=""):
    p = dict(project) if project else {}
    action = "/project/update" if project else "/project/create"
    hidden = f'<input type="hidden" name="id" value="{project["id"]}">' if project else ""
    content = f"""
    <form class="panel" method="post" action="{action}" enctype="multipart/form-data">
      {hidden}
      <div class="form-grid">
        <label>Лот<input name="title" required value="{escape(p.get('title'))}"></label>
        <label>Категория<input name="category" required value="{escape(p.get('category'))}" placeholder="Apartment / Villa"></label>
        <label>Район<input name="district" required value="{escape(p.get('district'))}"></label>
        <label>Название здания<input name="building" required value="{escape(p.get('building'))}"></label>
        <label>Комнаты<input name="rooms" required value="{escape(p.get('rooms'))}" placeholder="1BR / Studio"></label>
        <label>Санузлы<input name="bathrooms" value="{escape(p.get('bathrooms'))}"></label>
        <label>Этаж<select name="floor_level" required><option {selected(p.get('floor_level'),'Высокий')}>Высокий</option><option {selected(p.get('floor_level'),'Низкий')}>Низкий</option><option {selected(p.get('floor_level'),'Средний')}>Средний</option></select></label>
        <label>Парковка<input name="parking" value="{escape(p.get('parking'))}" placeholder="1 место / нет данных"></label>
        <label>Статус<select name="availability" required><option {selected(p.get('availability'),'Свободно')}>Свободно</option><option {selected(p.get('availability'),'В аренде')}>В аренде</option></select></label>
        <label>Меблировка<input name="furnishing" value="{escape(p.get('furnishing'))}" placeholder="Furnished / Unfurnished"></label>
        <label>Балкон<input name="balcony" value="{escape(p.get('balcony'))}" placeholder="Есть / нет данных"></label>
        <label>Площадь<input name="area" required value="{escape(p.get('area'))}" placeholder="850 sqft"></label>
        <label>Цена, AED<input type="number" step="1" name="price" required value="{escape(p.get('price'))}"></label>
        <label>Средняя цена рынка, AED<input type="number" step="1" name="market_price" value="{escape(p.get('market_price'))}"></label>
        <label>Original price, AED<input type="number" step="1" name="original_price" value="{escape(p.get('original_price'))}"></label>
        <label>Distress<select name="distress"><option value="0" {selected(str(p.get('distress',0)),'0')}>Нет</option><option value="1" {selected(str(p.get('distress',0)),'1')}>Да</option></select></label>
        <label>Фото/видео<input type="file" name="media" multiple accept="image/*,video/*"></label>
        <label class="wide">От кого<input name="source_from" value="{escape(p.get('source_from'))}" placeholder="Например: агент, собственник, партнёр"></label>
        <label class="wide">Дополнительное описание<textarea name="description">{escape(p.get('description'))}</textarea></label>
      </div>
      <p><button>{'Сохранить' if project else 'Добавить объект'}</button></p>
    </form>
    """
    if project:
        media = get_project_media(project["id"])
        image_options = "".join(
            f"""
            <div class="media-option">
              <img src="{app_media_path(m['file_name'])}" alt="">
              <label><input type="radio" name="cover_media_id" value="{m['id']}" form="cover-form" {'checked' if str(p.get('cover_media_id') or '') == str(m['id']) else ''}> Обложка мини-аппа</label>
              <div class="media-name">{escape(m['original_name'])}</div>
            </div>
            """
            for m in media
            if m["mime_type"].startswith("image/")
        )
        other_files = "".join(
            f"<p>{escape(m['original_name'])}</p>"
            for m in media
            if not m["mime_type"].startswith("image/")
        )
        content += f"""
        <div class='panel'>
          <h2>Медиа</h2>
          <form id="cover-form" method="post" action="/project/cover">
            <input type="hidden" name="id" value="{project['id']}">
            <div class="media-picker">{image_options or "<p class='muted'>Изображений пока нет.</p>"}</div>
            {f"<h3>Видео и другие файлы</h3>{other_files}" if other_files else ""}
            <p><button>Сохранить обложку мини-аппа</button></p>
          </form>
        </div>
        """
    return layout("Редактировать объект" if project else "Добавить объект", content, "new", message)


def selected(current, value):
    return "selected" if str(current or "") == value else ""


def lead_status_select(current):
    return "".join(
        f'<option value="{value}" {selected(current, value)}>{label}</option>'
        for value, label in LEAD_STATUSES
    )


def crm_client_name(row):
    name = " ".join(
        part for part in [row["first_name"], row["last_name"]] if part
    ).strip()
    return name or row["username"] or str(row["chat_id"])


def crm_contact_html(row):
    items = []
    if row["username"]:
        username = row["username"]
        items.append(f'<a class="crm-pill" href="https://t.me/{escape(username)}">✈️ @{escape(username)}</a>')
    items.append(f'<span class="crm-pill">ID {escape(row["chat_id"])}</span>')
    if row["contact_values"]:
        for value in str(row["contact_values"]).split(" | "):
            value = value.strip()
            if value:
                items.append(f'<span class="crm-pill">☎ {escape(value)}</span>')
    return "".join(items)


def crm_client_url(chat_id):
    path = f"/crm/client?chat_id={urllib.parse.quote(str(chat_id))}"
    return f"{ADMIN_BASE_URL}{path}" if ADMIN_BASE_URL else path


def crm_card_url(card_id):
    path = f"/crm/card?id={urllib.parse.quote(str(card_id))}"
    return f"{ADMIN_BASE_URL}{path}" if ADMIN_BASE_URL else path


def crm_status_options(current=None):
    with db() as conn:
        statuses = conn.execute("select * from crm_statuses order by position, id").fetchall()
    return "".join(
        f'<option value="{row["id"]}" {selected(current, row["id"])}>{escape(row["name"])}</option>'
        for row in statuses
    )


def crm_client_summary(chat_id):
    with db() as conn:
        row = conn.execute(
            """
            select
              s.*,
              coalesce((select group_concat(contact_value, ' | ') from leads where chat_id = s.chat_id and contact_value is not null and contact_value != ''), '') ||
                case when exists(select 1 from leads where chat_id = s.chat_id and contact_value is not null and contact_value != '')
                       and (exists(select 1 from custom_broadcast_leads where chat_id = s.chat_id and contact_value is not null and contact_value != '')
                            or exists(select 1 from personal_leads where chat_id = s.chat_id and contact_value is not null and contact_value != ''))
                     then ' | ' else '' end ||
                coalesce((select group_concat(contact_value, ' | ') from custom_broadcast_leads where chat_id = s.chat_id and contact_value is not null and contact_value != ''), '') ||
                case when exists(select 1 from custom_broadcast_leads where chat_id = s.chat_id and contact_value is not null and contact_value != '')
                       and exists(select 1 from personal_leads where chat_id = s.chat_id and contact_value is not null and contact_value != '')
                     then ' | ' else '' end ||
                coalesce((select group_concat(contact_value, ' | ') from personal_leads where chat_id = s.chat_id and contact_value is not null and contact_value != ''), '') contact_values
            from subscribers s
            where s.chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
    return row


def crm_page(query=None, message=""):
    query = query or {}
    search = query.get("q", [""])[0].strip()
    with db() as conn:
        statuses = conn.execute("select * from crm_statuses order by position, id").fetchall()
        cards = conn.execute(
            """
            select c.*, s.username,
              (select remind_at from crm_reminders where card_id = c.id and status='scheduled' order by remind_at asc limit 1) next_reminder_at,
              (select count(*) from crm_notes where card_id = c.id) note_count
            from crm_cards c
            left join subscribers s on s.chat_id = c.chat_id
            order by c.updated_at desc, c.id desc
            """
        ).fetchall()
    needle = search.casefold()
    cards_by_status = {row["id"]: [] for row in statuses}
    for row in cards:
        haystack = " ".join(
            str(part or "")
            for part in [
                row["title"],
                row["client_name"],
                row["contact_value"],
                row["budget"],
                row["request"],
                row["source"],
                row["username"],
                row["chat_id"],
            ]
        ).casefold()
        if not needle or needle in haystack:
            cards_by_status.setdefault(row["status_id"], []).append(row)
    columns = "".join(
        f"""
        <section class="crm-column">
          <h3>{escape(status['name'])} <span>{len(cards_by_status.get(status['id'], []))}</span></h3>
          <div class="crm-column-list">
            {''.join(crm_board_card(card) for card in cards_by_status.get(status['id'], [])) or '<p class="muted">Пока пусто.</p>'}
          </div>
        </section>
        """
        for status in statuses
    )
    status_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['position'])}</td>
          <td>
            <form class="actions" method="post" action="/crm/status/update">
              <input type="hidden" name="id" value="{row['id']}">
              <input name="name" required value="{escape(row['name'])}">
              <button class="secondary">Переименовать</button>
            </form>
          </td>
          <td>{len(cards_by_status.get(row['id'], []))}</td>
          <td>
            <form class="inline" method="post" action="/crm/status/delete" onsubmit="return confirm('Удалить колонку? Карточки будут перенесены в первую оставшуюся колонку.')">
              <input type="hidden" name="id" value="{row['id']}">
              <button class="danger">Удалить</button>
            </form>
          </td>
        </tr>
        """
        for row in statuses
    )
    content = f"""
    <div class="crm-toolbar">
      <form method="get" action="/crm" class="crm-toolbar-search">
        <input name="q" value="{escape(search)}" placeholder="Поиск: имя, контакт, бюджет, запрос, Telegram ID">
        <button>Найти</button>
        {'<a class="button secondary" href="/crm">Сбросить</a>' if search else ''}
      </form>
      <div class="crm-toolbar-actions">
        <button type="button" class="icon-button" title="Добавить карточку" onclick="document.getElementById('crm-card-dialog').showModal()">+</button>
        <button type="button" class="secondary" onclick="document.getElementById('crm-settings-dialog').showModal()">Настройки</button>
      </div>
    </div>
    <div class="crm-board">
      {columns}
    </div>
    <dialog class="modal" id="crm-card-dialog">
      <form class="modal-panel" method="post" action="/crm/card/create">
        <div class="modal-header">
          <div>
            <p class="eyebrow">CRM</p>
            <h2>Новая карточка</h2>
          </div>
          <button class="modal-close" type="button" onclick="this.closest('dialog').close()">×</button>
        </div>
        <div class="form-grid">
          <label>Статус<select name="status_id">{crm_status_options()}</select></label>
          <label>Название карточки<input name="title" required placeholder="Например: 1BR для инвестиций"></label>
          <label>Имя клиента<input name="client_name" placeholder="Имя клиента"></label>
          <label>Контакт<input name="contact_value" placeholder="Телефон, Telegram или WhatsApp"></label>
          <label>Бюджет<input name="budget" placeholder="Например: до 1.5M AED"></label>
          <label>Telegram ID<input name="chat_id" placeholder="Если нужно связать с подписчиком"></label>
          <label>Источник<input name="source" placeholder="Бот, рекомендация, Instagram"></label>
          <label class="wide">Запрос<textarea name="request" placeholder="Что ищет клиент, район, комнаты, цель покупки"></textarea></label>
        </div>
        <p class="actions"><button>Создать карточку</button><button class="secondary" type="button" onclick="this.closest('dialog').close()">Отмена</button></p>
      </form>
    </dialog>
    <dialog class="modal" id="crm-settings-dialog">
      <div class="modal-panel">
        <div class="modal-header">
          <div>
            <p class="eyebrow">Настройки CRM</p>
            <h2>Колонки и статусы</h2>
          </div>
          <button class="modal-close" type="button" onclick="this.closest('dialog').close()">×</button>
        </div>
        <form method="post" action="/crm/status/create">
          <label>Новая колонка<input name="name" required placeholder="Например: Документы"></label>
          <p class="actions"><button>Добавить колонку</button></p>
        </form>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Порядок</th><th>Колонка</th><th>Карточек</th><th></th></tr></thead>
            <tbody>{status_rows}</tbody>
          </table>
        </div>
        <p class="muted">Колонки можно создавать кастомно. Сейчас уже добавлена базовая воронка агента недвижимости.</p>
      </div>
    </dialog>
    """
    return layout("CRM", content, "crm", message)


def crm_board_card(card):
    contact = card["contact_value"] or (f"@{card['username']}" if card["username"] else "")
    return f"""
    <article class="crm-board-card">
      <a href="/crm/card?id={card['id']}"><strong>{escape(card['title'])}</strong></a>
      <p>{escape(card['client_name'] or 'Клиент не указан')}</p>
      {f'<p class="muted">{escape(contact)}</p>' if contact else ''}
      {f'<p class="muted">Бюджет: {escape(card["budget"])}</p>' if card["budget"] else ''}
      {f'<p class="muted">Напомнить: {escape(card["next_reminder_at"])}</p>' if card["next_reminder_at"] else ''}
      <form method="post" action="/crm/card/status" class="actions">
        <input type="hidden" name="id" value="{card['id']}">
        <select name="status_id">{crm_status_options(card['status_id'])}</select>
        <button class="secondary">Перенести</button>
      </form>
      <form method="post" action="/crm/card/delete" onsubmit="return confirm('Удалить CRM-карточку? Заявки и чат клиента останутся.')">
        <input type="hidden" name="id" value="{card['id']}">
        <button class="danger">Удалить</button>
      </form>
    </article>
    """


def get_crm_card(card_id):
    with db() as conn:
        return conn.execute(
            """
            select c.*, st.name status_name, s.username, s.first_name, s.last_name
            from crm_cards c
            left join crm_statuses st on st.id = c.status_id
            left join subscribers s on s.chat_id = c.chat_id
            where c.id = ?
            """,
            (card_id,),
        ).fetchone()


def crm_card_page(card_id, message=""):
    card = get_crm_card(card_id)
    if not card:
        return crm_page(message="CRM-карточка не найдена")
    chat_id = card["chat_id"]
    with db() as conn:
        notes = conn.execute("select * from crm_notes where card_id = ? order by created_at desc", (card_id,)).fetchall()
        reminders = conn.execute("select * from crm_reminders where card_id = ? order by remind_at desc", (card_id,)).fetchall()
        history = []
        if chat_id:
            history = conn.execute(
                """
                select created_at, event_type, payload from bot_events where chat_id = ?
                union all
                select created_at, 'message_' || direction, text from chat_messages where chat_id = ?
                order by created_at desc
                limit 30
                """,
                (chat_id, chat_id),
            ).fetchall()
    note_rows = "".join(
        f"<div class='crm-timeline-item'><strong>{escape(row['created_at'])}</strong><p>{escape(row['text'])}</p></div>"
        for row in notes
    )
    reminder_rows = "".join(
        f"<tr><td>{escape(row['remind_at'])}</td><td>{escape(row['text'])}</td><td>{'Отправлено' if row['status'] == 'sent' else 'Запланировано'}</td><td>{escape(row['sent_at'] or '')}</td></tr>"
        for row in reminders
    )
    history_rows = "".join(
        f"<div class='crm-timeline-item'><strong>{escape(event_label(row['event_type']))}</strong><span class='muted'>{escape(row['created_at'])}</span>{'<p>' + escape(row['payload']) + '</p>' if row['payload'] else ''}</div>"
        for row in history
    )
    content = f"""
    <div class="panel">
      <div class="crm-header">
        <div>
          <p class="eyebrow">CRM-карточка</p>
          <h2 class="crm-card-title">{escape(card['title'])}</h2>
          <p class="muted">{escape(card['status_name'] or '')}</p>
          <div class="crm-contact">
            {f'<span class="crm-pill">👤 {escape(card["client_name"])}</span>' if card["client_name"] else ''}
            {f'<span class="crm-pill">☎ {escape(card["contact_value"])}</span>' if card["contact_value"] else ''}
            {f'<span class="crm-pill">💰 {escape(card["budget"])}</span>' if card["budget"] else ''}
            {f'<a class="crm-pill" href="/crm/client?chat_id={escape(chat_id)}">Telegram ID {escape(chat_id)}</a>' if chat_id else ''}
          </div>
        </div>
        <div class="actions">
          {f'<a class="button secondary" href="/chats?chat_id={escape(chat_id)}">Открыть чат</a>' if chat_id else ''}
          <a class="button secondary" href="/crm">К доске</a>
          <form class="inline" method="post" action="/crm/card/delete" onsubmit="return confirm('Удалить CRM-карточку? Заявки и чат клиента останутся.')">
            <input type="hidden" name="id" value="{card['id']}">
            <button class="danger">Удалить карточку</button>
          </form>
        </div>
      </div>
      {f'<p>{escape(card["request"])}</p>' if card["request"] else ''}
      {f'<p class="muted">Источник: {escape(card["source"])}</p>' if card["source"] else ''}
    </div>
    <form class="panel" method="post" action="/crm/card/update">
      <h2>Редактировать карточку</h2>
      <input type="hidden" name="id" value="{card['id']}">
      <div class="form-grid">
        <label>Статус<select name="status_id">{crm_status_options(card['status_id'])}</select></label>
        <label>Название<input name="title" required value="{escape(card['title'])}"></label>
        <label>Имя клиента<input name="client_name" value="{escape(card['client_name'])}"></label>
        <label>Контакт<input name="contact_value" value="{escape(card['contact_value'])}"></label>
        <label>Бюджет<input name="budget" value="{escape(card['budget'])}"></label>
        <label>Telegram ID<input name="chat_id" value="{escape(chat_id or '')}"></label>
        <label>Источник<input name="source" value="{escape(card['source'])}"></label>
        <label class="wide">Запрос<textarea name="request">{escape(card['request'])}</textarea></label>
      </div>
      <p><button>Сохранить карточку</button></p>
    </form>
    <div class="crm-two-col">
      <form class="panel" method="post" action="/crm/note">
        <h2>Заметка</h2>
        <input type="hidden" name="card_id" value="{card['id']}">
        <input type="hidden" name="chat_id" value="{escape(chat_id or '0')}">
        <label>Комментарий<textarea name="text" required></textarea></label>
        <p><button>Добавить заметку</button></p>
      </form>
      <form class="panel" method="post" action="/crm/reminder">
        <h2>Напоминание</h2>
        <input type="hidden" name="card_id" value="{card['id']}">
        <input type="hidden" name="chat_id" value="{escape(chat_id or '0')}">
        <input type="hidden" name="client_name" value="{escape(card['client_name'] or card['title'])}">
        <input type="hidden" name="contact_value" value="{escape(card['contact_value'] or '')}">
        <label>Когда напомнить<input type="datetime-local" name="remind_at" required></label>
        <label>Что сделать<textarea name="text" required></textarea></label>
        <p><button>Поставить напоминание</button></p>
      </form>
    </div>
    <div class="panel">
      <h2>Напоминания</h2>
      <div class="table-scroll"><table><thead><tr><th>Когда</th><th>Задача</th><th>Статус</th><th>Отправлено</th></tr></thead><tbody>{reminder_rows or '<tr><td colspan="4" class="muted">Напоминаний пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Заметки</h2>
      <div class="crm-timeline">{note_rows or '<p class="muted">Заметок пока нет.</p>'}</div>
    </div>
    <div class="panel">
      <h2>История связанного Telegram-пользователя</h2>
      <div class="crm-timeline">{history_rows or '<p class="muted">Карточка не связана с Telegram-пользователем или истории пока нет.</p>'}</div>
    </div>
    """
    return layout("CRM-карточка", content, "crm", message)


def crm_client_page(chat_id, message=""):
    client = crm_client_summary(chat_id)
    if not client:
        return crm_page(message="Клиент не найден")
    with db() as conn:
        lot_leads = conn.execute(
            """
            select l.*, p.title project_title, p.district
            from leads l
            left join projects p on p.id = l.project_id
            where l.chat_id = ?
            order by l.created_at desc
            """,
            (chat_id,),
        ).fetchall()
        personal_leads = conn.execute("select * from personal_leads where chat_id = ? order by created_at desc", (chat_id,)).fetchall()
        custom_leads = conn.execute(
            """
            select l.*, b.text broadcast_text
            from custom_broadcast_leads l
            left join custom_broadcasts b on b.id = l.broadcast_id
            where l.chat_id = ?
            order by l.created_at desc
            """,
            (chat_id,),
        ).fetchall()
        seen = conn.execute(
            """
            select sp.seen_at, p.title, p.district, p.building, p.status
            from seen_projects sp
            left join projects p on p.id = sp.project_id
            where sp.chat_id = ?
            order by sp.seen_at desc
            limit 20
            """,
            (chat_id,),
        ).fetchall()
        events = conn.execute(
            """
            select created_at, event_type, project_id, payload
            from bot_events
            where chat_id = ?
            order by created_at desc
            limit 20
            """,
            (chat_id,),
        ).fetchall()
        messages = conn.execute("select * from chat_messages where chat_id = ? order by created_at desc limit 20", (chat_id,)).fetchall()
        notes = conn.execute("select * from crm_notes where chat_id = ? order by created_at desc", (chat_id,)).fetchall()
        reminders = conn.execute("select * from crm_reminders where chat_id = ? order by remind_at desc", (chat_id,)).fetchall()
    lot_lead_rows = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>{escape(row['project_title'] or 'Лот удалён')}<br><span class='muted'>{escape(row['district'] or '')}</span></td><td>{escape(row['contact_method'])}<br>{escape(row['contact_value'])}</td><td>{escape(LEAD_STATUS_LABELS.get(row['status'], row['status']))}</td></tr>"
        for row in lot_leads
    )
    personal_rows = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>Персональный подбор</td><td>{escape(row['contact_method'])}<br>{escape(row['contact_value'])}</td><td>{escape(LEAD_STATUS_LABELS.get(row['status'], row['status']))}</td></tr>"
        for row in personal_leads
    )
    custom_rows = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>{escape((row['broadcast_text'] or '')[:90])}</td><td>{escape(row['contact_method'])}<br>{escape(row['contact_value'])}</td><td>{escape(LEAD_STATUS_LABELS.get(row['status'], row['status']))}</td></tr>"
        for row in custom_leads
    )
    seen_rows = "".join(
        f"<tr><td>{escape(row['seen_at'])}</td><td>{escape(row['title'] or 'Лот удалён')}<br><span class='muted'>{escape(row['district'] or '')}, {escape(row['building'] or '')}</span></td><td>{escape(row['status'] or '')}</td></tr>"
        for row in seen
    )
    timeline_items = []
    for row in events:
        timeline_items.append((row["created_at"], event_label(row["event_type"]), row["payload"] or ""))
    for row in messages:
        label = "Сообщение клиента" if row["direction"] == "user" else "Ответ менеджера"
        timeline_items.append((row["created_at"], label, row["text"]))
    timeline_items.sort(key=lambda item: item[0] or "", reverse=True)
    timeline = "".join(
        f"<div class='crm-timeline-item'><strong>{escape(label)}</strong><span class='muted'>{escape(date)}</span>{f'<p>{escape(text)}</p>' if text else ''}</div>"
        for date, label, text in timeline_items[:30]
    )
    note_rows = "".join(
        f"<div class='crm-timeline-item'><strong>{escape(row['created_at'])}</strong><p>{escape(row['text'])}</p></div>"
        for row in notes
    )
    reminder_rows = "".join(
        f"<tr><td>{escape(row['remind_at'])}</td><td>{escape(row['text'])}</td><td>{'Отправлено' if row['status'] == 'sent' else 'Запланировано'}</td><td>{escape(row['sent_at'] or '')}</td></tr>"
        for row in reminders
    )
    content = f"""
    <div class="panel">
      <div class="crm-header">
        <div>
          <p class="eyebrow">Карточка клиента</p>
          <h2 class="crm-card-title">{escape(crm_client_name(client))}</h2>
          <p class="muted">{'@' + escape(client['username']) if client['username'] else 'Chat ID: ' + escape(client['chat_id'])}</p>
          <div class="crm-contact">{crm_contact_html(client)}</div>
        </div>
        <p class="actions">
          <a class="button secondary" href="/chats?chat_id={escape(chat_id)}">Открыть чат</a>
          <a class="button secondary" href="/crm">К CRM</a>
        </p>
      </div>
    </div>
    <div class="grid">
      <div class="metric"><strong>{len(lot_leads) + len(personal_leads) + len(custom_leads)}</strong><span>заявок</span></div>
      <div class="metric"><strong>{len(seen)}</strong><span>последних просмотров лотов</span></div>
      <div class="metric"><strong>{len(messages)}</strong><span>последних сообщений</span></div>
      <div class="metric"><strong>{len([r for r in reminders if r['status'] == 'scheduled'])}</strong><span>активных напоминаний</span></div>
    </div>
    <div class="crm-two-col">
      <form class="panel" method="post" action="/crm/note">
        <h2>Заметка</h2>
        <input type="hidden" name="chat_id" value="{escape(chat_id)}">
        <label>Комментарий менеджера<textarea name="text" required placeholder="Например: ищет 1BR до 1.5M, готов к покупке в сентябре"></textarea></label>
        <p><button>Добавить заметку</button></p>
      </form>
      <form class="panel" method="post" action="/crm/reminder">
        <h2>Напоминание</h2>
        <input type="hidden" name="chat_id" value="{escape(chat_id)}">
        <label>Когда напомнить<input type="datetime-local" name="remind_at" required></label>
        <label>Что сделать<textarea name="text" required placeholder="Связаться, уточнить бюджет, отправить подборку"></textarea></label>
        <p><button>Поставить напоминание</button></p>
      </form>
    </div>
    <div class="panel">
      <h2>Напоминания</h2>
      <div class="table-scroll"><table><thead><tr><th>Когда</th><th>Задача</th><th>Статус</th><th>Отправлено</th></tr></thead><tbody>{reminder_rows or '<tr><td colspan="4" class="muted">Напоминаний пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Заметки</h2>
      <div class="crm-timeline">{note_rows or '<p class="muted">Заметок пока нет.</p>'}</div>
    </div>
    <div class="panel">
      <h2>Заявки</h2>
      <div class="table-scroll"><table><thead><tr><th>Дата</th><th>Источник</th><th>Контакт</th><th>Статус</th></tr></thead><tbody>{lot_lead_rows + personal_rows + custom_rows or '<tr><td colspan="4" class="muted">Заявок пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>Просмотренные лоты</h2>
      <div class="table-scroll"><table><thead><tr><th>Дата</th><th>Лот</th><th>Статус</th></tr></thead><tbody>{seen_rows or '<tr><td colspan="3" class="muted">Просмотров пока нет.</td></tr>'}</tbody></table></div>
    </div>
    <div class="panel">
      <h2>История действий и переписки</h2>
      <div class="crm-timeline">{timeline or '<p class="muted">Истории пока нет.</p>'}</div>
    </div>
    """
    return layout("CRM: карточка клиента", content, "crm", message)


def leads_page():
    with db() as conn:
        leads = conn.execute(
            """
            select l.*, p.title project_title from leads l
            left join projects p on p.id = l.project_id
            order by l.created_at desc
            """
        ).fetchall()
        custom_leads = conn.execute(
            """
            select l.*, b.text broadcast_text from custom_broadcast_leads l
            left join custom_broadcasts b on b.id = l.broadcast_id
            order by l.created_at desc
            """
        ).fetchall()
        personal_leads = conn.execute(
            """
            select * from personal_leads
            order by created_at desc
            """
        ).fetchall()
        web_leads = conn.execute(
            """
            select l.*, p.title project_title from web_leads l
            left join projects p on p.id = l.project_id
            order by l.created_at desc
            """
        ).fetchall()
    rows = "".join(
        f"""
        <tr>
          <td>{escape(l['created_at'])}</td>
          <td><strong>{escape(l['name'])}</strong><br><span class='muted'>@{escape(l['username']) if l['username'] else escape(l['chat_id'])}</span></td>
          <td>{escape(l['project_title'] or 'Лот удалён')}</td>
          <td>{escape(l['contact_method'])}<br>{escape(l['contact_value'])}</td>
          <td>
            <form method="post" action="/lead/status" class="actions">
              <input type="hidden" name="id" value="{l['id']}">
              <select name="status">{lead_status_select(l['status'])}</select>
              <button>Сохранить</button>
            </form>
          </td>
          <td>
            <form method="post" action="/lead/delete" class="inline"><input type="hidden" name="id" value="{l['id']}"><button class="danger">Удалить</button></form>
          </td>
        </tr>
        """
        for l in leads
    )
    custom_rows = "".join(
        f"""
        <tr>
          <td>{escape(l['created_at'])}</td>
          <td><strong>{escape(l['name'])}</strong><br><span class='muted'>@{escape(l['username']) if l['username'] else escape(l['chat_id'])}</span></td>
          <td>{escape((l['broadcast_text'] or '')[:120])}</td>
          <td>{escape(l['contact_method'])}<br>{escape(l['contact_value'])}</td>
          <td>
            <form method="post" action="/custom-lead/status" class="actions">
              <input type="hidden" name="id" value="{l['id']}">
              <select name="status">{lead_status_select(l['status'])}</select>
              <button>Сохранить</button>
            </form>
          </td>
          <td>
            <form method="post" action="/custom-lead/delete" class="inline"><input type="hidden" name="id" value="{l['id']}"><button class="danger">Удалить</button></form>
          </td>
        </tr>
        """
        for l in custom_leads
    )
    personal_rows = "".join(
        f"""
        <tr>
          <td>{escape(l['created_at'])}</td>
          <td><strong>{escape(l['name'])}</strong><br><span class='muted'>@{escape(l['username']) if l['username'] else escape(l['chat_id'])}</span></td>
          <td>{escape(l['contact_method'])}<br>{escape(l['contact_value'])}</td>
          <td>
            <form method="post" action="/personal-lead/status" class="actions">
              <input type="hidden" name="id" value="{l['id']}">
              <select name="status">{lead_status_select(l['status'])}</select>
              <button>Сохранить</button>
            </form>
          </td>
          <td>
            <form method="post" action="/personal-lead/delete" class="inline"><input type="hidden" name="id" value="{l['id']}"><button class="danger">Удалить</button></form>
          </td>
        </tr>
        """
        for l in personal_leads
    )
    web_rows = "".join(
        f"""
        <tr>
          <td>{escape(l['created_at'])}</td>
          <td><strong>{escape(l['name'] or 'Не указано')}</strong></td>
          <td>{escape(l['project_title'] or 'Лот не выбран / удалён')}</td>
          <td>{escape(l['contact_value'])}</td>
          <td>{escape(l['message'])}</td>
          <td>
            <form method="post" action="/web-lead/status" class="actions">
              <input type="hidden" name="id" value="{l['id']}">
              <select name="status">{lead_status_select(l['status'])}</select>
              <button>Сохранить</button>
            </form>
          </td>
          <td>
            <form method="post" action="/web-lead/delete" class="inline"><input type="hidden" name="id" value="{l['id']}"><button class="danger">Удалить</button></form>
          </td>
        </tr>
        """
        for l in web_leads
    )
    empty = '<tr><td colspan="6" class="muted">Заявок пока нет.</td></tr>'
    return layout(
        "Заявки",
        f"""
        <div class="panel">
          <h2>Заявки из мини-приложения</h2>
          <table><thead><tr><th>Дата</th><th>Клиент</th><th>Лот</th><th>Контакт</th><th>Комментарий</th><th>Статус</th><th>Удалить</th></tr></thead><tbody>{web_rows or '<tr><td colspan="7" class="muted">Заявок из мини-приложения пока нет.</td></tr>'}</tbody></table>
        </div>
        <div class="panel">
          <h2>Заявки на персональный подбор</h2>
          <table><thead><tr><th>Дата</th><th>Клиент</th><th>Контакт</th><th>Статус</th><th>Удалить</th></tr></thead><tbody>{personal_rows or '<tr><td colspan="5" class="muted">Заявок на персональный подбор пока нет.</td></tr>'}</tbody></table>
        </div>
        <div class="panel">
          <h2>Заявки по лотам</h2>
          <table><thead><tr><th>Дата</th><th>Клиент</th><th>Объект</th><th>Контакт</th><th>Статус</th><th>Удалить</th></tr></thead><tbody>{rows or empty}</tbody></table>
        </div>
        <div class="panel">
          <h2>Заявки по свободным рассылкам</h2>
          <table><thead><tr><th>Дата</th><th>Клиент</th><th>Рассылка</th><th>Контакт</th><th>Статус</th><th>Удалить</th></tr></thead><tbody>{custom_rows or '<tr><td colspan="6" class="muted">Заявок по свободным рассылкам пока нет.</td></tr>'}</tbody></table>
        </div>
        """,
        "leads",
    )


def broadcasts_page(message=""):
    with db() as conn:
        projects = conn.execute("select id,title from projects where status='active' order by created_at desc").fetchall()
        items = conn.execute(
            "select b.*, p.title project_title from broadcasts b left join projects p on p.id=b.project_id order by b.created_at desc"
        ).fetchall()
        custom_items = conn.execute(
            "select * from custom_broadcasts order by created_at desc limit 20"
        ).fetchall()
    options = "".join(f"<option value='{p['id']}'>{escape(p['title'])}</option>" for p in projects)
    rows = "".join(
        f"<tr><td>{escape(i['send_at'])}</td><td>{escape(i['project_title'])}</td><td>{escape(i['status'])}</td><td>{i['sent_count']}</td></tr>"
        for i in items
    )
    custom_rows = "".join(
        f"<tr><td>{escape(i['send_at'] or i['created_at'])}</td><td>{escape(i['text'][:120])}{'...' if len(i['text']) > 120 else ''}</td><td>{escape(i['status'])}</td><td>{i['sent_count']}</td></tr>"
        for i in custom_items
    )
    content = f"""
    <form class="panel" method="post" action="/broadcast/custom" enctype="multipart/form-data">
      <h2>Свободная рассылка</h2>
      <div class="form-grid">
        <label class="wide">Текст рассылки<textarea name="text" required placeholder="Напишите сообщение для подписчиков"></textarea></label>
        <label class="wide">Фото/видео<input type="file" name="media" multiple accept="image/*,video/*"></label>
        <label>Дата и время отправки<input type="datetime-local" name="send_at"></label>
      </div>
      <p class="actions">
        <button name="mode" value="send_now">Отправить сейчас</button>
        <button class="secondary" name="mode" value="schedule">Запланировать</button>
      </p>
    </form>
    <div class="panel">
      <h2>История свободных рассылок</h2>
      <table><thead><tr><th>Дата/время</th><th>Текст</th><th>Статус</th><th>Отправлено</th></tr></thead><tbody>{custom_rows or '<tr><td colspan="4" class="muted">Свободных рассылок пока нет.</td></tr>'}</tbody></table>
    </div>
    <form class="panel" method="post" action="/broadcast/create">
      <h2>Рассылка лота по расписанию</h2>
      <div class="form-grid">
        <label>Объект<select name="project_id" required>{options}</select></label>
        <label>Дата и время отправки<input type="datetime-local" name="send_at" required></label>
        <label>&nbsp;<button>Запланировать</button></label>
      </div>
    </form>
    <div class="panel">
      <h2>Запланированные рассылки лотов</h2>
    <table><thead><tr><th>Когда</th><th>Объект</th><th>Статус</th><th>Отправлено</th></tr></thead><tbody>{rows or '<tr><td colspan="4" class="muted">Рассылок пока нет.</td></tr>'}</tbody></table>
    </div>
    """
    return layout("Рассылки", content, "broadcasts", message)


def subscribers_page(message=""):
    with db() as conn:
        rows_data = conn.execute("select * from subscribers order by subscribed_at desc").fetchall()
    rows = "".join(
        f"""
        <tr>
          <td>{escape(s['chat_id'])}</td>
          <td>{escape(s['first_name'])} {escape(s['last_name'])}<br><span class='muted'>@{escape(s['username'])}</span></td>
          <td>
            <form method="post" action="/subscriber/tag" class="actions">
              <input type="hidden" name="chat_id" value="{escape(s['chat_id'])}">
              <select name="subscriber_tag">{subscriber_tag_select(s['subscriber_tag'])}</select>
              <button>Сохранить</button>
            </form>
          </td>
          <td>{escape(s['subscribed_at'])}</td>
          <td>{escape(s['last_seen_at'])}</td>
          <td>
            <a class="button secondary" href="/subscriber/chat?chat_id={escape(s['chat_id'])}">Открыть чат</a>
          </td>
        </tr>
        """
        for s in rows_data
    )
    empty = '<tr><td colspan="6" class="muted">Подписчиков пока нет.</td></tr>'
    return layout(
        "Подписчики",
        f"""
        <form class="panel" method="post" action="/subscriber/segment-message">
          <h2>Написать сегменту</h2>
          <div class="form-grid">
            <label>Сегмент<select name="segment">
              <option value="all">Все подписчики</option>
              <option value="realtors">Риелторы</option>
              <option value="end_users">Конечники</option>
              <option value="active_7d">Активные за 7 дней</option>
              <option value="clicked_interest">Нажимали интерес</option>
              <option value="has_lot_leads">Оставляли заявку по лоту</option>
              <option value="has_personal_leads">Оставляли персональный подбор</option>
            </select></label>
            <label class="wide">Сообщение<textarea name="text" required placeholder="Напишите сообщение, оно придёт пользователям в бот"></textarea></label>
          </div>
          <p><button>Отправить сегменту</button></p>
        </form>
        <p class="actions"><a class="button secondary" href="/subscribers/stats">Открыть статистику подписчиков</a></p>
        <div class="panel">
          <h2>Подписчики</h2>
          <div class="table-scroll"><table><thead><tr><th>Chat ID</th><th>Имя</th><th>Тег</th><th>Подписался</th><th>Последняя активность</th><th>Написать</th></tr></thead><tbody>{rows or empty}</tbody></table></div>
        </div>
        """,
        "subscribers",
        message,
    )


def chat_display_name(row):
    return " ".join(
        part for part in [row["first_name"], row["last_name"]] if part
    ) or row["username"] or str(row["chat_id"])


def chat_search_text(row):
    return " ".join(
        str(part)
        for part in [
            row["chat_id"],
            row["username"],
            row["first_name"],
            row["last_name"],
            chat_display_name(row),
            row["contact_values"],
            row["contact_names"],
        ]
        if part
    ).casefold()


def chats_page(chat_id="", message="", search=""):
    search = (search or "").strip()
    with db() as conn:
        conversations_all = conn.execute(
            """
            select
              s.*,
              cm.text last_text,
              cm.direction last_direction,
              cm.created_at last_message_at,
              coalesce((select group_concat(contact_value, ' ') from leads where chat_id = s.chat_id), '') ||
                ' ' || coalesce((select group_concat(contact_value, ' ') from custom_broadcast_leads where chat_id = s.chat_id), '') ||
                ' ' || coalesce((select group_concat(contact_value, ' ') from personal_leads where chat_id = s.chat_id), '') contact_values,
              coalesce((select group_concat(name, ' ') from leads where chat_id = s.chat_id), '') ||
                ' ' || coalesce((select group_concat(name, ' ') from custom_broadcast_leads where chat_id = s.chat_id), '') ||
                ' ' || coalesce((select group_concat(name, ' ') from personal_leads where chat_id = s.chat_id), '') contact_names,
              (select count(*) from chat_messages where chat_id = s.chat_id) message_count,
              (
                select count(*)
                from chat_messages unread
                where unread.chat_id = s.chat_id
                  and unread.direction = 'user'
                  and (s.chat_read_at is null or unread.created_at > s.chat_read_at)
              ) unread_count
            from subscribers s
            left join chat_messages cm on cm.id = (
              select id from chat_messages
              where chat_id = s.chat_id
              order by created_at desc, id desc
              limit 1
            )
            order by coalesce(cm.created_at, s.last_seen_at, s.subscribed_at) desc
            """
        ).fetchall()
        conversations = [
            row for row in conversations_all
            if not search or search.casefold() in chat_search_text(row)
        ]
        if not chat_id and conversations:
            chat_id = str(conversations[0]["chat_id"])
        if chat_id:
            conn.execute("update subscribers set chat_read_at = ? where chat_id = ?", (iso_now(), chat_id))
            conversations_all = conn.execute(
                """
                select
                  s.*,
                  cm.text last_text,
                  cm.direction last_direction,
                  cm.created_at last_message_at,
                  coalesce((select group_concat(contact_value, ' ') from leads where chat_id = s.chat_id), '') ||
                    ' ' || coalesce((select group_concat(contact_value, ' ') from custom_broadcast_leads where chat_id = s.chat_id), '') ||
                    ' ' || coalesce((select group_concat(contact_value, ' ') from personal_leads where chat_id = s.chat_id), '') contact_values,
                  coalesce((select group_concat(name, ' ') from leads where chat_id = s.chat_id), '') ||
                    ' ' || coalesce((select group_concat(name, ' ') from custom_broadcast_leads where chat_id = s.chat_id), '') ||
                    ' ' || coalesce((select group_concat(name, ' ') from personal_leads where chat_id = s.chat_id), '') contact_names,
                  (select count(*) from chat_messages where chat_id = s.chat_id) message_count,
                  (
                    select count(*)
                    from chat_messages unread
                    where unread.chat_id = s.chat_id
                      and unread.direction = 'user'
                      and (s.chat_read_at is null or unread.created_at > s.chat_read_at)
                  ) unread_count
                from subscribers s
                left join chat_messages cm on cm.id = (
                  select id from chat_messages
                  where chat_id = s.chat_id
                  order by created_at desc, id desc
                  limit 1
                )
                order by coalesce(cm.created_at, s.last_seen_at, s.subscribed_at) desc
                """
            ).fetchall()
            conversations = [
                row for row in conversations_all
                if not search or search.casefold() in chat_search_text(row)
            ]
        subscriber = conn.execute("select * from subscribers where chat_id = ?", (chat_id,)).fetchone() if chat_id else None
        messages = conn.execute(
            "select * from chat_messages where chat_id = ? order by created_at asc",
            (chat_id,),
        ).fetchall() if subscriber else []

    search_query = urllib.parse.urlencode({"q": search}) if search else ""
    search_suffix = f"&{search_query}" if search_query else ""
    search_form = f"""
    <form class="chat-search" method="get" action="/chats">
      <input name="q" value="{escape(search)}" placeholder="Имя, username, телефон, WhatsApp, Chat ID">
      <button>Найти</button>
      {f'<a class="button secondary" href="/chats">Сбросить</a>' if search else ''}
    </form>
    """
    dialog_rows = "".join(
        f"""
        <a class="{'active' if str(row['chat_id']) == str(chat_id) else ''}" href="/chats?chat_id={escape(row['chat_id'])}{search_suffix}">
          <div class="chat-list-title">
            <strong>{escape(chat_display_name(row))}</strong>
            {f'<span class="unread-badge">{row["unread_count"]}</span>' if row["unread_count"] else ''}
          </div>
          <div class="chat-preview">{escape('@' + row['username'] if row['username'] else 'Chat ID: ' + str(row['chat_id']))}</div>
          <div class="chat-preview">{escape(('Менеджер: ' if row['last_direction'] == 'admin' else 'Клиент: ') + row['last_text'] if row['last_text'] else 'Сообщений пока нет')}</div>
        </a>
        """
        for row in conversations
    )

    if not subscriber:
        content = f"""
        <div class="chat-layout">
          <section class="panel chat-sidebar">
            <h2>Диалоги</h2>
            {search_form}
            <div class="chat-list">{dialog_rows or '<p class="muted" style="padding:0 18px 18px">Ничего не найдено.</p>'}</div>
          </section>
          <section class="panel chat-shell">
            <div class="chat-header"><div><h2>Выберите чат</h2><p class="muted">Откройте диалог из списка слева или найдите клиента по контакту.</p></div></div>
            <div class="chat-thread"></div>
          </section>
        </div>
        """
        return layout("Чаты", content, "chats", message)

    display_name = chat_display_name(subscriber)
    bubbles = "".join(
        f"""
        <div class="chat-bubble {'admin' if row['direction'] == 'admin' else 'user'}">
          <div class="chat-meta">{escape('Менеджер' if row['direction'] == 'admin' else display_name)} · {escape(row['created_at'])}</div>
          {escape(row['text'])}
        </div>
        """
        for row in messages
    )
    content = f"""
    <div class="chat-layout active-chat">
      <section class="panel chat-sidebar">
        <h2>Диалоги</h2>
        {search_form}
        <div class="chat-list">{dialog_rows or '<p class="muted" style="padding:0 18px 18px">Диалогов пока нет.</p>'}</div>
      </section>
      <section class="panel chat-shell">
        <div class="chat-header">
          <div>
            <h2>{escape(display_name)}</h2>
            <p class="muted">{'@' + escape(subscriber['username']) if subscriber['username'] else 'Chat ID: ' + escape(subscriber['chat_id'])}</p>
          </div>
          <a class="button secondary" href="/subscribers">К подписчикам</a>
        </div>
        <div class="chat-thread">{bubbles or '<p class="muted">Истории переписки пока нет.</p>'}</div>
        <form class="chat-compose" method="post" action="/subscriber/message">
          <input type="hidden" name="chat_id" value="{escape(chat_id)}">
          <label>Ответить клиенту<textarea name="text" required placeholder="Напишите ответ клиенту"></textarea></label>
          <p><button>Отправить</button></p>
        </form>
      </section>
    </div>
    """
    return layout("Чаты", content, "chats", message)


def login_page(message=""):
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex, nofollow"><title>Вход</title><style>{CSS}</style></head>
<body class="login"><form method="post" action="/login"><h1>Realty Bot</h1>{f'<div class="notice">{escape(message)}</div>' if message else ''}<label>Логин<input name="username" autocomplete="username"></label><label>Пароль<input name="password" type="password" autocomplete="current-password"></label><button>Войти</button></form></body></html>"""


def save_project(form, project_id=None):
    values = {
        "title": form_value(form, "title"),
        "category": form_value(form, "category"),
        "district": form_value(form, "district"),
        "building": form_value(form, "building"),
        "rooms": form_value(form, "rooms"),
        "bathrooms": form_value(form, "bathrooms"),
        "floor_level": form_value(form, "floor_level"),
        "parking": form_value(form, "parking"),
        "availability": form_value(form, "availability"),
        "furnishing": form_value(form, "furnishing"),
        "balcony": form_value(form, "balcony"),
        "area": form_value(form, "area"),
        "price": form_value(form, "price"),
        "market_price": form_value(form, "market_price") or None,
        "distress": int(form_value(form, "distress", "0") or 0),
        "original_price": form_value(form, "original_price") or None,
        "source_from": form_value(form, "source_from"),
        "description": form_value(form, "description"),
        "updated_at": iso_now(),
    }
    with db() as conn:
        if project_id:
            values["id"] = project_id
            conn.execute(
                """
                update projects set title=:title, category=:category, district=:district, building=:building,
                  rooms=:rooms, bathrooms=:bathrooms, floor_level=:floor_level, parking=:parking,
                  availability=:availability, furnishing=:furnishing, balcony=:balcony, area=:area,
                  price=:price, market_price=:market_price, distress=:distress, original_price=:original_price,
                  source_from=:source_from, description=:description, updated_at=:updated_at
                where id=:id
                """,
                values,
            )
            new_id = project_id
        else:
            values["created_at"] = iso_now()
            cur = conn.execute(
                """
                insert into projects(title, category, district, building, rooms, bathrooms, floor_level,
                  parking, availability, furnishing, balcony, area, price, market_price, distress,
                  original_price, source_from, description, created_at, updated_at)
                values(:title, :category, :district, :building, :rooms, :bathrooms, :floor_level,
                  :parking, :availability, :furnishing, :balcony, :area, :price, :market_price,
                  :distress, :original_price, :source_from, :description, :created_at, :updated_at)
                """,
                values,
            )
            new_id = cur.lastrowid

    if isinstance(form, cgi.FieldStorage) and "media" in form:
        files = form["media"]
        if not isinstance(files, list):
            files = [files]
        for item in files:
            if not item.filename:
                continue
            ext = Path(item.filename).suffix.lower()
            safe_name = f"{new_id}_{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
            target = UPLOAD_DIR / safe_name
            with target.open("wb") as out:
                shutil.copyfileobj(item.file, out)
            mime = item.type or mimetypes.guess_type(item.filename)[0] or "application/octet-stream"
            with db() as conn:
                cur = conn.execute(
                    "insert into media(project_id, file_name, original_name, mime_type, created_at) values (?, ?, ?, ?, ?)",
                    (new_id, safe_name, item.filename, mime, iso_now()),
                )
                media_id = cur.lastrowid
                if mime.startswith("image/"):
                    conn.execute(
                        """
                        update projects
                        set cover_media_id = coalesce(cover_media_id, ?)
                        where id = ?
                        """,
                        (media_id, new_id),
                    )
    return new_id


def save_project_cover(project_id, cover_media_id):
    project_id = int(project_id)
    cover_media_id = int(cover_media_id)
    with db() as conn:
        media = conn.execute(
            "select id from media where id = ? and project_id = ? and mime_type like 'image/%'",
            (cover_media_id, project_id),
        ).fetchone()
        if not media:
            return False
        conn.execute(
            "update projects set cover_media_id = ?, updated_at = ? where id = ?",
            (cover_media_id, iso_now(), project_id),
        )
    return True


def delete_project(project_id):
    project_id = int(project_id)
    with db() as conn:
        media_rows = conn.execute("select file_name from media where project_id = ?", (project_id,)).fetchall()
        for table_name in ("seen_projects", "media", "broadcasts"):
            conn.execute(f"delete from {table_name} where project_id = ?", (project_id,))
        conn.execute("delete from projects where id = ?", (project_id,))
    for row in media_rows:
        path = UPLOAD_DIR / row["file_name"]
        if path.exists():
            path.unlink()
    collage_path = DATA_DIR / "generated" / f"project_{project_id}_collage.jpg"
    if collage_path.exists():
        collage_path.unlink()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_html(self, body, status=200, headers=None):
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def send_text(self, body, content_type="text/plain; charset=utf-8", status=200):
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_bytes(self, data, content_type, filename):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def current_user(self):
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie(raw)
        if "session" not in jar:
            return None
        return verify_signature(jar["session"].value)

    def require_auth(self):
        if not self.current_user():
            self.redirect("/login")
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/login":
            self.send_html(login_page())
        elif path == "/robots.txt":
            self.send_text(robots_txt())
        elif path == "/sitemap.xml":
            self.send_text(sitemap_xml(), "application/xml; charset=utf-8")
        elif path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers()
        elif path.startswith("/yandex_") and path.endswith(".html") and "/" not in path.removeprefix("/"):
            verification_file = APP_DIR / path.removeprefix("/")
            if verification_file.exists():
                self.send_html(verification_file.read_text(encoding="utf-8"))
            else:
                self.send_html("Not found", status=404)
        elif path == "/":
            self.send_html(app_projects_page(query, base_path=""))
        elif path == "/lot":
            try:
                project_id = int(query.get("id", ["0"])[0])
            except ValueError:
                project_id = 0
            self.send_html(app_project_page(project_id, base_path=""))
        elif path == "/app":
            self.send_html(app_projects_page(query))
        elif path == "/app/lot":
            try:
                project_id = int(query.get("id", ["0"])[0])
            except ValueError:
                project_id = 0
            self.send_html(app_project_page(project_id))
        elif path.startswith("/media/"):
            self.serve_media(path.removeprefix("/media/"), include_body=True)
        elif not self.require_auth():
            return
        elif path == "/admin":
            self.send_html(dashboard())
        elif path == "/projects/export.xlsx":
            filename = f"active_lots_{now_local().strftime('%Y-%m-%d')}.xlsx"
            self.send_bytes(
                build_active_projects_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename,
            )
        elif path == "/stats/export.xlsx":
            period = parse_stats_period(query)
            filename = f"bot_stats_{now_local().strftime('%Y-%m-%d')}.xlsx"
            self.send_bytes(
                build_statistics_xlsx(period),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename,
            )
        elif path == "/subscribers/stats/export.xlsx":
            period = parse_stats_period(query)
            filename = f"subscriber_stats_{now_local().strftime('%Y-%m-%d')}.xlsx"
            self.send_bytes(
                build_subscriber_statistics_xlsx(period),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename,
            )
        elif path == "/project/new":
            self.send_html(project_form())
        elif path == "/project/edit":
            project = get_project(int(query.get("id", ["0"])[0]))
            self.send_html(project_form(project) if project else dashboard("Объект не найден"))
        elif path == "/crm":
            self.send_html(crm_page(query))
        elif path == "/crm/card":
            self.send_html(crm_card_page(query.get("id", [""])[0]))
        elif path == "/crm/client":
            self.send_html(crm_client_page(query.get("chat_id", [""])[0]))
        elif path == "/leads":
            self.send_html(leads_page())
        elif path == "/stats":
            self.send_html(statistics_page(query))
        elif path == "/subscribers/stats":
            self.send_html(subscriber_statistics_page(query))
        elif path == "/broadcasts":
            self.send_html(broadcasts_page())
        elif path == "/chats":
            self.send_html(chats_page(query.get("chat_id", [""])[0], search=query.get("q", [""])[0]))
        elif path == "/subscriber/chat":
            self.send_html(chats_page(query.get("chat_id", [""])[0], search=query.get("q", [""])[0]))
        elif path == "/subscribers":
            self.send_html(subscribers_page())
        else:
            self.send_html("Not found", status=404)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/media/"):
            self.serve_media(path.removeprefix("/media/"), include_body=False)
        else:
            self.send_response(404)
            self.end_headers()

    def serve_media(self, name, include_body=True):
        safe = Path(urllib.parse.unquote(name)).name
        target = UPLOAD_DIR / safe
        if not target.exists():
            self.send_response(404)
            self.end_headers()
            return
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        if include_body:
            with target.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/login":
            form = parse_form(self)
            username = form_value(form, "username")
            password = form_value(form, "password")
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.send_header("Set-Cookie", f"session={sign(username)}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
            else:
                self.send_html(login_page("Неверный логин или пароль"), status=401)
            return

        if path in ("/app/lead", "/lead"):
            form = parse_form(self)
            project_id = int(form_value(form, "project_id", "0") or 0)
            name = form_value(form, "name").strip()
            contact = form_value(form, "contact").strip()
            message = form_value(form, "message").strip()
            session_id = form_value(form, "catalog_session_id").strip()[:80]
            tg_user = None
            tg_user_raw = form_value(form, "tg_user_json").strip()
            if tg_user_raw:
                try:
                    tg_user = json.loads(tg_user_raw)
                except json.JSONDecodeError:
                    tg_user = None
            if not contact:
                self.send_html(app_project_page(project_id, "Укажите телефон, WhatsApp или Telegram."), status=400)
                return
            create_web_lead(project_id, name, contact, message, tg_user=tg_user, session_id=session_id)
            base_path = "" if path == "/lead" else "/app"
            self.send_html(app_project_page(project_id, "Спасибо, заявка отправлена. @roi_counter свяжется с вами.", base_path=base_path))
            return

        if path == "/app/event":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode() if length else "{}"
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                payload = {}
            try:
                project_id = int(payload.get("project_id") or 0) or None
            except (TypeError, ValueError):
                project_id = None
            record_catalog_event(
                payload.get("event_type"),
                project_id=project_id,
                tg_user=payload.get("tg_user"),
                payload=payload,
            )
            self.send_response(204)
            self.end_headers()
            return

        if not self.require_auth():
            return

        if path == "/project/create":
            project_id = save_project(parse_form(self))
            self.redirect(f"/project/edit?id={project_id}")
        elif path == "/project/update":
            form = parse_form(self)
            project_id = int(form_value(form, "id"))
            save_project(form, project_id=project_id)
            self.redirect(f"/project/edit?id={project_id}")
        elif path == "/project/archive":
            form = parse_form(self)
            with db() as conn:
                conn.execute("update projects set status='archived', updated_at=? where id=?", (iso_now(), form_value(form, "id")))
            self.redirect("/admin")
        elif path == "/project/delete":
            form = parse_form(self)
            delete_project(form_value(form, "id"))
            self.redirect("/admin")
        elif path == "/project/cover":
            form = parse_form(self)
            project_id = form_value(form, "id")
            cover_media_id = form_value(form, "cover_media_id")
            if project_id and cover_media_id and save_project_cover(project_id, cover_media_id):
                self.redirect(f"/project/edit?id={project_id}")
            else:
                project = get_project(int(project_id or 0))
                self.send_html(project_form(project, "Выберите изображение для обложки") if project else dashboard("Лот не найден"), status=400)
        elif path == "/project/send":
            form = parse_form(self)
            count = send_to_all(int(form_value(form, "id")))
            self.send_html(dashboard(f"Отправлено подписчикам: {count}"))
        elif path == "/lead/status":
            form = parse_form(self)
            status = form_value(form, "status")
            if status in LEAD_STATUS_LABELS:
                with db() as conn:
                    conn.execute("update leads set status = ? where id = ?", (status, form_value(form, "id")))
            self.redirect("/leads")
        elif path == "/lead/delete":
            form = parse_form(self)
            with db() as conn:
                conn.execute("delete from leads where id = ?", (form_value(form, "id"),))
            self.redirect("/leads")
        elif path == "/custom-lead/status":
            form = parse_form(self)
            status = form_value(form, "status")
            if status in LEAD_STATUS_LABELS:
                with db() as conn:
                    conn.execute("update custom_broadcast_leads set status = ? where id = ?", (status, form_value(form, "id")))
            self.redirect("/leads")
        elif path == "/custom-lead/delete":
            form = parse_form(self)
            with db() as conn:
                conn.execute("delete from custom_broadcast_leads where id = ?", (form_value(form, "id"),))
            self.redirect("/leads")
        elif path == "/personal-lead/status":
            form = parse_form(self)
            status = form_value(form, "status")
            if status in LEAD_STATUS_LABELS:
                with db() as conn:
                    conn.execute("update personal_leads set status = ? where id = ?", (status, form_value(form, "id")))
            self.redirect("/leads")
        elif path == "/personal-lead/delete":
            form = parse_form(self)
            with db() as conn:
                conn.execute("delete from personal_leads where id = ?", (form_value(form, "id"),))
            self.redirect("/leads")
        elif path == "/web-lead/status":
            form = parse_form(self)
            status = form_value(form, "status")
            if status in LEAD_STATUS_LABELS:
                with db() as conn:
                    conn.execute("update web_leads set status = ? where id = ?", (status, form_value(form, "id")))
            self.redirect("/leads")
        elif path == "/web-lead/delete":
            form = parse_form(self)
            with db() as conn:
                conn.execute("delete from web_leads where id = ?", (form_value(form, "id"),))
            self.redirect("/leads")
        elif path == "/subscriber/message":
            form = parse_form(self)
            chat_id = form_value(form, "chat_id")
            text = form_value(form, "text").strip()
            if text and chat_id:
                ok = send_admin_message(chat_id, text)
                self.send_html(chats_page(chat_id, "Сообщение отправлено" if ok else "Не удалось отправить сообщение"))
            else:
                self.send_html(chats_page(chat_id, "Введите текст сообщения"), status=400)
        elif path == "/subscriber/segment-message":
            form = parse_form(self)
            segment = form_value(form, "segment", "all")
            text = form_value(form, "text").strip()
            if not text:
                self.send_html(subscribers_page("Введите текст сообщения"), status=400)
                return
            count, total = send_segment_message(segment, text)
            self.send_html(subscribers_page(f"Отправлено: {count} из {total}"))
        elif path == "/subscriber/tag":
            form = parse_form(self)
            chat_id = form_value(form, "chat_id")
            tag = form_value(form, "subscriber_tag")
            if tag in SUBSCRIBER_TAG_LABELS:
                with db() as conn:
                    conn.execute("update subscribers set subscriber_tag = ? where chat_id = ?", (tag or None, chat_id))
            self.redirect("/subscribers")
        elif path == "/crm/note":
            form = parse_form(self)
            chat_id = form_value(form, "chat_id")
            card_id = form_value(form, "card_id")
            text = form_value(form, "text").strip()
            if text and (chat_id or card_id):
                with db() as conn:
                    conn.execute(
                        "insert into crm_notes(chat_id, card_id, text, created_at) values (?, ?, ?, ?)",
                        (chat_id or 0, card_id or None, text, iso_now()),
                    )
                if card_id:
                    self.redirect(f"/crm/card?id={urllib.parse.quote(str(card_id))}")
                else:
                    self.redirect(f"/crm/client?chat_id={urllib.parse.quote(str(chat_id))}")
            else:
                self.send_html(crm_client_page(chat_id, "Введите текст заметки"), status=400)
        elif path == "/crm/reminder":
            form = parse_form(self)
            chat_id = form_value(form, "chat_id")
            card_id = form_value(form, "card_id")
            client_name = form_value(form, "client_name").strip()
            contact_value = form_value(form, "contact_value").strip()
            text = form_value(form, "text").strip()
            remind_at_raw = form_value(form, "remind_at").strip()
            try:
                remind_at = datetime.fromisoformat(remind_at_raw).replace(microsecond=0).isoformat()
            except (TypeError, ValueError):
                remind_at = ""
            if (chat_id or card_id) and text and remind_at:
                with db() as conn:
                    conn.execute(
                        """
                        insert into crm_reminders(chat_id, card_id, client_name, contact_value, text, remind_at, status, created_at)
                        values (?, ?, ?, ?, ?, ?, 'scheduled', ?)
                        """,
                        (chat_id or 0, card_id or None, client_name or None, contact_value or None, text, remind_at, iso_now()),
                    )
                if card_id:
                    self.redirect(f"/crm/card?id={urllib.parse.quote(str(card_id))}")
                else:
                    self.redirect(f"/crm/client?chat_id={urllib.parse.quote(str(chat_id))}")
            else:
                self.send_html(crm_client_page(chat_id, "Заполните текст и дату напоминания"), status=400)
        elif path == "/crm/status/create":
            form = parse_form(self)
            name = form_value(form, "name").strip()
            if name:
                with db() as conn:
                    max_position = conn.execute("select coalesce(max(position), 0) c from crm_statuses").fetchone()["c"]
                    conn.execute(
                        "insert into crm_statuses(name, position, created_at) values (?, ?, ?)",
                        (name, max_position + 1, iso_now()),
                    )
            self.redirect("/crm")
        elif path == "/crm/status/update":
            form = parse_form(self)
            status_id = form_value(form, "id")
            name = form_value(form, "name").strip()
            if status_id and name:
                with db() as conn:
                    conn.execute("update crm_statuses set name = ? where id = ?", (name, status_id))
            self.redirect("/crm")
        elif path == "/crm/status/delete":
            form = parse_form(self)
            status_id = form_value(form, "id")
            if status_id:
                with db() as conn:
                    statuses = conn.execute("select id from crm_statuses order by position, id").fetchall()
                    if len(statuses) > 1:
                        fallback = next((row["id"] for row in statuses if str(row["id"]) != str(status_id)), None)
                        if fallback:
                            conn.execute("update crm_cards set status_id = ?, updated_at = ? where status_id = ?", (fallback, iso_now(), status_id))
                            conn.execute("delete from crm_statuses where id = ?", (status_id,))
            self.redirect("/crm")
        elif path == "/crm/card/create":
            form = parse_form(self)
            status_id = form_value(form, "status_id")
            title = form_value(form, "title").strip()
            chat_id_raw = form_value(form, "chat_id").strip()
            try:
                chat_id = int(chat_id_raw) if chat_id_raw else None
            except ValueError:
                chat_id = None
            if title and status_id:
                now = iso_now()
                with db() as conn:
                    cur = conn.execute(
                        """
                        insert into crm_cards(status_id, chat_id, title, client_name, contact_value, budget, request, source, created_at, updated_at)
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            status_id,
                            chat_id,
                            title,
                            form_value(form, "client_name").strip(),
                            form_value(form, "contact_value").strip(),
                            form_value(form, "budget").strip(),
                            form_value(form, "request").strip(),
                            form_value(form, "source").strip(),
                            now,
                            now,
                        ),
                    )
                self.redirect(f"/crm/card?id={cur.lastrowid}")
            else:
                self.send_html(crm_page(message="Заполните название карточки"), status=400)
        elif path == "/crm/card/update":
            form = parse_form(self)
            card_id = form_value(form, "id")
            chat_id_raw = form_value(form, "chat_id").strip()
            try:
                chat_id = int(chat_id_raw) if chat_id_raw else None
            except ValueError:
                chat_id = None
            with db() as conn:
                conn.execute(
                    """
                    update crm_cards
                    set status_id=?, chat_id=?, title=?, client_name=?, contact_value=?, budget=?, request=?, source=?, updated_at=?
                    where id=?
                    """,
                    (
                        form_value(form, "status_id"),
                        chat_id,
                        form_value(form, "title").strip(),
                        form_value(form, "client_name").strip(),
                        form_value(form, "contact_value").strip(),
                        form_value(form, "budget").strip(),
                        form_value(form, "request").strip(),
                        form_value(form, "source").strip(),
                        iso_now(),
                        card_id,
                    ),
                )
            self.redirect(f"/crm/card?id={urllib.parse.quote(str(card_id))}")
        elif path == "/crm/card/status":
            form = parse_form(self)
            card_id = form_value(form, "id")
            status_id = form_value(form, "status_id")
            with db() as conn:
                conn.execute(
                    "update crm_cards set status_id=?, updated_at=? where id=?",
                    (status_id, iso_now(), card_id),
                )
            self.redirect("/crm")
        elif path == "/crm/card/delete":
            form = parse_form(self)
            card_id = form_value(form, "id")
            if card_id:
                with db() as conn:
                    conn.execute("delete from crm_notes where card_id = ?", (card_id,))
                    conn.execute("delete from crm_reminders where card_id = ?", (card_id,))
                    conn.execute("delete from crm_cards where id = ?", (card_id,))
            self.redirect("/crm")
        elif path == "/broadcast/create":
            form = parse_form(self)
            send_at_raw = form_value(form, "send_at")
            send_at = datetime.fromisoformat(send_at_raw).replace(microsecond=0).isoformat()
            with db() as conn:
                conn.execute(
                    "insert into broadcasts(project_id, send_at, created_at) values (?, ?, ?)",
                    (form_value(form, "project_id"), send_at, iso_now()),
                )
            self.send_html(broadcasts_page("Рассылка запланирована"))
        elif path == "/broadcast/custom":
            form = parse_form(self)
            text = form_value(form, "text").strip()
            if not text:
                self.send_html(broadcasts_page("Введите текст рассылки"), status=400)
                return
            mode = form_value(form, "mode", "send_now")
            send_at_raw = form_value(form, "send_at").strip()
            if mode == "schedule" and not send_at_raw:
                self.send_html(broadcasts_page("Укажите дату и время для запланированной свободной рассылки"), status=400)
                return
            send_at = datetime.fromisoformat(send_at_raw).replace(microsecond=0).isoformat() if send_at_raw else None
            status = "scheduled" if mode == "schedule" else "sending"
            if mode != "schedule":
                send_at = None
            with db() as conn:
                cur = conn.execute(
                    "insert into custom_broadcasts(text, send_at, status, created_at) values (?, ?, ?, ?)",
                    (text, send_at, status, iso_now()),
                )
                broadcast_id = cur.lastrowid
            media_items = save_uploaded_files(form, "media", BROADCAST_UPLOAD_DIR, f"broadcast_{broadcast_id}")
            with db() as conn:
                for item in media_items:
                    conn.execute(
                        """
                        insert into custom_broadcast_media(broadcast_id, file_name, original_name, mime_type, created_at)
                        values (?, ?, ?, ?, ?)
                        """,
                        (broadcast_id, item["file_name"], item["original_name"], item["mime_type"], iso_now()),
                    )
            if send_at:
                self.send_html(broadcasts_page("Свободная рассылка запланирована"))
            else:
                count = send_custom_to_all(broadcast_id, text, media_items)
                with db() as conn:
                    conn.execute(
                        "update custom_broadcasts set status='sent', sent_at=?, sent_count=? where id=?",
                        (iso_now(), count, broadcast_id),
                    )
                self.send_html(broadcasts_page(f"Свободная рассылка отправлена. Получателей: {count}"))
        else:
            self.send_html("Not found", status=404)


def main():
    init_db()
    configure_bot_menu_button()
    stop_event = threading.Event()
    bot_thread = threading.Thread(target=bot_loop, args=(stop_event,), daemon=True)
    scheduler_thread = threading.Thread(target=scheduler_loop, args=(stop_event,), daemon=True)
    bot_thread.start()
    scheduler_thread.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def shutdown(*_):
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"Admin UI: http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
