# advanced_rate_limiter.py
"""
Упрощённая система rate limiting с защитой от спама.
Использует Token Bucket + детекцию дубликатов + прогрессивные штрафы.
Сложная смысловая валидация вынесена в LLM-цензор.
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
import hashlib

logger = logging.getLogger(__name__)


class TokenBucket:
    """
    Token Bucket Algorithm для rate limiting.
    
    Позволяет короткие всплески активности, но ограничивает длительный спам.
    """
    
    def __init__(self, capacity: int, refill_rate: float):
        """
        Args:
            capacity: Максимальное количество токенов (размер ведра)
            refill_rate: Скорость пополнения токенов в секунду
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.last_refill = datetime.now()
    
    def consume(self, tokens: int = 1) -> bool:
        """
        Попытка потратить токены.
        
        Returns:
            bool: True если токены доступны
        """
        self._refill()
        
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
    
    def _refill(self):
        """Пополняет токены на основе времени."""
        now = datetime.now()
        time_passed = (now - self.last_refill).total_seconds()
        new_tokens = time_passed * self.refill_rate
        
        self.tokens = min(self.capacity, self.tokens + new_tokens)
        self.last_refill = now
    
    def time_until_token(self) -> float:
        """Время до появления следующего токена."""
        self._refill()
        if self.tokens >= 1:
            return 0
        tokens_needed = 1 - self.tokens
        return tokens_needed / self.refill_rate


class SpamDetector:
    """
    Упрощённый детектор спама на основе дубликатов.
    Сложная логика вынесена в LLM-цензор.
    """
    
    def __init__(self, window_size: int = 60, max_duplicates: int = 3):
        """
        Args:
            window_size: Размер окна для детекции (секунды)
            max_duplicates: Максимум повторений одного сообщения (ОСЛАБЛЕНО: 3)
        """
        self.window_size = window_size
        self.max_duplicates = max_duplicates
        self.message_hashes: Dict[int, List[Tuple[str, datetime]]] = {}
    
    def is_spam(self, user_id: int, message: str) -> Tuple[bool, Optional[str]]:
        """
        Проверяет сообщение на спам (только дубликаты).
        
        Returns:
            Tuple[bool, str]: (is_spam, reason)
        """
        now = datetime.now()
        msg_hash = self._hash_message(message)
        
        # Инициализация для нового пользователя
        if user_id not in self.message_hashes:
            self.message_hashes[user_id] = []
        
        # Очистка старых сообщений (за пределами окна)
        recent_messages = [
            (h, ts) for h, ts in self.message_hashes[user_id]
            if now - ts < timedelta(seconds=self.window_size)
        ]
        self.message_hashes[user_id] = recent_messages
        
        # Подсчёт ТОЧНЫХ дубликатов
        duplicate_count = sum(1 for h, _ in recent_messages if h == msg_hash)
        
        if duplicate_count >= self.max_duplicates:
            return True, f"🚫 Вы отправили одинаковое сообщение {duplicate_count + 1} раз. Пожалуйста, подождите немного."
        
        # Добавляем текущее сообщение
        self.message_hashes[user_id].append((msg_hash, now))
        
        return False, None
    
    def _hash_message(self, message: str) -> str:
        """Создаёт хеш сообщения для сравнения."""
        # Нормализуем: удаляем пробелы, приводим к lowercase
        normalized = message.lower().strip().replace(' ', '')
        return hashlib.md5(normalized.encode()).hexdigest()[:16]
    
    def reset_user(self, user_id: int):
        """Сбрасывает историю пользователя."""
        if user_id in self.message_hashes:
            del self.message_hashes[user_id]


class AdvancedRateLimiter:
    """
    Упрощённый rate limiter с Token Bucket + детекцией дубликатов + прогрессивными штрафами.
    Сложная смысловая проверка вынесена в LLM-цензор.
    """
    
    def __init__(
        self,
        capacity: int = 5,           # Размер ведра (burst) - ОСЛАБЛЕНО
        refill_rate: float = 0.25,   # ОСЛАБЛЕНО: 0.25 токена/сек = 15 запросов/минуту (было 0.12-0.2)
        burst_window: int = 8,       # ОСЛАБЛЕНО: 8 секунд (было 5)
        max_burst: int = 3,          # ОСЛАБЛЕНО: 3 запроса (было 2)
        penalty_multiplier: float = 2.0,  # Множитель штрафа
        whitelist: Optional[List[int]] = None
    ):
        """
        Args:
            capacity: Размер ведра токенов
            refill_rate: Скорость пополнения (токенов в секунду)
            burst_window: Окно для детекции быстрых всплесков (секунды)
            max_burst: Максимум запросов за burst_window
            penalty_multiplier: Множитель для прогрессивных штрафов
            whitelist: Список освобождённых user_id
        """
        self.buckets: Dict[int, TokenBucket] = {}
        self.burst_requests: Dict[int, List[datetime]] = {}
        self.violations: Dict[int, int] = {}  # Счётчик нарушений
        self.blocked_until: Dict[int, datetime] = {}  # жёсткая блокировка
        self.spam_detector = SpamDetector(window_size=60, max_duplicates=3)  # ОСЛАБЛЕНО: 3 дубликата
        self.warning_history: Dict[int, List[datetime]] = {}  # история предупреждений
        self.warning_expiry = 3600  # Предупреждения хранятся 1 час
        
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.burst_window = burst_window
        self.max_burst = max_burst
        self.penalty_multiplier = penalty_multiplier
        self.whitelist = set(whitelist) if whitelist else set()
        
        logger.info(f"AdvancedRateLimiter (RELAXED): capacity={capacity}, refill={refill_rate}/s, burst={max_burst}/{burst_window}s")
    
    def is_allowed(self, user_id: int, message: str = "", action: str = "default") -> Tuple[bool, Optional[str]]:
        """
        Проверяет, разрешён ли запрос.
        
        Args:
            user_id: ID пользователя
            message: Текст сообщения (для детекции спама)
            action: Тип действия
            
        Returns:
            Tuple[bool, str]: (is_allowed, error_message)
        """
        # Whitelist всегда проходит
        if user_id in self.whitelist:
            return True, None
        
        now = datetime.now()
        
        # 0. ПРОВЕРКА ЖЁСТКОЙ БЛОКИРОВКИ (приоритет #1)
        if user_id in self.blocked_until:
            unblock_time = self.blocked_until[user_id]
            if now < unblock_time:
                remaining = int((unblock_time - now).total_seconds())
                violations = self.violations.get(user_id, 0)
                logger.warning(f"User {user_id} is BLOCKED until {unblock_time}. Violations: {violations}")
                return False, f"🔒 Ваш аккаунт временно заблокирован за спам.\n⏳ Разблокировка через {remaining} секунд.\n⚠️ Нарушений: {int(violations)}"
            else:
                # Блокировка истекла
                del self.blocked_until[user_id]
                logger.info(f"User {user_id} unblocked")
        
        # 1. ПРОВЕРКА НА СПАМ (только дубликаты)
        if message:
            is_spam, spam_reason = self.spam_detector.is_spam(user_id, message)
            if is_spam:
                self._add_violation(user_id)
                violations = self.violations.get(user_id, 0)
                
                # Проверяем историю предупреждений
                has_recent_warning = self._has_recent_warning(user_id, now)
                
                if has_recent_warning:
                    # Если есть предупреждение за последний час - сразу блокируем
                    penalty_time = self._calculate_penalty_time(user_id)
                    self.blocked_until[user_id] = now + timedelta(seconds=penalty_time)
                    self._add_warning(user_id, now)
                    logger.warning(f"User {user_id} spam AGAIN after recent warning. BLOCKING for {penalty_time}s")
                    minutes = penalty_time // 60
                    return False, f"🚫 Повторное нарушение после предупреждения!\n\n⏱️ Вы заблокированы на {minutes} минут."
                elif violations >= 3:  # ОСЛАБЛЕНО: 3+ нарушений (было 2+)
                    # 3+ нарушения подряд - блокировка
                    penalty_time = self._calculate_penalty_time(user_id)
                    self.blocked_until[user_id] = now + timedelta(seconds=penalty_time)
                    self._add_warning(user_id, now)
                    logger.warning(f"User {user_id} spam detected {violations} times. BLOCKING for {penalty_time}s")
                    minutes = penalty_time // 60
                    return False, f"{spam_reason}\n\n⚠️ Обнаружено {violations} нарушений подряд. Вы заблокированы на {minutes} минут за спам."
                else:
                    # Первое/второе предупреждение - сохраняем в историю
                    self._add_warning(user_id, now)
                    logger.warning(f"Spam detected from user {user_id}: {spam_reason} (violations: {violations})")
                    return False, f"{spam_reason}\n\n⚠️ При следующем нарушении в течение часа вы будете заблокированы."
        
        # 2. ПРОВЕРКА BURST (быстрые всплески)
        if user_id not in self.burst_requests:
            self.burst_requests[user_id] = []
        
        # Очистка старых burst-запросов
        self.burst_requests[user_id] = [
            ts for ts in self.burst_requests[user_id]
            if now - ts < timedelta(seconds=self.burst_window)
        ]
        
        if len(self.burst_requests[user_id]) >= self.max_burst:
            self._add_violation(user_id)
            penalty_time = self._calculate_penalty_time(user_id)
            
            # При превышении burst - жёсткая блокировка
            self.blocked_until[user_id] = now + timedelta(seconds=penalty_time)
            
            logger.warning(f"Burst limit exceeded for user {user_id}. BLOCKING for {penalty_time}s. Violations: {self.violations.get(user_id, 0)}")
            return False, f"⚠️ Слишком быстро! Вы заблокированы на {penalty_time} секунд.\n(Обнаружено {len(self.burst_requests[user_id])} запросов за {self.burst_window} секунд)"
        
        # 3. ПРОВЕРКА TOKEN BUCKET
        if user_id not in self.buckets:
            self.buckets[user_id] = TokenBucket(self.capacity, self.refill_rate)
        
        if not self.buckets[user_id].consume():
            wait_time = int(self.buckets[user_id].time_until_token()) + 1
            wait_time = self._apply_penalty(user_id, wait_time)
            logger.warning(f"Token bucket exhausted for user {user_id} (action={action})")
            return False, f"⏳ Превышен лимит запросов. Подождите {wait_time} секунд."
        
        # Запрос разрешён
        self.burst_requests[user_id].append(now)
        self._decay_violations(user_id)  # Постепенно снижаем счётчик нарушений
        
        logger.debug(f"Request allowed: user {user_id}, action '{action}', tokens left: {self.buckets[user_id].tokens:.2f}")
        return True, None
    
    def _add_violation(self, user_id: int):
        """Добавляет нарушение пользователю."""
        if user_id not in self.violations:
            self.violations[user_id] = 0
        self.violations[user_id] += 1
        logger.info(f"User {user_id} violations: {self.violations[user_id]}")
    
    def _add_warning(self, user_id: int, timestamp: datetime):
        """Добавляет предупреждение в историю."""
        if user_id not in self.warning_history:
            self.warning_history[user_id] = []
        self.warning_history[user_id].append(timestamp)
        # Удаляем старые предупреждения (>1 часа)
        self._clean_old_warnings(user_id, timestamp)
    
    def _has_recent_warning(self, user_id: int, now: datetime) -> bool:
        """Проверяет, есть ли предупреждение за последний час."""
        if user_id not in self.warning_history:
            return False
        
        self._clean_old_warnings(user_id, now)
        return len(self.warning_history[user_id]) > 0
    
    def _clean_old_warnings(self, user_id: int, now: datetime):
        """Удаляет предупреждения старше 1 часа."""
        if user_id not in self.warning_history:
            return
        
        self.warning_history[user_id] = [
            ts for ts in self.warning_history[user_id]
            if now - ts < timedelta(seconds=self.warning_expiry)
        ]
    
    def _decay_violations(self, user_id: int):
        """Постепенно снижает счётчик нарушений при корректном поведении."""
        if user_id in self.violations and self.violations[user_id] > 0:
            self.violations[user_id] = max(0, self.violations[user_id] - 0.1)
    
    def _calculate_penalty_time(self, user_id: int) -> int:
        """Вычисляет время штрафа на основе нарушений.
        
        Прогрессия: 1 мин → 5 мин → 10 мин → 1 час → 3 часа → 1 день → 1 неделя
        """
        violations = int(self.violations.get(user_id, 0))  # Округляем до целого
        
        # Прогрессивная шкала штрафов
        if violations <= 1:
            return 60  # 1 минута
        elif violations == 2:
            return 300  # 5 минут
        elif violations == 3:
            return 600  # 10 минут
        elif violations == 4:
            return 3600  # 1 час
        elif violations == 5:
            return 10800  # 3 часа
        elif violations == 6:
            return 86400  # 1 день (24 часа)
        else:
            return 604800  # 1 неделя (7 дней) - максимум
    
    def _apply_penalty(self, user_id: int, base_time: int) -> int:
        """Применяет прогрессивный штраф к времени ожидания."""
        violations = self.violations.get(user_id, 0)
        if violations > 0:
            penalized = int(base_time * (1 + violations * 0.5))
            return min(penalized, 180)  # Максимум 3 минуты
        return base_time
    
    def add_to_whitelist(self, user_id: int):
        """Добавляет пользователя в whitelist."""
        if user_id in self.whitelist:
            return
        self.whitelist.add(user_id)
        logger.info(f"User {user_id} added to advanced rate limiter whitelist")
    
    def reset_user(self, user_id: int, keep_violations: bool = False):
        """Полностью сбрасывает состояние пользователя.
        
        Args:
            keep_violations: Если True, счётчик нарушений сохраняется
        """
        if user_id in self.buckets:
            del self.buckets[user_id]
        if user_id in self.burst_requests:
            del self.burst_requests[user_id]
        if not keep_violations and user_id in self.violations:
            del self.violations[user_id]
        if user_id in self.blocked_until:
            del self.blocked_until[user_id]
        if user_id in self.warning_history:
            del self.warning_history[user_id]
        self.spam_detector.reset_user(user_id)
        logger.info(f"Reset state for user {user_id} (violations kept: {keep_violations})")
    
    def get_user_stats(self, user_id: int) -> Dict:
        """Возвращает статистику пользователя."""
        bucket = self.buckets.get(user_id)
        blocked_until = self.blocked_until.get(user_id)
        is_blocked = blocked_until and datetime.now() < blocked_until
        
        return {
            "tokens": round(bucket.tokens, 2) if bucket else self.capacity,
            "violations": self.violations.get(user_id, 0),
            "recent_requests": len(self.burst_requests.get(user_id, [])),
            "is_whitelisted": user_id in self.whitelist,
            "is_blocked": is_blocked,
            "blocked_until": blocked_until.isoformat() if blocked_until else None
        }
