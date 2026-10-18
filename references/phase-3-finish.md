# Фаза 3: Финал (шаги 7–9)

Эта фаза - проверка и сборка. Структурные и содержательные проверки,
polish, post-mortem, и финальная сборка FB2.

**Перед началом:** проверь, что `progress.json` показывает `current_step: 7`.
Если нет - запусти `load_state.py` и сверься.

---

## Шаг 7. Verify (структурная проверка)

```bash
python3 {baseDir}/scripts/phase1_prepare/glossary.py validate-manifest "<temp_dir>"
# Флаг --strict превращает предупреждения в ошибки.
```

Что проверяется:
- Каждому `chunk*.md` соответствует `output_chunk*.md` (1:1)
- Ни один output-файл не пустой
- SHA-256 source chunk'ов совпадает с манифестом
- **Character ratio** RU/EN в пределах 0.6–2.0
- **English leak detection**: непереведённые английские фрагменты >80 символов

При наличии issues:
- Warnings (ratio, leaks) → переходят на QA-шаг (шаг 7.5) для разбора
- Errors (отсутствие файлов, hash mismatch) → повторный перевод чанка
  (макс. 2 попытки на чанк). Используй `--strict` если warnings - это
  блокер для твоего случая.

**После шага 7:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 7 \
    --note "Verify passed (or warnings noted)"
```

---

## Шаг 7.5. QA (содержательная проверка)

Зачем: даже если manifest OK, в переводе могут быть потерянные абзацы,
вставки от ИИ, проигнорированный глоссарий. Этот шаг ловит то, что
структурная валидация не видит.

**Запуск**: после шага 7, перед шагом 8. Субагент получает source + output
для каждого чанка и промпт `prompts/phase3_finish/qa_chunk.md`.

**Максимум 2 чанка на один субагент.** QA - это внимательное попарное
сравнение оригинала и перевода; больше 2 чанков за раз приводит к
поверхностной проверке и пропущенным issues. Запускай субагентов
последовательно (или небольшими группами), по 2 чанка на каждого.

**QA - только про смысл.** Длина, английские вставки, число абзацев,
непереведённые заголовки и сломанная разметка детерминированы и уже
проверены скриптами (`verify_batch.py`, per-chunk `quality_check.py
--chunk`, `verify_polish.py`). Не проси LLM их оценивать.

**Кто пишет файл.** Субагент возвращает JSON **в ответе** (не создаёт файл).
Оркестратор сохраняет его в `<temp_dir>/process/output_chunkNNNN.qa.json`
через Write tool (обычный JSON, не через `python3 -c`). Формат - JSON с
`issues` массивом (см. `prompts/phase3_finish/qa_chunk.md`).

**Решения по результатам QA:**

| severity | category | Решение |
|----------|----------|---------|
| high | любая | Отправить чанк на повторный перевод с явной инструкцией по issue |
| medium > 2 на чанк | любая | То же |
| medium | terminology, voice | Добавить в глоссарий / уточнить голос |
| low | любые | Принять и идти дальше (показать пользователю в саммари) |

Если QA-issues неприемлемы, повторный перевод запускается тем же
субагент-инструкциями шага 5b (фаза 2); к промпту добавляется блок:
```
Найденные проблемы в предыдущей версии: <issues из QA>.
При переводе устрани их.
```

### 7.5.1. После QA - запись состояния

После шага 7.5 все `output_chunkNNNN.qa.json` сохранены на диск, а
high-severity issues либо устранены повторным переводом, либо помечены
для ручной обработки.

**После шага 7.5:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 7 \
    --note "QA done: N chunks OK, M medium, K high (re-translated)" \
    --set qa_completed=true
```

---

## Шаг 7.6. Pronoun consistency check (кросс-чанковая сверка рода)

**Цель**: поймать случаи, когда один персонаж переведён с разным родом
в разных чанках. Скрипт `pronoun_check.py` только собирает данные.
**Все решения принимает LLM-агент.**

```bash
# 1. Собрать данные и записать в файл
python3 {baseDir}/scripts/phase3_finish/pronoun_check.py "<temp_dir>" --save "<temp_dir>/process/pronoun_input.json"

# 2. Запустить субагента с промптом prompts/phase3_finish/сверка_местоимений.md
#    Передать pronoun_input.json + glossary.json.
#    Субагент вернёт JSON с тремя списками:
#    - conflicts_to_resolve (конфликты рода между чанками)
#    - to_resolve_from_unknown (можно установить род по контексту)
#    - intentional_by_author (намеренные смены рода - НЕ трогать)

# 3. Сохрани вывод субагента в <temp_dir>/pronoun_check_report.json
#    через Write tool (это обычный JSON-файл; НЕ используй python3 -c
#    с кириллицей в пути на Windows - см. references/windows-powershell.md)

# 4. Покажи пользователю N рекомендаций из pronoun_check_report.json.
#    Спроси, какие применить. Для каждой одобренной - используй
#    edit_glossary_template.py (НЕ str.replace). После правки -
#    glossary.py validate-glossary, затем glossary.py count-frequencies.
```

**Когда запускать**:
- После шага 7.5 (QA), если QA нашёл gender-конфликты.
- После шага 8 (polish), как финальную сверку.
- Перед шагом 8.5 (post-mortem), чтобы данные были полными.

**Что НЕ делает этот шаг**:
- Не правит текст.
- Не обновляет glossary.json автоматически.
- Не «угадывает» gender по косвенным уликам. Это решение агента.

**После шага 7.6 - запиши состояние:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 7 \
    --note "pronoun_check done" --set pronoun_check_done=true
```
(current_step останется 8, но note + pronoun_check_done флаг зафиксируют,
что 7.6 выполнен - важно для resume после compaction.)

**Правило сопоставления персонажей:**

Если оркестратор пишет временный скрипт для применения рекомендаций
из `pronoun_check_report.json` к `glossary.json` (например, `_applygender.py`),
он обязан матчить персонажей по **точному значению `source`** или по
**token-boundary**, а не по подстроке. Сопоставление по подстроке
(`key in source.lower()`) даёт ложные срабатывания:

- `key="brig"` ложно матчит `source="brightsand"` (префикс!), и
  несуществующий персонаж `brightsand` получает `male` от настоящего
  `Brig`.
- `key="al"` ложно матчит `source="Alfred"`, `source="Albert"`,
  `source="Hal"`, `source="Sal"` - все получат род от Al.

**Правильная реализация** - точное совпадение или regex с границами слова:

```python
# Правильно - точное совпадение без учёта регистра:
if key.lower() == source.lower():
    apply_gender(glossary, source, gender)

# Или - token-boundary regex:
import re

pattern = re.compile(r"\b" + re.escape(key) + r"\b", re.IGNORECASE)
if pattern.fullmatch(source):  # всё поле source - это key
    apply_gender(glossary, source, gender)
```

**Лучший подход:** вообще не применять автоматически. Скрипт
`pronoun_check.py` только собирает данные, рекомендации хранятся в
`pronoun_check_report.json`, а человек применяет их вручную через
редактирование `glossary.json`. Это и было задумано в скилле;
автоматизация - на свой страх и риск.

Подробнее: `prompts/phase3_finish/сверка_местоимений.md`.

---

## Шаг 8. Полировка (грамматика + литература, **per-chunk**)

Polish идёт **по чанкам параллельно**: меньше контекст на субагента,
выше качество за те же деньги.

### 8a. Опциональная сборка черновика (если нужно)

```bash
# Только если хочется посмотреть на объединённый текст после шага 7.6.
# --no-fb2 пропускает финальную FB2-сборку; output.md всё равно создастся.
python3 {baseDir}/scripts/phase3_finish/merge_and_build.py --temp-dir "<temp_dir>" \
    --title "Название" --no-fb2
```

Обычно этот шаг пропускают - merge_and_build делается в шаге 9.

### 8b. Per-chunk polish (LLM-агенты)

Для каждого `output_chunkNNNN.md` (где `output_chunkNNNN.qa.json`
отсутствует или не содержит `high` issues после повторного прогона):

Запустить субагента с промптом `prompts/phase3_finish/полировка_чанка.md`.
Субагент получает:

- `chunkNNNN.md` - английский оригинал
- `output_chunkNNNN.md` - русский черновик (читает, но не перезаписывает)
- `output_chunkNNNN.meta.json` - наблюдения переводчика
- **`output_chunkNNNN.qa.json`** - QA-issues из шага 7.5
  (субагент обязан прочитать его и устранить перечисленные
  issues - terminology/voice/plot - в рамках грамматического +
   литературного прохода; см. промпт `полировка_чанка.md`)
- `structural_units.json` - если в чанке есть эпиграф/сноска
- `narrator_hints.json` - к какому arc принадлежит чанк
- `<temp_dir>/голос_книги.md` - для сверки голоса

Полировка объединяет 20 грамматических категорий
(`prompts/phase3_finish/грамматическая_обработка.md`)
и 14 литературных категорий
(`prompts/phase3_finish/литературная_обработка.md`),
плюс особые правила для эпиграфов и POV-смен, **плюс устранение
конкретных issues из `qa.json`**. Оба промпта - чеклисты с решающими
правилами; развёрнутые примеры «Было/Должно» лежат в
`references/примеры_обработки.md` и читаются по запросу.

**Запуск**: **строго 1 чанк на один субагент.** Polish - это внимательная
правка с применением 20 грамматических + 14 литературных категорий +
устранение QA-issues; при 2+ чанках объёма вывода может не хватить (output
token limit), если много исправлений. Субагент пишет **только** в
`<temp_dir>/process/output_chunkNNNN.polished.md`; про `.bak` и подмену
файла он не думает.

### 8b.1. Применение polish-вывода (скрипт)

`output_chunkNNNN.md` за субагента подменяет скрипт
`scripts/phase3_finish/apply_polish.py`. Он проверяет контракт (непустой
файл, ratio длины, число абзацев, число заголовков) и, если всё в
порядке, сохраняет `output_chunkNNNN.md.bak` и атомарно подменяет
`output_chunkNNNN.md`, после чего удаляет `*.polished.md`:

```bash
python3 {baseDir}/scripts/phase3_finish/apply_polish.py "<temp_dir>" chunk0007
# или разом для всех готовых *.polished.md:
python3 {baseDir}/scripts/phase3_finish/apply_polish.py "<temp_dir>"
# Exit code: 0 - подменено, 1 - контракт нарушен (файлы не тронуты), 2 - нечего делать.
```

### 8b.2. Контракт завершения polish

Polish - обратимый пасс: он должен **улучшать** черновик, а не ломать.
После каждого polish-сабагента проверить:

1. **Файл существует и > 0 байт.**
2. **Не потеряна половина текста.** Ratio длины polish-вывода к `.bak`
   должен быть в пределах `[polish].ratio_min..ratio_max` (0.5-2.0) **и**
   не отклоняться от медианы ratio по всей книге больше чем на
   `[polish].median_tolerance` (25%). Абсолютные границы слишком широкие:
   чанк, потерявший почти половину текста, ловится именно по медиане.
3. **Число абзацев не изменилось** больше чем на
   `max(2, bak_paragraphs * [polish].max_para_delta_ratio)` (по умолчанию
   10%). Потеря нескольких абзацев - самый явный признак обрезки.
4. **Markdown-структура не сломана:** число `#`/`##` заголовков в
   polish-выводе должно быть в пределах ±2 от исходного. Если
   заголовки исчезли или добавились массово - red flag.

**Штатный скрипт проверки:** `scripts/phase3_finish/verify_polish.py`
прогоняет эти проверки по всем чанкам сразу и печатает summary.
Запускать после всех polish-сабагентов батча (или после всего шага 8).
Чанк **без `.bak` - это FAIL**: если polish прошёл без бэкапа,
контракт проверить нечем.

```bash
python3 {baseDir}/scripts/phase3_finish/verify_polish.py "<temp_dir>"
# Печатает:
#   OK:    27/30 chunks pass polish contract
#   FAIL:  chunk0007 (ratio=0.32 < 0.5)
#   FAIL:  chunk0012 (ratio=0.40 deviates from book median 0.99 (+/-25%))
#   FAIL:  chunk0015 (no .bak — polish contract unverifiable)
# Exit code: 0 если все OK, 1 если есть FAIL.
```

`.bak` создаёт `apply_polish.py` (шаг 8b.1) перед подменой, поэтому к
моменту `verify_polish.py` он уже на месте. Если FAIL - восстановить
оригинал (`cp output_chunkNNNN.md.bak output_chunkNNNN.md`) и пометить
чанк как **`polish_failed`** (перевод при этом в порядке):

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py mark-polish-failed "<temp_dir>" chunkNNNN "polish_ratio_or_headings_violation"
```

**`mark-polish-failed` не ставит `failed: true`.** Такой чанк на
следующем `run_state.py plan` остаётся в `unchanged_chunk_ids` (перевод
валиден) и лишь попадает в `polish_failed_chunk_ids` - можно повторить
polish или принять восстановленный черновик. `mark-failed` (жёсткий
`failed`) используй только когда сломан сам перевод. Смотри
`run_state.py status`; не через post_mortem (post_mortem.py не читает
run_state.json).

### 8c. Альтернатива: монолитный polish

Только для книг с плотными межглавными связями: собираешь `output.md` через
`merge_and_build.py` и запускаешь субагентов с
`prompts/phase3_finish/грамматическая_обработка.md`
+ `prompts/phase3_finish/литературная_обработка.md` на весь файл.
По умолчанию - per-chunk.

### 8d. После polish - запись состояния

**После шага 8:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 8 \
    --note "Polish done: N chunks OK, M polish_failed" \
    --append-list failed_chunks=chunk0007 \
    --append-list failed_chunks=chunk0012
```

---

## Шаг 8.6. Финальная структурная проверка output.md

После polish и перед пост-мортемом/сборкой - собрать `output.md` и
прогнать структурные проверки на системные MT-баги:

```bash
# 1. Собрать output.md (без финальной FB2-сборки; output.md создаётся для проверок)
python3 {baseDir}/scripts/phase3_finish/merge_and_build.py --temp-dir "<temp_dir>" \
    --title "Название" --no-fb2

# 2. Структурные проверки
python3 {baseDir}/scripts/phase3_finish/quality_check.py "<temp_dir>"
```

**Что проверяет `quality_check.py`** (только системные MT-баги, не
литературное качество):

- **Orphan footnotes** - `[^N]` в тексте без `[^N]:` определения.
- **English leaks** - ASCII-раны > 80 символов (непереведённые куски).
- **Words in two alphabets** - слова с латиницей внутри кириллицы (`dошло`,
  `Gород`): сбой раскладки, невидимый для LLM-пассов. Только лог, текст не
  правится; слова из `target` глоссария (намеренно смешанные) пропускаются.
- **Per-chapter ratio** - длина перевода по главам вне 0.6×–2.0× от
  оригинала.
- **Garbage em-dashes** - `— — —`, `—–—` (3+ разделителя с пробелами -
  MT-мусор). **Не флагает инлайн `---`** (это эм-тире источника;
  конвертируется в `—` на этапе translate/polish - см. правило 16 в
  `translate_chunk.md`).
- **JS artifacts** - `[object Object]`, `undefined`, `null`, `NaN`.
- **Unclosed code fences** - нечётное число ` ``` ` (сломанная разметка).
- **Empty headings** - `## ` без текста после.
- **Doubled punctuation** - `.,`, `,.`, `?.`, `!.`. **Не флагает `..`**
  внутри путей (`../images/...`) или эллипсисов (`...`) - это легитимные
  конструкции.
- **Leftover placeholders** - `{baseDir}`, `{TERM_TABLE}` и т.п. - bug
  оркестратора.
- **English dialogue quotes** - `"Привет," сказал он` - англоязычные кавычки
  вместо тире (категория 20).
- **Untranslated chapter headings** - `# Один`, `## Два` - числительные-слова
  вместо `Глава N` (правило 15).
- **Dash / hyphen** - `Москва - столица` - пробельный дефис там, где нужно
  тире `—` (категория 16).
- **Dates / numerals** - `марта 5-го`, `в году 2025` - английский порядок в
  датах и годах (категория 17).
- **Paired spellings** - `в течении часа`, `иметь ввиду`, `в отличии от` -
  слипшиеся/раздельные пары (категория 18).

Последние три, а также `mixed_alphabets`, `english_dialogue_quotes` и
`untranslated_chapter_headings` доступны и как per-chunk ранняя проверка:
`quality_check.py "<temp_dir>" --chunk chunkNNNN` сразу после перевода
чанка (см. `references/phase-2-translate.md`).

Exit codes: 0 = OK или warnings; 1 = issues (или warnings с `--strict`);
2 = temp_dir невалиден.

**Действия по результатам:**
- Warnings без `--strict` → принять, идти к шагу 8.5 (post-mortem
  увидит эти паттерны и включит в отчёт).
- Issues с `--strict` → перед пост-мортемом исправить конкретные чанки
  (или принять осознанно, если, например, английский leak - это
  намеренная цитата в тексте).

`quality_check.py` не заменяет QA (шаг 7.5) - QA работает по чанкам с
LLM, quality_check работает по собранному `output.md` с regex.
Они дополняют друг друга.

---

## Шаг 8.5. Пост-мортем анализ

**Цель**: после полного прогона понять, что требует ручной проверки,
и что точно не проблема. Скрипт `post_mortem.py` только собирает данные.
**Анализ делает LLM-агент.**

```bash
# 1. Собрать данные
python3 {baseDir}/scripts/phase3_finish/post_mortem.py "<temp_dir>" \
    --save "<temp_dir>/process/post_mortem_input.json"

# 2. Запустить субагента с промптом prompts/phase3_finish/пост_мортем.md
#    Передать post_mortem_input.json + (опц.) pronoun_check_report.json
#    Агент вернёт Markdown-отчёт.

# 3. Сохранить отчёт
mv <репорт_от_агента> "<temp_dir>/post_mortem.md"
```

**Что агент делает с данными** (см. `prompts/phase3_finish/пост_мортем.md`):
- Считает распределение issues по severity и category.
- Строит карту проблем по главам (используя `chunk_sections.json`).
- Выделяет сюжетные места, требующие внимания.
- Даёт 3-5 конкретных рекомендаций, привязанных к чанкам.
- Перечисляет, что точно НЕ проблема.

**Что НЕ делает**:
- Не правит текст.
- Не обновляет glossary.json (это работа переводчика).
- Не «выдумывает» issues.

### 8.5.1. После post-mortem - запись состояния

Покажи пользователю `post_mortem.md`. Спроси: (а) принять как есть и
идти к шагу 9, (б) вернуться к конкретным чанкам из рекомендаций Top-5.
После решения - шаг 9.

**После шага 8.5:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 8 \
    --note "Post-mortem done. Report: post_mortem.md" \
    --set post_mortem_path="<temp_dir>/post_mortem.md"
```

---

## Шаг 9. Финальная сборка (только FB2)

```bash
python3 {baseDir}/scripts/phase3_finish/merge_and_build.py --temp-dir "<temp_dir>" \
    --title "Название" [--author "Автор"] [--genre prose_counter]
```

**Формат вывода - только FB2.** HTML и EPUB не генерируются: FB2 -
целевой формат для русских читалок и библиотек. Если нужен другой
формат - конвертируйте FB2 → EPUB/HTML/MOBI внешним инструментом после прогона.

Скрипт:
1. Сливает `output_chunk*.md` → `output.md` (для отладки и пост-мортема).
2. **Удаляет TOC-секции** (унаследованные от исходного EPUB) - FB2
   читалки автоматически генерируют оглавление по заголовкам глав.
   Детекция не полагается на ссылки: после фазы 1 якоря Calibre уже
   разлинкованы, и оглавление часто остаётся просто набором заголовков.
   Функция `common.strip_toc_block` принимает за конец оглавления первый
   заголовок, за которым идёт настоящий текст, - поэтому оглавление
   удаляется целиком, а первая глава (даже одноимённая) остаётся.
3. **Собирает FB2 напрямую через `scripts/phase3_finish/fb2_builder.py`**
   (на базе `lxml.etree`, не через Pandoc). Это даёт корректное
   форматирование:
   - Заголовки глав (`# ...` / `## ...`) → `<section><title>...</title>...</section>`.
   - Эпиграфы (`> ...` blockquotes перед заголовком) → `<epigraph>...<text-author>...</text-author></epigraph>`,
     **причём переносятся в начало следующей секции** (согласно FB2 2.0
     XSD-схеме, `<epigraph>` должен идти сразу после `<title>`, до
     контента).
   - Изображения (`![alt](path)`) → `<image xl:href="#_img_N"/>` с
     бинарным содержимым в `<binary id="_img_N" content-type="...">base64...</binary>`.
   - Сноски (`[^N]` + `[^N]: ...`) → `<a xl:href="#note_N" xl:type="simple"><sup>N</sup></a>`
     + `<body name="notes"><section id="note_N">...</section></body>`.
   - Прямая речь с тире сохраняется как обычный `<p>`.
   - Сценовые разделители (`***` на отдельной строке) → `<empty-line/>`.
   - **Экранирования Markdown**: `\*`, `\_`, `\\` и т.п. разворачиваются в
     литеральные символы (без этого `\*` из списков книг ломал разбор -
     бэкслеш попадал в текст, а звёздочка открывала курсив). Одиночный
     `\` в конце строки (жёсткий перенос) завершает абзац.
   - **Ремонт секций под XSD** (`_repair_sections`): секция, у которой
     после сборки нет ни текста, ни дочерних секций, получает `<subtitle>`
     вместо `<title>` (рубрика вроде «повесть» или «Другие книги автора»);
     лишние ведущие картинки выносятся в отдельные секции. Так титульная
     страница, «Содержание» и хвостовые рубрики не дают XSD-ошибок
     «empty section».
4. **Форматирует вывод через `scripts/phase3_finish/pretty_fb2.py`** -
   каждый `<p>` остаётся на одной строке (включая inline-теги), block
   containers - каждый на своей строке с отступом. Пробелы вокруг
   `<emphasis>` вставляются только между двумя словарными символами, так
   что курсив рядом с запятой или точкой не даёт артефактов вида
   «слово , а?» / «слово .».
5. **Валидирует результат по FB2 2.0 XSD-схеме**
   (`scripts/phase3_finish/schemas/FictionBook.xsd` + 3 импортированные).
   Если валидация провалилась - ошибки печатаются в stderr, **но файл
   всё равно записывается**.
6. Записывает `book.fb2` через atomic write (`.tmp` + rename).

**Результат**: `book.fb2` (и `output.md` - для отладки/пост-мортема).

**Про заголовки глав:** субагенты на шаге 5 уже должны были перевести
"One" → "Глава 1", "Two" → "Глава 2" и т.д. (см. `translate_chunk.md`
правило 15). Если в `output.md` остались числительные-слова ("Один",
"Два") как заголовки - это сигнал, что субагент пропустил правило 15.
Можно прогнать post-mortem (шаг 8.5) или точечно перевести заголовки
вручную перед сборкой. `quality_check.py` (шаг 8.6) ловит такие
непереведённые заголовки автоматически.

**Если FB2 не собирается** - смотри stderr, там будет указан проблемный чанк.
Можно временно переключиться на Pandoc-сборку флагом `--pandoc-fallback`
(только для отладки; результат будет с теми же проблемами вёрстки,
что и при прямой сборке Pandoc'ом):
```bash
python3 {baseDir}/scripts/phase3_finish/merge_and_build.py --temp-dir "<temp_dir>" \
    --title "Название" --pandoc-fallback
```

**Если XSD-валидация провалилась** - открой `book.fb2` в любом XML
редакторе и посмотри stderr-вывод fb2_builder. Пустые секции и секции
только с картинками билдер исправляет сам, поэтому частые причины теперь
такие:
- Субагент вставил raw HTML внутрь Markdown (например, `<div>` или
  незакрытый тег). Это ломает XML. Решение: найти и убрать HTML в
  `output_chunk*.md`.
- `<genre>` не из списка - поменяйте на валидный через параметр `--genre`
  (если запускаете скрипт напрямую) или отредактируйте FB2 после сборки.
  Валидные жанры: `prose_counter` (по умолчанию), `sf_fantasy`,
  `fantasy_fight`, `det_classic`, `prose_classic`, и т.д. - полный
  список в `scripts/phase3_finish/schemas/FictionBookGenres.xsd`.

**После шага 9 - финал:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 9 \
    --note "FB2 built: book.fb2" \
    --set fb2_path="<temp_dir>/book.fb2"
# После save_state, current_step станет 10 = "all done".
```

Сообщи пользователю:
> Книга переведена и собрана в FB2: `<temp_dir>/book.fb2` (N KB).
> Пост-мортем отчёт: `<temp_dir>/post_mortem.md`.
> Рекомендую открыть FB2 в любой читалке (FBReader, CoolReader) для
> финальной вычитки. Если есть проблемы с вёрсткой - см. stderr-вывод
> fb2_builder (XSD-ошибки) и шаг 9 выше.

---

## Что важно помнить на этой фазе

- **Per-chunk polish - основной режим.** Монолитный polish (8c) - только
  для книг с плотными межглавными связями.
- **`verify_polish.py` обязателен** после polish всех чанков.
- **`quality_check.py` - дополнение к QA, не замена.** QA ловит
  содержательные баги (LLM), quality_check - структурные (regex).
- **FB2 собирается напрямую через `fb2_builder.py`, не через Pandoc.**
  Pandoc-сборка (`--pandoc-fallback`) - только для отладки.
- **`progress.json` обновляется после каждого шага.** Это обязательный
  контракт - без него resume после compaction сломается.
- **Молчание по ходу работы.** Пользователю пиши только с вопросом или
  с сообщением о переходе к следующей стадии: без отчётов об отдельных
  проверках и скриптах, без промежуточных итогов (глобальный инвариант 9
  в `SKILL.md`). Финальный отчёт - после шага 9.
