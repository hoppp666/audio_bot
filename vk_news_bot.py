#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vk-news-bot: RSS-ленты -> фильтр и дедупликация -> перевод -> картинки -> пост в VK.
"""

import argparse
import difflib
import hashlib
import html as html_mod
import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    HAS_TRANSLATOR = True
except Exception:
    HAS_TRANSLATOR = False

try:
    import vk_api
    HAS_VK = True
except Exception:
    HAS_VK = False

BASE_DIR = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IMG_JUNK = ("sprite", "logo", "avatar", "placeholder", "blank.gif", "1x1",
            "tracking", "beacon", ".svg")
MAX_MESSAGE_LEN = 3800
# Маркер "в базу внесены изменения": workflow коммитит seen.sqlite только когда
# бот реально что-то записал (публикация/отметка неудачи). Без него запуск без
# изменений создаёт пустые коммиты и ловит конфликт git push.
DB_DIRTY_MARKER = BASE_DIR / "data" / ".db_changed"

log = logging.getLogger("vk-news-bot")


def setup_logging(workdir: Path) -> None:
    logs = workdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(logs / "bot.log", encoding="utf-8")])


def norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[^a-zа-яё0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:120]


def title_key(title: str) -> str:
    return hashlib.sha1(norm_title(title).encode("utf-8")).hexdigest()


def clean_title(title: str, source_name: str = "") -> str:
    t = html_mod.unescape(title or "").strip()
    t = re.split(r"\s+[|·]\s+", t)[0].strip()
    if source_name:
        t = re.sub(rf"\s+-\s+{re.escape(source_name)}\s*$", "", t,
                   flags=re.IGNORECASE).strip()
    return t


def smart_truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if idx > limit * 0.55:
        return cut[: idx + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def looks_russian(text: str) -> bool:
    if not text:
        return True
    ru = len(re.findall(r"[а-яё]", text.lower()))
    latin = len(re.findall(r"[a-z]", text.lower()))
    return ru > latin


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS posted(
                        url TEXT PRIMARY KEY,
                        title_key TEXT,
                        status TEXT,
                        attempts INTEGER DEFAULT 1,
                        post_id TEXT,
                        created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS titles(
                        title_key TEXT PRIMARY KEY,
                        norm_text TEXT,
                        seen_at TEXT)""")
    try:
        conn.execute("ALTER TABLE titles ADD COLUMN norm_text TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def is_duplicate(conn: sqlite3.Connection, url: str, title: str,
                 fuzzy_threshold: float) -> tuple[bool, str]:
    row = conn.execute("SELECT status, attempts FROM posted WHERE url=?",
                       (url,)).fetchone()
    if row:
        if row[0] == "posted":
            return True, "уже публиковали этот URL"
        if row[1] >= 3:
            return True, "3 неудачные попытки по этому URL"
    key = title_key(title)
    if conn.execute("SELECT 1 FROM titles WHERE title_key=?", (key,)).fetchone():
        return True, "уже публиковали такой заголовок"
    norm = norm_title(title)
    if len(norm) >= 12:
        recent = [r[0] for r in conn.execute(
            "SELECT norm_text FROM titles WHERE norm_text IS NOT NULL "
            "ORDER BY seen_at DESC LIMIT 300") if r[0]]
        for other in recent:
            if difflib.SequenceMatcher(None, norm, other).ratio() > fuzzy_threshold:
                return True, "похожий заголовок уже публиковался"
    return False, ""


def mark_posted(conn: sqlite3.Connection, url: str, title: str,
                post_id: str = "", status: str = "posted") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = title_key(title)
    norm = norm_title(title)
    with conn:
        conn.execute(
            "INSERT INTO posted(url, title_key, status, attempts, post_id, created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
            "status=excluded.status, attempts=attempts+1, post_id=excluded.post_id",
            (url, key, status, 1, post_id, now))
        if status == "posted":
            conn.execute(
                "INSERT OR IGNORE INTO titles(title_key, norm_text, seen_at) VALUES(?,?,?)",
                (key, norm, now))
    try:
        DB_DIRTY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        DB_DIRTY_MARKER.touch()
    except Exception:
        pass


def _looks_like_feed(data: bytes) -> bool:
    head = data[:3000].lower()
    return (b"<rss" in head or b"<feed" in head or b"<rdf" in head
            or b"http://www.w3.org/2005/atom" in head)


def fetch_feed(url: str, session: requests.Session, alt_urls=None):
    last_exc = None
    for u in [url] + [a for a in (alt_urls or []) if a]:
        try:
            resp = session.get(u, timeout=30)
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except Exception as e:
            last_exc = e
            log.debug("прямой запрос не прошёл %s: %s", u, e)
    try:
        out = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", "40", "-A", UA, url],
            capture_output=True, timeout=60)
        if out.returncode == 0 and out.stdout and _looks_like_feed(out.stdout):
            log.info("лента получена через curl-фоллбек: %s", url)
            return feedparser.parse(out.stdout)
    except Exception as e:
        last_exc = last_exc or e
    raise last_exc or RuntimeError(f"не удалось загрузить ленту {url}")


def entry_dt(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def entry_text(entry) -> str:
    raw = ""
    if entry.get("content"):
        raw = entry.content[0].get("value", "")
    if not raw:
        raw = entry.get("summary", "") or ""
    text = BeautifulSoup(raw, "lxml").get_text(" ", strip=True)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)(continue reading|read more|the post .{0,40} appeared first).*", "", text)
    return text.strip()


def pass_filters(entry, src: dict, cfg: dict) -> tuple[bool, str]:
    dt = entry_dt(entry)
    max_age = timedelta(hours=cfg["max_age_hours"])
    if dt and datetime.now(timezone.utc) - dt > max_age:
        return False, "старше лимита возраста"
    title = entry.get("title", "")
    body = entry_text(entry)
    hay = f"{title} {body}".lower()
    for kw in cfg["global_exclude"]:
        if kw.lower() in hay:
            return False, f"глобальный стоп-фильтр: {kw}"
    inc = src.get("include_keywords") or []
    if inc and not any(kw.lower() in hay for kw in inc):
        return False, "не совпал ни один ключ включения"
    for kw in src.get("exclude_keywords", []) or []:
        if kw.lower() in hay:
            return False, f"стоп-ключ: {kw}"
    return True, ""


def collect_image_candidates(entry, article_url: str) -> list[str]:
    urls: list[str] = []

    def add(u):
        if not u or not u.startswith("http"):
            return
        low = u.lower()
        if any(j in low for j in IMG_JUNK):
            return
        if u not in urls:
            urls.append(u)

    for m in getattr(entry, "media_content", None) or []:
        add(m.get("url"))
    for m in getattr(entry, "media_thumbnail", None) or []:
        add(m.get("url"))
    for e in getattr(entry, "enclosures", None) or []:
        href = e.get("href") or ""
        if "image" in (e.get("type") or "") or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            add(href)
    for l in getattr(entry, "links", None) or []:
        if l.get("rel") == "enclosure" and "image" in (l.get("type") or ""):
            add(l.get("href"))
    raw = ""
    if entry.get("content"):
        raw = entry.content[0].get("value", "")
    if not raw:
        raw = entry.get("summary", "") or ""
    for img in BeautifulSoup(raw, "lxml").find_all("img"):
        add(img.get("src") or img.get("data-src"))
    return urls


def get_og_image(url: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(url, timeout=25, headers={"User-Agent": UA})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for prop in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
            tag = (soup.find("meta", property=prop)
                   or soup.find("meta", attrs={"name": prop}))
            if tag and tag.get("content"):
                cand = urljoin(resp.url, tag["content"].strip())
                if cand.startswith("http"):
                    return cand
    except Exception as e:
        log.debug("og:image не получен для %s: %s", url, e)
    return None


def download_image(url: str, session: requests.Session, tmp_dir: Path,
                   min_width: int, index: int) -> Path | None:
    try:
        resp = session.get(url, timeout=30, headers={"User-Agent": UA}, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "image" not in ctype and not url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return None
        data = resp.raw.read(15 * 1024 * 1024, decode_content=True)
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.width < min_width or img.height < min_width * 0.5:
            return None
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.width > 1600:
            img = img.resize((1600, int(img.height * 1600 / img.width)),
                             Image.LANCZOS)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f"img_{index}_{int(time.time())}.jpg"
        img.save(path, "JPEG", quality=88)
        return path
    except Exception as e:
        log.debug("картинка не скачалась %s: %s", url, e)
        return None


def get_post_images(entry, article_url: str, session: requests.Session,
                    tmp_dir: Path, want: int, min_width: int) -> list[tuple[Path, str]]:
    candidates = collect_image_candidates(entry, article_url)
    og = get_og_image(article_url, session) if article_url else None
    if og:
        candidates.append(og)
    paths: list[tuple[Path, str]] = []
    seen_urls: set[str] = set()
    for i, u in enumerate(candidates):
        if len(paths) >= want:
            break
        if u in seen_urls:
            continue
        seen_urls.add(u)
        p = download_image(u, session, tmp_dir, min_width, i)
        if p:
            paths.append((p, u))
    return paths


def translate_text(text: str) -> str:
    if not text or looks_russian(text):
        return text
    if HAS_TRANSLATOR:
        for _ in range(2):
            try:
                out = GoogleTranslator(source="auto", target="ru").translate(text)
                if out:
                    return out
            except Exception:
                time.sleep(3)
        try:
            out = MyMemoryTranslator(source="en-US", target="ru-RU").translate(text)
            if out:
                return out
        except Exception:
            pass
    log.warning("перевод недоступен, оставляю оригинальный текст")
    return text


def llm_rewrite(title: str, body: str) -> tuple[str, str] | None:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    system = ("Ты редактор развлекательного паблика ВКонтакте про моду, звёзд и музыку. "
              "Получишь заголовок и текст зарубежной новости. Перепиши её живым "
              "русским языком: цепляющий заголовок до 70 знаков и 2-3 коротких абзаца "
              "(до 800 знаков суммарно), без воды, без ссылок и без выдумывания фактов. "
              "Формат ответа: первая строка - заголовок, пустая строка, затем текст поста.")
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.7,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user",
                                "content": f"Заголовок: {title}\n\nТекст: {body}"}]},
            timeout=90)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        parts = content.split("\n", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    except Exception as e:
        log.warning("LLM-редактура не удалась (%s), использую обычный перевод", e)
    return None


def build_message(title: str, body: str, rubric: dict, source_name: str,
                  hashtags: list[str], cfg: dict) -> str:
    title_clean = re.sub(r"https?://\S+", "", title).strip()
    body_clean = re.sub(r"https?://\S+", "", body).strip()

    lines = [f"{rubric['emoji']} {title_clean}", "", body_clean]
    if hashtags:
        short = cfg.get("group_shortname", "")
        tags = " ".join(f"#{t}@{short}" if short else f"#{t}" for t in hashtags)
        lines += ["", tags]
    if cfg.get("credit_source"):
        lines += ["", f"Фото: {source_name}"]
    msg = "\n".join(lines).strip()
    if len(msg) > MAX_MESSAGE_LEN:
        msg = smart_truncate(msg, MAX_MESSAGE_LEN)
    return msg


def _vk_err_code(e) -> int | None:
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code
    m = re.match(r"\[(\d+)\]", str(e))
    return int(m.group(1)) if m else None


VK_RATE_CODES = {6, 9, 10}


def vk_call(fn, what: str = "", tries: int = 2, base_delay: float = 20.0):
    for attempt in range(1, tries + 1):
        try:
            time.sleep(1.0)
            return fn()
        except Exception as e:
            code = _vk_err_code(e)
            if code in VK_RATE_CODES and attempt < tries:
                delay = base_delay * attempt
                log.warning("VK лимит (%s) на %s - пауза %d сек, попытка %d/%d",
                            code or e, what or "метод", delay, attempt, tries)
                time.sleep(delay)
                continue
            raise


def _last_post_ts_local(conn: sqlite3.Connection) -> int:
    """Время последнего успешного поста по локальной базе (unix-время)."""
    try:
        row = conn.execute(
            "SELECT created_at FROM posted WHERE status='posted' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return 0
    if not row or not row[0]:
        return 0
    try:
        dt = datetime.fromisoformat(row[0])
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _interval_ok(latest_post_ts: int,
                 min_interval_minutes: int) -> tuple[bool, str]:
    if min_interval_minutes > 0 and latest_post_ts > 0:
        elapsed_min = (int(time.time()) - latest_post_ts) / 60.0
        if elapsed_min < min_interval_minutes:
            wait_left = int(min_interval_minutes - elapsed_min)
            return False, (f"с прошлого поста прошло {int(elapsed_min)} мин "
                           f"(минимум {min_interval_minutes} мин). Ждём ещё ~{wait_left} мин.")
    return True, ""


def check_wall_and_interval(conn: sqlite3.Connection, min_interval_minutes: int) -> tuple[bool, str]:
    token = os.getenv("VK_TOKEN", "").strip()
    gid = os.getenv("GROUP_ID", "").strip()
    if not token or not gid or not HAS_VK:
        return True, ""

    vk = vk_api.VkApi(token=token, api_version="5.199")
    api = vk.get_api()

    try:
        # tries=3, паузы 30 и 60 сек: кратковременный flood control (код 9)
        # обычно спадает за минуту-две, раньше паузы в 15 сек не хватало
        resp = vk_call(lambda: api.wall.get(owner_id=-int(gid), count=50,
                                            filter="owner"),
                       what="wall.get", tries=3, base_delay=30)
        items = resp.get("items", [])
    except Exception as e:
        code = _vk_err_code(e)
        if code in VK_RATE_CODES:
            # Раньше здесь пропускался весь запуск, но flood control может
            # держаться часами и бот вообще переставал публиковать. Теперь
            # пропускаем только синхронизацию истории и проверяем интервал
            # по локальной базе (seen.sqlite хранится в репозитории).
            log.warning("VK ограничил wall.get (код %s) после 3 попыток - "
                        "синхронизацию истории пропускаю, интервал проверяю "
                        "по локальной базе", code)
            return _interval_ok(_last_post_ts_local(conn), min_interval_minutes)
        if "27" in str(e)[:8] or "group auth" in str(e):
            log.info("у токена сообщества история стены недоступна - работаю по локальной базе")
            return _interval_ok(_last_post_ts_local(conn), min_interval_minutes)
        log.warning("история стены недоступна (%s), работаю с локальной базой", e)
        return _interval_ok(_last_post_ts_local(conn), min_interval_minutes)

    now_ts = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    latest_post_ts = 0

    with conn:
        for it in items:
            post_ts = int(it.get("date") or 0)
            if post_ts > latest_post_ts:
                latest_post_ts = post_ts

            text = (it.get("text") or "").strip()
            first = next((l.strip() for l in text.splitlines() if l.strip()), "")
            title = re.sub(r"^[^\wа-яёА-ЯЁ]+", "", first, flags=re.UNICODE).strip()
            norm = norm_title(title)
            key = title_key(title) if title else ""
            if key and len(norm) >= 12:
                conn.execute(
                    "INSERT OR IGNORE INTO titles(title_key, norm_text, seen_at) VALUES(?,?,?)",
                    (key, norm, now_iso))
                n += 1

    log.info("история стены: учтено %d заголовков при дедупликации", n)

    if not items:  # стена пуста - проверяем интервал по локальной базе
        latest_post_ts = _last_post_ts_local(conn)

    return _interval_ok(latest_post_ts, min_interval_minutes)


def upload_wall_photos(vk, image_paths: list[Path], gid: int) -> list[str]:
    api = vk.get_api()
    server = vk_call(lambda: api.photos.getWallUploadServer(group_id=gid),
                     what="photos.getWallUploadServer")
    files: list[tuple] = []
    handles = []
    for i, p in enumerate(image_paths, 1):
        fh = open(p, "rb")
        handles.append(fh)
        files.append((f"photo{i}", (p.name, fh, "image/jpeg")))
    try:
        resp = requests.post(server["upload_url"], files=files, timeout=120)
        resp.raise_for_status()
        up = resp.json()
    finally:
        for fh in handles:
            fh.close()
    saved = vk_call(lambda: api.photos.saveWallPhoto(
                        group_id=gid, server=up["server"],
                        photo=up["photo"], hash=up["hash"]),
                    what="photos.saveWallPhoto")
    return [f"photo{ph['owner_id']}_{ph['id']}" for ph in saved]


def vk_publish(message: str, images: list[tuple[Path, str]]) -> int:
    token = os.environ["VK_TOKEN"]
    gid = int(os.environ["GROUP_ID"])
    vk = vk_api.VkApi(token=token, api_version="5.199")
    api = vk.get_api()
    attachments: list[str] = []
    image_paths = [p for p, _ in images]
    if image_paths:
        photos_blocked = False
        try:
            attachments += upload_wall_photos(vk, image_paths, gid)
        except Exception as e:
            code = _vk_err_code(e)
            estr = str(e)
            if code == 27 or "group auth" in estr:
                # Токен сообщества: VK не даёт загружать фото через API
                # (ошибка 27, известное ограничение с 2026). Публикуем
                # пост текстом без картинок, чтобы новость не терялась.
                photos_blocked = True
                log.warning("загрузка фото недоступна с этим токеном (код 27) - "
                            "публикую текстом без картинок")
            else:
                raise
        finally:
            for p in image_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            if not photos_blocked:
                time.sleep(4)
        if photos_blocked:
            # строки "Фото: Источник" в посте без картинок быть не должно
            message = re.sub(r"\s*Фото: .*$", "", message)
    params = {"owner_id": -gid, "from_group": 1, "message": message,
              "attachments": ",".join(attachments)}
    resp = vk_call(lambda: api.wall.post(**params), what="wall.post", tries=2, base_delay=20)
    return resp["post_id"]


def gather_candidates(cfg: dict, conn: sqlite3.Connection,
                      session: requests.Session, limit_per_feed: int) -> list[dict]:
    candidates: list[dict] = []
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        name = src.get("name", src["url"])
        try:
            feed = fetch_feed(src["url"], session, src.get("alt_urls"))
        except Exception as e:
            log.warning("[%s] лента недоступна: %s", name, e)
            continue
        taken = 0
        for entry in feed.entries:
            if taken >= limit_per_feed:
                break
            url = entry.get("link", "")
            title = clean_title(entry.get("title", ""), name)
            if not url or not title:
                continue
            ok, why = pass_filters(entry, src, cfg)
            if not ok:
                log.info("[%s] пропущено (%s): %s", name, why, title[:70])
                continue
            dup, why = is_duplicate(conn, url, title,
                                    cfg["fuzzy_dedup_threshold"])
            if dup:
                log.info("[%s] дубликат (%s): %s", name, why, title[:70])
                continue
            dt = entry_dt(entry)
            candidates.append({
                "source": name, "rubric": src.get("rubric", "fashion"),
                "url": url, "title": title,
                "body": smart_truncate(entry_text(entry), 900),
                "dt": dt,
                "hashtags": src.get("hashtags", None),
                "entry": entry,
            })
            taken += 1
    candidates.sort(key=lambda c: c["dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
                    reverse=True)
    return candidates


def pick_diverse(candidates: list[dict], n: int) -> list[dict]:
    picked: list[dict] = []
    seen_src: set[str] = set()
    for c in candidates:
        if len(picked) >= n:
            break
        if c["source"] in seen_src:
            continue
        picked.append(c)
        seen_src.add(c["source"])
    for c in candidates:
        if len(picked) >= n:
            break
        if c not in picked:
            picked.append(c)
    return picked


def process_candidate(item: dict, cfg: dict, session: requests.Session,
                      tmp_dir: Path) -> tuple[str, list[Path]] | None:
    rubric = cfg["rubrics"].get(item["rubric"], cfg["rubrics"]["fashion"])
    title_ru = translate_text(item["title"])
    body_ru = translate_text(item["body"])

    rewritten = llm_rewrite(title_ru, body_ru)
    if rewritten:
        title_ru, body_ru = rewritten
        log.info("текст отредактирован через LLM")
    else:
        body_ru = smart_truncate(body_ru, 800)

    images = []
    if cfg.get("images_per_post", 0) > 0:
        images = get_post_images(item["entry"], item["url"], session, tmp_dir,
                                 cfg["images_per_post"], cfg["min_image_width"])
    if cfg.get("require_image") and not images:
        log.warning("нет подходящих картинок для: %s", item["title"][:70])
        return None
    item["image_urls"] = [u for _, u in images]

    hashtags = item["hashtags"] if item["hashtags"] is not None         else rubric.get("hashtags", [])
    msg = build_message(title_ru, body_ru, rubric, item["source"],
                        hashtags, cfg)
    return msg, images


def save_draft(drafts_dir: Path, item: dict, msg: str) -> Path:
    drafts_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zа-я0-9]+", "-", item["title"].lower())[:40].strip("-")
    path = drafts_dir / f"{stamp}-{slug}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Источник: {item['source']}\n\n")
        f.write("----- ТЕКСТ ПОСТА -----\n\n")
        f.write(msg + "\n\n")
        f.write("----- КАРТИНКИ -----\n")
        for u in item.get("image_urls", []) or []:
            f.write(f"- {u}\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="RSS -> VK автопостинг")
    parser.add_argument("--config", default=str(BASE_DIR / "sources.yaml"))
    parser.add_argument("--post", action="store_true",
                        help="реально публиковать (иначе только черновики)")
    parser.add_argument("--max", type=int, default=None,
                        help="сколько постов за запуск (по умолчанию из sources.yaml)")
    parser.add_argument("--limit", type=int, default=12,
                        help="сколько свежих записей просматривать в каждой ленте")
    parser.add_argument("--reset-db", action="store_true",
                        help="забыть всё опубликованное и начать с чистого листа")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for k, v in (cfg.get("settings") or {}).items():
        cfg.setdefault(k, v)
    if args.max:
        cfg["settings"]["posts_per_run"] = args.max
    settings = cfg["settings"]

    setup_logging(BASE_DIR)
    log.info("=== старт, режим: %s ===",
             "ПУБЛИКАЦИЯ" if args.post else "черновики (dry-run)")
    shutil.rmtree(BASE_DIR / "data" / "tmp_images", ignore_errors=True)

    conn = init_db(BASE_DIR / "data" / "seen.sqlite")
    if args.reset_db:
        conn.execute("DELETE FROM posted")
        conn.execute("DELETE FROM titles")
        conn.commit()
        log.info("база дедупликации очищена")

    if args.post:
        if not os.getenv("VK_TOKEN") or not os.getenv("GROUP_ID"):
            log.error("не заданы VK_TOKEN и GROUP_ID (см. .env и README.md)")
            return 2
        min_interval = int(settings.get("min_interval_minutes", 115))
        can_post, reason = check_wall_and_interval(conn, min_interval)
        if not can_post:
            log.info("Пропуск запуска: %s", reason)
            # код 3 = "пропуск": в workflow по нему не коммитим seen.sqlite
            return 3

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    candidates = gather_candidates(cfg, conn, session, args.limit)
    log.info("кандидатов после фильтров: %d", len(candidates))

    to_post = pick_diverse(candidates, settings["posts_per_run"])
    if not to_post:
        log.info("новых новостей под публикацию нет")
        return 0

    tmp_dir = BASE_DIR / "data" / "tmp_images"
    published = 0
    failed = 0
    rate_limited = False
    for i, item in enumerate(to_post):
        log.info("готовим: %s (%s)", item["title"][:70], item["source"])
        try:
            prepared = process_candidate(item, cfg, session, tmp_dir)
        except Exception as e:
            log.error("ошибка подготовки: %s", e)
            prepared = None
        if not prepared:
            if args.post:
                mark_posted(conn, item["url"], item["title"], status="failed")
            failed += 1
            continue
        msg, images = prepared

        if args.post:
            try:
                post_id = vk_publish(msg, images)
                mark_posted(conn, item["url"], item["title"], post_id=str(post_id))
                log.info("опубликовано: post_id=%s", post_id)
                published += 1
            except Exception as e:
                log.error("ошибка публикации: %s", e)
                estr = str(e)
                code = _vk_err_code(e)
                hint = {
                    5: "токен недействителен или отозван - пересоздайте ключ",
                    15: "у токена нет права 'Стена' - пересоздайте ключ с правом Стена",
                    18: "стена группы закрыта - включите её в настройках группы",
                    27: "токен сообщества: фото бот публикует текстом; ошибка 27 здесь = не прошла и текстовая публикация, проверьте право 'Управление' у ключа",
                    100: "проверьте GROUP_ID (число без минуса)",
                }.get(code)
                if hint:
                    log.error("подсказка (код %s): %s", code, hint)
                if "27" in estr[:8] or "group auth" in estr:
                    log.error("токен сообщества: текстовая публикация тоже не прошла - "
                              "проверьте, что ключу выдано право 'Управление сообществом'")
                    mark_posted(conn, item["url"], item["title"], status="failed")
                elif code in VK_RATE_CODES:
                    log.error("VK включил антифлуд (код %s). Останавливаю работу до следующего запуска.", code)
                    rate_limited = True
                    failed += 1
                    break
                else:
                    mark_posted(conn, item["url"], item["title"], status="failed")
                failed += 1
        else:
            path = save_draft(BASE_DIR / "drafts", item, msg)
            log.info("черновик сохранён: %s", path.name)
            published += 1

        if args.post and i < len(to_post) - 1:
            time.sleep(20)

    log.info("=== готово: опубликовано %d, ошибок %d (в обработке было %d) ===",
             published, failed, len(to_post))
    if rate_limited and published == 0:
        # Ничего не опубликовано из-за антифлуда VK, база не менялась:
        # код 3 = "пропуск запуска" (workflow вернёт зелёный статус без коммита)
        return 3
    return 1 if (failed > 0 and published == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
