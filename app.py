#!/usr/bin/env python3
import cgi
import hashlib
import hmac
import html
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
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "4"))

SEND_WINDOW_START = dt_time(9, 0)
SEND_WINDOW_END = dt_time(21, 0)
LEAD_STATUSES = [
    ("new", "Новая"),
    ("contacted", "Связались"),
    ("qualified", "Квалифицирована"),
    ("viewing_scheduled", "Показ назначен"),
    ("offer", "Оффер / переговоры"),
    ("closed", "Сделка закрыта"),
    ("lost", "Неактуальна"),
]
LEAD_STATUS_LABELS = dict(LEAD_STATUSES)


def now_local():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)


def iso_now():
    return now_local().replace(microsecond=0).isoformat()


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
              subscribed_at text not null,
              last_seen_at text,
              last_daily_sent_at text
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
            """
        )
        ensure_column(conn, "projects", "tg_media_file_id", "text")
        ensure_column(conn, "projects", "tg_media_kind", "text")
        ensure_column(conn, "projects", "tg_media_signature", "text")
        ensure_column(conn, "projects", "source_from", "text")
        ensure_column(conn, "subscribers", "language", "text")
        ensure_column(conn, "custom_broadcasts", "send_at", "text")
        ensure_column(conn, "custom_broadcast_leads", "status", "text not null default 'new'")


def ensure_column(conn, table_name, column_name, column_type):
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table_name})").fetchall()}
    if column_name not in columns:
        conn.execute(f"alter table {table_name} add column {column_name} {column_type}")


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
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode())
    except Exception:
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
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as err:
        try:
            print(err.read().decode(), file=sys.stderr)
        except Exception:
            pass
        traceback.print_exc()
        return None
    except Exception:
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


def send_message(chat_id, text, keyboard=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if keyboard:
        payload["reply_markup"] = keyboard
    return telegram_api("sendMessage", payload)


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
            return result
    return send_message(chat_id, caption, keyboard=keyboard)


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


def next_project_for(chat_id, exclude_id=None):
    with db() as conn:
        params = [chat_id]
        extra = ""
        if exclude_id:
            extra = "and p.id != ?"
            params.append(exclude_id)
        return conn.execute(
            f"""
            select p.* from projects p
            left join seen_projects s on s.project_id = p.id and s.chat_id = ?
            where p.status = 'active' and s.project_id is null {extra}
            order by p.created_at asc, p.id asc
            limit 1
            """,
            params,
        ).fetchone()


def send_project(chat_id, project):
    if not project:
        send_no_projects_message(chat_id)
        return False
    caption = project_caption(project)
    media = get_project_media(project["id"])
    send_project_card(chat_id, project, media, caption, project_actions_keyboard(project["id"]))
    mark_seen(chat_id, project["id"])
    return True


def project_actions_keyboard(project_id):
    return inline_keyboard(
        [
            [{"text": "💬 Хочу узнать подробнее", "callback_data": f"interest:{project_id}"}],
            [{"text": "👀 Смотреть еще", "callback_data": f"next:{project_id}"}],
        ]
    )


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


def send_project_actions(chat_id, project_id):
    send_message(chat_id, ".", keyboard=project_actions_keyboard(project_id), parse_mode=None)


def notify_admin(text):
    for chat_id in [item.strip() for item in ADMIN_CHAT_ID.split(",") if item.strip()]:
        send_message(chat_id, text)


def upsert_subscriber(user, chat_id):
    with db() as conn:
        conn.execute(
            """
            insert into subscribers(chat_id, username, first_name, last_name, subscribed_at, last_seen_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(chat_id) do update set
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name,
              last_seen_at=excluded.last_seen_at
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


def lead_name(user):
    parts = [user.get("first_name"), user.get("last_name")]
    return " ".join([p for p in parts if p]) or user.get("username") or str(user.get("id"))


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
    return lead_id


def handle_start(chat_id, user):
    upsert_subscriber(user, chat_id)
    send_project(chat_id, first_active_project())


def handle_text(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    text = message.get("text", "")
    contact = message.get("contact")
    upsert_subscriber(user, chat_id)
    if text == "/start":
        handle_start(chat_id, user)
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
        send_message(chat_id, "Спасибо, получили контакт. @roi_counter свяжется с вами.", keyboard=json.dumps({"remove_keyboard": True}))
        return

    if sub["state"] == "awaiting_personal_whatsapp":
        create_personal_lead(chat_id, user, method="whatsapp", value=text)
        with db() as conn:
            conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
        send_message(chat_id, "Спасибо, получили WhatsApp. @roi_counter свяжется с вами.", keyboard=json.dumps({"remove_keyboard": True}))
        return

    if not sub["state_project_id"]:
        send_message(chat_id, "Нажмите «Смотреть объекты», чтобы получить актуальный лот.")
        return

    if sub["state"] == "awaiting_whatsapp":
        create_lead(project_id, chat_id, user, method="whatsapp", value=text)
        with db() as conn:
            conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
        send_message(chat_id, "Спасибо, получили WhatsApp. Менеджер свяжется с вами.", keyboard=json.dumps({"remove_keyboard": True}))
        return

    if sub["state"] == "awaiting_custom_whatsapp":
        create_custom_broadcast_lead(project_id, chat_id, user, method="whatsapp", value=text)
        with db() as conn:
            conn.execute("update subscribers set state=null, state_project_id=null where chat_id = ?", (chat_id,))
        send_message(chat_id, "Спасибо, получили WhatsApp. Менеджер свяжется с вами.", keyboard=json.dumps({"remove_keyboard": True}))
        return

    send_message(chat_id, "Получил сообщение. Если хотите оставить заявку, выберите способ связи под объектом.")


def handle_callback(callback):
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
        send_project(chat_id, next_project_for(chat_id))
        return

    if data == "personal_interest":
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
        create_personal_lead(chat_id, user, method="telegram", value=f"@{user.get('username')}" if user.get("username") else str(chat_id))
        send_message(chat_id, "Спасибо. @roi_counter напишет вам в Telegram.")
        return

    if data == "personal_phone":
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_personal_phone', state_project_id = null where chat_id = ?",
                (chat_id,),
            )
        keyboard = reply_keyboard([[{"text": "📞 Отправить телефон", "request_contact": True}]])
        send_message(chat_id, "Нажмите кнопку ниже, чтобы поделиться номером телефона.", keyboard=keyboard)
        return

    if data == "personal_whatsapp":
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_personal_whatsapp', state_project_id = null where chat_id = ?",
                (chat_id,),
            )
        send_message(chat_id, "Напишите номер WhatsApp одним сообщением.")
        return

    action, _, raw_project_id = data.partition(":")
    if action == "lang" and raw_project_id in ("ru", "en"):
        with db() as conn:
            conn.execute("update subscribers set language = ? where chat_id = ?", (raw_project_id, chat_id))
        send_project(chat_id, first_active_project())
        return

    try:
        project_id = int(raw_project_id)
    except ValueError:
        project_id = None

    if action == "next":
        send_project(chat_id, next_project_for(chat_id, exclude_id=project_id))
    elif action == "custom_interest" and project_id:
        keyboard = inline_keyboard(
            [
                [{"text": "✈️ Написать в Telegram", "callback_data": f"custom_tg:{project_id}"}],
                [{"text": "📞 Оставить телефон", "callback_data": f"custom_phone:{project_id}"}],
                [{"text": "🟢 Оставить WhatsApp", "callback_data": f"custom_whatsapp:{project_id}"}],
            ]
        )
        send_message(chat_id, "Как вам удобнее, чтобы менеджер связался с вами?", keyboard=keyboard)
    elif action == "custom_tg" and project_id:
        create_custom_broadcast_lead(project_id, chat_id, user, method="telegram", value=f"@{user.get('username')}" if user.get("username") else str(chat_id))
        send_message(chat_id, "Спасибо. Менеджер напишет вам в Telegram.")
    elif action == "custom_phone" and project_id:
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_custom_phone', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        keyboard = reply_keyboard([[{"text": "📞 Отправить телефон", "request_contact": True}]])
        send_message(chat_id, "Нажмите кнопку ниже, чтобы поделиться номером телефона.", keyboard=keyboard)
    elif action == "custom_whatsapp" and project_id:
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_custom_whatsapp', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        send_message(chat_id, "Напишите номер WhatsApp одним сообщением.")
    elif action in ("interest", "contact") and project_id:
        keyboard = inline_keyboard(
            [
                [{"text": "✈️ Написать в Telegram", "callback_data": f"contact_tg:{project_id}"}],
                [{"text": "📞 Оставить телефон", "callback_data": f"contact_phone:{project_id}"}],
                [{"text": "🟢 Оставить WhatsApp", "callback_data": f"contact_whatsapp:{project_id}"}],
            ]
        )
        send_message(chat_id, "Как вам удобнее, чтобы менеджер связался с вами?", keyboard=keyboard)
    elif action == "contact_tg" and project_id:
        create_lead(project_id, chat_id, user, method="telegram", value=f"@{user.get('username')}" if user.get("username") else str(chat_id))
        send_message(chat_id, "Спасибо. Менеджер напишет вам в Telegram.")
    elif action == "contact_phone" and project_id:
        with db() as conn:
            conn.execute(
                "update subscribers set state = 'awaiting_phone', state_project_id = ? where chat_id = ?",
                (project_id, chat_id),
            )
        keyboard = reply_keyboard([[{"text": "Отправить телефон", "request_contact": True}]])
        send_message(chat_id, "Нажмите кнопку ниже, чтобы поделиться номером телефона.", keyboard=keyboard)
    elif action == "contact_whatsapp" and project_id:
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
    project = get_project(project_id)
    if not project:
        return 0
    count = 0
    with db() as conn:
        subscribers = conn.execute("select chat_id from subscribers").fetchall()
    for sub in subscribers:
        if send_project(sub["chat_id"], project):
            count += 1
            time.sleep(0.05)
    return count


def custom_broadcast_keyboard(broadcast_id):
    return inline_keyboard(
        [[{"text": "💬 Хочу узнать подробнее", "callback_data": f"custom_interest:{broadcast_id}"}]]
    )


def send_custom_to_all(broadcast_id, text, media_items):
    count = 0
    safe_text = html.escape(text)
    with db() as conn:
        subscribers = conn.execute("select chat_id from subscribers").fetchall()
    for sub in subscribers:
        chat_id = sub["chat_id"]
        sent = False
        keyboard = custom_broadcast_keyboard(broadcast_id)
        if media_items:
            caption = safe_text if len(safe_text) <= 1024 else None
            if len(safe_text) > 1024:
                sent = bool(send_message(chat_id, safe_text, keyboard=keyboard))
            if len(media_items) == 1:
                if caption:
                    sent = bool(
                        telegram_api_multipart(
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
                    ) or sent
                else:
                    sent = bool(send_media_path(chat_id, media_items[0]["path"], media_items[0]["mime_type"])) or sent
            else:
                sent = bool(send_media_album_paths(chat_id, media_items, caption=caption)) or sent
                if caption:
                    send_message(chat_id, "Подробнее:", keyboard=keyboard, parse_mode=None)
        else:
            sent = bool(send_message(chat_id, safe_text, keyboard=keyboard))
        if sent:
            count += 1
        time.sleep(0.05)
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


def in_send_window(moment):
    t = moment.time()
    return SEND_WINDOW_START <= t <= SEND_WINDOW_END


def scheduler_loop(stop_event):
    while not stop_event.is_set():
        try:
            moment = now_local()
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
                    subscribers = conn.execute("select * from subscribers").fetchall()
                today = moment.date().isoformat()
                for sub in subscribers:
                    if sub["last_daily_sent_at"] and sub["last_daily_sent_at"].startswith(today):
                        continue
                    project = next_project_for(sub["chat_id"])
                    if project and send_project(sub["chat_id"], project):
                        with db() as conn:
                            conn.execute(
                                "update subscribers set last_daily_sent_at = ? where chat_id = ?",
                                (iso_now(), sub["chat_id"]),
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


def layout(title, content, active="projects", message=""):
    nav = [
        ("projects", "/", "Объекты"),
        ("new", "/project/new", "Добавить"),
        ("leads", "/leads", "Заявки"),
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
th,td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-size:12px; font-weight:700; background:#fafbf9; }
tr:last-child td { border-bottom:0; }
.status { display:inline-flex; align-items:center; padding:3px 8px; border-radius:999px; background:#e8f1ed; color:var(--accent); font-size:12px; font-weight:700; }
.status.archived { background:#eee; color:#777; }
.actions { display:flex; gap:8px; flex-wrap:wrap; }
button, .button { border:0; background:var(--accent); color:#fff; padding:9px 12px; border-radius:7px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-block; }
button.secondary, .button.secondary { background:#eef2ef; color:var(--text); border:1px solid var(--line); }
button.danger { background:var(--danger); }
form.inline { display:inline; }
.form-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }
label { display:grid; gap:6px; color:var(--muted); font-weight:700; font-size:12px; }
input, select, textarea { width:100%; border:1px solid var(--line); border-radius:7px; padding:10px 11px; background:#fff; color:var(--text); font:inherit; }
textarea { min-height:110px; resize:vertical; }
.wide { grid-column:1 / -1; }
.notice { background:#fff7e8; border:1px solid #ead6b5; color:#614111; padding:12px 14px; border-radius:8px; margin-bottom:16px; }
.login { min-height:100vh; display:grid; grid-template-columns:1fr; place-items:center; background:var(--bg); }
.login form { width:min(420px,calc(100vw - 32px)); background:#fff; border:1px solid var(--line); border-radius:8px; padding:26px; display:grid; gap:14px; }
.login h1 { font-size:24px; }
.muted { color:var(--muted); }
@media (max-width:900px) { body { grid-template-columns:1fr; } aside { position:static; } .grid,.form-grid { grid-template-columns:1fr; } main { padding:22px 16px 44px; } }
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
    <table>
      <thead><tr><th>Объект</th><th>Комнаты</th><th>Цена</th><th>Статус</th><th>Действия</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="5" class="muted">Пока нет объектов.</td></tr>'}</tbody>
    </table>
    """
    return layout("Объекты", content, "projects", message)


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
        content += "<div class='panel'><h2>Медиа</h2>" + (
            "".join(f"<p>{escape(m['original_name'])}</p>" for m in media) or "<p class='muted'>Файлов пока нет.</p>"
        ) + "</div>"
    return layout("Редактировать объект" if project else "Добавить объект", content, "new", message)


def selected(current, value):
    return "selected" if str(current or "") == value else ""


def lead_status_select(current):
    return "".join(
        f'<option value="{value}" {selected(current, value)}>{label}</option>'
        for value, label in LEAD_STATUSES
    )


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
    empty = '<tr><td colspan="6" class="muted">Заявок пока нет.</td></tr>'
    return layout(
        "Заявки",
        f"""
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


def subscribers_page():
    with db() as conn:
        rows_data = conn.execute("select * from subscribers order by subscribed_at desc").fetchall()
    rows = "".join(
        f"<tr><td>{escape(s['chat_id'])}</td><td>{escape(s['first_name'])} {escape(s['last_name'])}<br><span class='muted'>@{escape(s['username'])}</span></td><td>{escape(s['subscribed_at'])}</td><td>{escape(s['last_daily_sent_at'])}</td></tr>"
        for s in rows_data
    )
    empty = '<tr><td colspan="4" class="muted">Подписчиков пока нет.</td></tr>'
    return layout(
        "Подписчики",
        f"<table><thead><tr><th>Chat ID</th><th>Имя</th><th>Подписался</th><th>Последняя дневная отправка</th></tr></thead><tbody>{rows or empty}</tbody></table>",
        "subscribers",
    )


def login_page(message=""):
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход</title><style>{CSS}</style></head>
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
                conn.execute(
                    "insert into media(project_id, file_name, original_name, mime_type, created_at) values (?, ?, ?, ?, ?)",
                    (new_id, safe_name, item.filename, mime, iso_now()),
                )
    return new_id


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
        elif path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers()
        elif path.startswith("/media/"):
            self.serve_media(path.removeprefix("/media/"), include_body=True)
        elif not self.require_auth():
            return
        elif path == "/":
            self.send_html(dashboard())
        elif path == "/project/new":
            self.send_html(project_form())
        elif path == "/project/edit":
            project = get_project(int(query.get("id", ["0"])[0]))
            self.send_html(project_form(project) if project else dashboard("Объект не найден"))
        elif path == "/leads":
            self.send_html(leads_page())
        elif path == "/broadcasts":
            self.send_html(broadcasts_page())
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
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"session={sign(username)}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
            else:
                self.send_html(login_page("Неверный логин или пароль"), status=401)
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
            self.redirect("/")
        elif path == "/project/delete":
            form = parse_form(self)
            delete_project(form_value(form, "id"))
            self.redirect("/")
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
