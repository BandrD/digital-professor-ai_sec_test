# database.py
import sqlite3
import logging
from datetime import timedelta, timezone
import os
import json
import pytz
from typing import Optional, Dict, Any
import re 
import datetime
from pathlib import Path
import pytz
import json

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = str(PROJECT_ROOT / "data" / "bot.db")

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# Инициализация базы данных v3: Добавляем топики + теория, реализована через телеграф 
def init_db():
    conn = get_connection()
    c = conn.cursor()

    # users
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            timezone TEXT,
            ping_time TEXT,
            is_admin INTEGER DEFAULT 0,
            student_id TEXT DEFAULT NULL -- НОВОЕ: Столбец для номера студенческого
        )
    ''')

    cols = [r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if 'student_id' not in cols:
        try:
            c.execute("ALTER TABLE users ADD COLUMN student_id TEXT DEFAULT NULL")
        except Exception as e:
            logger.debug("Could not ALTER users add student_id: %s", e)

    # responses
    c.execute('''
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            status TEXT,
            timestamp TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')

    # courses
    c.execute('''
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Назначение курсов 
    c.execute('''
        CREATE TABLE IF NOT EXISTS course_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            assigned_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'assigned',
            n_times INTEGER NOT NULL,
            FOREIGN KEY(course_id) REFERENCES courses(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')
    
    # weekly_progress: факт выполнения заданий по неделям
    c.execute('''
        CREATE TABLE IF NOT EXISTS weekly_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            fact INTEGER DEFAULT 0,
            kpi INTEGER NOT NULL,
            UNIQUE(user_id, course_id, week_start),
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(course_id) REFERENCES courses(id)
        )
    ''')


    # topics (новая таблица: курс -> топики)
    c.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            theory TEXT DEFAULT '',  -- пока заглушка для теории
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        )
    ''')

    # Миграция: добавляем created_by если его нет
    cols = [r['name'] for r in c.execute("PRAGMA table_info(topics)").fetchall()]
    if 'created_by' not in cols:
        try:
            c.execute("ALTER TABLE topics ADD COLUMN created_by INTEGER")
        except Exception as e:
            logger.debug("Could not ALTER topics add created_by: %s", e)
    
    # Миграция: добавляем telegraph_link если его нет
    if 'telegraph_link' not in cols:
        try:
            c.execute("ALTER TABLE topics ADD COLUMN telegraph_link TEXT DEFAULT NULL")
        except Exception as e:
            logger.debug("Could not ALTER topics add telegraph_link: %s", e)

    # tasks: теперь можно привязать к topic_id
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL, -- оставляем для обратной совместимости
            topic_id INTEGER,           -- новое поле
            task_type TEXT NOT NULL,    -- 'test', 'open', 'practical'
            title TEXT,
            description TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reference_answer TEXT,      -- эталонный ответ для open/practical
            evaluation_criteria TEXT,   -- критерии оценки
            max_score INTEGER DEFAULT 10,
            FOREIGN KEY(course_id) REFERENCES courses(id),
            FOREIGN KEY(topic_id) REFERENCES topics(id)
        )
    ''')
    
    # Миграция: если topic_id отсутствует — добавить
    cols = [r['name'] for r in c.execute("PRAGMA table_info(tasks)").fetchall()]
    if 'topic_id' not in cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN topic_id INTEGER REFERENCES topics(id)")
        except Exception as e:
            logger.debug("Could not ALTER tasks add topic_id (may already exist): %s", e)

    # Миграция: если created_by отсутствует в tasks — добавить
    if 'created_by' not in cols:
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        except Exception as e:
            logger.debug("Could not ALTER tasks add created_by: %s", e)

    # test questions (each question belongs to a task of type 'test')
    c.execute('''
        CREATE TABLE IF NOT EXISTS test_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            order_idx INTEGER DEFAULT 0,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )
    ''')

    # choices for each test question
    c.execute('''
        CREATE TABLE IF NOT EXISTS test_choices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            choice_text TEXT NOT NULL,
            is_correct INTEGER DEFAULT 0,
            FOREIGN KEY(question_id) REFERENCES test_questions(id)
        )
    ''')

    # task_results: сохраняем ответы студента
    c.execute('''
        CREATE TABLE IF NOT EXISTS task_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            question_id INTEGER,
            choice_id INTEGER,
            answer_text TEXT,
            file_id TEXT,
            score INTEGER,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            feedback TEXT,  -- обратная связь от LLM/преподавателя
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')
        # --- Big5: текущий профиль и история пересчётов ---
    c.execute('''
        CREATE TABLE IF NOT EXISTS big5_profiles (
            user_id INTEGER PRIMARY KEY,
            extraversion INTEGER NOT NULL,
            agreeableness INTEGER NOT NULL,
            conscientiousness INTEGER NOT NULL,
            emotional_stability INTEGER NOT NULL,
            openness INTEGER NOT NULL,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            meta_data TEXT DEFAULT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS big5_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            extraversion INTEGER NOT NULL,
            agreeableness INTEGER NOT NULL,
            conscientiousness INTEGER NOT NULL,
            emotional_stability INTEGER NOT NULL,
            openness INTEGER NOT NULL,
            meta_data TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, week_start),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')

    # --- users: флаг, что первичный опрос Big5 пройден ---
    cols = [r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if 'big5_initialized' not in cols:
        try:
            c.execute("ALTER TABLE users ADD COLUMN big5_initialized INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug("Could not ALTER users add big5_initialized: %s", e)

    # --- users: A/B группа пользователя ---
    cols = [r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if 'ab_group' not in cols:
        try:
            c.execute("ALTER TABLE users ADD COLUMN ab_group TEXT DEFAULT NULL")
        except Exception as e:
            logger.debug("Could not ALTER users add ab_group: %s", e)

    # --- users: курс Moodle пользователя ---
    cols = [r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if 'moodle_course' not in cols:
        try:
            c.execute("ALTER TABLE users ADD COLUMN moodle_course TEXT DEFAULT NULL")
        except Exception as e:
            logger.debug("Could not ALTER users add moodle_course: %s", e)

    # --- responses: привязка к задаче + meta для контекста ---
    cols = [r['name'] for r in c.execute("PRAGMA table_info(responses)").fetchall()]
    if 'task_id' not in cols:
        try:
            c.execute("ALTER TABLE responses ADD COLUMN task_id INTEGER REFERENCES tasks(id)")
        except Exception as e:
            logger.debug("Could not ALTER responses add task_id: %s", e)
    if 'meta_data' not in cols:
        try:
            c.execute("ALTER TABLE responses ADD COLUMN meta_data TEXT")
        except Exception as e:
            logger.debug("Could not ALTER responses add meta_data: %s", e)
    
    # ping_logs: история отправленных пингов
    c.execute('''
        CREATE TABLE IF NOT EXISTS ping_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ping_type TEXT NOT NULL, -- 'initial_survey', 'standard', 'dynamic'
            sent_at TEXT NOT NULL,   -- Время отправки
            message_text TEXT NOT NULL, -- Отправленный текст
            big5_profile_id INTEGER, -- Ссылка на big5_profiles на момент отправки (опционально)
            meta_data TEXT DEFAULT NULL,  -- Дополнительные данные (например, Big5 значения на момент отправки)
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(big5_profile_id) REFERENCES big5_profiles(user_id) -- Предполагается, что user_id в big5_profiles уникален
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS user_ping_settings (
            user_id INTEGER PRIMARY KEY,
            pings_per_week INTEGER NOT NULL DEFAULT 5, -- По умолчанию 5 раз в неделю (будни)
            ping_days TEXT DEFAULT '["mon", "tue", "wed", "thu", "fri"]', -- JSON-строка с днями недели, по умолчанию будни
            last_ping_date TEXT, -- Дата последнего пинга (YYYY-MM-DD) 
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')

    #хранение логов рага 
    c.execute('''
        CREATE TABLE IF NOT EXISTS rag_interaction_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_query TEXT NOT NULL,
            retrieved_context TEXT, -- извлекаемый контекст
            llm_response TEXT,      -- ответ модели 
            llm_tokens_used INTEGER,-- Общее количество токенов, использованных LLM
            user_feedback INTEGER,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')
    
    # Таблица для учёта использования токенов LLM
    c.execute('''
        CREATE TABLE IF NOT EXISTS llm_token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            event TEXT NOT NULL,             -- событие (evaluate_task, generate_hint, big5_calculation и т.д.)
            model TEXT NOT NULL,              -- название модели (yandexgpt, yandexgpt-lite)
            provider TEXT,                    -- провайдер
            input_tokens INTEGER,             -- входные токены
            output_tokens INTEGER,            -- выходные токены
            total_tokens INTEGER NOT NULL,    -- общее количество токенов
            user_id INTEGER,                  -- опционально: ID пользователя
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')
    # weekly_pings: еженедельные опросы пользователей
    c.execute('''
        CREATE TABLE IF NOT EXISTS weekly_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            student_id TEXT,
            username TEXT,
            answer1 TEXT, -- ответ на первый вопрос (да/нет)
            answer2 TEXT, -- ответ на второй вопрос (да/нет или название темы)
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')

    #полная сессия диалога пользователя с RAG 
    c.execute('''
        CREATE TABLE IF NOT EXISTS rag_dialogue_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id TEXT NOT NULL, -- Уникальный ID сессии (например, UUID)
            message_role TEXT NOT NULL CHECK(message_role IN ('user', 'assistant')), -- 'user' или 'assistant'
            message_text TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')  

    conn.commit()
    conn.close()
    logger.info("Database initialized / migrated")

    migrate_add_feedback_to_rag_logs()
    migrate_add_llm_analysis_to_rag_logs()
    drop_legacy_message_history()


def migrate_add_llm_analysis_to_rag_logs():
    """
    Добавляет колонку llm_analysis в rag_interaction_logs при необходимости.
    Хранит внутренний analysis (например STEP 1..5) отдельно от llm_response.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA table_info(rag_interaction_logs)")
        cols = [row[1] for row in cursor.fetchall()]  # row[1] = name
        if "llm_analysis" not in cols:
            cursor.execute("ALTER TABLE rag_interaction_logs ADD COLUMN llm_analysis TEXT")
            conn.commit()
            logger.info("Added llm_analysis column to rag_interaction_logs")
    except Exception as e:
        logger.error(f"Failed to ensure llm_analysis column in rag_interaction_logs: {e}")
    finally:
        conn.close()

# ------------------ legacy / user functions (сохраним старые имена) -------------------
def save_user(user_id, username, timezone=None, ping_time=None, is_admin=0, student_id=None, ab_group=None, moodle_course=None):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO users (user_id, username, timezone, ping_time, is_admin, student_id, ab_group, moodle_course)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, username, timezone, ping_time, is_admin, student_id, ab_group, moodle_course))
    conn.commit()
    conn.close()
    logger.info(f"Saved user: {user_id} / {username} / group: {ab_group} / moodle_course: {moodle_course}")

def get_user(user_id):
    """Возвращает dict или None"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, timezone, ping_time, is_admin, student_id, ab_group, big5_initialized, moodle_course FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)

def get_users():
    """Возвращает список tuples (user_id, timezone, ping_time) — совместимо с scheduler.py"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, timezone, ping_time FROM users")
    rows = c.fetchall()
    conn.close()
    return [(r[0], r[1], r[2]) for r in rows]

def update_user_timezone(user_id, timezone):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (timezone, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, timezone) VALUES (?, ?)", (user_id, timezone))
    conn.commit()
    conn.close()
    logger.info(f"Updated timezone for {user_id} -> {timezone}")

def update_user_ping_time(user_id, ping_time):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET ping_time = ? WHERE user_id = ?", (ping_time, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, ping_time) VALUES (?, ?)", (user_id, ping_time))
    conn.commit()
    conn.close()
    logger.info(f"Updated ping_time for {user_id} -> {ping_time}")

# --- NEW: update student_id ---
def update_user_student_id(user_id: int, student_id: str):
    """Updates or inserts the student's ID for the given user."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET student_id = ? WHERE user_id = ?", (student_id, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, student_id) VALUES (?, ?)", (user_id, student_id))
    conn.commit()
    conn.close()
    logger.info(f"Updated student_id for {user_id} -> {student_id}")

# --- NEW: update moodle_course ---
def update_user_moodle_course(user_id: int, moodle_course: str):
    """Updates or inserts the Moodle course for the given user."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET moodle_course = ? WHERE user_id = ?", (moodle_course, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, moodle_course) VALUES (?, ?)", (user_id, moodle_course))
    conn.commit()
    conn.close()
    logger.info(f"Updated moodle_course for {user_id} -> {moodle_course}")

# ------------------ courses / tasks / tests API -------------------
def create_course(title: str, description: str, created_by: int = None, assign_to_user_id: int = None):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO courses (title, description, created_by) VALUES (?, ?, ?)",
              (title, description, created_by))
    course_id = c.lastrowid
    if assign_to_user_id:
        c.execute("INSERT INTO course_assignments (course_id, user_id) VALUES (?, ?)",
                  (course_id, assign_to_user_id))
    conn.commit()
    conn.close()
    logger.info(f"Created course {course_id}: {title}")
    return course_id

def get_course(course_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM courses WHERE id = ?", (course_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def list_all_courses():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM courses ORDER BY id")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def assign_course(course_id: int, user_id: int):
    conn = get_connection()
    c = conn.cursor()
    # avoid duplicates
    c.execute("SELECT id FROM course_assignments WHERE course_id = ? AND user_id = ?", (course_id, user_id))
    if c.fetchone():
        conn.close()
        logger.info(f"Course {course_id} already assigned to {user_id}")
        return False
    c.execute("INSERT INTO course_assignments (course_id, user_id) VALUES (?, ?)", (course_id, user_id))
    conn.commit()
    conn.close()
    logger.info(f"Assigned course {course_id} -> user {user_id}")
    return True

def get_assigned_courses(user_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT courses.* FROM courses
        JOIN course_assignments ON courses.id = course_assignments.course_id
        WHERE course_assignments.user_id = ?
        ORDER BY courses.id
    """, (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# tasks
def add_task(course_id: int, task_type: str, title: str = None, description: str = None, created_by: int = None):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO tasks (course_id, task_type, title, description, created_by)
        VALUES (?, ?, ?, ?, ?)
    """, (course_id, task_type, title, description, created_by))
    tid = c.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"Added task {tid} to course {course_id}")
    return tid

def get_tasks_for_course(course_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE course_id = ? ORDER BY id", (course_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_task(task_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def update_task(task_id: int, title: str = None, description: str = None):
    conn = get_connection()
    c = conn.cursor()
    updates = []
    params = []
    if title is not None:
        updates.append("title = ?")
        params.append(title)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if updates:
        params.append(task_id)
        c.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    conn.close()
    logger.info(f"Updated task {task_id}")

def remove_task(task_id: int):
    conn = get_connection()
    c = conn.cursor()
    # delete choices/questions associated
    c.execute("DELETE FROM test_choices WHERE question_id IN (SELECT id FROM test_questions WHERE task_id = ?)", (task_id,))
    c.execute("DELETE FROM test_questions WHERE task_id = ?", (task_id,))
    c.execute("DELETE FROM task_results WHERE task_id = ?", (task_id,))
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    logger.info(f"Removed task {task_id}")

# test questions & choices
def add_test_question(task_id: int, question_text: str, order_idx: int = 0):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO test_questions (task_id, question_text, order_idx) VALUES (?, ?, ?)",
              (task_id, question_text, order_idx))
    qid = c.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"Added question {qid} to test {task_id}")
    return qid

def add_test_choice(question_id: int, choice_text: str, is_correct: int = 0):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO test_choices (question_id, choice_text, is_correct) VALUES (?, ?, ?)",
              (question_id, choice_text, int(bool(is_correct))))
    cid = c.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"Added choice {cid} to question {question_id}")
    return cid

def get_test_questions_with_choices(task_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM test_questions WHERE task_id = ? ORDER BY order_idx, id", (task_id,))
    questions = []
    for q in c.fetchall():
        qd = dict(q)
        c2 = conn.cursor()
        c2.execute("SELECT * FROM test_choices WHERE question_id = ?", (qd['id'],))
        qd['choices'] = [dict(ch) for ch in c2.fetchall()]
        questions.append(qd)
    conn.close()
    return questions

def get_question(question_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM test_questions WHERE id = ?", (question_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_choice(choice_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM test_choices WHERE id = ?", (choice_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


#task results 2.0
def save_task_result(task_id: int, user_id: int, question_id: int = None, choice_id: int = None,
                     answer_text: str = None, file_id: str = None, score: int = None,
                     feedback: str = None):
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        INSERT INTO task_results (
            task_id, user_id, question_id, choice_id, answer_text, file_id, score, feedback
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (task_id, user_id, question_id, choice_id, answer_text, file_id, score, feedback))

    rid = c.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"Saved result {rid} for user {user_id} task {task_id} (score={score}, feedback={feedback})")
    return rid


def get_user_results(user_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM task_results WHERE user_id = ? ORDER BY submitted_at DESC", (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def is_admin(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT is_admin 
        FROM users 
        WHERE user_id = ?
    """, (user_id,))
    result = c.fetchone()
    conn.close()
    
    if result and result[0] == 1:
        logger.info(f"User {user_id} is an admin.")
        return True
    else:
        logger.info(f"User {user_id} is not an admin or does not exist.")
        return False
    
#------assign_course v2.0: сущности---------
def assign_course(course_id, user_id, n_times):
    """
    Назначает курс пользователю, добавляя запись в таблицу course_assignments.
    Помимо курса, пользователю добавляется таргет на взаимодейсвтие с курсов n_times раз в неделю.
    
    Args:
        course_id (int): ID курса.
        user_id (int): ID пользователя.
        n_times (int): Колчество раз в неделю, которое пользователь должен взаимодействовать с курсом. 
        За взаимодействие считается одно решенное задание. 
    
    Returns:
        bool: True, если назначение успешно, False в случае ошибки.
    """
    conn = get_connection()
    c = conn.cursor()
    
    try:
        # Проверяем, существует ли пользователь
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            logger.error(f"Пользователь с user_id {user_id} не найден.")
            return False
        
        # Проверяем, существует ли курс
        c.execute("SELECT id FROM courses WHERE id = ?", (course_id,))
        if not c.fetchone():
            logger.error(f"Курс с course_id {course_id} не найден.")
            return False
        
        # Проверяем, не назначен ли уже курс этому пользователю
        c.execute("""
            SELECT id FROM course_assignments 
            WHERE course_id = ? AND user_id = ?
        """, (course_id, user_id))
        if c.fetchone():
            logger.warning(f"Курс {course_id} уже назначен пользователю {user_id}.")
            return False
        
        # Добавляем запись о назначении курса
        c.execute("""
            INSERT INTO course_assignments (course_id, user_id, n_times, status)
            VALUES (?, ?,?, 'assigned')
        """, (course_id, user_id, n_times))
        
        conn.commit()
        logger.info(f"Курс {course_id} успешно назначен пользователю {user_id}. Рекомендуем решать {n_times} заданий в неделю для успешного прохождения курса")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при назначении курса {course_id} пользователю {user_id}: {e}")
        return False
        
    finally:
        conn.close()


#Новая реализация для проверки выполнгения заданий, с учетом критериев: тест 60%, открытое задание - 80%. Код - 90%
def is_task_completed(task_id, user_id):
    """Проверяет, выполнено ли задание пользователем с учетом минимальных порогов по типу задания"""
    conn = get_connection()
    c = conn.cursor()
    
    # Выбираем запись с максимальным score для данного task_id и user_id
    c.execute("""
        SELECT t.task_type, tr.score 
        FROM tasks t
        LEFT JOIN task_results tr ON t.id = tr.task_id 
            AND tr.user_id = ?
            AND tr.question_id IS NULL
            AND tr.score = (
                SELECT MAX(score)
                FROM task_results
                WHERE task_id = t.id AND user_id = ? AND question_id IS NULL
            )
        WHERE t.id = ?
    """, (user_id, user_id, task_id))
    result = c.fetchone()
    conn.close()
    
    logger.debug(f"is_task_completed: task_id={task_id}, user_id={user_id}, result={result}")
    
    if result is None:
        logger.debug(f"Task not found or no result: task_id={task_id}, user_id={user_id}")
        return False
    
    task_type, score = result
    
    if score is None:
        logger.debug(f"No score for task_id={task_id}, user_id={user_id}")
        return False
    
    if task_type == 'test':
        logger.debug(f"Test task, score={score}, completed={score >= 60}")
        return score >= 60
    elif task_type == 'open':
        logger.debug(f"Open task, score={score}, completed={score >= 8}")
        return score >= 8
    elif task_type == 'practical':
        logger.debug(f"Practical task, score={score}, completed={score >= 9}")
        return score >= 9
    else:
        logger.warning(f"Unknown task type: {task_type}, assuming completed")
        return True



#v2.0 
def is_course_completed(course_id, user_id):
    """Проверяет, выполнены ли все задания курса пользователем с учетом порогов"""
    conn = get_connection()
    c = conn.cursor()

    # Получаем все задания курса
    c.execute("SELECT id FROM tasks WHERE course_id = ?", (course_id,))
    tasks = [row[0] for row in c.fetchall()]
    
    if not tasks:
        conn.close()
        logger.debug(f"No tasks for course_id={course_id}")
        return False

    # Проверяем, выполнены ли все задания
    completed = all(is_task_completed(task_id, user_id) for task_id in tasks)
    
    conn.close()
    logger.debug(f"Course completed: course_id={course_id}, user_id={user_id}, completed={completed}")
    return completed

### --------------------- Добавляем новые функции для топиков------------
def get_topics_for_course(course_id: int):
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM topics WHERE course_id = ? ORDER BY id", (course_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_topic(topic_id: int):
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM topics WHERE id = ?", (topic_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_tasks_for_topic(topic_id: int):
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE topic_id = ? ORDER BY id", (topic_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

#Для треккинга прогресса 
def is_topic_completed(topic_id, user_id):
    """Проверяет, выполнены ли все задания топика пользователем с учетом порогов"""
    conn = get_connection()
    c = conn.cursor()

    # Получаем все задания топика
    c.execute("SELECT id FROM tasks WHERE topic_id = ?", (topic_id,))
    tasks = [row[0] for row in c.fetchall()]
    
    if not tasks:
        conn.close()
        logger.debug(f"No tasks for topic_id={topic_id}")
        return False

    # Проверяем, выполнены ли все задания
    completed = all(is_task_completed(task_id, user_id) for task_id in tasks)
    
    conn.close()
    logger.debug(f"Topic completed: topic_id={topic_id}, user_id={user_id}, completed={completed}")
    return completed

#------------------Набор функций для работы с KPI--------------------------------------
def get_week_start(dt: datetime = None) -> str:
    """Возвращает ISO-дату понедельника текущей недели"""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")

def update_weekly_progress(user_id: int, course_id: int, kpi: int):
    """Инкремент факта выполнения задания в текущей неделе"""
    conn = get_connection()
    c = conn.cursor()
    week_start = get_week_start()

    c.execute('''
        INSERT INTO weekly_progress (user_id, course_id, week_start, fact, kpi)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(user_id, course_id, week_start)
        DO UPDATE SET fact = fact + 1
    ''', (user_id, course_id, week_start, kpi))

    conn.commit()
    conn.close()

def get_user_courses_with_progress(user_id: int):
    """Возвращает список назначенных курсов с KPI и фактом на эту неделю"""
    conn = get_connection()
    c = conn.cursor()
    week_start = get_week_start()

    c.execute('''
        SELECT c.title, ca.course_id, ca.n_times, 
               COALESCE(wp.fact, 0) as fact
        FROM course_assignments ca
        JOIN courses c ON c.id = ca.course_id
        LEFT JOIN weekly_progress wp 
            ON wp.course_id = ca.course_id 
           AND wp.user_id = ca.user_id 
           AND wp.week_start = ?
        WHERE ca.user_id = ?
    ''', (week_start, user_id))

    rows = c.fetchall()
    conn.close()
    return rows

def get_kpi_for_user_course(user_id: int, course_id: int) -> int:
    '''
    Получаем KPI из таблицы course_assignments 
    '''
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT n_times FROM course_assignments WHERE user_id=? AND course_id=?", (user_id, course_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

### ------------------Вспомогательная логика для биг5-------------------------------------------

#Хелперы дат для биг5  
def _parse_iso(ts: str) -> datetime:
    # пытаемся парсить как UTC ISO или sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS")
    try:
        return datetime.fromisoformat(ts.replace('Z','+00:00'))
    except Exception:
        return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

def week_start_for(dt: datetime, tzname: str) -> str:
    """Понедельник 00:00 локальной зоны -> в формате YYYY-MM-DD (дата понедельника в локали)."""
    tz = pytz.timezone(tzname)
    local = dt.astimezone(tz)
    monday = local - timedelta(days=local.weekday())
    monday0 = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    # храним week_start как локальную календарную дату (без времени)
    return monday0.strftime("%Y-%m-%d")

def current_week_bounds(tzname: str):
    tz = pytz.timezone(tzname)
    now = datetime.datetime.now(tz)
    ws = week_start_for(now, tzname)      # строка
    ws_dt = tz.localize(datetime.datetime.strptime(ws, "%Y-%m-%d"))
    we_dt = ws_dt + timedelta(days=7)
    return ws_dt.astimezone(timezone.utc), we_dt.astimezone(timezone.utc)  # UTC datetimes


#CRUD для Big5 и пингов
def is_big5_initialized(user_id: int) -> bool:
    conn = get_connection(); c = conn.cursor()
    row = c.execute("SELECT big5_initialized FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row['big5_initialized'])

def mark_big5_initialized(user_id: int):
    conn = get_connection(); c = conn.cursor()
    c.execute("UPDATE users SET big5_initialized=1 WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()

def upsert_big5_profile(user_id: int, ev: int, ag: int, co: int, es: int, opn: int, meta_data: str = None):
    conn = get_connection(); c = conn.cursor()
    exists = c.execute("SELECT 1 FROM big5_profiles WHERE user_id=?", (user_id,)).fetchone()
    if exists:
        c.execute("""
            UPDATE big5_profiles
               SET extraversion=?, agreeableness=?, conscientiousness=?, emotional_stability=?, openness=?,
                   last_updated=CURRENT_TIMESTAMP, meta_data=?
             WHERE user_id=?
        """, (ev, ag, co, es, opn, meta_data, user_id))
    else:
        c.execute("""
            INSERT INTO big5_profiles(user_id, extraversion, agreeableness, conscientiousness, emotional_stability, openness, meta_data)
            VALUES(?,?,?,?,?,?,?)
        """, (user_id, ev, ag, co, es, opn, meta_data))
    conn.commit(); conn.close()
    logger.debug(f"Big5_profiles updated for {user_id}. Data: {ev, ag, co, es, opn, meta_data}")

def get_big5_profile(user_id: int):
    conn = get_connection(); c = conn.cursor()
    row = c.execute("SELECT * FROM big5_profiles WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row

def insert_big5_history(user_id: int, week_start: str, ev: int, ag: int, co: int, es: int, opn: int, meta_data: str = None):
    conn = get_connection(); c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO big5_history(user_id, week_start, extraversion, agreeableness, conscientiousness, emotional_stability, openness, meta_data)
        VALUES(?,?,?,?,?,?,?,?)
    """, (user_id, week_start, ev, ag, co, es, opn, meta_data))
    conn.commit(); conn.close()
    logger.debug(f"Big5 history updated for {user_id}. Data: {week_start, ev, ag, co, es, opn, meta_data}")



def save_user_response(user_id: int, status: str, meta_data: Optional[Dict[Any, Any]] = None):
    """
    Сохраняет ответ пользователя на пинг.
    Время сохраняется в UTC в формате 'YYYY-MM-DD HH:MM:SS' для согласованности с task_results.submitted_at.

    Args:
        user_id (int): ID пользователя.
        status (str): Статус ответа (например, "Готов приступить").
        meta_data (dict, optional): Дополнительные метаданные.
    """
    conn = get_connection()
    c = conn.cursor()
    
    # Получаем текущее время в UTC и форматируем его как в task_results.submitted_at
    # Это решает проблему несоответствия форматов/таймзон
    timestamp_utc_str = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger(__name__) # Получаем логгер для модуля
    logger.debug(f"Saving response for user {user_id}: status='{status}', meta_data={meta_data}, ts='{timestamp_utc_str}'")

    try:
        c.execute('''
            INSERT INTO responses (user_id, status, timestamp, meta_data)
            VALUES (?, ?, ?, ?)
        ''', (user_id, status, timestamp_utc_str, json.dumps(meta_data, ensure_ascii=False) if meta_data else None))
        
        conn.commit()
        logger.info(f"Saved response for user {user_id}: status='{status}', ts='{timestamp_utc_str}'")
    except Exception as e:
        logger.error(f"Failed to save response for user {user_id}: {e}", exc_info=True)
        conn.rollback() # Откатываем транзакцию в случае ошибки
        raise # Перебрасываем исключение
    finally:
        conn.close()

def link_last_response_to_task(user_id: int, task_id: int, submitted_at_utc: str | None):
    """
    Находим последний ответ на пинг ДО времени сдачи, у которого task_id IS NULL, 
    и проставляем привязку.
    """
    logger.debug(f"Attempting to link last response for user {user_id} to task {task_id}. Submitted at: {submitted_at_utc}")
    conn = get_connection()
    c = conn.cursor()
    
    try:
        row = None
        if submitted_at_utc:
            # берём последний response до момента сдачи
            row = c.execute("""
                SELECT id FROM responses
                WHERE user_id=? AND task_id IS NULL AND datetime(timestamp) <= datetime(?)
                ORDER BY datetime(timestamp) DESC LIMIT 1
            """, (user_id, submitted_at_utc)).fetchone()
        else:
            # если времени нет, просто последний незалинкуемый
            row = c.execute("""
                SELECT id FROM responses
                WHERE user_id=? AND task_id IS NULL
                ORDER BY datetime(timestamp) DESC LIMIT 1
            """, (user_id,)).fetchone()

        if row:
            logger.info(f"Found response ID {row['id']} to link for user {user_id}. Linking to task {task_id}.")
            c.execute("UPDATE responses SET task_id=? WHERE id=?", (task_id, row['id']))
            conn.commit()
            logger.debug(f"Successfully linked response ID {row['id']} to task {task_id} for user {user_id}.")
        else:
            logger.warning(f"No unlinked response found for user {user_id} before {submitted_at_utc} to link to task {task_id}. "
                           f"Check if a ping was sent or if it was already linked.")
    except Exception as e:
        logger.error(f"Error linking last response for user {user_id} to task {task_id}: {e}", exc_info=True)
    finally:
        conn.close()


# def get_week_metrics_for_user(user_id: int, week_start: str, week_end: str):
#     """
#     Собираем агрегаты за неделю [week_start, week_end).
#     """
#     logger.debug(f"Collecting weekly metrics for user {user_id} from {week_start} to {week_end}")
#     conn = get_connection()
#     c = conn.cursor()

#     try:
#         # все ответы на пинги в окне
#         responses = c.execute("""
#             SELECT id, status, timestamp, task_id, meta_data
#             FROM responses
#             WHERE user_id=? AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)
#             ORDER BY datetime(timestamp) ASC
#         """, (user_id, week_start, week_end)).fetchall()
#         logger.debug(f"Found {len(responses)} responses for user {user_id} in the week range.")

#         # все факты по всем курсам за неделю (из weekly_progress)
#         progress = c.execute("""
#             SELECT SUM(fact) as fact, SUM(kpi) as kpi
#             FROM weekly_progress
#             WHERE user_id=? AND date(week_start)=date(?)
#         """, (user_id, week_start)).fetchone()
        
#         # Обработка случая, если progress None (нет записей)
#         if progress:
#              fact = int(progress['fact'] or 0)
#              kpi = int(progress['kpi'] or 0)
#         else:
#              fact = 0
#              kpi = 0
#         logger.debug(f"Progress for user {user_id}: fact={fact}, kpi={kpi}")

#         # задержки: response -> выполненная задача (если привязана)
#         delays = []
#         logger.debug(f"Calculating delays for {len(responses)} responses...")
#         for r in responses:
#             logger.debug(f"Processing response ID {r['id']} for user {user_id}: task_id={r['task_id']}, status='{r['status']}'")
#             if r['task_id']:
#                 tr = c.execute("""
#                     SELECT submitted_at FROM task_results
#                     WHERE task_id=? AND user_id=?
#                     ORDER BY datetime(submitted_at) DESC LIMIT 1
#                 """, (r['task_id'], user_id)).fetchone()
#                 if tr:
#                     try:
#                         dt_ping = _parse_iso(r['timestamp'])
#                         dt_task = _parse_iso(tr['submitted_at'])
#                         delay_min = max(0, int((dt_task - dt_ping).total_seconds() // 60))
#                         delays.append({"status": r['status'], "delay_min": delay_min})
#                         logger.debug(f"Calculated delay for response ID {r['id']} (task {r['task_id']}): {delay_min} minutes")
#                     except Exception as e:
#                          logger.error(f"Error calculating delay for response ID {r['id']} (task {r['task_id']}): {e}. Ping TS: {r['timestamp']}, Task TS: {tr['submitted_at']}", exc_info=True)
#                 else:
#                      logger.warning(f"Task result not found for task_id {r['task_id']} and user {user_id} when calculating delay for response {r['id']}.")
#             else:
#                  logger.debug(f"Response ID {r['id']} has no task_id, skipping delay calculation.")

#         metrics = {
#             "responses": [{"status": r["status"], "ts": r["timestamp"], "task_id": r["task_id"]} for r in responses],
#             "tasks_completed": fact,
#             "fact": fact,
#             "kpi": kpi,
#             "delays": delays
#         }
#         logger.debug(f"Final metrics for user {user_id}: responses={len(metrics['responses'])}, delays={len(metrics['delays'])}, fact={metrics['fact']}, kpi={metrics['kpi']}")
#         return metrics

#     except Exception as e:
#         logger.error(f"Error collecting weekly metrics for user {user_id}: {e}", exc_info=True)
#         # Возвращаем пустые/нулевые метрики в случае ошибки, чтобы не сломать пересчет
#         return {
#             "responses": [],
#             "tasks_completed": 0,
#             "fact": 0,
#             "kpi": 0,
#             "delays": []
#         }
#     finally:
#         conn.close()


# async def recalc_big5_for_user_week(user_id: int, tzname: str, week_start_local: str, llm_json_fn):
#     """
#     week_start_local: YYYY-MM-DD — понедельник по локальной таймзоне пользователя.
#     llm_json_fn(prompt: str) -> dict  # функция вызова LLM, вернёт словарь с ключами шкал Big5.
#     """
#     logger.info(f"Starting Big5 recalculation for user {user_id}, week starting {week_start_local} (timezone: {tzname})")
    
#     try:
#         # границы окна в UTC для SQL
#         tz = pytz.timezone(tzname)
#         ws_local = tz.localize(datetime.strptime(week_start_local, "%Y-%m-%d"))
#         we_local = ws_local + timedelta(days=7)
#         ws_utc = ws_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#         we_utc = we_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
#         logger.debug(f"Calculated UTC window for user {user_id}: {ws_utc} to {we_utc}")

#         metrics = get_week_metrics_for_user(user_id, ws_utc, we_utc)
#         logger.info(f"Collected metrics for user {user_id}: responses={len(metrics['responses'])}, tasks_completed={metrics['tasks_completed']}, fact={metrics['fact']}, kpi={metrics['kpi']}, delays={len(metrics['delays'])}")
#         logger.debug(f"Full metrics object for user {user_id}: {metrics}")

#         # Проверка на случай, если нет данных для пересчета
#         if not metrics['responses'] and metrics['fact'] == 0 and metrics['kpi'] == 0:
#              logger.info(f"No activity data found for user {user_id} for week {week_start_local}. Skipping Big5 recalculation or using default values.")
#              # Можно здесь остановить пересчет или использовать дефолтные значения.
#              # Пока продолжим, LLM должна обработать пустые данные.

#         prompt = f"""
# Ты ассистент, оценивающий личностные черты Big5 по активности студента за неделю.

# Данные недели:
# - Ответы на пинги (со статусами): {metrics['responses']}
# - KPI: факт={metrics['fact']} из план={metrics['kpi']}
# - Выполнено заданий: {metrics['tasks_completed']}
# - Задержки между пингами и выполнением (минуты): {metrics['delays']}

# Правила интерпретации:
# 1) "Готов" + выполнено быстро (<60 мин) — рост добросовестности.
# 2) "Не хочу", но задание выполнено — сильный рост добросовестности.
# 3) "Отложу" и выполнено в тот же день — умеренный рост добросовестности.
# 4) KPI выполнен — лёгкий рост всех черт (кроме обратной эмоциональной нестабильности).
# 5) KPI провален — лёгкое снижение добросовестности и открытости.

# Верни JSON:
# {{"extraversion":int,"agreeableness":int,"conscientiousness":int,"emotional_stability":int,"openness":int}}
# Все значения от 1 до 5.
# """
#         logger.debug(f"Sending prompt to LLM for user {user_id}: {prompt}")

#         model = await llm_json_fn(prompt)
#         logger.debug(f"LLM returned for user {user_id}: {model}")

#         # Проверка результата LLM
#         if not isinstance(model, dict):
#             logger.error(f"LLM did not return a dictionary for user {user_id}. Got: {model}")
#             raise ValueError("LLM returned invalid format")

#         # берём предыдущее значение и делаем EMA-смешивание (плавность)
#         prev = get_big5_profile(user_id)
#         prev_info = f"Previous profile: {prev}" if prev else "No previous profile found."
#         logger.debug(f"Applying EMA for user {user_id}. {prev_info}")

#         # Значения по умолчанию 3, если LLM не вернула корректные данные
#         default_value = 3
#         llm_ev = int(model.get("extraversion", default_value))
#         llm_ag = int(model.get("agreeableness", default_value))
#         llm_co = int(model.get("conscientiousness", default_value))
#         llm_es = int(model.get("emotional_stability", default_value))
#         llm_op = int(model.get("openness", default_value))

#         # Ограничиваем значения от 1 до 5
#         def clamp(val): return max(1, min(5, val))
#         llm_ev, llm_ag, llm_co, llm_es, llm_op = map(clamp, [llm_ev, llm_ag, llm_co, llm_es, llm_op])

#         if prev:
#             alpha = 0.65
#             ev = round(alpha * llm_ev + (1 - alpha) * prev['extraversion'])
#             ag = round(alpha * llm_ag + (1 - alpha) * prev['agreeableness'])
#             co = round(alpha * llm_co + (1 - alpha) * prev['conscientiousness'])
#             es = round(alpha * llm_es + (1 - alpha) * prev['emotional_stability'])
#             op = round(alpha * llm_op + (1 - alpha) * prev['openness'])
#             # Clamp again after EMA
#             ev, ag, co, es, op = map(clamp, [ev, ag, co, es, op])
#             logger.debug(f"EMA applied for user {user_id}: LLM({llm_ev},{llm_ag},{llm_co},{llm_es},{llm_op}) -> New({ev},{ag},{co},{es},{op})")
#         else:
#             ev, ag, co, es, op = llm_ev, llm_ag, llm_co, llm_es, llm_op
#             logger.debug(f"No previous profile, using LLM values directly for user {user_id}: ({ev},{ag},{co},{es},{op})")

#         meta_data = json.dumps({
#             "week_start": week_start_local, 
#             "metrics": metrics, 
#             "raw_model_output": model,
#             "used_ema": prev is not None
#         }, ensure_ascii=False) # ensure_ascii для читаемости
        
#         upsert_big5_profile(user_id, ev, ag, co, es, op, meta_data=meta_data)
#         insert_big5_history(user_id, week_start_local, ev, ag, co, es, op, meta_data=meta_data)
#         logger.info(f"Big5 recalculated and saved for user {user_id}: E:{ev}, A:{ag}, C:{co}, ES:{es}, O:{op}")

#     except Exception as e:
#         logger.error(f"Critical error during Big5 recalculation for user {user_id}: {e}", exc_info=True)

# utils.py (или bot.py)

# ... (существующие импорты) ...
# from digital_professor.database import get_fortnight_metrics_for_user, get_big5_profile, upsert_big5_profile, insert_big5_history
# from digital_professor.llm import call_llm_json # Предполагается, что функция существует


# ... (другие функции) ...

async def recalc_big5_for_user_week(user_id: int, tzname: str, week_start_local: str, llm_json_fn):
    """
    Пересчитывает профиль Big5 для пользователя за последние две недели.
    week_start_local: YYYY-MM-DD — понедельник по локальной таймзоне пользователя.
    llm_json_fn(prompt: str) -> dict  # функция вызова LLM, вернёт словарь с ключами шкал Big5.
    """
    logger.info(f"Starting Big5 recalculation for user {user_id}, week starting {week_start_local} (timezone: {tzname})")
    
    try:
        # --- ОКНО: последние 14 дней до текущего момента ---
        tz = pytz.timezone(tzname)
        now_local = datetime.datetime.now(tz)
        start_local = now_local - datetime.timedelta(days=14)
        end_local = now_local

        # Переводим в UTC для SQL
        ws_metrics_utc = start_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        we_metrics_utc = end_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # --- КОНЕЦ ---
        
        # --- НОВОЕ: Сбор метрик за 2 недели ---
        logger.debug(f"Fetching fortnight metrics for user {user_id} from {ws_metrics_utc} to {we_metrics_utc} (UTC)")
        metrics = get_fortnight_metrics_for_user(user_id, ws_metrics_utc, we_metrics_utc)
        # Добавляем Moodle-метрики поверх базовых
        try:
            from digital_professor.moodle_client import MoodleClient
            client = await MoodleClient.from_env()
            async with client as m:
                u = get_user(user_id)
                student_number = (u or {}).get('student_id')
                if student_number:
                    metrics['moodle'] = await m.get_student_moodle_metrics(student_number, ws_metrics_utc, we_metrics_utc)
                else:
                    metrics['moodle'] = {"summary": {"percent_avg_now": 0, "unstarted_now": 0, "started_now": 0, "completed_now": 0, "started_14d": 0, "completed_14d": 0}, "courses": []}
        except Exception as e:
            logger.error(f"Error collecting Moodle metrics for user {user_id}: {e}")
            metrics['moodle'] = {"summary": {"percent_avg_now": 0, "unstarted_now": 0, "started_now": 0, "completed_now": 0, "started_14d": 0, "completed_14d": 0}, "courses": []}
        logger.info(f"Collected fortnight metrics for user {user_id}: responses={len(metrics['responses'])}, rag_on_topic_messages={metrics.get('rag',{}).get('on_topic_messages',0)}, moodle_courses={len(metrics.get('moodle',{}).get('courses',[]))}")
        logger.debug(f"Full metrics object for user {user_id}: {metrics}")
        # --- КОНЕЦ НОВОГО ---

        # --- НОВОЕ: Формирование промпта с новыми метриками ---
        # Получаем имя пользователя для персонализации (если нужно)
        username = "Студент" # Значение по умолчанию
        try:
             # Предполагается, что application доступна глобально или передана
             # chat = await application.bot.get_chat(user_id) 
             # username = chat.first_name or chat.username or "Студент"
             pass # Пока оставим заглушку, можно улучшить позже
        except:
             pass
        
        # Определяем уровень активности для калибровки оценок
        has_responses = len(metrics['responses']) > 0
        has_rag = metrics.get('rag',{}).get('on_topic_messages', 0) > 0
        has_moodle = metrics['moodle']['summary'].get('completed_14d', 0) > 0
        activity_signals = sum([has_responses, has_rag, has_moodle])
        
        # Получаем предыдущий профиль для контекста
        prev = get_big5_profile(user_id)
        prev_values = ""
        if prev:
            prev_values = f"""
## Previous Values
Предыдущие значения студента:
- Extraversion: {prev['extraversion']}/5
- Agreeableness: {prev['agreeableness']}/5  
- Conscientiousness: {prev['conscientiousness']}/5
- Emotional Stability: {prev['emotional_stability']}/5
- Openness: {prev['openness']}/5
"""

        prompt = f"""
## Role
Ты ассистент, оценивающий личностные черты Big5 по активности студента за две недели.

## Input
Данные за период с {start_local.strftime('%Y-%m-%d')} по {end_local.strftime('%Y-%m-%d')} (по часовому поясу {tzname}):
- Ответы на пинги (со статусами): {metrics['responses']}
- Прогресс в Moodle (сводка): {metrics['moodle']['summary']}
- Прогресс по курсам Moodle: {[{k: v for k, v in c.items() if k in ('id','fullname','percent_now','counts_now','counts_14d')} for c in metrics['moodle']['courses']]}
- Последние завершения (Moodle): {[{k: v for k, v in r.items() if k in ('type','name','ts')} for c in metrics['moodle']['courses'] for r in c.get('recent', [])]}
- Взаимодействия с RAG (короткий дайджест on-topic диалогов): {metrics.get('rag',{}).get('conversations', [])}
- Метрики RAG (on-topic): {metrics.get('rag', {})}

{prev_values}

## Metrics Notes
- moodle.summary.percent_avg_now — средний процент завершения по курсам
- counts_now — текущее число активностей (unstarted/started/completed)
- counts_14d — начатые/завершённые за последние 14 дней  
- rag.on_topic_messages — учебные сообщения пользователя
- rag.days_active — дни с учебной активностью в RAG
- rag.conversations — последние учебные диалоги (сокращенные)

## Evaluation Rules
1. При НИЗКОЙ активности (мало данных): 
   - Если активности почти нет — ОБЯЗАТЕЛЬНО снижай conscientiousness и openness на 1-2 пункта
   - Extraversion ДОЛЖЕН снизиться на 1-2 при отсутствии социальной активности
   - Agreeableness может снизиться на 1 при негативных/коротких ответах
   - НЕ держи оценки выше 3 без активности!
   
2. При СРЕДНЕЙ активности:
   - Будь СМЕЛЕЕ в изменениях (±2-3 пункта) — это ВАЖНО!
   - "Готов приступить" → рост conscientiousness (+2) и extraversion (+2)
   - "Не хочу"/"Отложу" → снижение conscientiousness (-2) и extraversion (-1)
   - RAG-активность → ОБЯЗАТЕЛЬНО повышай openness (+2) и agreeableness (+1)
   - Даже 10+ RAG сообщений = рост openness минимум на +2
   
3. При ВЫСОКОЙ активности:
   - Давай МАКСИМАЛЬНЫЕ изменения (до ±3-4 пункта)!
   - Много завершений в Moodle → рост conscientiousness (+3) и openness (+2-3)
   - Частые "Готов приступить" → рост extraversion (+2-3) ОБЯЗАТЕЛЬНО
   - Качественные RAG-диалоги → рост openness (+3) и agreeableness (+2)
   - 30+ RAG сообщений = максимальный рост openness и extraversion
   - Негативные паттерны → резкое снижение (-2-3)

4. КРИТИЧЕСКИ ВАЖНО:
   - НЕ БОЙСЯ больших изменений! Меняй на ±2-3 пункта СМЕЛО
   - Extraversion и Agreeableness ДОЛЖНЫ меняться при наличии сигналов
   - При первом пересчете — используй 3 как базу, но сразу корректируй
   - Emotional stability меняй осторожно (±1), но остальные - СМЕЛО
   - ВСЕГДА давай изменения если есть хоть какая-то активность

Уровень активности за период: {"Низкий" if activity_signals <= 1 else "Средний" if activity_signals == 2 else "Высокий"}

Верни JSON с целыми числами от 1 до 5:
{{"extraversion":int,"agreeableness":int,"conscientiousness":int,"emotional_stability":int,"openness":int}}
"""
        # --- КОНЕЦ НОВОГО ---
        
        logger.debug(f"Sending prompt to LLM for user {user_id} (first 500 chars): {prompt[:500]}...")
        model = await llm_json_fn(prompt) # Предполагается, что llm_json_fn - это async def
        logger.debug(f"LLM returned for user {user_id}: {model}")

        # --- ПРОВЕРКА И ОБРАБОТКА РЕЗУЛЬТАТА LLM ---
        if not isinstance(model, dict):
            logger.error(f"LLM did not return a dictionary for user {user_id}. Got: {model}")
            raise ValueError("LLM returned invalid format")

        # Значения по умолчанию 3, если LLM не вернула корректные данные
        default_value = 3
        llm_ev = int(model.get("extraversion", default_value))
        llm_ag = int(model.get("agreeableness", default_value))
        llm_co = int(model.get("conscientiousness", default_value))
        llm_es = int(model.get("emotional_stability", default_value))
        llm_op = int(model.get("openness", default_value))

        # Ограничиваем значения от 1 до 5
        def clamp(val): return max(1, min(5, val))
        llm_ev, llm_ag, llm_co, llm_es, llm_op = map(clamp, [llm_ev, llm_ag, llm_co, llm_es, llm_op])
        
        # --- КОНЕЦ НОВОГО ---

        # --- ПРИМЕНЕНИЕ EMA С АДАПТИВНЫМИ ВЕСАМИ И ОГРАНИЧЕНИЯМИ ---
        prev = get_big5_profile(user_id)
        
        # СПЕЦИАЛЬНАЯ ОБРАБОТКА ДЛЯ СЛУЧАЯ БЕЗ АКТИВНОСТИ
        if prev:
            has_any_activity = (
                len(metrics['responses']) > 0 or
                metrics.get('rag',{}).get('on_topic_messages', 0) > 0 or
                metrics['moodle']['summary'].get('completed_14d', 0) > 0 or
                metrics['moodle']['summary'].get('started_14d', 0) > 0
            )
            
            if not has_any_activity:
                logger.info(f"No activity detected for user {user_id}, applying moderate penalty")
                # При отсутствии активности применяем умеренное снижение
                # Conscientiousness и Openness снижаются больше (отсутствие учебной активности)
                llm_ev = max(1, prev['extraversion'] - 1)  # Может снизиться на 1
                llm_ag = prev['agreeableness']  # Остается стабильным
                llm_co = max(1, prev['conscientiousness'] - 1)  # Снижается на 1
                llm_es = prev['emotional_stability']  # Остается стабильным
                llm_op = max(1, prev['openness'] - 1)  # Снижается на 1 (нет любознательности)
        prev_info = f"Previous profile: {prev}" if prev else "No previous profile found."
        logger.debug(f"Applying EMA for user {user_id}. {prev_info}")

        if prev:
            # Определяем альфа на основе уровня активности
            # activity_signals уже вычислен выше
            
            # МАКСИМАЛЬНАЯ ЧУВСТВИТЕЛЬНОСТЬ v2
            if activity_signals == 0:
                # Нет активности - минимальный вес новому
                alpha = 0.25  # было 0.2
            elif activity_signals == 1:
                # Слабая активность
                alpha = 0.5  # было 0.4
            elif activity_signals == 2:
                # Средняя активность - КРИТИЧЕСКИ ВАЖНО!
                alpha = 0.70  # было 0.55 - основная проблема была здесь!
            else:
                # Высокая активность (все три сигнала)
                alpha = 0.80  # было 0.65 - максимальный вес новой оценке
            
            logger.debug(f"Activity signals for user {user_id}: {activity_signals}, using alpha={alpha}")
            
            # EMA с округлением до целого
            ev_raw = alpha * llm_ev + (1 - alpha) * prev['extraversion']
            ag_raw = alpha * llm_ag + (1 - alpha) * prev['agreeableness']
            co_raw = alpha * llm_co + (1 - alpha) * prev['conscientiousness']
            es_raw = alpha * llm_es + (1 - alpha) * prev['emotional_stability']
            op_raw = alpha * llm_op + (1 - alpha) * prev['openness']
            
            # АГРЕССИВНОЕ УМНОЕ ОКРУГЛЕНИЕ
            def smart_round(new_val_raw, old_val):
                """Округление с учетом направления изменения - более агрессивное"""
                diff = new_val_raw - old_val
                if abs(diff) > 0.25:  # Снижен порог с 0.3 до 0.25 для большей чувствительности
                    # Округляем в сторону изменения
                    if diff > 0:
                        return min(5, int(new_val_raw + 0.35))  # Более агрессивно вверх
                    else:
                        return max(1, int(new_val_raw + 0.65))  # Более агрессивно вниз
                return round(new_val_raw)
            
            ev = smart_round(ev_raw, prev['extraversion'])
            ag = smart_round(ag_raw, prev['agreeableness'])
            co = smart_round(co_raw, prev['conscientiousness'])
            es = smart_round(es_raw, prev['emotional_stability'])
            op = smart_round(op_raw, prev['openness'])
            
            # МИНИМАЛЬНЫЕ ОГРАНИЧЕНИЯ НА ИЗМЕНЕНИЯ
            # Максимальное доверие изменениям при активности
            if activity_signals == 0:
                max_delta = 1  # Без активности - максимум ±1
            elif activity_signals == 1:
                max_delta = 2  # Слабая активность - до ±2
            elif activity_signals == 2:
                max_delta = 3  # Средняя активность - до ±3
            else:
                max_delta = 4  # Высокая активность - до ±4 (почти без ограничений)
            
            def limit_change(new_val, old_val, max_d):
                """Ограничивает изменение значения"""
                delta = new_val - old_val
                if abs(delta) > max_d:
                    return old_val + (max_d if delta > 0 else -max_d)
                return new_val
            
            ev = limit_change(ev, prev['extraversion'], max_delta)
            ag = limit_change(ag, prev['agreeableness'], max_delta)
            co = limit_change(co, prev['conscientiousness'], max_delta)
            es = limit_change(es, prev['emotional_stability'], min(2, max_delta))  # ES все еще консервативнее
            op = limit_change(op, prev['openness'], max_delta)
            
            # Финальный clamp к границам [1, 5]
            ev, ag, co, es, op = map(clamp, [ev, ag, co, es, op])
            
            logger.debug(f"EMA applied for user {user_id}: LLM({llm_ev},{llm_ag},{llm_co},{llm_es},{llm_op}) -> "
                        f"Raw({ev_raw:.1f},{ag_raw:.1f},{co_raw:.1f},{es_raw:.1f},{op_raw:.1f}) -> "
                        f"Limited({ev},{ag},{co},{es},{op})")
        else:
            # Первый расчет - используем значения LLM напрямую, но с ограничениями
            ev, ag, co, es, op = llm_ev, llm_ag, llm_co, llm_es, llm_op
            logger.debug(f"No previous profile, using LLM values directly for user {user_id}: ({ev},{ag},{co},{es},{op})")
        # --- КОНЕЦ EMA ---

        meta = json.dumps({
            "week_start": week_start_local, 
            "metrics_window_start": start_local.strftime('%Y-%m-%d'),
            "metrics_window_end": end_local.strftime('%Y-%m-%d'),
            "metrics": metrics, 
            "raw_model_output": model,
            "used_ema": prev is not None
        }, ensure_ascii=False)
        
        upsert_big5_profile(user_id, ev, ag, co, es, op, meta_data=meta)
        insert_big5_history(user_id, week_start_local, ev, ag, co, es, op, meta_data=meta)
        logger.info(f"Big5 recalculated and saved for user {user_id}: E:{ev}, A:{ag}, C:{co}, ES:{es}, O:{op}")

    except Exception as e:
        logger.error(f"Error in recalc_big5_for_user_week for user {user_id}: {e}", exc_info=True)
        # ВАЖНО: Не пробрасываем исключение дальше, чтобы не остановить пересчет для других пользователей


def log_ping(user_id: int, ping_type: str, sent_at: str, message_text: str, big5_profile_id: int | None = None, meta_data: dict | None = None):
    """
    Логирует отправленный пинг в таблицу ping_logs.
    """
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO ping_logs (user_id, ping_type, sent_at, message_text, big5_profile_id, meta_data)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, ping_type, sent_at, message_text, big5_profile_id, json.dumps(meta_data, ensure_ascii=False) if meta_data else None))
        conn.commit()
        logger.info(f"Logged ping for user {user_id} (type: {ping_type})")
    except Exception as e:
        logger.error(f"Failed to log ping for user {user_id}: {e}")
    finally:
        conn.close()

        # database.py
def is_user_registered(user_id: int) -> bool:
    """
    Проверяет, зарегистрирован ли пользователь.
    Пользователь считается зарегистрированным, если есть запись в БД с именем
    и номером студенческого билета (поля username и student_id в таблице users).
    """
    conn = get_connection()
    c = conn.cursor()
    res = c.execute("SELECT 1 FROM users WHERE user_id = ? AND username IS NOT NULL AND username != '' AND student_id IS NOT NULL AND student_id != ''", (user_id,)).fetchone()
    conn.close()
    return res is not None

def create_user_stub(user_id: int, ab_group: str = None):
    """
    Создает заготовку пользователя в БД с указанной A/B группой.
    Используется при старте с метаданными для сохранения группы до полной регистрации.
    """
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR IGNORE INTO users (user_id, ab_group) 
            VALUES (?, ?)
        """, (user_id, ab_group))
        conn.commit()
        logger.info(f"Created user stub for {user_id} with group: {ab_group}")
    except Exception as e:
        logger.error(f"Error creating user stub for {user_id}: {e}")
    finally:
        conn.close()

# --- НОВЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С НАСТРОЙКАМИ ПИНГОВ ---

def set_user_ping_schedule(user_id: int, pings_per_week: int, ping_days_list: list[str] | None = None):
    """
    Устанавливает расписание пингов для пользователя.

    Args:
        user_id (int): ID пользователя.
        pings_per_week (int): Количество пингов в неделю (1-7).
                              Если ping_days_list не задан, используется для определения дней.
        ping_days_list (list[str] | None): Список дней недели (например, ['mon', 'wed', 'fri']).
                                           Если None или пуст, подставляются стандартные дни по частоте.
    """
    import json
    conn = get_connection()
    c = conn.cursor()

    # --- 1. Нормализуем значение частоты ---
    pings_per_week = max(1, min(7, pings_per_week))

    # --- 2. Если ping_days_list не передан — определяем его по частоте ---
    if not ping_days_list or len(ping_days_list) == 0:
        if pings_per_week == 1:
            ping_days_list = ['mon']
        elif pings_per_week == 2:
            ping_days_list = ['tue', 'thu']
        elif pings_per_week == 3:
            ping_days_list = ['mon', 'wed', 'fri']
        elif pings_per_week == 5:
            ping_days_list = ['mon', 'tue', 'wed', 'thu', 'fri']
        elif pings_per_week == 7:
            ping_days_list = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        else:
            # fallback на один день, чтобы не остаться без расписания
            ping_days_list = ['mon']

    # --- 3. Валидация и нормализация дней ---
    valid_days = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}
    filtered_days = [day.lower()[:3] for day in ping_days_list if day.lower()[:3] in valid_days]
    ping_days_json = json.dumps(sorted(list(set(filtered_days)))) if filtered_days else None

    # --- 4. Сохранение в БД ---
    try:
        c.execute('''
            INSERT INTO user_ping_settings (user_id, pings_per_week, ping_days)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pings_per_week = excluded.pings_per_week,
                ping_days = excluded.ping_days
        ''', (user_id, pings_per_week, ping_days_json))
        conn.commit()
        logger.info(f"Set ping schedule for user {user_id}: freq={pings_per_week}, days={ping_days_json}")
    except Exception as e:
        logger.error(f"Error setting ping schedule for user {user_id}: {e}", exc_info=True)
    finally:
        conn.close()


def get_user_ping_settings(user_id: int) -> dict:
    """
    Получает настройки пингов для пользователя.
    
    Returns:
        dict: Словарь с ключами:
              'pings_per_week' (int),
              'ping_days' (list или None),
              'last_ping_date' (str или None).
    """
    import json
    conn = get_connection()
    conn.row_factory = sqlite3.Row  # гарантируем доступ по именам колонок
    c = conn.cursor()
    
    try:
        row = c.execute('''
            SELECT pings_per_week, ping_days, last_ping_date
            FROM user_ping_settings
            WHERE user_id = ?
        ''', (user_id,)).fetchone()
        
        if not row:
            # Создаем запись с DEFAULT значениями из схемы БД
            logger.info(f"No ping settings found for user {user_id}, creating with DEFAULT values")
            c.execute("""
                INSERT INTO user_ping_settings (user_id) VALUES (?)
            """, (user_id,))
            conn.commit()
            
            # Получаем созданную запись с DEFAULT значениями
            row = c.execute('''
                SELECT pings_per_week, ping_days, last_ping_date
                FROM user_ping_settings
                WHERE user_id = ?
            ''', (user_id,)).fetchone()
            
            if not row:
                # Fallback если что-то пошло не так
                return {
                    'pings_per_week': 5,
                    'ping_days': ['mon', 'tue', 'wed', 'thu', 'fri'],
                    'last_ping_date': None
                }

        # Приводим Row → dict, чтобы избежать ошибок при .get()
        row_dict = dict(row)

        # Обработка ping_days
        ping_days_list = None
        if row_dict.get('ping_days'):
            try:
                ping_days_list = json.loads(row_dict['ping_days'])
                # Можно фильтровать только будни, если нужно:
                # ping_days_list = [d for d in ping_days_list if d in {'mon','tue','wed','thu','fri'}]
            except json.JSONDecodeError:
                logger.warning(f"Invalid ping_days JSON for user {user_id}: {row_dict['ping_days']}")
                ping_days_list = None

        return {
            'pings_per_week': row_dict.get('pings_per_week', 5),
            'ping_days': ping_days_list,
            'last_ping_date': row_dict.get('last_ping_date')
        }

    except Exception as e:
        logger.error(f"Error getting ping settings for user {user_id}: {e}", exc_info=True)
        return {
            'pings_per_week': 5,
            'ping_days': None,
            'last_ping_date': None
        }

    finally:
        conn.close()

def update_user_last_ping_date(user_id: int, date_str: str):
    """Обновляет дату последнего пинга для пользователя."""
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('''
            UPDATE user_ping_settings
            SET last_ping_date = ?
            WHERE user_id = ?
        ''', (date_str, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating last ping date for user {user_id}: {e}")
    finally:
        conn.close()

# --- Вспомогательные функции для получения данных пользователя ---
# Убедитесь, что эти функции существуют или добавьте их

def get_user_timezone(user_id: int) -> str | None:
    """Возвращает строку часового пояса пользователя."""
    conn = get_connection()
    c = conn.cursor()
    try:
        row = c.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row['timezone'] if row else None
    finally:
        conn.close()

def get_user_ping_time(user_id: int) -> str | None:
    """Возвращает строку времени пинга пользователя (HH:MM)."""
    conn = get_connection()
    c = conn.cursor()
    try:
        row = c.execute("SELECT ping_time FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row['ping_time'] if row else None
    finally:
        conn.close()

def is_user_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    conn = get_connection()
    c = conn.cursor()
    try:
        row = c.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row['is_admin'])
    finally:
        conn.close()

def set_user_ab_group(user_id: int, ab_group: str) -> bool:
    """Устанавливает A/B/C группу пользователю. Возвращает True при успехе."""
    if not ab_group:
        return False
    g = ab_group.strip().lower()
    if g not in {'a','b','c'}:
        logger.warning(f"Invalid ab_group '{ab_group}' for user {user_id}")
        return False
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("UPDATE users SET ab_group=? WHERE user_id=?", (g, user_id))
        if c.rowcount == 0:
            # гарантируем наличие пользователя записи (вставляем только user_id)
            c.execute("INSERT OR IGNORE INTO users(user_id, ab_group) VALUES(?,?)", (user_id, g))
            if c.rowcount == 0:
                # если вставка проигнорирована (есть строка без update?), сделаем ещё раз апдейт
                c.execute("UPDATE users SET ab_group=? WHERE user_id=?", (g, user_id))
        conn.commit()
        logger.info(f"Set AB group for user {user_id} -> {g}")
        return True
    except Exception as e:
        logger.error(f"Error setting ab_group for user {user_id}: {e}")
        return False
    finally:
        conn.close()

def get_user_ab_group(user_id: int) -> str:
    """Возвращает группу пользователя ('a'|'b'|'c'). Если не задано — по умолчанию 'a' (fail-closed)."""
    conn = get_connection()
    c = conn.cursor()
    try:
        row = c.execute("SELECT ab_group FROM users WHERE user_id=?", (user_id,)).fetchone()
        g = row['ab_group'] if row else None
    except Exception:
        g = None
    finally:
        conn.close()
    if not g:
        return 'a'  # дефолт: закрытый доступ (без RAG/адаптации)
    return str(g).strip().lower()

#логи рага
def log_rag_interaction(
    user_id: int,
    user_query: str,
    retrieved_context: str | None,
    llm_response: str | None,
    llm_tokens_used: int | None,
    llm_analysis: str | None = None,
):
    """
    Логирует одно взаимодействие пользователя с RAG.

    Args:
        user_id (int): ID пользователя.
        user_query (str): Вопрос пользователя.
        retrieved_context (str | None): Контекст, извлеченный RAG.
        llm_response (str | None): Ответ, сгенерированный LLM.
        llm_tokens_used (int | None): Количество токенов, использованных LLM.
    """
    conn = get_connection()
    c = conn.cursor()
    try:
        # Совместимость: если колонка llm_analysis ещё не добавлена, пишем по старой схеме
        cols = [r["name"] for r in c.execute("PRAGMA table_info(rag_interaction_logs)").fetchall()]
        if "llm_analysis" in cols:
            c.execute(
                """
                INSERT INTO rag_interaction_logs (user_id, user_query, retrieved_context, llm_response, llm_tokens_used, llm_analysis)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, user_query, retrieved_context, llm_response, llm_tokens_used, llm_analysis),
            )
        else:
            c.execute(
                """
                INSERT INTO rag_interaction_logs (user_id, user_query, retrieved_context, llm_response, llm_tokens_used)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, user_query, retrieved_context, llm_response, llm_tokens_used),
            )
        conn.commit()
        log_id = c.lastrowid
        logger.debug(f"Logged RAG interaction for user {user_id}. Log ID: {log_id}")
        return log_id
    except Exception as e:
        logger.error(f"Failed to log RAG interaction for user {user_id}: {e}")
        return None
    finally:
        conn.close()


# def get_fortnight_metrics_for_user(user_id: int, start_utc: str, end_utc: str) -> dict:
#     """
#     Собирает агрегаты за период [start_utc, end_utc) для пользователя.
#     Используется для пересчета Big5.
    
#     Args:
#         user_id (int): ID пользователя.
#         start_utc (str): Начало окна в формате '%Y-%m-%d %H:%M:%S' (UTC).
#         end_utc (str): Конец окна в формате '%Y-%m-%d %H:%M:%S' (UTC).
        
#     Returns:
#         dict: Словарь с метриками.
#     """
#     logger.debug(f"Collecting fortnight metrics for user {user_id} from {start_utc} to {end_utc}")
#     conn = get_connection()
#     c = conn.cursor()

#     try:
#         # 1. Все ответы на пинги в окне
#         responses = c.execute("""
#             SELECT id, status, timestamp, task_id, meta_data
#               FROM responses
#              WHERE user_id=? AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)
#              ORDER BY datetime(timestamp) ASC
#         """, (user_id, start_utc, end_utc)).fetchall()

#         # 2. Все факты по всем курсам за период запрашиваем напрямую из результатов выполненных заданий 
#         task_results_in_period = c.execute("""
#             SELECT task_id, score, submitted_at
#               FROM task_results
#              WHERE user_id=? AND datetime(submitted_at) >= datetime(?) AND datetime(submitted_at) < datetime(?)
#         """, (user_id, start_utc, end_utc)).fetchall()
        
#         fact = len([tr for tr in task_results_in_period if tr['score'] is not None and tr['score'] >= 1]) # Кол-во сданных
#         # kpi = общее количество заданий, которые были назначены и должны были быть сданы в этот период.
#         # Пока используем кол-во task_results как приблизительный kpi.
#         kpi = len(task_results_in_period) 
        
#         tasks_completed = fact # Для совместимости со старым кодом

#         # 3. Задержки: response -> выполненная задача (если привязана)
#         delays = []
#         for r in responses:
#             if r['task_id']:
#                 # Ищем соответствующий task_result
#                 tr = c.execute("""
#                     SELECT submitted_at FROM task_results
#                      WHERE task_id=? AND user_id=? AND datetime(submitted_at) >= datetime(?)
#                      ORDER BY datetime(submitted_at) DESC LIMIT 1
#                 """, (r['task_id'], user_id, r['timestamp'])).fetchone() # submitted_at >= response.timestamp
#                 if tr:
#                     try:
#                         dt_ping = _parse_iso(r['timestamp'])
#                         dt_task = _parse_iso(tr['submitted_at'])
#                         delay_min = max(0, int((dt_task - dt_ping).total_seconds() // 60))
#                         delays.append({"status": r['status'], "delay_min": delay_min})
#                     except Exception as e:
#                         logger.warning(f"Error parsing dates for delay calc (user {user_id}, resp {r['id']}): {e}")

#         # 4. Взаимодействия с RAG за период
#         rag_interactions = c.execute("""
#             SELECT id, user_query, llm_response, timestamp
#               FROM rag_interaction_logs
#              WHERE user_id=? AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)
#              ORDER BY datetime(timestamp) ASC
#         """, (user_id, start_utc, end_utc)).fetchall()
        
#         # 5. Результаты заданий за период (уже получены в task_results_in_period)
        
#         conn.close()

#         metrics = {
#             "responses": [{"status": r["status"], "ts": r["timestamp"], "task_id": r["task_id"]} for r in responses],
#             "tasks_completed": tasks_completed,
#             "fact": fact,
#             "kpi": kpi,
#             "delays": delays,
#             "rag_interactions": [{"query": ri["user_query"][:100], "response": ri["llm_response"][:100], "ts": ri["timestamp"]} for ri in rag_interactions], # Обрезаем для компактности
#             "task_results": [{"task_id": tr["task_id"], "score": tr["score"], "ts": tr["submitted_at"]} for tr in task_results_in_period]
#         }
        
#         logger.debug(f"Collected fortnight metrics for user {user_id}: responses={len(metrics['responses'])}, tasks_completed={metrics['fact']}, kpi={metrics['kpi']}, delays={len(metrics['delays'])}, rag_interactions={len(metrics['rag_interactions'])}, task_results={len(metrics['task_results'])}")
        
#         return metrics

#     except Exception as e:
#         logger.error(f"Error collecting fortnight metrics for user {user_id}: {e}", exc_info=True)
#         conn.close()
#         # Возвращаем пустые метрики в случае ошибки
#         return {
#             "responses": [],
#             "tasks_completed": 0,
#             "fact": 0,
#             "kpi": 0,
#             "delays": [],
#             "rag_interactions": [],
#             "task_results": []
#         }
def get_fortnight_metrics_for_user(user_id: int, start_utc: str, end_utc: str) -> dict:
    """
    Собирает агрегаты за период [start_utc, end_utc) для пользователя.
    Используется для пересчета Big5.
    
    Args:
        user_id (int): ID пользователя.
        start_utc (str): Начало окна в формате '%Y-%m-%d %H:%M:%S' (UTC).
        end_utc (str): Конец окна в формате '%Y-%m-%d %H:%M:%S' (UTC).
        
    Returns:
        dict: Словарь с метриками.
    """
    logger.debug(f"Collecting fortnight metrics for user {user_id} from {start_utc} to {end_utc}")
    conn = get_connection()
    c = conn.cursor()

    try:
        # 1. Все ответы на пинги в окне
        responses = c.execute("""
            SELECT id, status, timestamp, task_id, meta_data
              FROM responses
             WHERE user_id=? AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)
             ORDER BY datetime(timestamp) ASC
        """, (user_id, start_utc, end_utc)).fetchall()
        # 2. УБИРАЕМ синтетические метрики: task_results/fact/kpi/delays

        delays = []

        # 3. Взаимодействия с RAG за период (новый способ — сессии)
        sessions = c.execute("""
            SELECT DISTINCT session_id
              FROM rag_dialogue_history
             WHERE user_id=? AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)
        """, (user_id, start_utc, end_utc)).fetchall()
        
        # Конфиги для дайджеста
        DIGEST_SESSIONS = 3  # K
        DIGEST_MESSAGES_PER_SESSION = 10  # M
        MAX_USER_CHARS = 120
        MAX_ASSIST_CHARS = 100

        on_topic_messages = 0
        days_active_set = set()
        on_topic_sessions_data = []  # для сортировки и дайджеста

        for session_row in sessions:
            session_id = session_row['session_id']
            rows = c.execute("""
                SELECT message_role, message_text, timestamp
                  FROM rag_dialogue_history
                 WHERE user_id=? AND session_id=?
                 ORDER BY datetime(timestamp) ASC
            """, (user_id, session_id)).fetchall()

            if not rows:
                continue

            # Определяем окно сессии
            ts_start = rows[0]['timestamp']
            ts_end = rows[-1]['timestamp']

            # Проверяем on-topic по retrieved_context в пределах сессии
            ri = c.execute("""
                SELECT 1 FROM rag_interaction_logs
                 WHERE user_id=?
                   AND datetime(timestamp) >= datetime(?)
                   AND datetime(timestamp) <= datetime(?)
                   AND retrieved_context IS NOT NULL
                 LIMIT 1
            """, (user_id, ts_start, ts_end)).fetchone()
            session_on_topic = bool(ri)

            # Если on-topic — учитываем метрики и готовим дайджест
            if session_on_topic:
                # Считаем on_topic_messages как число user-сообщений в сессии
                for r in rows:
                    if r['message_role'] == 'user':
                        on_topic_messages += 1
                        try:
                            days_active_set.add((r['timestamp'] or '')[:10])
                        except Exception:
                            pass

                # Формируем дайджест сообщений
                digest_msgs = []
                skipped = max(0, len(rows) - DIGEST_MESSAGES_PER_SESSION)
                for r in rows[:DIGEST_MESSAGES_PER_SESSION]:
                    text = (r['message_text'] or '')
                    if r['message_role'] == 'user':
                        if len(text) > MAX_USER_CHARS:
                            text = text[:MAX_USER_CHARS] + '…'
                    else:  # assistant
                        if len(text) > MAX_ASSIST_CHARS:
                            text = text[:MAX_ASSIST_CHARS] + '…'
                    digest_msgs.append({"role": r['message_role'], "text": text})

                on_topic_sessions_data.append({
                    "ts_end": ts_end,
                    "depth": len(rows),
                    "messages": digest_msgs,
                    "skipped_in_session": skipped,
                })

        conn.close()

        # Сортируем on-topic сессии по времени окончания, берём последние DIGEST_SESSIONS
        on_topic_sessions_sorted = sorted(on_topic_sessions_data, key=lambda s: s["ts_end"])  # asc
        on_topic_sessions_sorted = on_topic_sessions_sorted[-DIGEST_SESSIONS:]

        metrics = {
            "responses": [{"status": r["status"], "ts": r["timestamp"], "task_id": r["task_id"]} for r in responses],
            # синтетика удалена
            "delays": delays,
            "rag": {
                "on_topic_messages": on_topic_messages,
                "days_active": len(days_active_set),
                "conversations": [
                    {
                        "messages": s["messages"],
                        "skipped_in_session": s["skipped_in_session"],
                    } for s in on_topic_sessions_sorted
                ],
            },
            # moodle будет добавлен в вызывающем коде (в recalc_big5_for_user_week)
        }
        
        logger.debug(
            f"Collected fortnight metrics for user {user_id}: "
            f"responses={len(metrics['responses'])}, delays={len(metrics['delays'])}, "
            f"rag_on_topic_messages={metrics.get('rag',{}).get('on_topic_messages',0)}"
        )
        
        return metrics

    except Exception as e:
        logger.error(f"Error collecting fortnight metrics for user {user_id}: {e}", exc_info=True)
        conn.close()
        return {
            "responses": [],
            "delays": [],
            "rag": {"on_topic_messages": 0, "days_active": 0, "conversations": []},
        }


def is_valid_student_id(student_id: str) -> bool:
    """
    Проверяет, является ли строка допустимым номером студенческого билета.
    Допустимый формат: от 6 до 12 цифр.
    
    Args:
        student_id (str): Строка для проверки.
        
    Returns:
        bool: True, если строка соответствует формату, иначе False.
    """
    if not student_id:
        return False
    # Проверяем, что строка состоит только из цифр и имеет длину от 6 до 12
    return bool(re.fullmatch(r'\d{6,12}', student_id))
# --- КОНЕЦ НОВОГО ---

def log_rag_dialogue_message(user_id: int, session_id: str, role: str, text: str):
    """
    Логирует одно сообщение в истории RAG-диалога.
    
    Args:
        user_id (int): ID пользователя.
        session_id (str): Уникальный ID сессии диалога.
        role (str): Роль отправителя ('user' или 'assistant').
        text (str): Текст сообщения.
    """
    if role not in ['user', 'assistant']:
        logger.warning(f"Invalid role '{role}' for RAG dialogue message. Must be 'user' or 'assistant'. Skipping log.")
        return
        
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO rag_dialogue_history (user_id, session_id, message_role, message_text, timestamp)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, session_id, role, text))
        conn.commit()
        logger.debug(f"Logged RAG dialogue message for user {user_id}, session {session_id[:8]}: {role}: {text[:50]}...")
    except Exception as e:
        logger.error(f"Failed to log RAG dialogue message for user {user_id}, session {session_id[:8]}: {e}")
    finally:
        conn.close()

def add_llm_token_usage(
    event: str,
    model: str,
    total_tokens: int,
    input_tokens: int = None,
    output_tokens: int = None,
    user_id: int = None,
    provider: str = None
):
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO llm_token_usage (
                event, model, provider, input_tokens, output_tokens, total_tokens, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (event, model, provider, input_tokens, output_tokens, total_tokens, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to log LLM token usage: {e}")
    finally:
        conn.close()


# ==================== СИСТЕМА ФИДБЕКА ====================

def migrate_add_feedback_to_rag_logs():
    """
    Добавляет колонку user_feedback в rag_interaction_logs при необходимости.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(rag_interaction_logs)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'user_feedback' not in columns:
            cursor.execute('ALTER TABLE rag_interaction_logs ADD COLUMN user_feedback INTEGER')
            conn.commit()
            logger.info("Added user_feedback column to rag_interaction_logs")
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to ensure user_feedback column in rag_interaction_logs: {e}")
        return False
    finally:
        conn.close()


def drop_legacy_message_history():
    """
    Удаляет устаревшую таблицу message_history, если она существует.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS message_history")
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to drop legacy message_history table: {e}")
    finally:
        conn.close()


def update_rag_interaction_feedback(log_id: int, user_feedback: int | None) -> bool:
    """
    Обновляет колонку user_feedback для записи rag_interaction_logs.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE rag_interaction_logs SET user_feedback = ? WHERE id = ?",
            (user_feedback, log_id)
        )
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.debug(f"Updated feedback for rag_interaction_logs ID {log_id} -> {user_feedback}")
        else:
            logger.warning(f"rag_interaction_logs ID {log_id} not found for feedback update")
        return updated
    except Exception as e:
        logger.error(f"Failed to update feedback for rag_interaction_logs ID {log_id}: {e}")
        return False
    finally:
        conn.close()


def get_rag_interaction(log_id: int) -> dict | None:
    """
    Возвращает запись rag_interaction_logs по ID.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rag_interaction_logs WHERE id = ?", (log_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_feedback_stats(user_id: int | None = None) -> dict:
    """
    Возвращает статистику фидбека по rag_interaction_logs.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute('''
                SELECT 
                    COUNT(*) AS total_messages,
                    SUM(CASE WHEN user_feedback = 1 THEN 1 ELSE 0 END) AS likes,
                    SUM(CASE WHEN user_feedback = -1 THEN 1 ELSE 0 END) AS dislikes,
                    SUM(CASE WHEN user_feedback IS NOT NULL THEN 1 ELSE 0 END) AS total_feedback
                FROM rag_interaction_logs
                WHERE user_feedback IS NOT NULL
            ''')
        else:
            cursor.execute('''
                SELECT 
                    COUNT(*) AS total_messages,
                    SUM(CASE WHEN user_feedback = 1 THEN 1 ELSE 0 END) AS likes,
                    SUM(CASE WHEN user_feedback = -1 THEN 1 ELSE 0 END) AS dislikes,
                    SUM(CASE WHEN user_feedback IS NOT NULL THEN 1 ELSE 0 END) AS total_feedback
                FROM rag_interaction_logs
                WHERE user_feedback IS NOT NULL AND user_id = ?
            ''', (user_id,))
        row = cursor.fetchone()
        if row:
            return {
                'total_messages': row['total_messages'] or 0,
                'likes': row['likes'] or 0,
                'dislikes': row['dislikes'] or 0,
                'total_feedback': row['total_feedback'] or 0,
            }
        return {
            'total_messages': 0,
            'likes': 0,
            'dislikes': 0,
            'total_feedback': 0,
        }
    finally:
        conn.close()


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С ЕЖЕНЕДЕЛЬНЫМИ ОПРОСАМИ ---

def save_weekly_ping(user_id: int, student_id: str | None, username: str | None, 
                     answer1: str, answer2: str):
    """
    Сохраняет ответы пользователя на еженедельный опрос.
    
    Args:
        user_id: ID пользователя
        student_id: Номер студенческого билета
        username: Имя пользователя
        answer1: Ответ на первый вопрос (да/нет)
        answer2: Ответ на второй вопрос (да/нет или название темы)
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            INSERT INTO weekly_pings (user_id, student_id, username, answer1, answer2, timestamp)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, student_id, username, answer1, answer2))
        conn.commit()
        logger.info(f"Saved weekly ping response for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving weekly ping for user {user_id}: {e}")
        raise
    finally:
        conn.close()


def get_weekly_pings_by_user(user_id: int):
    """
    Получает все еженедельные опросы пользователя.
    
    Args:
        user_id: ID пользователя
        
    Returns:
        Список словарей с данными опросов
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            SELECT * FROM weekly_pings
            WHERE user_id = ?
            ORDER BY timestamp DESC
        ''', (user_id,))
        rows = c.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def has_weekly_ping_this_week(user_id: int) -> bool:
    """
    Проверяет, был ли уже отправлен еженедельный опрос пользователю на этой неделе.
    
    Args:
        user_id: ID пользователя
        
    Returns:
        True, если опрос уже был отправлен на этой неделе
    """
    from datetime import datetime
    import pytz
    
    conn = get_connection()
    try:
        # Получаем часовой пояс пользователя
        user = get_user(user_id)
        if not user or not user.get('timezone'):
            return False
        
        user_tz = pytz.timezone(user['timezone'])
        now = datetime.now(user_tz)
        week_start = week_start_for(now, user['timezone'])
        
        # Проверяем, есть ли запись на этой неделе
        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) as count FROM weekly_pings
            WHERE user_id = ? AND timestamp >= ?
        ''', (user_id, week_start))
        row = c.fetchone()
        return (row['count'] or 0) > 0
    finally:
        conn.close()
