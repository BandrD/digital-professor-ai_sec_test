import os
import re
import datetime
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv


class MoodleClient:
    """
    Асинхронный клиент для Moodle Web Services API.

    Цели/возможности:
    - По номеру студенческого (custom profile field `Student_number`) найти Moodle user_id.
    - Получить список курсов пользователя.
    - Рассчитать процент завершения по курсам (по заданиям и тестам).
    - Найти следующее невыполненное задание (assign/quiz) и дать прямую ссылку.

    Зависимости от окружения (подхватываются автоматически):
    - MOODLE_URL — базовый URL Moodle (например, https://elearn.urfu.ru)
    - MOODLE_TOKEN — токен доступа к Moodle Web Services API
    - MOODLE_PRIMARY_COURSE_ID или COURSE_ID — ID курса, в котором точно есть все участники (для поиска user_id по Student_number)
    - STUDENT_NUMBER_FIELD (опционально) — имя custom-поля профиля (по умолчанию `Student_number`, запасной вариант `student_number`).
    """

    def __init__(self, base_url: str, token: str, primary_course_id: Optional[int] = None, student_number_field: Optional[str] = None, completion_mode: str = "status"):
        # Удаляем завершающий '/'
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.api_endpoint = f"{self.base_url}/webservice/rest/server.php"
        self.primary_course_id = primary_course_id
        self.student_number_field = student_number_field or "Student_number"
        # Режим подсчёта процента завершения:
        #  - 'status' (по статусам подач/попыток) — по умолчанию
        #  - 'grade'  (по оценкам)
        #  - 'native' (по completion API)
        self.completion_mode = (completion_mode or "status").strip().lower()
        # HTTP-клиент создаём в __aenter__
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        # Создаём HTTP-клиент один раз на сессию
        # Увеличиваем таймаут для медленных запросов к Moodle API
        timeout = httpx.Timeout(60.0, connect=10.0)  # 60 сек на чтение, 10 сек на подключение
        self._client = httpx.AsyncClient(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    @classmethod
    async def from_env(cls) -> "MoodleClient":
        """
        Создаёт клиент, читая переменные окружения. В качестве удобства
        дополнительно пробует загрузить ../config.txt, чтобы не дублировать конфиг.
        """
        # Поддержка локального config.txt (как в moodle-adapter)
        project_root = Path(__file__).resolve().parents[2]
        config_txt = project_root / "config.txt"
        if os.path.exists(config_txt):
            load_dotenv(config_txt)
        load_dotenv(project_root / ".env")

        base_url = os.getenv("MOODLE_URL", "https://elearn.urfu.ru")
        # Новая схема: MOODLE_TOKEN_1/COURSE_ID_1; поддерживаем старые как фолбэк
        token = os.getenv("MOODLE_TOKEN_1") or os.getenv("MOODLE_TOKEN")
        primary_course_id = os.getenv("MOODLE_PRIMARY_COURSE_ID") or os.getenv("COURSE_ID_1") or os.getenv("COURSE_ID")
        student_number_field = os.getenv("STUDENT_NUMBER_FIELD", "Student_number")
        try:
            primary_course_id_int = int(primary_course_id) if primary_course_id else None
        except ValueError:
            primary_course_id_int = None

        if not token:
            raise RuntimeError("MOODLE_TOKEN не задан. Укажите его в .env или config.txt")
        completion_mode = os.getenv("MOODLE_COMPLETION_MODE", "status")
        return cls(
            base_url=base_url,
            token=token,
            primary_course_id=primary_course_id_int,
            student_number_field=student_number_field,
            completion_mode=completion_mode,
        )

    # -------------------- НИЗКОУРОВНЕВЫЕ ВЫЗОВЫ API --------------------
    async def _call(self, function: str, **params) -> Any:
        """
        Базовый вызов Moodle WS. Возвращает JSON-результат или поднимает исключение при ошибке.
        Включает retry логику для таймаутов и временных сетевых ошибок.
        """
        if not self._client:
            # Защита от неправильного использования без async with
            timeout = httpx.Timeout(60.0, connect=10.0)  # 60 сек на чтение, 10 сек на подключение
            self._client = httpx.AsyncClient(timeout=timeout)
        
        payload = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        payload.update(params)
        
        # Retry логика для таймаутов и временных сетевых ошибок
        max_retries = 3
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                resp = await self._client.post(self.api_endpoint, data=payload)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("exception"):
                    # Превращаем в понятную ошибку
                    raise RuntimeError(f"Moodle API error in {function}: {data.get('message')}")
                return data
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException) as e:
                last_exception = e
                if attempt < max_retries - 1:
                    # Экспоненциальная задержка: 2, 4, 8 секунд
                    wait_time = 2 ** (attempt + 1)
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Последняя попытка не удалась
                    raise
            except Exception as e:
                # Для других ошибок не делаем retry
                raise
        
        # Не должно сюда дойти, но на всякий случай
        if last_exception:
            raise last_exception

    # -------------------- ОБЁРТКИ НАД ФУНКЦИЯМИ WS --------------------
    async def get_enrolled_users(self, course_id: int) -> List[Dict[str, Any]]:
        return await self._call("core_enrol_get_enrolled_users", courseid=course_id)

    async def get_user_courses(self, user_id: int) -> List[Dict[str, Any]]:
        # Возвращает список курсов пользователя. Требует прав у токена.
        return await self._call("core_enrol_get_users_courses", userid=user_id)

    async def get_user_grade_items(self, course_id: int, user_id: int) -> Dict[str, Any]:
        return await self._call("gradereport_user_get_grade_items", courseid=course_id, userid=user_id)

    async def get_course_assignments(self, course_id: int) -> Dict[str, Any]:
        return await self._call("mod_assign_get_assignments", **{"courseids[0]": course_id})

    async def get_course_quizzes(self, course_id: int) -> Dict[str, Any]:
        return await self._call("mod_quiz_get_quizzes_by_courses", **{"courseids[0]": course_id})

    async def get_activities_completion_status(self, course_id: int, user_id: int) -> Dict[str, Any]:
        """
        Нативный API для статусов завершения активностей (если разрешено политикой Moodle и токеном).
        Возвращает JSON с ключом 'statuses' (список), элементы которого содержат
        как минимум поля 'cmid', 'modname' и 'state'.
        """
        return await self._call("core_completion_get_activities_completion_status", courseid=course_id, userid=user_id)

    async def get_course_contents(self, course_id: int) -> List[Dict[str, Any]]:
        """
        Возвращает структуру курса по секциям в естественном порядке отображения.
        Каждый модуль содержит ключи: id (cmid), name, modname ('assign'|'quiz'|...), instance (id экземпляра).
        """
        data = await self._call("core_course_get_contents", courseid=course_id)
        return data if isinstance(data, list) else []

    async def get_assign_submissions_by_ids(self, assignment_ids: List[int]) -> Dict[str, Any]:
        """
        Возвращает подачу(и) по списку assignment ids (mod_assign_get_submissions).
        Формат ответа Moodle может отличаться:
          - { "assignments": [ { "assignmentid": <id>, "submissions": [...] }, ... ] }
          - { "courses": [ { "assignments": [ { "id": <id>, "submissions": [...] }, ... ] } ] }
        """
        params: Dict[str, Any] = {}
        for idx, aid in enumerate(assignment_ids or []):
            params[f"assignmentids[{idx}]"] = aid
        return await self._call("mod_assign_get_submissions", **params)

    async def get_quiz_user_attempts(self, quiz_id: int, user_id: int, status: str = "all") -> Dict[str, Any]:
        """
        Возвращает попытки пользователя по конкретному тесту (mod_quiz_get_user_attempts).
        """
        return await self._call("mod_quiz_get_user_attempts", quizid=quiz_id, userid=user_id, status=status)

    # -------------------- ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ --------------------
    @staticmethod
    def _to_cf_map(cf: Any) -> Dict[str, Any]:
        """Преобразует customfields в словарь {shortname: value}."""
        if isinstance(cf, dict):
            return cf
        if isinstance(cf, list):
            out: Dict[str, Any] = {}
            for item in cf:
                if isinstance(item, dict) and item.get("shortname"):
                    out[item["shortname"]] = item.get("value")
            return out
        return {}

    @staticmethod
    def _parse_grade_value(grade_value: Any) -> Optional[float]:
        """
        Преобразует формат Moodle gradeformatted в float, если возможно.
        Возвращает None, если значения нет или оно непригодно.
        """
        if grade_value is None:
            return None
        s = str(grade_value).strip()
        if s in ("", "-", "Нет"):
            return None
        try:
            return float(s.replace(",", "."))
        except Exception:
            return None

    @staticmethod
    def _build_assign_url(base_url: str, a: Dict[str, Any]) -> str:
        cmid = a.get("cmid")
        return f"{base_url}/mod/assign/view.php?id={cmid or a.get('id')}"

    @staticmethod
    def _build_quiz_url(base_url: str, q: Dict[str, Any]) -> str:
        cmid = q.get("coursemodule")
        return f"{base_url}/mod/quiz/view.php?id={cmid or q.get('id')}"

    # -------------------- ВЫСОКОУРОВНЕВЫЕ ОПЕРАЦИИ --------------------
    async def _find_user_id_in_course(self, course_id: int, student_number: str) -> Optional[int]:
        """
        Находит Moodle user_id, просматривая участников в указанном курсе и сравнивая customfield.
        Возвращает None, если нет доступа к курсу или пользователь не найден.
        """
        try:
            users = await self.get_enrolled_users(course_id)
        except RuntimeError as e:
            # Обрабатываем ошибки доступа к курсу (токен не имеет прав)
            error_msg = str(e)
            if "контексте курса" in error_msg or "не доступны" in error_msg or "access" in error_msg.lower():
                # Токен не имеет доступа к этому курсу - это нормально, просто возвращаем None
                return None
            # Другие ошибки пробрасываем дальше
            raise
        
        wanted_keys = [self.student_number_field, self.student_number_field.lower()]
        for u in users:
            cf_map = self._to_cf_map(u.get("customfields"))
            sn = None
            for k in wanted_keys:
                if k in cf_map:
                    sn = cf_map.get(k)
                    break
            if sn and str(sn).strip() == str(student_number).strip():
                return int(u.get("id"))
        return None

    async def get_moodle_user_id_by_student_number(self, student_number: str) -> Optional[int]:
        """
        Находит Moodle user_id, просматривая участников в `primary_course_id` и сравнивая customfield.
        Этот способ надёжно работает, если все нужные студенты есть в указанном курсе.
        """
        if not self.primary_course_id:
            return None
        return await self._find_user_id_in_course(self.primary_course_id, student_number)

    @staticmethod
    def _extract_gradeitems(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(result, dict):
            return []
        if isinstance(result.get("gradeitems"), list):
            return result.get("gradeitems") or []
        ug = result.get("usergrades")
        if isinstance(ug, list) and ug:
            items: List[Dict[str, Any]] = []
            for entry in ug:
                gi = entry.get("gradeitems") if isinstance(entry, dict) else None
                if isinstance(gi, list):
                    items.extend(gi)
            return items
        return []

    async def compute_course_completion_from_gradeitems(self, course_id: int, user_id: int) -> Tuple[int, List[str]]:
        """
        Считает процент завершения курса по грейдбуку, учитывая только задания и тесты.
        Возвращает (percent_complete, unfinished_names), где unfinished_names — имена активностей без оценки или с 0/отсутствующей оценкой.
        """
        gi = await self.get_user_grade_items(course_id, user_id)
        items = self._extract_gradeitems(gi)
        module_items = [it for it in items if it.get("itemmodule") in ("assign", "quiz")]
        total = len(module_items)
        if total == 0:
            return 0, []
        finished = 0
        unfinished_names: List[str] = []
        for it in module_items:
            name = it.get("itemname") or ""
            gv = self._parse_grade_value(it.get("gradeformatted"))
            if gv is not None and gv > 0.0:
                finished += 1
            else:
                unfinished_names.append(name)
        percent = int(round(finished * 100.0 / total))
        return percent, unfinished_names

    async def compute_course_completion_natively(self, course_id: int, user_id: int) -> Tuple[int, List[int]]:
        """
        Подсчёт процента завершения через native completion API.
        Учитываем только активности типов assign/quiz для согласованности с подходом по оценкам.
        Возвращает (percent_complete, incomplete_cmids).
        """
        try:
            data = await self.get_activities_completion_status(course_id, user_id)
        except Exception:
            # Если API не доступен — считаем 0 и пустой список незавершённых
            return 0, []
        statuses = data.get("statuses", []) if isinstance(data, dict) else []
        # Фильтруем только assign/quiz
        filtered = [s for s in statuses if s.get("modname") in ("assign", "quiz")]
        total = len(filtered)
        if total == 0:
            return 0, []
        completed = 0
        incomplete_cmids: List[int] = []
        for s in filtered:
            cmid = int(s.get("cmid")) if s.get("cmid") is not None else None
            # state: 1 (complete), 2 (complete pass) — считаем завершенными; 0/None — нет
            state = s.get("state")
            try:
                is_completed = int(state) >= 1
            except Exception:
                is_completed = False
            if is_completed:
                completed += 1
            else:
                if cmid:
                    incomplete_cmids.append(cmid)
        percent = int(round(completed * 100.0 / total))
        return percent, incomplete_cmids

    async def compute_course_completion_by_status(self, course_id: int, user_id: int) -> int:
        """
        Считает процент завершения по статусам подач (assign) и попыток (quiz), как есть:
          - assign: пусто или "new|notgraded" → не начато; "submitted|notgraded"/"submitted|graded" → выполнено
          - quiz:  отсутствуют попытки → не начато; "finished|<балл>" → выполнено ("inprogress/overdue/abandoned" → не выполнено)
        Возвращает целый процент [0..100].
        """
        # Собираем активности курса
        try:
            assigns_data = await self.get_course_assignments(course_id)
        except Exception:
            assigns_data = {}
        try:
            quizzes_data = await self.get_course_quizzes(course_id)
        except Exception:
            quizzes_data = {}

        assigns: List[Dict[str, Any]] = []
        quizzes: List[Dict[str, Any]] = []
        if isinstance(assigns_data, dict) and assigns_data.get("courses"):
            assigns = (assigns_data["courses"][0].get("assignments") or [])
        if isinstance(quizzes_data, dict) and quizzes_data.get("quizzes"):
            quizzes = (quizzes_data.get("quizzes") or [])

        assign_ids: List[int] = []
        for a in assigns:
            try:
                aid = int(a.get("id")) if a.get("id") is not None else None
            except Exception:
                aid = None
            if aid is not None:
                assign_ids.append(aid)

        # Забираем подачу(и) по списку assign ids
        submissions_by_assign: Dict[int, List[Dict[str, Any]]] = {}
        if assign_ids:
            try:
                subs_res = await self.get_assign_submissions_by_ids(assign_ids)
            except Exception:
                subs_res = {}
            # Разные формы ответа
            if isinstance(subs_res, dict) and isinstance(subs_res.get("assignments"), list):
                for item in subs_res.get("assignments") or []:
                    aid = item.get("assignmentid", item.get("id"))
                    try:
                        aid_int = int(aid) if aid is not None else None
                    except Exception:
                        aid_int = None
                    if aid_int is None:
                        continue
                    submissions_by_assign[aid_int] = item.get("submissions") or []
            elif isinstance(subs_res, dict) and isinstance(subs_res.get("courses"), list):
                for course_entry in subs_res.get("courses") or []:
                    for a in (course_entry.get("assignments") or []):
                        aid = a.get("id")
                        try:
                            aid_int = int(aid) if aid is not None else None
                        except Exception:
                            aid_int = None
                        if aid_int is None:
                            continue
                        submissions_by_assign[aid_int] = a.get("submissions") or []

        total = 0
        completed = 0

        # Оцениваем assign по подачам
        for a in assigns:
            try:
                aid = int(a.get("id")) if a.get("id") is not None else None
            except Exception:
                aid = None
            if aid is None:
                continue
            total += 1
            user_sub = None
            for s in submissions_by_assign.get(aid) or []:
                try:
                    if int(s.get("userid", -1)) == int(user_id):
                        user_sub = s
                        break
                except Exception:
                    continue
            if user_sub:
                status = (user_sub.get("status") or "").strip().lower()  # new|draft|submitted|reopened
                gradingstatus = (user_sub.get("gradingstatus") or "").strip().lower()  # notgraded|graded|inreview|...
                # Выполнено только когда отправлено (submitted), независимо от проверки
                if status == "submitted":
                    completed += 1
            else:
                # пусто => не начато
                pass

        # Оцениваем quiz по попыткам
        for q in quizzes:
            try:
                qid = int(q.get("id")) if q.get("id") is not None else None
            except Exception:
                qid = None
            if qid is None:
                continue
            total += 1
            latest = None
            try:
                attempts = await self.get_quiz_user_attempts(qid, int(user_id))
                atts = attempts.get("attempts") or [] if isinstance(attempts, dict) else []
                for a in atts:
                    try:
                        if latest is None or int(a.get("attempt", 0)) > int(latest.get("attempt", 0)):
                            latest = a
                    except Exception:
                        continue
            except Exception:
                latest = None
            if latest:
                state = (latest.get("state") or "").strip().lower()  # inprogress|finished|overdue|abandoned
                if state == "finished":
                    completed += 1

        if total == 0:
            return 0
        return int(round(completed * 100.0 / total))

    async def _collect_unstarted_sets(self, course_id: int, user_id: int) -> Tuple[set, set]:
        """
        Возвращает множества instance-id незапущенных активностей:
          - assign: нет подачи у пользователя или статус 'new'
          - quiz:   нет попыток у пользователя
        """
        # Assigns list
        assigns: List[Dict[str, Any]] = []
        try:
            assigns_data = await self.get_course_assignments(course_id)
            if isinstance(assigns_data, dict) and assigns_data.get("courses"):
                assigns = (assigns_data["courses"][0].get("assignments") or [])
        except Exception:
            assigns = []

        # Build assign ids
        assign_ids: List[int] = []
        for a in assigns:
            try:
                aid = int(a.get("id")) if a.get("id") is not None else None
            except Exception:
                aid = None
            if aid is not None:
                assign_ids.append(aid)

        # Fetch submissions for assigns
        submissions_by_assign: Dict[int, List[Dict[str, Any]]] = {}
        if assign_ids:
            try:
                subs_res = await self.get_assign_submissions_by_ids(assign_ids)
            except Exception:
                subs_res = {}
            if isinstance(subs_res, dict) and isinstance(subs_res.get("assignments"), list):
                for item in subs_res.get("assignments") or []:
                    aid = item.get("assignmentid", item.get("id"))
                    try:
                        aid_int = int(aid) if aid is not None else None
                    except Exception:
                        aid_int = None
                    if aid_int is None:
                        continue
                    submissions_by_assign[aid_int] = item.get("submissions") or []
            elif isinstance(subs_res, dict) and isinstance(subs_res.get("courses"), list):
                for course_entry in subs_res.get("courses") or []:
                    for a in (course_entry.get("assignments") or []):
                        aid = a.get("id")
                        try:
                            aid_int = int(aid) if aid is not None else None
                        except Exception:
                            aid_int = None
                        if aid_int is None:
                            continue
                        submissions_by_assign[aid_int] = a.get("submissions") or []

        unstarted_assign_ids: set = set()
        for a in assigns:
            try:
                aid = int(a.get("id")) if a.get("id") is not None else None
            except Exception:
                aid = None
            if aid is None:
                continue
            user_sub = None
            for s in submissions_by_assign.get(aid) or []:
                try:
                    if int(s.get("userid", -1)) == int(user_id):
                        user_sub = s
                        break
                except Exception:
                    continue
            if not user_sub:
                unstarted_assign_ids.add(aid)
            else:
                status = (user_sub.get("status") or "").strip().lower()
                if status in ("", "new"):
                    unstarted_assign_ids.add(aid)

        # Quizzes list
        quizzes: List[Dict[str, Any]] = []
        try:
            quizzes_data = await self.get_course_quizzes(course_id)
            if isinstance(quizzes_data, dict) and quizzes_data.get("quizzes"):
                quizzes = (quizzes_data.get("quizzes") or [])
        except Exception:
            quizzes = []

        unstarted_quiz_ids: set = set()
        for q in quizzes:
            try:
                qid = int(q.get("id")) if q.get("id") is not None else None
            except Exception:
                qid = None
            if qid is None:
                continue
            try:
                attempts = await self.get_quiz_user_attempts(qid, int(user_id))
                atts = attempts.get("attempts") or [] if isinstance(attempts, dict) else []
                if len(atts) == 0:
                    unstarted_quiz_ids.add(qid)
            except Exception:
                # На ошибку API считаем не начатым, чтобы не советовать начатые по ошибке
                unstarted_quiz_ids.add(qid)

        return unstarted_assign_ids, unstarted_quiz_ids

    async def find_next_unstarted_by_order(self, course_id: int, user_id: int) -> Optional[Dict[str, str]]:
        """
        Возвращает первое НЕ начатое (assign/quiz) в порядке секций курса.
        """
        try:
            ua_ids, uq_ids = await self._collect_unstarted_sets(course_id, user_id)
        except Exception:
            ua_ids, uq_ids = set(), set()
        if not ua_ids and not uq_ids:
            return None
        # Подготовим словари дедлайнов для assign/quiz
        assign_due: Dict[int, int] = {}
        quiz_close: Dict[int, int] = {}
        try:
            assigns_data = await self.get_course_assignments(course_id)
            if isinstance(assigns_data, dict) and assigns_data.get("courses"):
                for a in (assigns_data["courses"][0].get("assignments") or []):
                    try:
                        aid = int(a.get("id")) if a.get("id") is not None else None
                        if aid is not None:
                            assign_due[aid] = int(a.get("duedate") or 0)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            quizzes_data = await self.get_course_quizzes(course_id)
            if isinstance(quizzes_data, dict) and quizzes_data.get("quizzes"):
                for q in (quizzes_data.get("quizzes") or []):
                    try:
                        qid = int(q.get("id")) if q.get("id") is not None else None
                        if qid is not None:
                            quiz_close[qid] = int(q.get("timeclose") or 0)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            contents = await self.get_course_contents(course_id)
        except Exception:
            contents = []
        for section in contents or []:
            for mod in section.get("modules", []) or []:
                modname = mod.get("modname")
                instance = mod.get("instance")
                # видимость и наличие валидного URL обязательны
                uservisible = bool(mod.get("uservisible", True))
                visible = mod.get("visible", 1)
                try:
                    visible = bool(int(visible))
                except Exception:
                    visible = bool(visible)
                visible_on_page = mod.get("visibleoncoursepage", 1)
                try:
                    visible_on_page = bool(int(visible_on_page))
                except Exception:
                    visible_on_page = bool(visible_on_page)
                availabilityinfo = (mod.get("availabilityinfo") or "").strip()
                url = mod.get("url") or ""
                if (not uservisible) or (not visible) or (not visible_on_page) or availabilityinfo or (not url):
                    continue
                if instance is None:
                    continue
                try:
                    inst = int(instance)
                except Exception:
                    continue
                if modname == "assign" and inst in ua_ids:
                    ret: Dict[str, Any] = {"name": mod.get("name", ""), "url": url}
                    dd = assign_due.get(inst)
                    if dd and dd > 0:
                        ret["deadline_ts"] = dd
                    return ret
                if modname == "quiz" and inst in uq_ids:
                    ret: Dict[str, Any] = {"name": mod.get("name", ""), "url": url}
                    tc = quiz_close.get(inst)
                    if tc and tc > 0:
                        ret["deadline_ts"] = tc
                    return ret
        return None

    async def _collect_unfinished_pairs(self, course_id: int, user_id: int) -> List[Tuple[str, int]]:
        """
        Собирает список пар (itemmodule, iteminstance) для элементов без оценки/с 0 баллов.
        Используется для сопоставления с модулями в структуре курса.
        """
        gi = await self.get_user_grade_items(course_id, user_id)
        items = self._extract_gradeitems(gi)
        pairs: List[Tuple[str, int]] = []
        for it in items:
            mod = it.get("itemmodule")
            inst = it.get("iteminstance")
            if mod not in ("assign", "quiz") or inst is None:
                continue
            gv = self._parse_grade_value(it.get("gradeformatted"))
            if gv is None or gv <= 0.0:
                try:
                    pairs.append((str(mod), int(inst)))
                except Exception:
                    pass
        return pairs

    async def find_next_unfinished_by_order(self, course_id: int, user_id: int) -> Optional[Dict[str, str]]:
        """
        Выбирает следующее невыполненное задание в порядке модулей курса (core_course_get_contents).
        Сопоставление делаем по (modname, instance), что надёжнее имени.
        Возвращает {name, url} или None.
        """
        # Список незавершённых элементов из грейдбука по парам (modname, instance)
        try:
            unfinished_pairs = await self._collect_unfinished_pairs(course_id, user_id)
        except Exception:
            unfinished_pairs = []
        if not unfinished_pairs:
            return None
        unfinished_set = {(m, i) for m, i in unfinished_pairs}

        # Структура курса в порядке секций/модулей
        # Подготовим словари дедлайнов для assign/quiz
        assign_due: Dict[int, int] = {}
        quiz_close: Dict[int, int] = {}
        try:
            assigns_data = await self.get_course_assignments(course_id)
            if isinstance(assigns_data, dict) and assigns_data.get("courses"):
                for a in (assigns_data["courses"][0].get("assignments") or []):
                    try:
                        aid = int(a.get("id")) if a.get("id") is not None else None
                        if aid is not None:
                            assign_due[aid] = int(a.get("duedate") or 0)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            quizzes_data = await self.get_course_quizzes(course_id)
            if isinstance(quizzes_data, dict) and quizzes_data.get("quizzes"):
                for q in (quizzes_data.get("quizzes") or []):
                    try:
                        qid = int(q.get("id")) if q.get("id") is not None else None
                        if qid is not None:
                            quiz_close[qid] = int(q.get("timeclose") or 0)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            contents = await self.get_course_contents(course_id)
        except Exception:
            contents = []
        for section in contents or []:
            for mod in section.get("modules", []) or []:
                modname = mod.get("modname")
                instance = mod.get("instance")
                if modname in ("assign", "quiz") and instance is not None:
                    try:
                        key = (str(modname), int(instance))
                    except Exception:
                        key = None
                    if key and key in unfinished_set:
                        ret: Dict[str, Any] = {"name": mod.get("name", ""), "url": mod.get("url", "")}
                        try:
                            inst = int(instance)
                        except Exception:
                            inst = None
                        if modname == "assign" and inst is not None:
                            dd = assign_due.get(inst)
                            if dd and dd > 0:
                                ret["deadline_ts"] = dd
                        if modname == "quiz" and inst is not None:
                            tc = quiz_close.get(inst)
                            if tc and tc > 0:
                                ret["deadline_ts"] = tc
                        return ret
        return None

    async def find_next_unfinished_in_course(self, course_id: int, unfinished_names: List[str]) -> Optional[Dict[str, str]]:
        """
        Ищет первое невыполненное задание (assign/quiz) по именам из грейдбука и возвращает {name, url}.
        """
        if not unfinished_names:
            return None
        assigns = await self.get_course_assignments(course_id)
        quizzes = await self.get_course_quizzes(course_id)

        # Собираем список активностей в порядке: задания, затем тесты
        activities: List[Tuple[str, str, Optional[int]]] = []  # (name, url, deadline_ts)
        if assigns and isinstance(assigns, dict) and assigns.get("courses"):
            for a in assigns["courses"][0].get("assignments", []) or []:
                dd = None
                try:
                    dd = int(a.get("duedate") or 0) or None
                except Exception:
                    dd = None
                activities.append((a.get("name", ""), self._build_assign_url(self.base_url, a), dd))
        if quizzes and isinstance(quizzes, dict) and quizzes.get("quizzes"):
            for q in quizzes["quizzes"] or []:
                tc = None
                try:
                    tc = int(q.get("timeclose") or 0) or None
                except Exception:
                    tc = None
                activities.append((q.get("name", ""), self._build_quiz_url(self.base_url, q), tc))

        # Находим первую активность, имя которой есть среди незавершённых
        unfinished_set = {n for n in unfinished_names if n}
        for name, url, due_ts in activities:
            if name and name in unfinished_set:
                ret: Dict[str, Any] = {"name": name, "url": url}
                if due_ts and due_ts > 0:
                    ret["deadline_ts"] = due_ts
                return ret
        return None

    async def _get_course_fullname_for_user(self, user_id: int, course_id: int) -> str:
        """
        Возвращает fullname курса, если пользователь записан на него; иначе 'Курс <id>'.
        """
        try:
            courses = await self.get_user_courses(user_id)
            for c in courses or []:
                try:
                    if int(c.get("id")) == int(course_id):
                        return c.get("fullname") or c.get("shortname") or f"Курс {course_id}"
                except Exception:
                    continue
        except Exception:
            pass
        return f"Курс {course_id}"

    async def _get_course_names_for_user(self, user_id: int, course_id: int) -> Tuple[str, Optional[str]]:
        """
        Возвращает (fullname, shortname) если пользователь записан на курс; иначе ('Курс <id>', None).
        """
        try:
            courses = await self.get_user_courses(user_id)
            for c in courses or []:
                try:
                    if int(c.get("id")) == int(course_id):
                        fullname = c.get("fullname") or c.get("shortname") or f"Курс {course_id}"
                        shortname = c.get("shortname")
                        return fullname, shortname
                except Exception:
                    continue
        except Exception:
            pass
        return f"Курс {course_id}", None

    async def get_progress_for_course(self, course_id: int, user_id: int) -> Dict[str, Any]:
        """
        Считает процент и находит следующее невыполненное для конкретного курса и пользователя.
        Возвращает {course_id, course_name, percent, next_task}.
        """
        if self.completion_mode == "native":
            percent, _incomplete = await self.compute_course_completion_natively(course_id, user_id)
            # Для next_task всегда используем грейдбук + структуру курса
            _pg, unfinished_names = await self.compute_course_completion_from_gradeitems(course_id, user_id)
        elif self.completion_mode == "status":
            percent = await self.compute_course_completion_by_status(course_id, user_id)
            # Для next_task в статусном режиме показываем только НЕ начатые
            unfinished_names = []
        else:
            percent, unfinished_names = await self.compute_course_completion_from_gradeitems(course_id, user_id)

        use_order = os.getenv("MOODLE_USE_COURSE_ORDER", "1").strip().lower() in ("1", "true", "yes")
        next_task = None
        if self.completion_mode == "status":
            # Только НЕ начатые по порядку курса
            if use_order:
                next_task = await self.find_next_unstarted_by_order(course_id, user_id)
        else:
            if unfinished_names:
                if use_order:
                    next_task = await self.find_next_unfinished_by_order(course_id, user_id)
                if not next_task:
                    next_task = await self.find_next_unfinished_in_course(course_id, unfinished_names)

        course_full, course_short = await self._get_course_names_for_user(user_id, course_id)
        return {
            "course_id": course_id,
            "course_name": course_full,
            "course_shortname": course_short,
            "percent": percent,
            "next_task": next_task,
        }

    async def get_student_progress_for_course(self, course_id: int, student_number: str) -> Optional[Dict[str, Any]]:
        """
        Ищет user_id по student_number. Сначала пытается найти в primary_course_id,
        если не найден — ищет в самом запрашиваемом курсе (course_id).
        Если найден — возвращает get_progress_for_course(course_id, user_id). Иначе None.
        Обрабатывает ошибки доступа к курсу и возвращает None при отсутствии прав.
        """
        # Сначала пытаемся найти в primary_course_id (быстрее, если студент там есть)
        user_id = await self.get_moodle_user_id_by_student_number(student_number)
        
        # Если не найден в primary_course_id, ищем в самом запрашиваемом курсе
        if not user_id:
            user_id = await self._find_user_id_in_course(course_id, student_number)
        
        if not user_id:
            return None
        
        # Пытаемся получить прогресс, обрабатывая ошибки доступа
        try:
            return await self.get_progress_for_course(course_id, user_id)
        except RuntimeError as e:
            # Обрабатываем ошибки доступа к курсу (токен не имеет прав)
            error_msg = str(e)
            if "контексте курса" in error_msg or "не доступны" in error_msg or "access" in error_msg.lower() or "permission" in error_msg.lower():
                # Токен не имеет доступа к этому курсу - возвращаем None
                return None
            # Другие ошибки пробрасываем дальше
            raise

    async def get_student_progress_and_next_task(self, student_number: str, max_courses_for_text: int = 5) -> Dict[str, Any]:
        """
        Собирает прогресс по всем парам (MOODLE_TOKEN_N, COURSE_ID_N), заданным в окружении.
        Также поддерживает синонимы без индекса (MOODLE_TOKEN/COURSE_ID) — при отсутствии пары с индексом 1.
        Для каждой пары:
          1) Ищем user_id по student_number в этом курсе
          2) Если найден — считаем процент и находим следующую активность согласно текущему режиму
        Возвращает объединённый результат.
        """
        base_url = self.base_url
        student_field = self.student_number_field
        mode = self.completion_mode

        # Собираем пары токен/курс по индексам
        token_map: Dict[int, str] = {}
        course_map: Dict[int, int] = {}
        token_re = re.compile(r"^MOODLE_TOKEN_(\d+)$")
        course_re = re.compile(r"^COURSE_ID_(\d+)$")

        for key, value in os.environ.items():
            m = token_re.match(key)
            if m and value:
                try:
                    idx = int(m.group(1))
                except Exception:
                    continue
                token_map[idx] = value
            m2 = course_re.match(key)
            if m2 and value:
                try:
                    idx2 = int(m2.group(1))
                    course_map[idx2] = int(value)
                except Exception:
                    continue

        # Поддержка несвязанных с индексом переменных как пары с индексом 1, если она не задана
        if 1 not in token_map:
            base_token = os.getenv("MOODLE_TOKEN")
            if base_token:
                token_map[1] = base_token
        if 1 not in course_map:
            base_cid = os.getenv("COURSE_ID")
            try:
                if base_cid:
                    course_map[1] = int(base_cid)
            except Exception:
                pass

        # Финальные пары: индексы, у которых есть и токен, и id курса
        pairs: List[Tuple[str, int]] = []
        for idx in sorted(set(token_map.keys()) & set(course_map.keys())):
            tok = token_map[idx]
            cid = course_map[idx]
            if tok and cid:
                pairs.append((tok, cid))

        results: List[Dict[str, Any]] = []
        any_user_id: Optional[int] = None

        for tok, course_id in pairs:
            if not tok or not course_id:
                continue
            client = MoodleClient(
                base_url=base_url,
                token=tok,
                primary_course_id=course_id,
                student_number_field=student_field,
                completion_mode=mode,
            )
            async with client as m:
                uid = await m.get_moodle_user_id_by_student_number(student_number)
                if not uid:
                    continue
                any_user_id = any_user_id or uid
                prog = await m.get_progress_for_course(course_id, uid)
                if prog:
                    results.append({
                        "id": prog.get("course_id"),
                        "fullname": prog.get("course_name"),
                        "percent": prog.get("percent", 0),
                        "next_task": prog.get("next_task"),
                        "course_shortname": prog.get("course_shortname"),
                    })

        global_next = None
        for r in results:
            if r.get("next_task"):
                global_next = {"course_id": r["id"], **r["next_task"]}
                break

        return {
            "user_id": any_user_id,
            "courses": [{"id": r["id"], "fullname": r["fullname"], "percent": r["percent"]} for r in results],
            "next_task": global_next,
            "course_details": results,
        }

    # -------------------- AGG: МОТРИКА ДЛЯ BIG5 --------------------
    @staticmethod
    def _parse_utc_str_to_epoch(ts: str) -> Optional[int]:
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None

    @staticmethod
    def _classify_assign_status(status: Optional[str]) -> str:
        s = (status or "").strip().lower()
        if s in ("", "new", "notgraded"):
            return "unstarted"
        if s == "submitted":
            return "completed"
        # draft, reopened, inreview, etc.
        return "started"

    @staticmethod
    def _classify_quiz_state(state: Optional[str]) -> str:
        st = (state or "").strip().lower()
        if st == "finished":
            return "completed"
        if st in ("", None):
            return "unstarted"
        # inprogress / overdue / abandoned
        return "started"

    async def get_student_moodle_metrics(self, student_number: str, window_start_utc: str, window_end_utc: str, recent_limit: int = 3) -> Dict[str, Any]:
        """
        Возвращает агрегаты по Moodle для студента за окно [window_start_utc, window_end_utc):
          - summary: percent_avg_now, started_14d, completed_14d, unstarted_now, started_now, completed_now
          - courses: по каждому курсу id, fullname, percent_now, counts_now, counts_14d, next_unstarted, recent (до 3 последних завершений)
        Все проценты считаются равновесно по курсам.
        """
        ws = self._parse_utc_str_to_epoch(window_start_utc)
        we = self._parse_utc_str_to_epoch(window_end_utc)
        if ws is None or we is None:
            ws = 0
            we = 0

        # Собираем пары токен/курс из окружения
        token_map: Dict[int, str] = {}
        course_map: Dict[int, int] = {}
        token_re = re.compile(r"^MOODLE_TOKEN_(\d+)$")
        course_re = re.compile(r"^COURSE_ID_(\d+)$")
        for key, value in os.environ.items():
            m = token_re.match(key)
            if m and value:
                try:
                    token_map[int(m.group(1))] = value
                except Exception:
                    pass
            m2 = course_re.match(key)
            if m2 and value:
                try:
                    course_map[int(m2.group(1))] = int(value)
                except Exception:
                    pass
        if 1 not in token_map:
            base_token = os.getenv("MOODLE_TOKEN")
            if base_token:
                token_map[1] = base_token
        if 1 not in course_map:
            base_cid = os.getenv("COURSE_ID")
            try:
                if base_cid:
                    course_map[1] = int(base_cid)
            except Exception:
                pass
        pairs: List[Tuple[str, int]] = []
        for idx in sorted(set(token_map.keys()) & set(course_map.keys())):
            tok = token_map[idx]
            cid = course_map[idx]
            if tok and cid:
                pairs.append((tok, cid))

        all_courses: List[Dict[str, Any]] = []
        percents: List[int] = []
        agg_unstarted_now = 0
        agg_started_now = 0
        agg_completed_now = 0
        agg_started_14d = 0
        agg_completed_14d = 0
        any_user_id: Optional[int] = None

        for tok, course_id in pairs:
            # создаём эпизодический клиент под конкретный токен курса
            client = MoodleClient(
                base_url=self.base_url,
                token=tok,
                primary_course_id=course_id,
                student_number_field=self.student_number_field,
                completion_mode=self.completion_mode,
            )
            async with client as m:
                uid = await m.get_moodle_user_id_by_student_number(student_number)
                if not uid:
                    continue
                any_user_id = any_user_id or uid

                # percent now по статусам
                try:
                    percent_now = await m.compute_course_completion_by_status(course_id, uid)
                except Exception:
                    percent_now = 0
                percents.append(percent_now)

                # названия курса
                course_full, _short = await m._get_course_names_for_user(uid, course_id)

                # Список активностей
                assigns_data = await m.get_course_assignments(course_id)
                quizzes_data = await m.get_course_quizzes(course_id)
                assigns: List[Dict[str, Any]] = []
                quizzes: List[Dict[str, Any]] = []
                if isinstance(assigns_data, dict) and assigns_data.get("courses"):
                    assigns = (assigns_data["courses"][0].get("assignments") or [])
                if isinstance(quizzes_data, dict) and quizzes_data.get("quizzes"):
                    quizzes = (quizzes_data.get("quizzes") or [])

                # Подачи по assign одним запросом
                assign_ids: List[int] = []
                for a in assigns:
                    try:
                        aid = int(a.get("id")) if a.get("id") is not None else None
                    except Exception:
                        aid = None
                    if aid is not None:
                        assign_ids.append(aid)
                submissions_by_assign: Dict[int, List[Dict[str, Any]]] = {}
                if assign_ids:
                    try:
                        subs_res = await m.get_assign_submissions_by_ids(assign_ids)
                    except Exception:
                        subs_res = {}
                    if isinstance(subs_res, dict) and isinstance(subs_res.get("assignments"), list):
                        for item in subs_res.get("assignments") or []:
                            aid = item.get("assignmentid", item.get("id"))
                            try:
                                aid_int = int(aid) if aid is not None else None
                            except Exception:
                                aid_int = None
                            if aid_int is None:
                                continue
                            submissions_by_assign[aid_int] = item.get("submissions") or []
                    elif isinstance(subs_res, dict) and isinstance(subs_res.get("courses"), list):
                        for course_entry in subs_res.get("courses") or []:
                            for a in (course_entry.get("assignments") or []):
                                aid = a.get("id")
                                try:
                                    aid_int = int(aid) if aid is not None else None
                                except Exception:
                                    aid_int = None
                                if aid_int is None:
                                    continue
                                submissions_by_assign[aid_int] = a.get("submissions") or []

                # Подсчёт now/14d и недавние завершения
                unstarted_now = 0
                started_now = 0
                completed_now = 0
                started_14d = 0
                completed_14d = 0
                recent: List[Dict[str, Any]] = []

                # assign классификация
                for a in assigns:
                    name = a.get("name", "")
                    try:
                        aid = int(a.get("id")) if a.get("id") is not None else None
                    except Exception:
                        aid = None
                    if aid is None:
                        continue
                    # найдем подачу пользователя
                    user_sub = None
                    for s in submissions_by_assign.get(aid) or []:
                        try:
                            if int(s.get("userid", -1)) == int(uid):
                                user_sub = s
                                break
                        except Exception:
                            continue
                    cls = self._classify_assign_status(user_sub.get("status") if user_sub else None)
                    if cls == "unstarted":
                        unstarted_now += 1
                    elif cls == "completed":
                        completed_now += 1
                        try:
                            tm = int(user_sub.get("timemodified")) if user_sub and user_sub.get("timemodified") is not None else None
                        except Exception:
                            tm = None
                        if tm and ws and we and ws <= tm < we:
                            completed_14d += 1
                            recent.append({"type": "assign", "name": name, "ts": tm})
                    else:
                        started_now += 1
                        try:
                            tm = int(user_sub.get("timemodified")) if user_sub and user_sub.get("timemodified") is not None else None
                        except Exception:
                            tm = None
                        if tm and ws and we and ws <= tm < we:
                            started_14d += 1

                # quiz классификация
                for q in quizzes:
                    qname = q.get("name", "")
                    try:
                        qid = int(q.get("id")) if q.get("id") is not None else None
                    except Exception:
                        qid = None
                    if qid is None:
                        continue
                    latest = None
                    try:
                        attempts = await m.get_quiz_user_attempts(qid, int(uid))
                        atts = attempts.get("attempts") or [] if isinstance(attempts, dict) else []
                        for a in atts:
                            try:
                                if latest is None or int(a.get("attempt", 0)) > int(latest.get("attempt", 0)):
                                    latest = a
                            except Exception:
                                continue
                    except Exception:
                        latest = None
                    if not latest:
                        unstarted_now += 1
                        continue
                    cls = self._classify_quiz_state(latest.get("state"))
                    if cls == "completed":
                        completed_now += 1
                        try:
                            tf = int(latest.get("timefinish")) if latest.get("timefinish") is not None else None
                        except Exception:
                            tf = None
                        if tf and ws and we and ws <= tf < we:
                            completed_14d += 1
                            recent.append({"type": "quiz", "name": qname, "ts": tf})
                    elif cls == "unstarted":
                        unstarted_now += 1
                    else:
                        started_now += 1
                        try:
                            ts_q = int(latest.get("timestart")) if latest.get("timestart") is not None else None
                        except Exception:
                            ts_q = None
                        if ts_q and ws and we and ws <= ts_q < we:
                            started_14d += 1

                # sort recent desc by ts and trim
                recent = sorted([r for r in recent if isinstance(r.get("ts"), int)], key=lambda r: r["ts"], reverse=True)[:max(0, recent_limit or 0)]

                # next_unstarted
                try:
                    next_unstarted = await m.find_next_unstarted_by_order(course_id, uid)
                except Exception:
                    next_unstarted = None

                all_courses.append({
                    "id": course_id,
                    "fullname": course_full,
                    "percent_now": percent_now,
                    "counts_now": {"unstarted": unstarted_now, "started": started_now, "completed": completed_now},
                    "counts_14d": {"started_14d": started_14d, "completed_14d": completed_14d},
                    "next_unstarted": next_unstarted,
                    "recent": [{"type": r["type"], "name": r["name"], "ts": r["ts"]} for r in recent],
                })
                agg_unstarted_now += unstarted_now
                agg_started_now += started_now
                agg_completed_now += completed_now
                agg_started_14d += started_14d
                agg_completed_14d += completed_14d

        percent_avg_now = int(round(sum(percents) / len(percents))) if percents else 0
        return {
            "summary": {
                "percent_avg_now": percent_avg_now,
                "unstarted_now": agg_unstarted_now,
                "started_now": agg_started_now,
                "completed_now": agg_completed_now,
                "started_14d": agg_started_14d,
                "completed_14d": agg_completed_14d,
            },
            "courses": all_courses,
        }


# Небольшой ручной тест (опционально):
# python -m digital-professor.moodle_client
async def _self_test():
    client = await MoodleClient.from_env()
    async with client as m:
        sn = os.getenv("TEST_STUDENT_NUMBER", "")
        if not sn:
            print("Установите TEST_STUDENT_NUMBER в .env для самопроверки.")
            return
        data = await m.get_student_progress_and_next_task(sn)
        print(data)


# if __name__ == "__main__":
#     asyncio.run(_self_test())


