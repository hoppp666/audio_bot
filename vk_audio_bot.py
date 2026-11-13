#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vk_audio_bot.py — автодобавление трендовых треков в музыку сообщества VK.

Как работает за один запуск:
  1. Находит треки-кандидаты:
     - официальный чарт «Популярное» (audio.getPopular);
     - либо поиск по запросам из audio.yaml (audio.search, по популярности);
     - если официальный audio API закрыт (ошибка 15/20) — веб-парсер из
       библиотеки vk_api (vk_api.audio.VkAudio), который работает без него.
  2. Отбирает: длительность в заданных пределах, трека ещё нет в музыке
     сообщества и он не добавлялся раньше (база data/audio.sqlite).
  3. Копирует трек в музыку сообщества (audio.add с group_id).
  4. Переименовывает по шаблону: «Название (Имя группы)» (audio.edit).

Требования:
  - ПОЛЬЗОВАТЕЛЬСКИЙ токен (ключ сообщества не подходит) с правом "audio":
    в ссылке авторизации scope=photos,wall,offline,audio;
  - у сообщества включён раздел «Музыка» (Управление -> Разделы).

Важно: с конца 2016 года VK закрывает audio API для сторонних приложений.
Если методы audio.* отвечают ошибкой 15/20 — доступ выдаётся по заявке
в поддержку VK (devsupport@corp.vk.com). Быстрая проверка:
  python vk_audio_bot.py --limit 3        (dry-run, ничего не добавляет)

Коды выхода: 0 - ок; 1 - ошибка; 2 - не хватает настроек; 3 - пропуск запуска.
"""

import argparse
import logging
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

try:
    import vk_api
    HAS_VK = True
except Exception:
    HAS_VK = False

BASE_DIR = Path(__file__).resolve().parent
# Маркер "в базу внесены изменения": workflow коммитит audio.sqlite только
# когда бот реально добавил треки (без пустых коммитов).
DB_DIRTY_MARKER = BASE_DIR / "data" / ".audio_db_changed"
# «Сутки» считаем по московскому времени.
MSK = timezone(timedelta(hours=3))
VK_RATE_CODES = {6, 9, 10}
VK_ACCESS_CODES = {15, 20}

log = logging.getLogger("vk-audio-bot")

DEFAULT_CONFIG = {
    "enabled": True,
    "per_run": 12,              # сколько треков добавлять за запуск
    "daily_limit": 50,          # максимум треков за сутки (по МСК)
    "min_duration": 60,         # короче = рингтоны, отбрасываем
    "max_duration": 600,        # дольше 10 минут - не трековый формат
    "use_popular": True,        # сначала чарт «Популярное» (audio.getPopular)
    "rename_template": "{title} ({group})",
    "group_name": "",           # пусто = получить автоматически из VK
    # Запросы для поиска трендового: каждый запуск берёт их в случайном
    # порядке, поэтому за день набирается разнообразие. Обновляйте список
    # время от времени - это ваши "тренды".
    "queries": [
        "новинки 2026", "хиты чарта", "top hits", "pop chart",
        "новая музыка", "dance hits", "rock hits", "hip hop 2026",
        "русские хиты", "remix 2026", "popular songs", "mood music",
    ],
}


def setup_logging(workdir: Path) -> None:
    logs = workdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(logs / "audio.log",
                                                      encoding="utf-8")])


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for k, v in data.items():
            if v is not None:
                cfg[k] = v
    return cfg


def _vk_err_code(e) -> int | None:
    code = getattr(e, "code", None)
    if isinstance(code, int):
        return code
    m = re.match(r"\[(\d+)\]", str(e))
    return int(m.group(1)) if m else None


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


def norm_text(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "").lower()
    t = re.sub(r"[^a-zа-яё0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_suffix(title: str, group: str) -> str:
    """Убирает хвост "(Имя группы)", добавленный нашим же переименованием."""
    t = title or ""
    if group:
        pat = re.compile(r"\s*[(\[]\s*" + re.escape(group.strip())
                         + r"\s*[)\]]\s*$", re.IGNORECASE)
        t = pat.sub("", t)
    return t.strip()


def safe_rename(orig: str, group: str, template: str) -> str:
    if not group:
        return (orig or "").strip()
    try:
        out = template.format(title=orig, group=group)
    except Exception:
        out = f"{orig} ({group})"
    return out.strip() or (orig or "").strip()


def track_key(artist: str, title: str, duration) -> str:
    return f"{norm_text(artist)}|{norm_text(title)}|{int(duration or 0)}"


# ---------------------------------------------------------------- база данных

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS added (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id   INTEGER NOT NULL,
        audio_id   INTEGER NOT NULL,
        artist     TEXT,
        title      TEXT,
        duration   INTEGER,
        added_at   TEXT NOT NULL)""")
    conn.commit()
    return conn


def added_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) FROM added WHERE substr(added_at,1,10)=?",
        (today,)).fetchone()
    return int(row[0]) if row else 0


def db_add(conn: sqlite3.Connection, owner_id: int, audio_id: int,
           artist: str, title: str, duration: int) -> None:
    conn.execute(
        "INSERT INTO added (owner_id, audio_id, artist, title, duration, added_at) "
        "VALUES (?,?,?,?,?,?)",
        (owner_id, audio_id, artist, title, duration,
         datetime.now(MSK).isoformat(timespec="seconds")))
    conn.commit()
    DB_DIRTY_MARKER.touch()


def db_keys(conn: sqlite3.Connection) -> set:
    """Ключи всех треков, которые бот когда-либо добавлял (защита от повторов,
    даже если трек из музыки сообщества потом удалили вручную)."""
    keys = set()
    try:
        for artist, title, dur in conn.execute(
                "SELECT artist, title, duration FROM added"):
            keys.add(track_key(artist or "", title or "", dur))
    except sqlite3.Error as e:
        log.warning("не смог прочитать базу добавленных: %s", e)
    return keys


# ---------------------------------------------------------------- VK helpers

def extract_items(resp) -> list:
    if isinstance(resp, dict):
        return resp.get("items") or []
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    return []


def get_group_name(vk, gid: int) -> str:
    try:
        resp = vk_call(lambda: vk.method("groups.getById", {"group_id": gid}),
                       "groups.getById")
        groups = []
        if isinstance(resp, dict):
            groups = resp.get("groups") or []
        elif isinstance(resp, list):
            groups = resp
        if isinstance(groups, dict):
            groups = groups.get("groups") or []
        if groups and isinstance(groups[0], dict):
            name = (groups[0].get("name") or "").strip()
            if name:
                return name
    except Exception as e:
        log.warning("не получил название группы (%s) - задайте group_name "
                    "в audio.yaml, иначе скобки с именем не добавятся", e)
    return ""


def community_keys(vk, gid: int, group_name: str,
                   max_pages: int = 10) -> set:
    """Ключи треков, уже лежащих в музыке сообщества (для защиты от дублей).

    Листает аудиозаписи сообщества страницами по 300 (до max_pages страниц,
    т.е. до 3000 треков) - при 50 добавлениях в сутки список быстро растёт.
    """
    keys = set()
    offset = 0
    for _ in range(max_pages):
        try:
            resp = vk_call(lambda o=offset: vk.method(
                "audio.get", {"owner_id": -gid, "count": 300, "offset": o}),
                "audio.get сообщества")
        except Exception as e:
            log.warning("не смог прочитать музыку сообщества (%s) - "
                        "проверка дублей только по локальной базе", e)
            break
        items = extract_items(resp)
        for it in items:
            title = strip_suffix(it.get("title") or "", group_name)
            keys.add(track_key(it.get("artist") or "", title,
                               it.get("duration")))
        if not items:
            break
        offset += len(items)
        if len(items) < 300 or offset >= 3000:
            break
    return keys


class AudioAccessDenied(Exception):
    """Официальный audio API закрыт для этого приложения/токена."""


def _scrape_candidates(vk, cfg) -> list:
    """Запасной поиск через веб-парсер vk_api.audio.VkAudio.

    Работает без официального audio API (парсит веб-интерфейс музыки),
    поэтому даёт результат даже когда audio.getPopular/audio.search
    отвечают ошибкой 15/20.
    """
    items: list = []
    try:
        from vk_api.audio import VkAudio
    except Exception as e:
        log.warning("веб-парсер аудио недоступен: %s", e)
        return items
    try:
        parser = VkAudio(vk)
        if cfg.get("use_popular", True):
            popular = []
            for t in parser.get_popular_iter(offset=0):
                popular.append(t)
                if len(popular) >= 100:
                    break
            items += popular
            log.info("веб-парсер: «Популярное» - %d треков", len(popular))
        if not items:
            for q in (cfg.get("queries") or []):
                got = parser.search(q=q, count=50)
                items += got or []
                log.info("веб-парсер: '%s' - %d треков", q, len(got or []))
                if len(items) >= 100:
                    break
    except Exception as e:
        log.warning("веб-парсер не сработал: %s", e)
    return items


def fetch_candidates(vk, cfg) -> tuple:
    """Ищет треки-кандидаты. Возвращает (список, официальный API закрыт)."""
    items: list = []
    official_denied = False

    if cfg.get("use_popular", True):
        try:
            resp = vk_call(lambda: vk.method("audio.getPopular",
                                             {"count": 200}),
                           "audio.getPopular")
            got = extract_items(resp)
            log.info("чарт «Популярное»: %d треков", len(got))
            items += got
        except Exception as e:
            log.warning("audio.getPopular не сработал: %s", e)
            if _vk_err_code(e) in VK_ACCESS_CODES:
                official_denied = True

    if not items:
        queries = list(cfg.get("queries") or [])
        random.shuffle(queries)  # каждый запуск смотрит разные запросы
        for q in queries:
            try:
                resp = vk_call(
                    lambda q=q: vk.method("audio.search",
                                          {"q": q, "sort": 2, "count": 100}),
                    f"audio.search '{q}'")
            except Exception as e:
                log.warning("audio.search '%s' не сработал: %s", q, e)
                if _vk_err_code(e) in VK_ACCESS_CODES:
                    official_denied = True
                continue
            got = extract_items(resp)
            log.info("поиск '%s': %d треков", q, len(got))
            items += got
            if len(items) >= 800:
                break

    if not items and official_denied:
        items = _scrape_candidates(vk, cfg)
    return items, official_denied


def filter_tracks(items, cfg, gid: int, existing: set,
                  group_name: str) -> list:
    min_d = int(cfg.get("min_duration", 60))
    max_d = int(cfg.get("max_duration", 600))
    seen = set()
    out = []
    for it in items:
        try:
            owner = int(it.get("owner_id"))
            aid = int(it.get("id"))
            dur = int(it.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        artist = (it.get("artist") or "").strip()
        title = (it.get("title") or "").strip()
        if not artist or not title:
            continue
        if dur < min_d or (max_d and dur > max_d):
            continue
        if owner == -gid:
            continue  # свой же трек из музыки сообщества
        key = track_key(artist, strip_suffix(title, group_name), dur)
        if key in existing or key in seen:
            continue
        seen.add(key)
        out.append({"owner_id": owner, "audio_id": aid, "artist": artist,
                    "title": title, "duration": dur})
    return out


def add_track(vk, gid: int, track: dict, group_name: str,
              template: str) -> int:
    """Копирует трек в музыку сообщества и переименовывает.

    Возвращает id добавленной аудиозаписи (0, если VK не вернул id).
    """
    resp = vk_call(lambda: vk.method("audio.add", {
        "owner_id": track["owner_id"],
        "audio_id": track["audio_id"],
        "group_id": gid,
    }), "audio.add")
    if isinstance(resp, dict):
        resp = resp.get("response") or resp.get("id") or 0
    try:
        new_id = int(resp)
    except (TypeError, ValueError):
        new_id = 0

    new_title = safe_rename(track["title"], group_name, template)
    if group_name:
        try:
            vk_call(lambda: vk.method("audio.edit", {
                "owner_id": -gid,
                "audio_id": new_id,
                "artist": track["artist"],
                "title": new_title,
            }), "audio.edit")
        except Exception as e:
            log.warning("трек добавлен, но переименовать не удалось (%s) - "
                        "останется с оригинальным названием", e)
    return new_id


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Трендовые треки -> музыка сообщества VK")
    parser.add_argument("--config", default=str(BASE_DIR / "audio.yaml"))
    parser.add_argument("--limit", type=int, default=0,
                        help="dry-run: показать до N кандидатов, ничего не добавляя")
    parser.add_argument("--reset-db", action="store_true",
                        help="забыть добавленные треки и начать с чистого листа")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    cfg = load_config(Path(args.config))
    setup_logging(BASE_DIR)
    log.info("=== старт, режим: %s ===", "dry-run" if args.limit else "ПУБЛИКАЦИЯ")

    if not cfg.get("enabled", True):
        log.info("автодобавление выключено (enabled: false)")
        return 3

    if not HAS_VK:
        log.error("не установлена библиотека vk_api (pip install vk_api)")
        return 2

    token = os.getenv("VK_TOKEN", "").strip()
    gid_raw = (os.getenv("AUDIO_GROUP_ID") or os.getenv("GROUP_ID") or "").strip()
    if not token or not gid_raw:
        log.error("не заданы VK_TOKEN и GROUP_ID (см. .env / секреты GitHub)")
        return 2
    try:
        gid = int(gid_raw)
    except ValueError:
        log.error("GROUP_ID должен быть числом без минуса")
        return 2

    conn = init_db(BASE_DIR / "data" / "audio.sqlite")
    if args.reset_db:
        conn.execute("DELETE FROM added")
        conn.commit()
        log.info("база добавленных треков очищена")

    already = added_today(conn)
    daily_limit = int(cfg.get("daily_limit", 5))
    if already >= daily_limit:
        log.info("дневной лимит исчерпан: %d из %d треков за сегодня - "
                 "пропуск запуска", already, daily_limit)
        return 3

    vk = vk_api.VkApi(token=token, api_version="5.199")

    group_name = (os.getenv("AUDIO_GROUP_NAME") or cfg.get("group_name") or "").strip()
    if not group_name:
        group_name = get_group_name(vk, gid)
    if not group_name:
        log.warning("название группы неизвестно - треки будут добавлены "
                    "без скобок с именем группы")

    existing = community_keys(vk, gid, group_name)
    known = db_keys(conn)
    log.info("известно треков: %d в музыке сообщества, %d добавлялись ранее",
             len(existing), len(known))
    existing |= known

    try:
        raw, official_denied = fetch_candidates(vk, cfg)
    except AudioAccessDenied as e:
        log.error("VK закрыл audio API для этого приложения: %s", e)
        return 1
    tracks = filter_tracks(raw, cfg, gid, existing, group_name)
    random.shuffle(tracks)  # разнообразие: каждый запуск берёт разные треки
    log.info("подходящих новых треков: %d", len(tracks))

    if official_denied and not tracks:
        log.error("официальный audio API закрыт (ошибка 15/20), веб-парсер "
                  "тоже не нашёл треков - автодобавление работать не может")
        log.error("что делать: токен должен быть пользовательским и с правом "
                  "'audio'; сторонним приложениям audio API выдаётся по заявке "
                  "в поддержку VK (devsupport@corp.vk.com)")
        return 1

    if args.limit:
        template = cfg.get("rename_template") or "{title} ({group})"
        for i, t in enumerate(tracks[:args.limit], 1):
            log.info("кандидат %d: %s - %s (%d сек)",
                     i, t["artist"],
                     safe_rename(t["title"], group_name, template),
                     t["duration"])
        log.info("dry-run: ничего не добавлено. Реальный запуск: python vk_audio_bot.py")
        return 0

    if not tracks:
        log.info("новых трендовых треков не нашлось - пропуск запуска")
        return 3

    per_run = int(cfg.get("per_run", 3))
    budget = max(0, min(per_run, daily_limit - already))
    if budget == 0:
        log.info("лимиты исчерпаны - пропуск запуска")
        return 3
    template = cfg.get("rename_template") or "{title} ({group})"

    added = 0
    failed = 0
    rate_limited = False
    for i, t in enumerate(tracks[:budget], 1):
        nice = f"{t['artist']} - {t['title']}"
        try:
            new_id = add_track(vk, gid, t, group_name, template)
        except Exception as e:
            code = _vk_err_code(e)
            if code in VK_RATE_CODES:
                log.error("VK антифлуд (код %s) - останавливаюсь "
                          "до следующего запуска", code)
                rate_limited = True
                break
            if code in VK_ACCESS_CODES:
                log.error("VK не даёт работать с музыкой этим токеном "
                          "(код %s): %s", code, e)
                log.error("подсказка: методы audio.* требуют ПОЛЬЗОВАТЕЛЬСКИЙ "
                          "токен с правом 'audio'; сторонним приложениям audio "
                          "API обычно выдаётся по заявке в поддержку VK "
                          "(devsupport@corp.vk.com)")
                failed += 1
                break
            log.error("не удалось добавить '%s': %s", nice, e)
            failed += 1
            continue
        db_add(conn, t["owner_id"], new_id, t["artist"], t["title"],
               t["duration"])
        added += 1
        log.info("добавлен трек %d/%d: %s (id %s) -> «%s»",
                 added, budget, nice, new_id,
                 safe_rename(t["title"], group_name, template))
        if i < budget:
            time.sleep(5)

    log.info("=== готово: добавлено %d, ошибок %d, за сегодня %d/%d ===",
             added, failed, already + added, daily_limit)
    if rate_limited and added == 0:
        # Ничего не добавлено из-за антифлуда, база не менялась:
        # код 3 = "пропуск запуска" (зелёный статус без коммита)
        return 3
    return 1 if (failed > 0 and added == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
