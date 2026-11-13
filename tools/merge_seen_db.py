#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Слияние двух баз дедупликации seen.sqlite (объединение записей).

Используется в GitHub Actions, когда `git push` отклонён: пока бот работал,
другой запуск успел закоммитить свою версию data/seen.sqlite. Git не умеет
сливать бинарные файлы (rebase падает с конфликтом), поэтому записи
объединяются на уровне SQL: отметки обоих запусков сохраняются и повторной
публикации одних и тех же новостей не будет.

Запуск: python3 tools/merge_seen_db.py <другая_база> <наша_база>

Строки из <другая_база> переносятся в <наша_база>; после работы <наша_база>
содержит объединение обеих. При конфликте одного и того же URL статус
"posted" имеет приоритет над "failed" (защита от повторной публикации).
"""
import sqlite3
import sys

POSTED_SCHEMA = """CREATE TABLE IF NOT EXISTS posted(
                    url TEXT PRIMARY KEY,
                    title_key TEXT,
                    status TEXT,
                    attempts INTEGER DEFAULT 1,
                    post_id TEXT,
                    created_at TEXT)"""

TITLES_SCHEMA = """CREATE TABLE IF NOT EXISTS titles(
                    title_key TEXT PRIMARY KEY,
                    norm_text TEXT,
                    seen_at TEXT)"""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("запуск: python3 tools/merge_seen_db.py <другая_база> <наша_база>",
              file=sys.stderr)
        return 2
    remote_path, local_path = argv[1], argv[2]

    src = sqlite3.connect(f"file:{remote_path}?mode=ro", uri=True)
    dst = sqlite3.connect(local_path)
    dst.execute(POSTED_SCHEMA)
    dst.execute(TITLES_SCHEMA)

    rows = src.execute(
        "SELECT url, title_key, status, attempts, post_id, created_at FROM posted"
    ).fetchall()
    for url, title_key, status, attempts, post_id, created_at in rows:
        dst.execute(
            """INSERT INTO posted(url, title_key, status, attempts, post_id, created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 status=CASE WHEN excluded.status='posted' AND posted.status<>'posted'
                             THEN 'posted' ELSE posted.status END,
                 attempts=MAX(COALESCE(posted.attempts,0), COALESCE(excluded.attempts,0)),
                 post_id=CASE WHEN excluded.status='posted' THEN excluded.post_id
                              ELSE posted.post_id END""",
            (url, title_key, status or "failed", attempts or 1,
             post_id or "", created_at))

    trows = src.execute(
        "SELECT title_key, norm_text, seen_at FROM titles"
    ).fetchall()
    for title_key, norm_text, seen_at in trows:
        dst.execute(
            "INSERT OR IGNORE INTO titles(title_key, norm_text, seen_at) VALUES(?,?,?)",
            (title_key, norm_text, seen_at))

    dst.commit()
    src.close()
    dst.close()
    print(f"слияние баз: перенесено {len(rows)} записей posted, "
          f"{len(trows)} заголовков")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
