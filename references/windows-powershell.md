# Windows + PowerShell: ограничения командной строки

На Windows с PowerShell есть известные проблемы с многострочными командами
и pipe. Главный агент (оркестратор) должен избегать этих ловушек.

---

## Проблема 1: многострочный `python3 -c "..."` и инлайн-циклы молча не работают

```powershell
# НЕ РАБОТАЕТ на PowerShell — молча, без вывода:
python3 -c "
import json
print(json.load(open('file.json')))
"

# ОПАСНО — list comprehensions / for-циклы тоже молчат:
python3 -c "print([x for x in range(10)])"
python3 -c "for x in lst: print(x)"

# РАБОТАЕТ — одна строка без циклов/генераторов:
python3 -c "import json; print(json.load(open('file.json')))"

# РАБОТАЕТ И НАДЁЖНО — скрипт в файле:
python3 script.py
```

**Правило для оркестратора (КРИТИЧНО):**

1. Любой Python-код сложнее одного выражения **пиши во временный
   `.py` файл** и запускай как `python3 <file>.py`. Это особенно
   касается for-циклов, list/dict comprehensions, lambda с генераторами,
   многострочных конструкций.
2. Не используй `python3 -c` ни для чего, кроме одного простого
   выражения без циклов. Если нужен цикл / comprehension / try-except —
   пиши файл. **На PowerShell инлайн-циклы молча возвращают пустой
   stdout**, что легко принять за «нет данных» и пойти по ложному пути.
3. Временные `.py`-файлы клади в `<temp_dir>/process/` (или рядом со
   скриптами скилла), не в корень рабочей директории — не засоряй
   пользовательское пространство. Префикс `_` в имени (`_verify.py`,
   `_apply.py`, `_fixman.py`) помогает отличать их от штатных скриптов
   скилла.

---

## Проблема 2: `<` redirection не работает в PowerShell

```powershell
# НЕ РАБОТАЕТ — '<' reserved for future use:
python3 script.py < input.json

# РАБОТАЕТ — Get-Content | python3:
Get-Content -Path "input.json" -Encoding UTF8 -Raw | python3 script.py

# РАБОТАЕТ — script читает файл сам:
python3 script.py --input input.json
```

**Правило:** для `merge_meta.py apply-merge` (который читает payload из
stdin) — **не используй stdin-чтение через pipe на Windows с кириллическими
путями**. Это нестабильно. Лучше:

- **Либо** `cd` в родительскую директорию, затем используй относительные
  пути + `Get-Content ... | python3 ...` с `-Encoding UTF8 -Raw`.
- **Либо (рекомендуется)** используй штатный template-скрипт
  `scripts/shared/apply_merge_template.py`. Скопируй его в
  `<temp_dir>/process/_apply.py`, отредактируй пути и список `decisions`,
  запусти. Скрипт вызывает `apply_merge()` напрямую через Python import,
  обходит stdin/CLI, и автоматически вкладывает `variants`/`proposed_variants`
  из `prepared` (с авто-детекцией поля — важно, т.к. поле называется
  по-разному для разных kind).

```bash
cp {baseDir}/scripts/shared/apply_merge_template.py "<temp_dir>/process/_apply.py"
# Отредактируй SKILL_DIR, TEMP_DIR и decisions в _apply.py
python3 "<temp_dir>/process/_apply.py"
```

---

## Проблема 3: пути с кириллицей

```powershell
# PowerShell может не передать кириллический путь правильно:
python3 script.py "C:\Users\User\Downloads\Перевод\book_temp"

# БЕЗОПАСНЕЕ — chdir в директорию сначала:
cd "C:\Users\User\Downloads\Перевод"
python3 script.py "book_temp"
```

**Правило:** если в пути есть кириллица — `cd` в родительскую директорию,
затем используй относительные пути.

---

## Windows-safe atomic write

**Если agent видит `PermissionError` / `WinError 5` при `apply-merge` или `record`**:
- Это **нормально на Windows** — `atomic_write_text` пытается rename
  `.tmp → target`, но если target держит открытой другая программа
  (антивирус, индексатор, редактор, file watcher) — rename падает с
  `WinError 5: Отказано в доступе`.
- Скрипт сам retry'ит до 5 раз с паузой 0.2 сек. Если все retry
  провалились — fallback на non-atomic `unlink + rename` (печатает
  WARNING в stderr, но успешно пишет файл). Данные не теряются —
  fallback срабатывает в 100% случаев.
- **НЕ нужно писать ad-hoc python-сниппеты для обхода** — это только
  тратит контекст и не решает проблему. Просто игнорируй это
  предупреждение; оно шумное, но безопасное.
- Если предупреждение сильно мешает — закрой редакторы, которые могут
  держать файл открытым (особенно VS Code с авто-рефрешем), и/или
  добавь папку `_temp/` в исключения антивируса.

---

## Кодировка: суррогаты и mixing encodings

**Если agent видит `UnicodeEncodeError` с `\udcXX`** — это не баг скрипта,
а данные с суррогатами. Скрипты уже обрабатывают это автоматически (через
`sanitize_for_json`). НЕ нужно писать ad-hoc python-сниппеты для поиска
суррогатов — это тратит контекст. Просто перезапусти скрипт, он сам
санирует данные.

---

## Резюме для оркестратора

| Ситуация | Решение |
|----------|---------|
| Нужен Python с циклом/генератором | Пиши `.py` файл в `<temp_dir>/process/` |
| `apply-merge` payload (JSON через stdin) | Пиши `_apply.py` с прямым вызовом `merge_meta.apply_merge()` |
| Путь с кириллицей | `cd` в родитель, используй относительные пути |
| `WinError 5` при atomic write | Игнорируй (это нормально на Windows, fallback сработает) |
| `UnicodeEncodeError` с суррогатами | Игнорируй (скрипт сам санирует) |
