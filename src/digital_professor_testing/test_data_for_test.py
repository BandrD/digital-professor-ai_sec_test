"""
test_data_for_test.py

Минимальный слой логирования для нагрузочного/AI-security тестирования.
Создаёт таблицу `test_data` в той же SQLite, что используется проектом, не меняя продовый schema/init_db.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from digital_professor.database import get_connection

logger = logging.getLogger(__name__)


def init_test_tables() -> None:
    """
    Создаёт таблицу test_data, если её нет.
    Таблица хранит: user_id, user_name, user_query, llm_response, status, reason, meta, created_at.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS test_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                user_query TEXT,
                llm_response TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                meta TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # индексы для быстрого анализа
        cur.execute("CREATE INDEX IF NOT EXISTS idx_test_data_user_id ON test_data(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_test_data_created_at ON test_data(created_at)")
        conn.commit()
        logger.info("Test table `test_data` initialized")
    finally:
        conn.close()


def log_test_interaction(
    *,
    user_id: int,
    user_name: Optional[str],
    user_query: str,
    status: str,
    reason: Optional[str] = None,
    llm_response: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """
    Логирует одну попытку вопроса пользователя (успешную или заблокированную).

    status: например 'ok' | 'blocked' | 'error'
    reason: например 'rate_limiter' | 'input_validator' | 'llm_censor' | 'exception'
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        meta_json = None
        if meta is not None:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)
            except Exception:
                meta_json = None

        cur.execute(
            """
            INSERT INTO test_data(user_id, user_name, user_query, llm_response, status, reason, meta)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                (user_name or None),
                (user_query or ""),
                (llm_response or None),
                (status or "error"),
                (reason or None),
                (meta_json or None),
            ),
        )
        conn.commit()
    except Exception as e:
        # НИКОГДА не ломаем тест бота из-за логирования
        logger.debug(f"Failed to write test_data row: {e}")
    finally:
        conn.close()


