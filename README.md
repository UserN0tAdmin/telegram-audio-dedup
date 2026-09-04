# Telegram Audio Deduplicator (dedup)

Скрипт наводит порядок в аудиокнижках и музыке в Telegram-чатах: синхронизирует
историю чатов в локальную SQLite-базу, находит дубликаты (и точные копии, и
«одна и та же песня в разном качестве и с разными именами»), по желанию
архивирует их в запасной чат и удаляет лишнее.

Работает через личный аккаунт (MTProto-клиент Kurigram), Python 3.12+, всё
крутится на asyncio.

## Что умеет

- **Синхронизация**: выкачивает метаданные всех аудио из указанных чатов в
  локальную БД (`music_library.sqlite`); повторные прогоны инкрементальные.
- **Точное сопоставление**: одинаковый `file_unique_id` (буквально тот же файл
  на серверах Telegram) и полные совпадения метаданных.
- **Нечёткое сопоставление (fuzzy)**: настраиваемый движок на RapidFuzz +
  NumPy — сравнивает имя файла и теги (performer/title), длительность и размер,
  с настраиваемыми весами, степенями строгости и штрафом за несовпадение чисел
  в названиях. Подробно: [docs/fuzzy.md](docs/fuzzy.md).
- **Архивация перед удалением**: дубликаты пересылаются в запасной чат
  (Избранное, канал — что укажешь), и только потом удаляются. Батч, который не
  удалось заархивировать, не удаляется.
- **Защита от кривых рук**: `dry_run` (симуляция), `report_only` (только
  отчёты), ignore-списки конкретных сообщений, regex-защита по имени/тегам,
  верификация каждого кандидата через API перед удалением, автоматический
  бэкап БД, блокировка второго экземпляра, контроль свободного места.
- **Утилиты**: текстовый отчёт о дублях со ссылками, скачивание всей аудиоты
  чата на диск, экспорты (txt/csv/xlsx), ремонт и оптимизация БД.

## Быстрый старт

```bash
# 1. Python 3.12+ и окружение
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .

# 2. Креды приложения Telegram (https://my.telegram.org)
cp .env.example .env               # Windows: copy .env.example .env
# → вписать в .env свои TG_API_ID и TG_API_HASH

# 3. Конфиг из полностью прокомментированного шаблона
cp config.example.cfg config.cfg   # Windows: copy config.example.cfg config.cfg
# → минимум: указать в [core] свои чаты в chat_list

# 4. Первый запуск: авторизация (создаст my_account.session) + синхронизация + отчёт
python main.py report
```

`report` ничего не удаляет: синхронизирует чаты в локальную БД и складывает
в `exports/` наглядные отчёты о найденных дубликатах. Для боевого прогона
остаётся выключить `dry_run` (он по умолчанию включён как страховка). Все
остальные настройки имеют разумные дефолты (подробно прокомментированы в самом
`config.example.cfg`); полный справочник —
[docs/configuration.md](docs/configuration.md).

Подробнее про установку, прокси и первый вход: [docs/installation.md](docs/installation.md).

## Команды

| Команда | Что делает |
|---|---|
| `python main.py` | Полный прогон: синхронизация → поиск дубликатов → (архивация →) удаление по `chat_list` |
| `python main.py report` | Отчёт о группах дубликатов со ссылками `t.me`, без удаления |
| `python main.py sync [чат] [--force]` | Только синхронизация (все чаты из `chat_list` или один); `--force` — полный перескан |
| `python main.py repair` | Ремонт и оптимизация БД (сверка битых записей с Telegram, VACUUM) |
| `python main.py download <чат>` | Скачать все аудио чата в `downloads/<chat_id>/` |
| `python main.py export filenames <чат>` | Имена файлов из БД в txt |
| `python main.py export filenames-url <чат>` | Имена + ссылки на сообщения в txt |
| `python main.py export cleaned-names [чат]` | Диагностика очистки имён (CSV) |
| `python main.py export cleaned-meta [чат]` | Диагностика очистки тегов performer+title (CSV) |
| `python main.py export xlsx [чат]` | Дамп таблиц БД в Excel |
| `python main.py search "<запрос>"` | Нечёткий поиск (с пониманием опечаток) по всем чатам БД: топ-200 совпадений в консоль, включая поиск по «мусору» в именах (сайты-качалки, расширения) |

`<чат>` — числовой ID, `@username` или ссылка `t.me`. Любую опцию
`config.cfg` можно перекрыть на один запуск аргументами: `--dry-run`,
`--chat`, `--threshold` или универсальным
`--set SECTION.OPTION=VALUE` (например, `python main.py --no-dry-run --chat @music_shiz`);
справочник — [docs/configuration.md](docs/configuration.md#перекрытие-аргументами-командной-строки).
Полное описание команд с примерами и типичными сценариями: [docs/usage.md](docs/usage.md).

## Документация

| Файл | Содержание |
|---|---|
| [docs/installation.md](docs/installation.md) | Установка, `.env`, первый вход, прокси (включая MTProto), структура каталогов |
| [docs/usage.md](docs/usage.md) | Все команды и режимы, пайплайн полного прогона, модель безопасности |
| [docs/configuration.md](docs/configuration.md) | Справочник всех секций `config.cfg` с дефолтами и рекомендациями |
| [docs/fuzzy.md](docs/fuzzy.md) | Как работает нечёткий поиск и как его тюнить |
| [tests/README.md](tests/README.md) | Тестовый набор: запуск, livedb-тесты, golden-числа |

## Технические детали

- **Вход**: `python main.py`; подкоманды разбирает argparse (`dedup/cli.py`),
  оркестрация — `main.py`, вся логика — в пакете `dedup/`.
- **БД**: SQLite (aiosqlite, WAL). Таблица `audios` хранит по одному ряду на
  аудиосообщение; `chat_sync_state` — курсоры инкрементальной синхронизации.
- **Зависимости**: kurigram (форк pyrogram), rapidfuzz, numpy, aiosqlite,
  openpyxl, fasteners, python-dotenv, uvloop/winloop. Единый манифест —
  `pyproject.toml`: зависимости и dev-группа (pytest, ruff, mypy и т.п. —
  `pip install -e . --group dev`).
- **Язык**: код, докстринги, логи и документация — на русском.

## Осторожно

Скрипт **удаляет сообщения в Telegram**. Механизмы безопасности расписаны в
[docs/usage.md](docs/usage.md), но главное правило одно: сначала
`python main.py report` и внимательный разбор групп — и только потом боевой
прогон с `dry_run = False`.

Пользуйтесь `[ignore_list]` и `[ignore_regex]`

## Лицензия

Проект распространяется под лицензией [GPL-3.0](COPYING).
