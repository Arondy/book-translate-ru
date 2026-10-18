# Фаза 2: Перевод (шаги 4–6)

Эта фаза - ядро пайплайна. Параллельные субагенты переводят чанки,
оркестратор мержит их наблюдения в глоссарий после каждого батча.

**После завершения фазы 2 - обязательная точка сжатия контекста**
(см. `references/compaction-points.md`).

**Перед началом:** проверь, что `progress.json` показывает `current_step: 4`.
Если нет - запусти `load_state.py` и сверься.

---

## Шаг 4. Планирование (resume)

Запусти детерминированный planner:

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py plan "<temp_dir>"
```

Вывод - JSON с категориями:

| Список | Что значит | Действие |
|--------|-----------|----------|
| `translation_chunk_ids` | нужно (пере-)перевести | шаг 5 |
| `record_only_chunk_ids` | output уже есть, но без записи в run_state | шаг 6a (только record) |
| `unchanged_chunk_ids` | output есть, глоссарий не менялся с момента записи | skip |
| `failed_chunk_ids` | ранее помечены `failed` после 2 retries | skip (требуют решения пользователя) |
| `polish_failed_chunk_ids` | перевод валиден, упал только polish | перевод не трогать; повторить polish или принять `.bak` |

Если `translation_chunk_ids` пуст - перейти сразу к шагу 7 (фаза 3).

**Опционально:** `--retranslate-untracked` форсирует перевод всех чанков без
записи в run_state (для случая, когда старые outputs от предыдущего прогона
нужно перевести заново).

Запись чанков как completed (после успешного перевода или для adopt):

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py record "<temp_dir>" chunk0001 chunk0002 ...
```

Пометить чанк как failed (после 2 неудачных retries):

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py mark-failed "<temp_dir>" chunkNNNN "причина"
```

Пометить падение polish, не ломая перевод (`plan` такой чанк не пропускает):

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py mark-polish-failed "<temp_dir>" chunkNNNN "причина"
```

Observability snapshot:

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py status "<temp_dir>"
```

**После шага 4 - запиши progress:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 4 \
    --note "Plan ready: N chunks to translate" \
    --set total_chunks=<N>
```

---

## Шаг 5. Параллельный перевод чанков (ядро пайплайна)

Работай **батчами**. Первый батч - 1–2 главы: на нём проверяется, что
субагенты выдают корректный формат и не плодят дубликаты имён по ещё не
пополненному глоссарию. Убедившись, что всё в порядке, увеличивай размер -
до 4–8 чанков за батч (зависит от лимитов API). Причина осторожности:
внутри батча главы переводятся по глоссарию, который ещё не пополнен
`merge`'ем после этого же батча, поэтому большой первый батч порождает
дубликаты имён.

### 5a. Собрать task-файлы для батча

Не запускай `print-terms-for-chunk` и `chunk_context.py` вручную и не
вставляй их вывод в промпт - это раздувает твой контекст. Одна команда
готовит по task-файлу на каждый чанк (абсолютные пути + уже вставленные
терм-таблица и neighbour context):

```bash
python3 {baseDir}/scripts/phase2_translate/prepare_batch.py "<temp_dir>" --range 1 6
# или по конкретным чанкам:
python3 {baseDir}/scripts/phase2_translate/prepare_batch.py "<temp_dir>" chunk0001 chunk0002
```

Результат: `<temp_dir>/process/task_chunkNNNN.md` - по одному на чанк.

> **Внимание Windows + PowerShell:** НЕ используй `python3 -c` для
> вычисления `chunk_sections[chunk_id]` - list comprehensions и циклы
> молча не работают на PowerShell. Если нужно узнать главу чанка -
> напиши `_lookup.py` файл в `<temp_dir>/process/` или прочитай
> `chunk_sections.json` через read_file (это короткий JSON).
> Подробнее: `references/windows-powershell.md`.

### 5b. Запустить субагентов

Каждый субагент получает **один task-файл**:

| Что | Откуда |
|-----|--------|
| Task-файл чанка | `<temp_dir>/process/task_chunkNNNN.md` |

Запусти субагента, передав ему путь к task-файлу и одну фразу: прочитать
task-файл и следовать инструкции из него. Task-файл сам ссылается на
`prompts/phase2_translate/translate_chunk.md` (абсолютный путь), задаёт
`BASE_DIR` для разрешения относительных путей, содержит терм-таблицу,
neighbour context и абсолютные пути ко всем входным и выходным файлам
(`chunkNNNN.md`, `output_chunkNNNN.md`, `output_chunkNNNN.meta.json`,
`голос_книги.md`, `narrator_hints.json`, `structural_units.json`).

Не вкладывай содержимое чанка или промпта в свой контекст - субагент
прочитает файлы сам через read_file.

Полная инструкция перевода, правила и схема meta.json - **только** в
`prompts/phase2_translate/translate_chunk.md`. SKILL.md не дублирует их.

### 5c. Дождаться завершения батча

После того как все субагенты батча завершились → шаг 6 (мерж наблюдений),
затем следующий батч.

### 5d. Surface-проверки чанка (сразу после перевода)

Как только субагент отдал чанк, прогони дешёвые детерминированные
проверки на поверхностный брак - это привязывает каждую проблему к
конкретному chunk id, и ретрай получает точный список:

```bash
python3 {baseDir}/scripts/phase3_finish/quality_check.py "<temp_dir>" --chunk chunk0001
```

Проверяются: слова с латиницей внутри кириллицы (`dошло`), английские
кавычки в диалогах (`"Привет," сказал он`), заголовки-числительные
(«Один», «Два»), пробельный дефис вместо тире (`Москва - столица`),
порядок в датах и годах (`марта 5-го`, `в году 2025`) и парные написания
(`в течении часа`, `иметь ввиду`, `в отличии от`). Те же проверки на
шаге 8.6 идут по склеенному `output.md`, но там результат не привязан к
чанку - поэтому лови их сразу. Ненулевой exit code = список точных проблем
для retry.

### 5e. Retry-политика для пустых/коротких outputs

**После завершения каждого батча** прогони проверку через штатный
скрипт:

```bash
python3 {baseDir}/scripts/shared/verify_batch.py "<temp_dir>" [<start>] [<end>]
```

Диапазон опционален: без `<start> <end>` скрипт берёт список чанков из
`manifest.json`, поэтому не печатает фантомных `missing output` за
номерами, которых в книге нет. Пример для батча чанков 1–6:
`python3 {baseDir}/scripts/shared/verify_batch.py "<temp_dir>" 1 6`

Скрипт проверяет:
1. `output_chunkNNNN.md` существует и не короче
   `[retry].min_output_chars` **символов** (не байтов) из `config.toml`
   (по умолчанию 100). 60 кириллических символов - это уже меньше порога,
   хотя в байтах их было бы 120.
2. Соотношение `len(output_chunkNNNN.md) / size` из `manifest.json` в
   пределах `[quality].ratio_min..ratio_max` - ловит обрезанный чанк.
3. `output_chunkNNNN.meta.json` существует (может быть пустым по содержимому,
   но файл обязан быть - его наличие трекается в `applied_meta_hashes`).

Exit code: `0` - всё в порядке, `1` - есть проблемы, `2` - нет
`manifest.json` или он не разбирается.

Также стоит проверить, что все meta-файлы - валидный JSON (до merge):

```bash
python3 {baseDir}/scripts/shared/check_metas.py "<temp_dir>"
```

Если мета malformed (например, субагент выдал неэкранированные кавычки),
merge_meta.py quarantines её молча. Лучше поймать это раньше.

**Что считать провалом:**
1. `output_chunkNNNN.md` не существует - sub-agent не создал файл.
2. `output_chunkNNNN.md` существует, но **< 100 символов** - пустой
   или оборванный output.
3. `output_chunkNNNN.meta.json` не существует - sub-agent не записал
   наблюдения. Может быть валидно (no-op meta), но файл обязан быть.

**Если любое условие не выполнено - retry** тем же промптом + дополнительной
инструкцией:

```
Предыдущая попытка вернула пустой или слишком короткий файл
(<N символов). Переведи чанк полностью, не пропускай содержимое.
Если в чанке есть структурные элементы (эпиграф, сноска) - обработай их
по prompts/phase2_translate/эпиграф_обработка.md.

После завершения - убедись, что оба файла созданы:
- <temp_dir>/process/output_chunkNNNN.md (> 100 символов)
- <temp_dir>/process/output_chunkNNNN.meta.json (может быть пустым по
  содержимому, но файл обязан существовать)
```

Максимум **2 retry на чанк**. После 2 неудач - пометить чанк как `failed`
в `run_state.json` через:
```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py mark-failed "<temp_dir>" chunkNNNN "причина"
```
И продолжить прогон. Не вешать весь батч на один упрямый чанк.

`failed`-чанки отображаются в финальном отчёте (шаг 8.5 post-mortem) и в
post_mortem.py `_note`. Пользователь принимает решение: перевести вручную,
увеличить retry, или принять как есть.

---

## Шаг 6. Мерж наблюдений в глоссарий (после каждого батча)

**Транзакционный мерж через `merge_meta.py`.** Скрипт делает всё
детерминированно - никаких LLM-решений в процессе мержа. Главный агент
только (а) читает `decisions_needed` и (б) выбирает option для каждого
decision.

### 6a. Записать состояние завершённых чанков

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py record "<temp_dir>" chunk0001 chunk0002 ...
```

Это записывает `entity_hashes` текущего глоссария в `run_state.json` для
каждого чанка. После этого selective re-translation (шаг 4) сможет
определить, какие чанки требуют переделки при правке глоссария.

### 6b. Подготовить merge plan

```bash
python3 {baseDir}/scripts/phase2_translate/merge_meta.py prepare-merge "<temp_dir>" \
    --out "<temp_dir>/process/merge_plan.json"
```

Скрипт пишет план в файл (в stdout - краткая сводка). Формат плана:
```json
{
  "auto_apply": [
    {"action": "add_entity", "source": "Manhattan", "target": "Манхэттен",
     "category": "place", "evidence_refs": ["chunk0001", "chunk0002"]},
    {"action": "add_alias", "variant": "Alicia", "to_source": "Alicia Brown"},
    {"action": "set_attribute", "entity_source": "Tai", "attribute": "gender",
     "value": "male", "evidence_refs": ["chunk0003"]}
  ],
  "decisions_needed": [
    {
      "id": "new_entity_conflict_Taig",
      "kind": "existing_entity_conflict",
      "entity_source": "Taig",
      "current_target": "Тайг",
      "variants": [{"target_proposal": "Тэйг", "category": "person",
                    "evidence": "...", "evidence_chunks": ["chunk0042"]}],
      "options": ["keep_current", "use_variant_0", "record_in_notes"]
    }
  ],
  "consumed_chunk_ids": ["chunk0001", "chunk0002", "chunk0042"],
  "malformed_meta_chunk_ids": ["chunk0099"]
}
```

Поле вариантов называется **одинаково - `variants`** для всех kind'ов
(раньше `proposed_variants` и `variants` различались без причины).

**Decision kinds и валидные choices:**

| Kind | Когда возникает | Валидные choices |
|------|----------------|-----------------|
| `alias` | sub-agent предложил, что `variant` - алиас к `candidate_source` | `yes_alias`, `no_separate_entity`, `skip` |
| `conflict` | sub-agent нашёл лучшее соответствие, чем в глоссарии | `keep_current`, `accept_proposed`, `record_in_notes` |
| `existing_entity_conflict` | sub-agent предложил новый target для существующей сущности | `keep_current`, `use_variant_N`, `record_in_notes` |
| `conflicting_new_entity_proposals` | разные sub-agents предложили разные target для новой сущности | `use_variant_N`, `skip` |
| `attribute_low_confidence` | гипотеза о gender/attribute с low confidence | `accept`, `skip` |
| `attribute_conflict` | sub-agent предлагает attribute, отличный от записанного | `keep_current`, `accept_proposed`, `record_in_notes` |

**Правила фильтрации `new_entities`** встроены в `merge_meta.py` и не требуют
ручной проверки - невалидные записи автоматически quarantined.

### 6c. Решить decisions (главный агент)

Прочитай `decisions_needed` из `merge_plan.json`. Для каждого:
- прочитать `evidence` и `evidence_chunks` (если нужно - открыть сами чанки)
- выбрать один option из массива `options`

Составь `decisions.json` - только пары `id` + `choice`:

```json
[
  {"id": "new_entity_conflict_Taig", "choice": "keep_current"},
  {"id": "conflicting_new_entity_proposals_42", "choice": "use_variant_1"}
]
```

`kind` и `variants` подтягивать вручную **не нужно** - `apply-merge` берёт
их из `merge_plan.json` по `id`. В payload - только `id` и `choice`.

### 6d. Применить merge (транзакционно)

```bash
python3 {baseDir}/scripts/phase2_translate/merge_meta.py apply-merge "<temp_dir>" \
    --plan "<temp_dir>/process/merge_plan.json" \
    --decisions "<temp_dir>/process/decisions.json"
```

Никакого stdin - файловый протокол работает одинаково на Linux и Windows,
в том числе с кириллицей в путях (см. `references/windows-powershell.md`).

Даже если `auto_apply` пуст и decisions не нужны - **всё равно** вызови
`apply-merge` (с пустым `decisions.json`: `[]`). Скрипт записывает
`consumed_chunk_ids`; иначе no-op metas будут пересканироваться вечно.

**Транзакционность:** `apply-merge` - all-or-nothing. Если любое decision
malformed (невалидный choice, missing fields, ссылка на несуществующую
сущность) - **весь batch abort'ится**, глоссарий **не мутирует**, хеши **не
записываются**. На non-zero exit - исправить decision и переотправить.

**При abort:** посмотри stderr, исправь конкретное malformed decision,
**перезапусти `apply-merge` с тем же payload**. Глоссарий не мутировал
(транзакционность), retry безопасен. `consumed_chunk_ids` не записались -
meta-файлы будут пере-сканироваться. Если retry 3 раза abort'ится на одном
decision - пропусти его (`choice: "skip"` для конфликтов, или убери из
`decisions`) и пометь в `--note`.

### 6e. Observability

```bash
python3 {baseDir}/scripts/phase2_translate/merge_meta.py status "<temp_dir>"

# Шлюз схемы - сразу после apply-merge:
python3 {baseDir}/scripts/phase1_prepare/glossary.py validate-glossary "<temp_dir>"
```

Вывод:
```
Meta merge status: <temp_dir>
  Output chunks:           N
  Meta files found:        M
  Consumed (in glossary):  K
  Unmerged (pending):      U
  Malformed:               X
  Outputs missing meta:    Y
```

Severity rules (не fail'ят прогон, но flag'аются):
- `unmerged_meta_files > 0` после шага 6 → bug, должен был поймать
- `malformed_meta_files > 0` → sub-agent выдал невалидную meta; вывести
  chunk_ids и предложение «fix by hand and re-run if needed»
- `meta_files_found < translated_chunks` → sub-agent-compliance issue
  (некоторые чанки не выдали meta вообще). Вывести missing chunk_ids.

### 6f. Интерактивное добавление новых терминов в словарь (после каждого батча)

**Проблема:** glossary-merge (`6b`–`6e`) добавляет новые сущности
автоматически с `confidence: "low"`, но пользователь обычно не видит
их до самого конца прогона (когда уже поздно что-то менять - все
последующие чанки уже использовали этот вариант). Если термин
переведён плохо, ошибка размножается на все следующие чанки.

**Правило:** после каждого `apply-merge` (после шага 6e) оркестратор
обязан проверить, появились ли в `glossary.json` новые сущности с
`confidence: "low"` и `notes: "auto-applied from sub-agent meta"`,
которых ещё не было в предыдущих батчах. Если да - сообщить
пользователю:

```
В этом батче в глоссарий добавлено N новых терминов:

  • «Silverstone» → «Серебристый камень» (term, встречен в chunk0007)
    evidence: «the silverstone glows at dawn»
  • «Incarnate Wheel» → «Колесо Инкарны» (term, встречен в chunk0012)
    evidence: «the Incarnate Wheel turned once more»

Хочешь отредактировать какой-нибудь из них сейчас, или продолжить
перевод с текущими вариантами? Если продолжить - все варианты будут
использованы в следующих чанках как есть.

Команды:
  • «ОК» / «продолжить» - оставить как есть, идти дальше
  • «поменяй X: Silverstone → Серебряный камень» - переписать вариант
  • «покажи chunk0007» - вывести контекст чанка для решения
```

**Что делать:**
1. Если пользователь сказал «продолжить» - идти к следующему батчу.
2. Если пользователь подтвердил варианты («ОК», «всё верно») - поднять
   их `confidence` с `low`/`medium` до `high` по обратной связи.
   Штатная команда (безопасная сериализация, не строковая подстановка):
   ```bash
   # подтвердить все свежедобавленные (low/medium) термины батча:
   python3 {baseDir}/scripts/phase1_prepare/glossary.py confirm-terms "<temp_dir>" --all
   # или точечно, по английскому названию (source) / id:
   python3 {baseDir}/scripts/phase1_prepare/glossary.py confirm-terms "<temp_dir>" --source "Silverstone"
   python3 {baseDir}/scripts/phase1_prepare/glossary.py confirm-terms "<temp_dir>" --id silverstone
   ```
   Команда ставит `confidence: "high"` и дописывает `notes: "confirmed by user"`
   (к существующей note, без дублей). Термины уже с `high` не трогает
   (кроме `--force`). После неё - обязательный шлюз:
   ```bash
   python3 {baseDir}/scripts/phase1_prepare/glossary.py validate-glossary "<temp_dir>"
   ```
3. Если пользователь предложил другой вариант - вручную поправить
   `glossary.json` (поле `target` у соответствующей сущности, поменять
   `confidence` на `"high"`, добавить `notes: "confirmed by user"`).
   После правки **провалидировать схему и пересчитать частоты**:
   ```bash
   python3 {baseDir}/scripts/phase1_prepare/glossary.py validate-glossary "<temp_dir>"
   python3 {baseDir}/scripts/phase1_prepare/glossary.py count-frequencies "<temp_dir>"
   ```
   `validate-glossary` - обязательный шлюз после **любой** записи в
   `glossary.json` (в том числе после `apply-merge`): он ловит потерю
   top-level `"version": 2`, пустой `terms`, дубликаты id/source и
   недостающие поля. Без `version` скрипты остановятся с ошибкой.

> **Не правь `glossary.json` строковой подстановкой!**
> JSON-массивы и объекты требуют правильных запятых между элементами.
> При строковой подстановке (через `str.replace`, sed, или ручной
> `edit` без полной сериализации) легко:
> - забыть запятую после предыдущей записи перед новой;
> - оставить лишнюю запятую перед закрывающей `]` или `}`;
> - повредить кавычки внутри строк.
>
> Это даёт `json.decoder.JSONDecodeError`, который всплывёт только на
> следующем шаге (например, на шаге 7.6 при `pronoun_check.py` или
> при `merge_meta.py apply-merge`).
>
> **Правильный способ:** используй штатный template-скрипт
> `scripts/shared/edit_glossary_template.py` - скопируй его в
> `<temp_dir>/process/_edit_glossary.py`, отредактируй пути и секцию
> "EDIT LOGIC", запусти. Скрипт делает `json.loads` + `json.dumps`
> (безопасная сериализация).
>
> ```bash
> cp {baseDir}/scripts/shared/edit_glossary_template.py "<temp_dir>/process/_edit_glossary.py"
> # Отредактируй TEMP_DIR и секцию EDIT LOGIC в _edit_glossary.py
> python3 "<temp_dir>/process/_edit_glossary.py"
> ```
>
> **Никогда не делай `glossary.json` edit через `str.replace` или sed.**
 4. Если пользователь изменил существующий вариант - оркестратор
    может предложить пере-перевести чанки, где этот термин уже
    использовался. Решение за пользователем; в `run_state.py` есть
    `plan --retranslate-untracked` для форсирования пере-перевода
    конкретных чанков.
 5. **Не задавать этот вопрос на каждом чанке** - только один раз
    после завершения батча (4–8 чанков). Если в батче не появилось
    новых low-confidence терминов - не беспокоить пользователя.

> **Антипаттерн:** молча проглотить всё, что sub-агенты предложили
> в `new_entities`, и не показать пользователю до самого конца
> прогона. На большой книге это приводит к тому, что половина
> имён переведена неудачно и пере-переводить слишком дорого.

### 6f.1. Проверка на near-duplicates (после каждого merge)

**Проблема:** `merge_meta.py` делает exact-match dedup (case-insensitive).
Это безопасно (fuzzy matching опасен), но пропускает near-duplicates:
- `Silver Compass` (singular) vs `Silver Compasses` (plural) - разные source
- `Nadia` vs `Nadia Verne` - token-subset
- `Al` vs `Alfred` (если `Al` - alias) - alias overlap

Субагенты могут добавить `Silver Compasses` как new entity, хотя
`Silver Compass` уже есть в глоссарии - и merge_meta это пропустит.

**После каждого merge** (особенно после последнего батча) проверяй:

```bash
python3 {baseDir}/scripts/phase1_prepare/glossary.py find-duplicates "<temp_dir>"
```

Скрипт non-destructive - только REPORTS, не mergeит. Найденные пары
объедини вручную через `edit_glossary_template.py` (один source → alias
другого, target оставь тот, что правильнее).

**Если 0 пар** - отлично, продолжай к 6g (точка сжатия).
**Если >5 пар** - возможно, проблема в промпте перевода (субагенты
систематически создают дубликаты). Проверь 2-3 чанка вручную и убедись,
что они видят терм-таблицу.

### 6g. После последнего батча - точка сжатия контекста

Когда все чанки переведены и смержены:

1. **Запиши состояние** в `progress.json`:
   ```bash
   python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 6 \
       --note "Все чанки переведены и смержены" \
       --append-list completed_milestones="phase-2-done"
   ```
2. **Предложи пользователю сжать контекст:**
   > Все чанки переведены и смержены. Глоссарий обновлён (N terms, M aliases).
   > Дальше - QA и polish, которые работают с файлами на диске через
   > субагентов. **Рекомендую сжать контекст сейчас** - это последняя
   > обязательная точка.
3. После compact (или без него) - переходи к фазе 3: прочитай
   `references/phase-3-finish.md` и начни с шага 7.

Подробнее про точки сжатия: `references/compaction-points.md`.

### 6h. Точка сжатия контекста (после merge каждого долгого батча)

Если батчей было много (≥3) и контекст раздувается - можно предлагать
compact и после промежуточных батчей. Не обязательно, но желательно
на больших книгах (≥ 40 чанков).

**Что можно забыть** (без потери качества) после каждого merge:
- содержимое конкретных meta-файлов (они на диске, нужны только для
  `merge_meta.py status` и post-mortem);
- evidence-цитаты из `decisions_needed` (уже приняты решения);
- промежуточные auto_apply payloads.

**Что оставить в контексте:**
- текущее состояние `glossary.json` (summary: сколько terms, сколько
  aliases, сколько conflicts pending);
- прогресс по чанкам (translation / record_only / unchanged / failed);
- любые `failed`-чанки с причинами.

**Никогда не делай compact автоматически без предложения.**

---

## Что важно помнить на этой фазе

- **Первый батч - 1-2 главы, дальше 4-8 чанков.** Большой первый батч
  плодит дубликаты имён: главы внутри батча переводятся по глоссарию, ещё
  не пополненному merge'ем.
- **После каждого батча - `verify_batch.py` + `check_metas.py` + шаг 6 целиком.** Не копи
  несколько батчей в очередь - merge накапливает конфликты, которые
  сложнее решать оптом.
- **`failed`-чанки - не блокатор.** Помечай и продолжай; проверяй через
  `run_state.py status` (не через post_mortem - `post_mortem.py` не читает
  `run_state.json`).
- **Не подгружай чанки в контекст оркестратора.** Если нужно посмотреть
  evidence из решения - открой нужный кусок через bash `head`, не
  read_file в основной поток.
- **Молчание по ходу перевода.** Пользователю пиши только с вопросом или
  с сообщением о переходе к следующей стадии: без отчётов о запуске
  скриптов, без комментариев «перевожу батч 3 из 7», без промежуточных
  итогов (глобальный инвариант 9 в `SKILL.md`).
