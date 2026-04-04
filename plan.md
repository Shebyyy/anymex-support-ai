# Архитектурный обзор: anymex-support-ai

## Текущее состояние

| Компонент       | Файл             | Строк      | Технология                    |
| --------------- | ---------------- | ---------- | ----------------------------- |
| Web dashboard   | `app.py`         | **3815**   | Flask (sync)                  |
| Discord bot     | `bot.py`         | **4010**   | discord.py (async)            |
| Base template   | `base.html`      | **1524**   | Jinja2 + Vanilla JS           |
| Board page      | `board.html`     | **1498**   | Jinja2 + Vanilla JS           |
| TODO page       | `todo_page.html` | **855**    | Jinja2 + Vanilla JS           |
| Шаблоны (всего) | 17 файлов        | ~6000+     | Jinja2 + ~500 строк JS в base |
| База данных     | GitHub API       | JSON файлы | github.com contents API       |
| Деплой          | Dockerfile       | 16         | gunicorn + bot.py             |

---

## 🔴 Критические проблемы

### 1. GitHub как база данных

**Это самая серьёзная архитектурная проблема.**

Все данные (TODO, комментарии, сессии, активность, конфиг, форумные посты) хранятся как JSON-файлы в GitHub-репозитории через Contents API.

```
gh_read()  → GET /repos/.../contents/todos.json     → base64 decode → JSON parse
gh_write() → PUT /repos/.../contents/todos.json      → JSON serialize → base64 encode → commit
```

Последствия:

| Проблема                  | Влияние                                                                 |
| ------------------------- | ----------------------------------------------------------------------- |
| **Race conditions**       | Два одновременных запроса → один перезаписывает другого (SHA конфликт)  |
| **Latency**               | Каждое чтение/запись — HTTP-запрос к GitHub API (~200-500ms)            |
| **Rate limits**           | GitHub API: 5000 req/hour (auth). При активном использовании — упрётесь |
| **Отсутствие индексов**   | Поиск TODO = загрузить весь файл + линейный обход                       |
| **Отсутствие транзакций** | Обновление TODO + комментарий = 2 HTTP-запроса без atomicity            |
| **Размер данных**         | При 1000 TODO todos.json > 1MB → каждый запрос грузит всё               |
| **Сессии в GitHub**       | `session_read_all()` → GitHub API вызов **на каждый HTTP-запрос**       |

> [!CAUTION]
> Особенно критична ситуация с сессиями: `get_session()` вызывается в каждом route handler и в `inject_nav_counts()` (context processor), что означает **минимум 2 GitHub API запроса на каждый pageload** только для авторизации.

### 2. Монолитные файлы

| Файл               | Строк                                                                                                                                                                                              | Что содержит |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| `app.py` (3815)    | Routes, auth, GitHub helpers, Discord helpers, forum sync, activity log, member cache, template enrichment, API endpoints — **всё в одном файле**                                                  |
| `bot.py` (4010)    | Bot commands, slash commands, GitHub helpers (дубль), embeds, health server, board rendering — **всё в одном файле**                                                                               |
| `base.html` (1524) | CSS (323 строки) + HTML (nav, modals) + **JS (750+ строк)**: Quick View, emoji picker, mention autocomplete, markdown rendering, notifications, paste images, unread tracker + ещё CSS (210 строк) |

**Проблемы:**

- Невозможно рефакторить одну часть без риска сломать другую
- IDE тормозит на файлах такого размера
- Merge conflicts при работе в команде
- Дублирование кода между app.py и bot.py (~30-40% общей логики)

### 3. Дублирование между app.py и bot.py

Код скопирован между файлами:

```python
# app.py (синхронный)
def gh_read(filepath, force=False):
    r = req.get(url, headers=gh_headers(), timeout=10)
    ...

# bot.py (асинхронный)
async def gh_read(session, filepath):
    async with session.get(url, headers=gh_headers()) as r:
        ...
```

Дублируются:

- `gh_read()` / `gh_write()` — полностью
- `STATUS_LABELS`, `STATUS_COLORS`, `STATUS_ICONS`, `PRIORITY_*` — полностью
- `TAG_COLORS` — полностью
- `DEFAULT_CONFIG` — полностью
- `FILE_*` константы — полностью
- `log_activity()` — почти полностью

### 4. Синхронный Flask с блокирующим I/O

`app.py` использует синхронный Flask + `requests` для всех внешних вызовов:

- GitHub API (gh_read/gh_write)
- Discord API (bot_get, discord_get)
- Bot notification (notify_bot_board)

**С gunicorn `--workers 1`** (Dockerfile) это означает: один долгий GitHub API запрос блокирует **все** параллельные запросы.

### 5. Фронтенд без компонентной модели

- **~500 строк JavaScript** в `base.html` (глобальные функции)
- **~800+ строк JS** в `board.html`, `todo_page.html` и других
- Рендеринг через `innerHTML` с хардкоженными стилями в строках
- Emoji picker (150+ строк), mention autocomplete (100+ строк), markdown renderer — всё в одном `<script>` блоке
- Нет модулей, бандлера, минификации

---

## 🟡 Средние проблемы

### 6. Отсутствие обработки ошибок и retry

```python
def gh_write(filepath, data, sha, msg):
    try:
        r = req.put(url, ...)
        return r.status_code in (200, 201)  # SHA conflict → silent False
    except Exception as e:
        print(f"[gh_write] Exception: {e}")
        return False
```

При SHA-конфликте (concurrent edit) данные просто теряются. Нет retry, нет уведомления пользователю.

### 7. Inline стили в JavaScript

```javascript
assignedEl.innerHTML = `<a style="text-decoration:none;display:inline-flex;
  align-items:center;gap:7px;background:var(--surface2);border:1px solid 
  var(--border);border-radius:20px;padding:3px 10px 3px 4px">...`;
```

Это повторяется десятки раз. Невозможно поддерживать, невозможно отлаживать.

### 8. Нет типизации и тестов

- 0 тестов (unit, integration, e2e)
- 0 type hints (кроме нескольких мест)
- 0 CI/CD pipeline
- `enrich_todo()` работает с `dict` без валидации — любое change может сломать все шаблоны

---

## Рекомендации по приоритету

### 🏃 Быстрые победы (1-2 дня, минимальный риск)

#### A1. Перенести сессии из GitHub в signed cookies

```python
# Сейчас (каждый запрос = GitHub API call):
def get_session():
    sid = request.cookies.get("sid")
    return session_get(sid) or {}

# Предлагаю (zero I/O):
# Flask session уже подписывает cookie через secret_key
# Просто использовать session["user"] = user_data
```

**Выигрыш:** -2 GitHub API запроса на каждый pageload. Это **удвоит** скорость загрузки каждой страницы.

#### A2. Вынести дублирующиеся константы в `shared.py`

```
anymex-support-ai/
├── shared.py          # STATUS_*, PRIORITY_*, TAG_*, FILE_*, DEFAULT_CONFIG
├── app.py             # import from shared
└── bot.py             # import from shared
```

**Выигрыш:** Изменение конфига/констант в одном месте вместо двух.

#### A3. Вынести inline JS-стили в CSS-классы

Создать утилитарные классы в `base.html`:

```css
.chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 3px 10px 3px 4px;
}
.chip-avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  flex-shrink: 0;
}
.meta-label {
  font-size: 0.72rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
```

Затем заменить `innerHTML` строки:

```javascript
// Было:
el.innerHTML = `<a style="text-decoration:none;display:inline-flex;...">`;

// Стало:
el.innerHTML = `<a class="chip">`;
```

---

### 📋 Среднесрочные (1-2 недели)

#### B1. Заменить GitHub DB на SQLite

```
anymex-support-ai/
├── db/
│   ├── models.py      # dataclasses или pydantic models
│   ├── database.py    # SQLite connection + CRUD
│   └── migrate.py     # Import existing GitHub JSON → SQLite
├── data.db            # SQLite database file
```

**Выигрыши:**

- Чтение/запись за ~1ms вместо ~300ms
- Индексы, поиск, фильтрация в SQL
- Транзакции — нет race conditions
- Нет rate limits
- Файл можно бэкапить в GitHub раз в час через cron (если нужна персистентность)

> [!IMPORTANT]
> Это **самое импактное** изменение, которое можно сделать. Каждая страница станет в 10-100x быстрее.

#### B2. Разбить app.py на модули

```
anymex-support-ai/
├── app/
│   ├── __init__.py        # Flask app factory
│   ├── config.py          # Config loading
│   ├── auth.py            # OAuth, session, decorators
│   ├── models.py          # Pydantic/dataclass models
│   ├── routes/
│   │   ├── pages.py       # HTML page routes
│   │   ├── api_todos.py   # /api/todo/* endpoints
│   │   ├── api_forum.py   # /api/forum/* endpoints
│   │   ├── api_members.py # /api/members/*
│   │   └── api_config.py  # /api/config
│   ├── services/
│   │   ├── todo.py        # TODO business logic
│   │   ├── comments.py    # Comment CRUD
│   │   ├── forum.py       # Discord forum sync
│   │   └── activity.py    # Activity logging
│   └── discord.py         # Discord API helpers
```

#### B3. Разбить base.html

```
templates/
├── base.html              # Минимальный shell: head + nav + main + scripts
├── partials/
│   ├── _nav.html          # Navigation
│   ├── _quick_view.html   # Quick View modal
│   └── _toasts.html       # Toast container
static/
├── css/
│   ├── base.css           # Design system tokens + globals
│   ├── components.css     # Buttons, cards, badges, pills
│   └── pages/             # Per-page overrides
├── js/
│   ├── api.js             # toast(), api() helper
│   ├── quick-view.js      # Quick View modal logic
│   ├── emoji-picker.js    # Emoji picker
│   ├── mentions.js        # @mention autocomplete
│   └── markdown.js        # Markdown renderer
```

---

### 🏗️ Долгосрочные (месяцы, если проект продолжит расти)

#### C1. Async бэкенд (Quart или FastAPI)

Flask не подходит для I/O-heavy приложений. С SQLite это менее критично, но если останется Discord API integration:

```python
# FastAPI example
@app.get("/board")
async def board():
    todos = await db.get_active_todos()
    return templates.TemplateResponse("board.html", {"todos": todos})
```

#### C2. Фронтенд-фреймворк (только если UI станет ещё сложнее)

Текущий UI уже очень сложный (kanban board, emoji picker, threaded comments, drag & drop). Если функционал продолжит расти — имеет смысл рассмотреть переход на:

- **Svelte/SvelteKit** — минимальный бандл, отличный DX, как замена Jinja2+vanilla JS
- **Vue 3 + Vite** — хорошо ложится на постепенную миграцию

Но это **не нужно сейчас**, если UI не планирует расти существенно.

#### C3. Типизация и тесты

```python
# Pydantic model вместо dict
class Todo(BaseModel):
    id: int
    title: str
    status: Literal["todo", "in_progress", "review_needed", "blocked", "done"]
    priority: Literal["low", "medium", "high"] = "medium"
    tags: list[str] = []
    ...
```

Pytest + httpx для API тестов. Playwright для E2E.

---

## Приоритизация

| #      | Действие                   | Усилие     | Импакт     | Риск         |
| ------ | -------------------------- | ---------- | ---------- | ------------ |
| **A1** | Сессии → Flask cookie      | 1 час      | 🟢🟢🟢     | Очень низкий |
| **A2** | shared.py для констант     | 30 мин     | 🟢         | Очень низкий |
| **A3** | CSS-классы вместо inline   | 3-4 часа   | 🟡🟡       | Низкий       |
| **B1** | SQLite вместо GitHub DB    | 1-2 недели | 🟢🟢🟢🟢🟢 | Средний      |
| **B2** | Разбить app.py на модули   | 3-5 дней   | 🟡🟡🟡     | Средний      |
| **B3** | Разбить base.html + static | 2-3 дня    | 🟡🟡       | Низкий       |
| **C1** | Async бэкенд               | 2-3 недели | 🟡🟡       | Высокий      |
| **C2** | Фронтенд-фреймворк         | 1-2 месяца | 🟡🟡🟡     | Высокий      |

## Открытые вопросы

1. **Сколько пользователей у дашборда?** — От этого зависит критичность перехода с GitHub DB
2. **Планируется ли рост функционала?** — Если да, модуляризация (B2) становится приоритетом
3. **Нужна ли совместимость с текущим GitHub DB?** — Бот и дашборд используют один и тот же JSON. Переход на SQLite = нужно менять оба
4. **Какие из предложенных шагов интересно рассмотреть?** — Могу начать с любого из quick wins или подготовить детальный план для B1/B2
