# AGENTS.md — book-translate-ru

Skill (по спецификации Agent Skills) для художественного перевода EN→RU: EPUB/FB2 → чанки → субагенты → глоссарий → полировка → FB2. Репозиторий — не приложение, а набор инструкций и скриптов: **устанавливается копированием папки в skills-каталог платформы** (`~/.claude/skills/`, `~/.kilo/skills/`). Любое изменение здесь влияет на все будущие переводы — после правок сверяй совместимость с `SKILL.md`, `references/` и `config.toml`.

## Языки

- По-русски: вся документация (`SKILL.md`, `README.md`, `references/`, `prompts/`, `templates/`), комментарии в `config.toml`, сообщения коммитов.
- По-английски: код, CLI-аргументы, поля JSON, docstring'и. Не смешивать в одном файле.

## Архитектура (неочевидно из имён файлов)

- `SKILL.md` — lean-оркестратор для агента-переводчика. Детальные шаги — в `references/phase-{1,2,3}-*.md`, читаются агентом по мере продвижения (устойчивость к сжатию контекста). Новые правила для пайплайна добавляй в `references/` и связывай из `SKILL.md`, а не только в скрипты.
- Пайплайн: 9 шагов, 3 фазы. Состояние — `progress.json` в `<книга>_temp/`, resume через `scripts/shared/load_state.py` / `save_state.py`. Принцип: оркестратор не делает LLM-работу сам — всё переводят/полируют субагенты по промптам из `prompts/`.
- Схемы данных (`glossary.json` с `"version": 2`, `meta.json`) — `references/meta-json-schema.md`. Термин-id генерируется из английского названия (`make_term_id` в `common.py`); правка `glossary.json` — только через шаблон `scripts/shared/edit_glossary_template.py`.
- Pandoc нужен **только** для конвертации EPUB→Markdown (шаг 1). Сборка FB2 (шаг 9) — напрямую на `lxml` + XSD-валидация (`scripts/phase3_finish/fb2_builder.py`).

## Команды и проверка

```bash
pip install -r requirements.txt    # только beautifulsoup4, lxml — stdlib-first, тяжёлые зависимости не добавлять
python3 {baseDir}/scripts/shared/config.py   # smoke-проверка конфига (вывод DEFAULTS)
```

- Скрипты вызываются как `python3 {baseDir}/scripts/<phase>/<script>.py "<temp_dir>"`, где `{baseDir}` — корень скилла (подставляется агентом на месте), `<temp_dir>` — рабочая папка книги. Запускаются из рабочей директории пользователя, где лежит `<книга>_temp/`.
- `config.toml` ищется: `cwd/config.toml` → `cwd/*_temp/config.toml` → корень скилла (`config.py`, `_find_config_toml`).
- **Тестов, линтера и CI нет.** Проверка — ручной прогон скрипта на реальной книге (создать `*_temp/` и гонять скрипты по шагам). У многих скриптов есть `__main__` smoke-блоки.

## Конвенции скриптов

- Импорт shared без `__init__.py`: в начале скрипта
  `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))`,
  затем `from common import ...`, `from config import ...` (образец: `scripts/phase2_translate/merge_meta.py:52`).
- Переиспользуй `common.py` (`process_dir`, `run_cmd`, `sha256_file`, `make_term_id`, `ensure_term_ids`) и `config.py` (`atomic_write_text/json`, `read_json_safe`, `sanitize_for_json`, `read_text_safe`) — не дублируй их в новых скриптах.
- `DEFAULTS` в `config.py` и значения в `config.toml` — два источника умолчаний; при правке одного синхронизируй другой. Секция `[parallelism] batch_size` — рекомендация оркестратору, скрипты её не читают.
- Выход скриптов — `sys.stdout` в UTF-8 (реконфигурится в `config.py`), файлы — через `atomic_write_*` (Windows-safe).

## Windows / PowerShell (репо разрабатывается на Windows)

Кратко: `python3 -c` с циклами/генераторами молча ничего не выводит — пиши временные `.py` файлы; `<` редирект не работает; при кириллических путях сначала `cd` в родителя, потом относительный путь; `WinError 5` при атомарной записи — норма (скрипт сам откатывается). Подробно: `references/windows-powershell.md` — обязателен к прочтению перед любым прогоном скриптов здесь.

## Git

- `*_temp/` и `__pycache__/` игнорируются: тестовые прогоны книг не коммитятся. `!.github/` — исключение для CI.
- Стиль коммитов — conventional, по-русски (`feat(glossary): ...`, `fix(...): ...`).
