from digital_professor.database import get_connection, logger, create_course, add_task, add_test_choice, add_test_question
from digital_professor.registry import course_creator
from digital_professor.telegraph_utils import telegraph_service
import logging


#create_course_from_template v3 - Добавлена логика топиков и теории. Теория подается через телеграф 
def create_course_from_template(title: str, description: str, topics: list, created_by: int = None):
    conn = get_connection()
    c = conn.cursor()

    # Создаем курс
    c.execute(
        "INSERT INTO courses (title, description, created_by, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        (title, description, created_by)
    )
    course_id = c.lastrowid

    # Сохраняем топики
    for topic in topics:
        t_title = topic.get("title")
        t_description = topic.get("description", "")
        t_theory = topic.get("theory", "")  # заглушка для теории
        telegraph_link = None  # пока None

        c.execute(
            "INSERT INTO topics (course_id, title, description, theory, telegraph_link, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (course_id, t_title, t_description, t_theory, telegraph_link, created_by)
        )
        topic_id = c.lastrowid

        # Сохраняем задания для этого топика
        for task in topic.get("tasks", []):
            task_type = task["type"]
            title = task.get("title")
            description = task.get("description")
            reference_answer = task.get("reference_answer", "")
            evaluation_criteria = task.get("evaluation_criteria", "")
            max_score = task.get("max_score", 10)

            c.execute(
                "INSERT INTO tasks (course_id, topic_id, task_type, title, description, reference_answer, evaluation_criteria, max_score, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (course_id, topic_id, task_type, title, description, reference_answer, evaluation_criteria, max_score, created_by)
            )
            task_id = c.lastrowid

            # Обработка специфичных данных для тестов
            if task_type == "test" and "questions" in task:
                for idx, question in enumerate(task["questions"], 1):
                    c.execute(
                        "INSERT INTO test_questions (task_id, question_text, order_idx) VALUES (?, ?, ?)",
                        (task_id, question["text"], idx)
                    )
                    question_id = c.lastrowid
                    for choice in question["choices"]:
                        c.execute(
                            "INSERT INTO test_choices (question_id, choice_text, is_correct) VALUES (?, ?, ?)",
                            (question_id, choice["text"], 1 if choice["is_correct"] else 0)
                        )

    conn.commit()
    conn.close()

    # Возвращаем course_id и topics для асинхронной обработки
    return course_id, topics

#Асинхронная функция для генерации телеграф ссылкок
async def generate_telegraph_links_for_course(course_id, topics):
    """Асинхронно генерирует Telegraph ссылки для всех топиков курса"""
    from digital_professor.database import get_connection
    
    conn = get_connection()
    c = conn.cursor()
    
    try:
        for i, topic in enumerate(topics):
            theory = topic.get("theory", "")
            if theory and theory.strip():
                # Генерируем ссылку через TelegraphService
                link = await telegraph_service.create_page(
                    title=topic["title"],
                    content=theory
                )
                
                if link:
                    # Получаем ID топика по порядку
                    c.execute(
                        "SELECT id FROM topics WHERE course_id = ? ORDER BY id LIMIT 1 OFFSET ?",
                        (course_id, i)
                    )
                    row = c.fetchone()
                    if row:
                        topic_id = row[0]
                        
                        # Сохраняем ссылку
                        c.execute(
                            "UPDATE topics SET telegraph_link = ? WHERE id = ?",
                            (link, topic_id)
                        )
                        conn.commit()
                        
                        logger.info(f"Создана Telegraph ссылка для топика {topic_id}: {link}")
                    else:
                        logger.warning(f"Не найден топик с индексом {i} для курса {course_id}")
                else:
                    logger.error(f"Не удалось создать Telegraph страницу для топика: {topic['title']}")
            else:
                logger.debug(f"Топик {topic.get('title')} не содержит теории, пропускаем")
    
    except Exception as e:
        logger.error(f"Ошибка генерации Telegraph ссылок для курса {course_id}: {e}", exc_info=True)
    finally:
        conn.close()


#Синтетический курс по ml с двумя топиками ----------------------------------
@course_creator("ml_basics", title="Базовый курс по Machine Learning",
                description="Основы машинного обучения: линейная и логистическая регрессия")
def create_course_ml_basics(created_by: int = None):
    topics = [
        {
            "title": "Линейная регрессия",
            "description": "Введение в задачу регрессии и метод наименьших квадратов.",
            "theory": """
# Линейная регрессия

**Что это?**
Линейная регрессия - это метод машинного обучения, который предсказывает непрерывные числовые значения на основе линейной зависимости между признаками и целевой переменной.

**Основная формула:**
y = β₀ + β₁x₁ + β₂x₂ + ... + βₙxₙ

Где:
- y - предсказываемое значение
- β₀ - свободный член (intercept)
- β₁, β₂, ..., βₙ - коэффициенты при признаках
- x₁, x₂, ..., xₙ - признаки

**Пример использования:**
Предсказание цены дома на основе площади, количества комнат и местоположения.

**Метод обучения:**
Метод наименьших квадратов - минимизирует сумму квадратов отклонений между реальными и предсказанными значениями.
""",
            "tasks": [
                {
                    "type": "test",
                    "title": "Тест по линейной регрессии",
                    "description": "Проверьте базовые знания о линейной регрессии",
                    "questions": [
                        {
                            "text": "Что предсказывает модель линейной регрессии?",
                            "choices": [
                                {"text": "Категориальные классы", "is_correct": False},
                                {"text": "Непрерывные числовые значения", "is_correct": True},
                                {"text": "Случайные значения", "is_correct": False},
                                {"text": "Вероятности классов", "is_correct": False},
                            ]
                        },
                        {
                            "text": "Какая функция ошибки чаще всего используется в линейной регрессии?",
                            "choices": [
                                {"text": "Кросс-энтропия", "is_correct": False},
                                {"text": "Среднеквадратичная ошибка (MSE)", "is_correct": True},
                                {"text": "Silhouette Score", "is_correct": False},
                                {"text": "Accuracy", "is_correct": False},
                            ]
                        },
                        {
                            "text": "Что делает коэффициент w в уравнении y = wx + b?",
                            "choices": [
                                {"text": "Смещает линию вверх или вниз", "is_correct": False},
                                {"text": "Определяет наклон линии", "is_correct": True},
                                {"text": "Задает количество признаков", "is_correct": False},
                                {"text": "Определяет вероятность", "is_correct": False},
                            ]
                        }
                    ]
                },
                {
                    "type": "open",
                    "title": "Открытое задание по линейной регрессии",
                    "description": "Объясните разницу между коэффициентами (w) и смещением (b) в линейной регрессии.",
                    "reference_answer": "Коэффициенты (w) определяют наклон линии и вес признаков, а смещение (b) задает пересечение линии с осью Y. Вместе они определяют уравнение прямой.",
                    "evaluation_criteria": "Правильное объяснение роли w (3 балла), роли b (3 балла), пример (2 балла)"
                },
                {
                    "type": "practical",
                    "title": "Практика: линейная регрессия",
                    "description": "Реализуйте простую линейную регрессию с использованием scikit-learn.",
                    "reference_answer": (
                        "from sklearn.linear_model import LinearRegression\n"
                        "import numpy as np\n\n"
                        "X = np.array([[1], [2], [3], [4], [5]])\n"
                        "y = np.array([2, 4, 6, 8, 10])\n\n"
                        "model = LinearRegression()\n"
                        "model.fit(X, y)\n\n"
                        "print(model.predict([[6]]))  # Ожидаемый результат: ~12\n"
                    ),
                    "evaluation_criteria": "Правильное использование LinearRegression (4), корректный fit/predict (3), читаемость кода (3)"
                }
            ]
        },
        {
            "title": "Логистическая регрессия",
            "description": "Введение в бинарную классификацию и модель логистической регрессии.",
            "theory": """
# Логистическая регрессия

**Что это?**
Логистическая регрессия - это метод машинного обучения для решения задач бинарной классификации, который предсказывает вероятность принадлежности к одному из двух классов.

**Основная формула:**
p = 1 / (1 + e^(-z))

Где z = β₀ + β₁x₁ + β₂x₂ + ... + βₙxₙ

**Функция активации:**
Сигмоидная функция преобразует любое значение в диапазон от 0 до 1, что интерпретируется как вероятность.

**Пример использования:**
- Предсказание вероятности заболевания по симптомам
- Определение спама в email
- Прогноз оттока клиентов

**Порог классификации:**
Обычно используется порог 0.5:
- Если p ≥ 0.5 → класс 1
- Если p < 0.5 → класс 0
""",
            "tasks": [
                {
                    "type": "test",
                    "title": "Тест по логистической регрессии",
                    "description": "Проверьте базовые знания о логистической регрессии",
                    "questions": [
                        {
                            "text": "Для чего используется логистическая регрессия?",
                            "choices": [
                                {"text": "Предсказание непрерывных значений", "is_correct": False},
                                {"text": "Бинарная классификация", "is_correct": True},
                                {"text": "Кластеризация данных", "is_correct": False},
                                {"text": "Снижение размерности", "is_correct": False},
                            ]
                        },
                        {
                            "text": "Какая функция активации используется в логистической регрессии?",
                            "choices": [
                                {"text": "ReLU", "is_correct": False},
                                {"text": "Sigmoid", "is_correct": True},
                                {"text": "Tanh", "is_correct": False},
                                {"text": "Softmax", "is_correct": False},
                            ]
                        },
                        {
                            "text": "Что предсказывает выход логистической регрессии?",
                            "choices": [
                                {"text": "Точное значение класса", "is_correct": False},
                                {"text": "Вероятность принадлежности к классу", "is_correct": True},
                                {"text": "Среднее значение признаков", "is_correct": False},
                                {"text": "Уровень ошибки", "is_correct": False},
                            ]
                        }
                    ]
                },
                {
                    "type": "open",
                    "title": "Открытое задание по логистической регрессии",
                    "description": "Объясните, почему логистическая регрессия подходит для бинарной классификации.",
                    "reference_answer": "Логистическая регрессия использует сигмоидную функцию, которая преобразует выход модели в вероятность от 0 до 1, что удобно для разделения на два класса.",
                    "evaluation_criteria": "Объяснено использование сигмоиды (4), связь с вероятностями (3), вывод о бинарной классификации (3)"
                },
                {
                    "type": "practical",
                    "title": "Практика: логистическая регрессия",
                    "description": "Обучите модель логистической регрессии на простом наборе данных (scikit-learn).",
                    "reference_answer": (
                        "from sklearn.linear_model import LogisticRegression\n"
                        "import numpy as np\n\n"
                        "X = np.array([[0], [1], [2], [3], [4], [5]])\n"
                        "y = np.array([0, 0, 0, 1, 1, 1])\n\n"
                        "model = LogisticRegression()\n"
                        "model.fit(X, y)\n\n"
                        "print(model.predict([[2]]))  # Ожидаемый результат: 0 или 1\n"
                    ),
                    "evaluation_criteria": "Правильное использование LogisticRegression (4), корректный fit/predict (3), читаемость кода (3)"
                }
            ]
        }
    ]

    return create_course_from_template(
        "Базовый курс по Machine Learning",
        "Основы машинного обучения: линейная и логистическая регрессия",
        topics,
        created_by
    )

# Реальный курс по vibe-кодингу ----------------------------------
@course_creator("vibe_coding", title="Вайб-кодинг для PM и PO",
                description="Как PM и PO могут использовать код для автоматизации и повышения эффективности")
def create_course_vibe_coding(created_by: int = None):
    topics = [
        {
            "title": "Вайб-кодинг для PM",
            "description": "Как PM может анализировать эффективность команды с помощью кода (без опыта программирования)",
            "theory": """
<h3>📈 Вайб-кодинг для PM</h3>

<h4>Как PM может анализировать эффективность команды с помощью кода (без опыта программирования)</h4>

<p>Ты — Product Manager. Ты не обязан писать код.<br>
Но ты обязан понимать данные.<br>
А ещё лучше — автоматизировать их анализ.<br><br>

Представь: у тебя есть файл <code>tasks.csv</code> — список задач команды. Ты хочешь быстро понять:</p>

<ul>
<li>Сколько задач сделано?</li>
<li>На что уходит больше всего времени?</li>
<li>Есть ли перекосы по приоритетам?</li>
</ul>

<p>Раньше ты делал это вручную.<br>
Сейчас — ты можешь написать скрипт, который сделает это за тебя.<br>
Даже если ты не программист. 🔥</p>

<br>

<h4>1.1 Что такое CSV и зачем он нужен</h4>

<p>CSV — это простой текстовый файл с таблицей.<br>
Каждая строка — задача.<br>
Каждый столбец — параметр:</p>

<pre><code>id, status, priority, estimate_hours
1, Done, High, 8
2, In Progress, Medium, 6
3, To Do, High, 10
</code></pre>

<p>Это твой источник данных.<br>
Ты не будешь считать вручную.<br>
Ты скажешь Python: <strong>"Прочитай файл, посчитай, покажи."</strong> 🐍</p>

<br>

<h4>1.2 Как Python помогает анализировать данные</h4>

<p>Python — язык, который идеально подходит для анализа данных.<br>
Два ключевых инструмента:</p>

<p><strong>Pandas</strong> — твой аналитический помощник</p>
<ul>
<li>Читает CSV одной строкой: <code>pd.read_csv("tasks.csv")</code>.</li>
<li>Позволяет фильтровать: <em>"Покажи все задачи со статусом Done"</em>.</li>
<li>Считает среднее, сумму, проценты.</li>
</ul>

<p>Пример:</p>
<pre><code>done_percent = (df["status"] == "Done").mean() * 100
</code></pre>

<p>Это значит:<br>
<strong>"Посчитай, сколько задач со статусом 'Done', подели на общее число, умножь на 100 — получишь процент."</strong></p>

<p><strong>Groupby</strong> — как группировка в Excel, но мощнее</p>
<pre><code>df.groupby("priority")["estimate_hours"].mean()
</code></pre>

<p><strong>"Сгруппируй задачи по приоритету и посчитай среднее время на каждую группу."</strong><br>
Это покажет, сколько времени в среднем уходит на High, Medium, Low.</p>

<br>

<h4>1.3 Как визуализировать данные (даже если ты не дизайнер)</h4>

<p>График лучше, чем таблица.<br>
Он сразу показывает дисбаланс.<br>
<strong>Matplotlib</strong> — библиотека для построения графиков. 📊</p>

<pre><code>avg_time.plot(kind="bar", title="Avg. Time by Priority")
plt.savefig("priority_chart.png")
</code></pre>

<p>Это создаст столбчатую диаграмму и сохранит её как картинку.<br>
Ты можешь вставить её в отчёт, презентацию, Slack — куда угодно.</p>

<br>

<h4>1.4 Как сделать отчет, который можно отправить команде</h4>

<p>Ты не просто хочешь цифры.<br>
Ты хочешь историю: что сделано, что задерживается, где риски.<br>
Создай файл <code>report.md</code> — это Markdown, простой текстовый формат, который читается везде (в Git, в Notion, в Telegram).</p>

<p>Пример содержания:</p>
<pre><code>## Отчет по задачам
- Завершено: 65.5%
### Среднее время:
- High: 8.2 ч
- Medium: 5.1 ч
- Low: 3.0 ч
</code></pre>

<p>Ты можешь сгенерировать такой файл автоматически — Python запишет туда результаты анализа. ✍️</p>

<br>

<h4>1.5 Как не сломать скрипт: обработка ошибок</h4>

<p>Иногда файл <code>tasks.csv</code> может:</p>
<ul>
<li>Не найтись.</li>
<li>Быть пустым.</li>
<li>Иметь битые данные.</li>
</ul>

<p>Хороший скрипт не падает, а говорит: <em>"Файл не найден"</em> — и не пытается считать.<br>
Для этого используется <code>try-except</code>:</p>

<pre><code>try:
    df = pd.read_csv("tasks.csv")
except FileNotFoundError:
    print("Ошибка: файл tasks.csv не найден")
</code></pre>

<p>Это как страховка. 🛡️<br>
Если что-то пошло не так — скрипт не умрёт, а сообщит об этом.</p>

<br>

<h4>1.6 Как использовать вайб-кодинг: промпты вместо кода</h4>

<p>Ты не должен помнить синтаксис.<br>
Ты должен понимать логику и давать ИИ правильные инструкции.<br>
Вот как ты можешь сформулировать промпт:</p>

<blockquote>
<p><strong>"Напиши Python-скрипт, который:</strong><br>
Читает CSV-файл <code>tasks.csv</code> с колонками: <code>id</code>, <code>status</code>, <code>priority</code>, <code>estimate_hours</code>.<br>
Считает процент задач со статусом 'Done'.<br>
Группирует задачи по приоритету и считает среднее время выполнения.<br>
Строит столбчатую диаграмму и сохраняет как <code>priority_chart.png</code>.<br>
Генерирует отчёт в формате Markdown <code>report.md</code> с процентами и средним временем.<br>
Обрабатывает ошибку, если файл не найден.<strong>"</strong></p>
</blockquote>

<p>ИИ (например, Qwen, ChatGPT) напишет код.<br>
Ты проверишь, запустишь — и получишь отчёт за 10 секунд. ⚡</p>

<br>

<h4>1.7 Что важно помнить PMу</h4>

<ul>
<li>Ты не должен быть программистом, но ты должен понимать, что можно автоматизировать.</li>
<li>Данные — твой главный инструмент принятия решений.</li>
<li>Отчёт должен быть понятен, даже если его читаешь не ты.</li>
<li>Автоматизация — это умножение твоего времени. Один раз написал скрипт — используешь каждую неделю.</li>
</ul>

<h4>🎯 Итог: твой чек-лист для автоматизации</h4>

<ul>
<li>✅ Есть CSV с задачами?</li>
<li>✅ Понимаешь, какие метрики важны (процент выполнения, среднее время)?</li>
<li>✅ Знаешь, как описать задачу для ИИ?</li>
<li>✅ Добавил обработку ошибок?</li>
<li>✅ Сохранил отчёт в понятном формате?</li>
</ul>

<p>Если да — ты уже не просто PM.<br>
Ты PM с суперсилой. 💪</p>

<br>

<h4>2. Что важно для PM: результат, а не активность</h4>

<p><strong>Вопрос:</strong> Что важнее — 1000 строк кода или одна завершённая задача?<br>
<strong>Правильный ответ:</strong> одна завершённая задача.<br><br>

Количество коммитов, строк кода, встреч — всё это процесс. А PM смотрит на результат.<br>
Если задача закрыта — значит, команда создала ценность. Если нет — значит, работа не доведена до конца.<br>
<strong>Task completion rate</strong> — ключевой индикатор. Он показывает, насколько команда выполняет то, что взяла на себя.</p>

<br>

<h4>3. Почему приоритеты нельзя игнорировать</h4>

<p>Представьте: 80% задач выполнено. Звучит отлично.<br>
Но если все эти 80% — низкоприоритетные, а главная задача горит неделю — это провал.<br><br>

Приоритеты — это фильтр ценности. Без них метрики врут.<br>
Анализ по приоритетам помогает понять:</p>

<ul>
<li>Работает ли команда над тем, что важно.</li>
<li>Не уходит ли она в "удобные", но бесполезные задачи.</li>
<li>Соответствует ли прогресс roadmap.</li>
</ul>
""",
            "tasks": [
                {
                    "type": "practical",
                    "title": "Практика: Анализ задач проекта",
                    "description": "Разработайте Python-скрипт для автоматизации анализа данных по задачам проекта (например, из CSV-файла).",
                    "reference_answer": '''import pandas as pd
import matplotlib.pyplot as plt


def generate_report():
    try:
        df = pd.read_csv("tasks.csv")
        done_percent = (df["status"] == "Done").mean() * 100
        avg_time = df.groupby("priority")["estimate_hours"].mean()
        
        # График
        avg_time.plot(kind="bar", title="Avg. Time by Priority")
        plt.savefig("priority_chart.png")
        
        # Отчет
        with open("report.md", "w") as f:
            f.write(f"## Отчет по задачам\\n- Завершено: {done_percent:.1f}%\\n")
            f.write("### Среднее время:\\n")
            for priority, time in avg_time.items():
                f.write(f"- {priority}: {time:.2f} ч\\n")
    except FileNotFoundError:
        print("Ошибка: файл tasks.csv не найден")


generate_report()''',
                    "evaluation_criteria": "Корректность обработки данных (проверка на наличие ошибок в CSV, обработка пустых значений) - 3 балла. Использование pandas для анализа и matplotlib для визуализации - 3 балла. Читаемость кода (PEP8, комментарии, структура функций) - 2 балла. Наличие обработки исключений (например, отсутствие файла) - 2 балла."
                },
                {
                    "type": "test",
                    "title": "Тест по вайб-кодингу для PM",
                    "description": "Проверьте понимание основ автоматизации для Product Manager",
                    "questions": [
                        {
                            "text": "Какой из следующих показателей наиболее важен для оценки эффективности команды разработки с точки зрения Product Manager?",
                            "choices": [
                                {"text": "Количество строк кода, написанных за неделю", "is_correct": False},
                                {"text": "Процент завершенных задач (task completion rate)", "is_correct": True},
                                {"text": "Количество проведённых встреч в спринте", "is_correct": False},
                                {"text": "Среднее время между коммитами", "is_correct": False},
                            ]
                        }
                    ]
                },
                {
                    "type": "open",
                    "title": "Открытый вопрос по приоритетам",
                    "description": "Почему важно учитывать приоритеты задач при анализе метрик проекта? Приведите пример, как неправильная интерпретация данных без учёта приоритетов может ввести в заблуждение.",
                    "reference_answer": "Учёт приоритетов позволяет понять, насколько эффективно команда работает над критически важными задачами. Например, если 80% задач завершены, но все они — низкого приоритета, а высокоприоритетные остаются висеть, это говорит о неправильном распределении ресурсов. Без анализа по приоритетам может сложиться ложное впечатление о высокой эффективности.",
                    "evaluation_criteria": "Упомянута связь приоритетов с бизнес-ценностями - 2 балла. Приведён пример искажения метрик - 3 балла. Логичность и ясность - 3 балла."
                }
            ]
        },
        {
            "title": "Вайб-кодинг для PO",
            "description": "Как PO может превращать 'сделать кнопку' в User Story с помощью кода",
            "theory": """
<h3>📋 Вайб-кодинг для PO</h3>

<h4>Как PO может превращать "сделать кнопку" в User Story с помощью кода</h4>

<p>Ты — Product Owner.<br>
Твоя задача — говорить на <strong>языке ценности</strong>, а не технических требований.</p>

<p><strong>Но требования приходят в виде:</strong></p>
<ul>
<li>"Добавить кнопку 'Купить'."</li>
<li>"Реализовать фильтр по цене."</li>
</ul>

<p>Это не User Story. Это указания.<br>
<strong>А User Story — это история пользователя:</strong></p>
<blockquote>
<p><strong>"Как пользователь, я хочу нажать кнопку 'Купить', чтобы оформить заказ."</strong></p>
</blockquote>

<p><strong>Проблема:</strong> таких требований может быть сотня.<br>
Переписывать вручную — <strong>долго, утомительно, скучно</strong>.<br>
<strong>Решение: автоматизировать</strong>.<br>
С помощью <strong>Python</strong> — и одного скрипта. 🤖</p>

<br>

<h4>1.1 Что такое User Story и зачем она нужна</h4>

<p><strong>User Story — это не просто шаблон</strong>.<br>
Это <strong>философия Agile</strong>.<br>
Она отвечает на три вопроса:</p>
<ul>
<li><strong>Кто?</strong> — роль пользователя.</li>
<li><strong>Что?</strong> — его желаемое действие.</li>
<li><strong>Зачем?</strong> — цель, выгода, ценность.</li>
</ul>

<p><strong>Пример:</strong></p>
<blockquote>
<p><strong>"Как пользователь, я хочу выбрать товар, чтобы оформить заказ."</strong></p>
</blockquote>

<p><strong>Без этого</strong> — команда не понимает смысла задачи.<br>
Она просто "делает кнопку", но не знает, <strong>зачем</strong>.</p>

<br>

<h4>1.2 Как автоматизировать перевод требований в User Story</h4>

<p>Ты не должен вручную переписывать каждое требование.<br>
Ты можешь написать скрипт, который:</p>
<ul>
<li>Прочитает файл <code>requirements.txt</code>.</li>
<li>Найдёт в тексте роль, действие, цель.</li>
<li>Сохранит всё в структурированный JSON.</li>
</ul>

<p>Для этого нужен <strong>Python и регулярные выражения</strong> (<code>re</code>).<br>
<strong>Регулярные выражения</strong> — это шаблон поиска.</p>

<p><strong>Например, строка:</strong></p>
<blockquote>
<p>"Добавить кнопку 'Купить'. Пользователь должен выбрать товар и оплатить."</p>
</blockquote>

<p><strong>Ты можешь сказать:</strong></p>
<blockquote>
<p><strong>"Найди всё после 'Добавить кнопку', затем — кто делает, и что он должен сделать."</strong></p>
</blockquote>

<p><strong>На языке Python это выглядит так:</strong></p>
<pre><code>pattern = r"(?:Добавить|Реализовать)\\s+\\"(.+?)\\"\\.\\s+(.+) (?:должен|может) (.+?)\\."
</code></pre>

<p><strong>Разберём:</strong></p>
<ul>
<li><code>(?:Добавить|Реализовать)</code> — ищет одно из слов (не захватывает).</li>
<li><code>\\"(.+?)\\"</code> — захватывает текст в кавычках (действие).</li>
<li><code>(.+)</code> — захватывает имя роли (например, "Пользователь").</li>
<li><code>(?:должен|может)</code> — ключевое слово.</li>
<li><code>(.+?)</code> — цель действия.</li>
</ul>
<p>Каждая часть попадает в <code>match.group(1)</code>, <code>group(2)</code> и т.д.</p>

<br>

<h4>1.3 Как обрабатывать неидеальные данные</h4>

<p>В реальности текст бывает:</p>
<ul>
<li>С опечатками.</li>
<li>Без кавычек.</li>
<li>С несколькими предложениями.</li>
<li>Пустыми строками.</li>
</ul>

<p><strong>Хороший скрипт должен быть устойчив к шуму.</strong></p>

<p><strong>Edge cases и как их обойти:</strong></p>
<ul>
<li><strong>Пустая строка:</strong> Проверяй: <code>if line.strip()</code></li>
<li><strong>Роль не указана:</strong> Используй <code>Unknown</code> или пропусти</li>
<li><strong>Нет цели:</strong> Не добавляй в результат</li>
<li><strong>Опечатка в "должен" → "дожен":</strong> Упрости паттерн или используй NLP</li>
</ul>

<p>Для сложных случаев можно подключить <strong>spaCy</strong> — библиотеку для анализа естественного языка.<br>
Она сама определит, где подлежащее, сказуемое, объект.<br>
Но для начала — хватит и регулярных выражений. 🧠</p>

<br>

<h4>1.4 Как сохранить результат в структурированном виде</h4>

<p><strong>JSON</strong> — идеальный формат для хранения данных.<br>
Он читается и в Python, и в Notion, и в Jira.<br>
<strong>Твой скрипт должен:</strong></p>
<ul>
<li>Собрать все найденные User Story в список.</li>
<li>Записать в <code>user_stories.json</code>:</li>
</ul>

<pre><code>[
  {
    "role": "пользователь",
    "action": "выбрать товар",
    "goal": "оплатить"
  }
]
</code></pre>

<p><strong>Используй:</strong></p>
<pre><code>json.dump(stories, f, indent=2, ensure_ascii=False)
</code></pre>
<ul>
<li><code>indent=2</code> — красивое форматирование.</li>
<li><code>ensure_ascii=False</code> — чтобы сохранялись русские буквы.</li>
</ul>

<br>

<h4>1.5 Как использовать вайб-кодинг: промпт вместо кода</h4>

<p>Ты не должен помнить синтаксис <code>re.search()</code> или <code>json.dump()</code>.<br>
Ты должен понимать логику и дать ИИ чёткое задание.<br>
<strong>Вот пример промпта:</strong></p>

<blockquote>
<p><strong>"Напиши Python-скрипт, который:</strong><br>
Читает файл <code>requirements.txt</code>, где каждая строка — требование вида:<br>
'Добавить кнопку "Купить". Пользователь должен выбрать товар и оплатить.'<br>
Извлекает:<br>
действие (в кавычках),<br>
роль (например, 'Пользователь'),<br>
цель (после 'должен' или 'может').<br>
Формирует словарь: <code>{'role': ..., 'action': ..., 'goal': ...}</code>.<br>
Сохраняет список таких словарей в <code>user_stories.json</code>.<br>
Обрабатывает пустые строки и строки без совпадений.<strong>"</strong></p>
</blockquote>

<p>ИИ (например, Qwen или ChatGPT) напишет рабочий код.<br>
Ты запускаешь — и получаешь структурированные User Story за секунды. ⚡</p>

<br>

<h4>1.6 Почему это важно для PO</h4>

<ul>
<li><strong>Ты экономишь время</strong>. Вместо 10 минут на одну задачу — 10 секунд на сотню.</li>
<li><strong>Ты улучшаешь качество требований</strong>. Автоматизация заставляет тебя формализовать шаблоны.</li>
<li><strong>Ты становишься мостом между бизнесом и командой</strong>. Ты не просто передаёшь "сделать", ты создаёшь контекст.</li>
<li><strong>Ты учишься думать как инженер</strong>. Даже если не пишешь код — ты понимаешь, как данные превращаются в систему.</li>
</ul>

<h4>🎯 Итог: твой чек-лист для автоматизации</h4>

<ul>
<li>✅ Есть файл с требованиями в текстовом виде?</li>
<li>✅ Видишь шаблон в формулировках (например, "Добавить...", "Реализовать...")?</li>
<li>✅ Можешь выделить: действие, роль, цель?</li>
<li>✅ Знаешь, как описать задачу для ИИ?</li>
<li>✅ Проверил, что скрипт работает на пустых и битых строках?</li>
</ul>

<p>Если да — ты уже не просто PO.<br>
Ты Product Owner с инструментами будущего. 🚀</p>

<br>

<h4>2. Почему User Story — стандарт Agile</h4>

<blockquote>
<p><strong>"Разработать модуль аутентификации" — это не про пользователя.</strong><br>
<strong>"Как пользователь, я хочу войти через Google" — это про удобство, про боль, про ценность.</strong></p>
</blockquote>

<p>User Story фокусирует команду <strong>не на технике, а на пользе</strong>.<br>
Она отвечает на вопрос: <strong>зачем это нужно</strong>?<br>
Без этого контекста разработка превращается в конвейер задач без смысла.</p>

<br>

<h4>3. Зачем давать команде свободу</h4>

<p>Если PO говорит:</p>
<blockquote>
<p><strong>"Сделайте авторизацию через JWT"</strong>,</p>
</blockquote>

<p>он лишает команду права думать.<br>
А если он говорит:</p>
<blockquote>
<p><strong>"Как пользователь, я хочу быстро входить, не вводя пароль"</strong>,</p>
</blockquote>

<p>— команда может предложить OAuth, биометрию, magic link — что угодно.<br>
<strong>User Story — это что и зачем</strong>.<br>
<strong>Как</strong> — решает команда.<br>
Это повышает вовлечённость, качество решений и скорость инноваций. 💡</p>
""",
            "tasks": [
                {
                    "type": "practical",
                    "title": "Практика: Преобразование требований в User Story",
                    "description": "Создайте скрипт на Python, который преобразует список требований из свободного текста в структурированные User Story",
                    "reference_answer": '''import re
import json

def parse_requirements():
    stories = []
    pattern = r"(?:Добавить|Реализовать)\\s+(?:кнопку|фильтр)\\s+\\"(.+?)\\"\\.\\s+(.+) (?:должен|может) (.+?)\\."
    
    with open("requirements.txt") as f:
        for line in f:
            match = re.search(pattern, line)
            if match:
                action = match.group(1).lower()
                role = match.group(2).split()[0].lower()
                goal = match.group(3)
                stories.append({"role": role, "action": action, "goal": goal})
    
    with open("user_stories.json", "w") as f:
        json.dump(stories, f, indent=2, ensure_ascii=False)

parse_requirements()''',
                    "evaluation_criteria": "Точность извлечения данных (проверка на сложных примерах, например, с опечатками) - 3 балла. Использование регулярных выражений или NLP-библиотек (например, spaCy) - 3 балла. Корректная обработка edge cases (пустые строки, отсутствие ролей) - 2 балла. Читаемость кода - 2 балла."
                },
                {
                    "type": "test",
                    "title": "Тест по User Story",
                    "description": "Проверьте понимание основ User Story и Agile",
                    "questions": [
                        {
                            "text": "Какой из следующих форматов лучше всего подходит для формулировки пользовательских требований в Agile?",
                            "choices": [
                                {"text": '"Разработать модуль аутентификации"', "is_correct": False},
                                {"text": '"Система должна поддерживать OAuth 2.0"', "is_correct": False},
                                {"text": '"Как пользователь, я хочу войти через Google, чтобы не создавать новый пароль"', "is_correct": True},
                                {"text": '"Добавить 5 API-эндпоинтов для авторизации"', "is_correct": False},
                            ]
                        }
                    ]
                },
                {
                    "type": "open",
                    "title": "Открытый вопрос по User Story",
                    "description": "Почему важно избегать технических деталей при формулировке User Story? Как это влияет на команду разработки?",
                    "reference_answer": "User Story должны описывать что нужно пользователю, а не как это реализовать. Это даёт команде свободу выбирать наилучшее техническое решение. Если PO сразу указывает реализацию (например, \"добавить JWT\"), команда может упустить более эффективные альтернативы. Кроме того, это способствует вовлечённости разработчиков в процесс принятия решений.",
                    "evaluation_criteria": "Понимание цели User Story (пользовательская ценность) - 3 балла. Упоминание автономии команды - 3 балла. Чёткость и логика - 2 балла."
                }
            ]
        }
    ]

    return create_course_from_template(
        "Вайб-кодинг для PM и PO",
        "Как PM и PO могут использовать код для автоматизации и повышения эффективности",
        topics,
        created_by
    )