# Фаза 2: Перевод (шаги 4–6)

Эта фаза — ядро пайплайна. Параллельные субагенты переводят чанки,
оркестратор мержит их наблюдения в глоссарий после каждого батча.

**После завершения фазы 2 — обязательная точка сжатия контекста**
(см. `references/compaction-points.md`).

**Перед началом:** проверь, что `progress.json` показывает `current_step: 4`.
Если нет — запусти `load_state.py` и сверься.

---

## Шаг 4. Планирование (resume)

Запусти детерминированный planner:

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py plan "<temp_dir>"
```

Вывод — JSON с четырьмя категориями:

| Список | Что значит | Действие |
|--------|-----------|----------|
| `translation_chunk_ids` | нужно (пере-)перевести | шаг 5 |
| `record_only_chunk_ids` | output уже есть, но без записи в run_state | шаг 6a (только record) |
| `unchanged_chunk_ids` | output есть, глоссарий не менялся с момента записи | skip |
| `failed_chunk_ids` | ранее помечены `failed` после 2 retries | skip (требуют решения пользователя) |

Если `translation_chunk_ids` пуст — перейти сразу к шагу 7 (фаза 3).

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

Observability snapshot:

```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py status "<temp_dir>"
```

**После шага 4 — запиши progress:**
```bash
python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 4 \
    --note "Plan ready: N chunks to translate" \
    --set total_chunks=<N>
```

---

## Шаг 5. Параллельный перевод чанков (ядро пайплайна)

Работать **батчами по 4–8 чанков** (зависит от лимитов API).

### 5a. Подготовить данные для каждого чанка

```bash
# терм-таблица (если glossary.json есть)
python3 {baseDir}/scripts/phase1_prepare/glossary.py print-terms-for-chunk "<temp_dir>" chunkNNNN.md

# neighbour context (prev/next excerpt ~300 символов)
python3 {baseDir}/scripts/phase2_translate/chunk_context.py "<temp_dir>" chunkNNNN.md
```

> **Внимание Windows + PowerShell:** НЕ используй `python3 -c` для
> вычисления `chunk_sections[chunk_id]` — list comprehensions и циклы
> молча не работают на PowerShell. Если нужно узнать главу чанка —
> напиши `_lookup.py` файл в `<temp_dir>/process/` или прочитай
> `chunk_sections.json` через read_file (это короткий JSON).
> Подробнее: `references/windows-powershell.md`.

### 5b. Запустить субагентов

Каждый субагент получает:

| Что | Откуда |
|-----|--------|
| Файл чанка | `chunkNNNN.md` |
| Целевой язык | русский |
| Терм-таблица | stdout `print-terms-for-chunk` (пусто → пропустить правило 2) |
| Neighbour context | stdout `chunk_context.py` (пусто → пропустить правило 3) |
| Голос книги | `голос_книги.md` (если есть) |
| **Narrator hints** | `narrator_hints.json` (к какому arc принадлежит чанк) |
| **Structural units** | `structural_units.json` (если в чанке есть эпиграф/сноска) |
| **Инструкция** | `prompts/phase2_translate/translate_chunk.md` (единый источник правды) |

Запусти субагента с промптом `prompts/phase2_translate/translate_chunk.md`,
передав ему:
- путь к чанк-файлу,
- терм-таблицу и neighbour context (короткие данные — в промпте),
- пути к файлам `голос_книги.md`, `narrator_hints.json`,
  `structural_units.json` (субагент прочтёт сам через read_file —
  не вкладывай их содержимое в промпт, это раздувает контекст).

Полная инструкция перевода, правила и схема meta.json — **только** в
`prompts/phase2_translate/translate_chunk.md`. SKILL.md не дублирует их.

### 5c. Дождаться завершения батча

После того как все субагенты батча завершились → шаг 6 (мерж наблюдений),
затем следующий батч.

### 5d. Retry-политика для пустых/коротких outputs

**После завершения каждого батча (а не после каждого subagent'а)**
обязательно прогоните проверку через штатный скрипт:

```bash
python3 {baseDir}/scripts/shared/verify_batch.py "<temp_dir>" <start> <end>
```

Например, для батча чанков 1–6: `python3 {baseDir}/scripts/shared/verify_batch.py "<temp_dir>" 1 6`

Скрипт проверяет:
1. `output_chunkNNNN.md` существует и **> 100 символов** (нормальный
   перевод чанка из ~30000 символов оригинала не может быть короче 100).
2. `output_chunkNNNN.meta.json` существует (может быть пустым по содержимому,
   но файл обязан быть — его наличие трекается в `applied_meta_hashes`).

Также стоит проверить, что все meta-файлы — валидный JSON (до merge):

```bash
python3 {baseDir}/scripts/shared/check_metas.py "<temp_dir>"
```

Если мета malformed (например, субагент выдал неэкранированные кавычки),
merge_meta.py quarantines её молча. Лучше поймать это раньше.

**Что считать провалом:**
1. `output_chunkNNNN.md` не существует — sub-agent не создал файл.
2. `output_chunkNNNN.md` существует, но **< 100 символов** — пустой
   или оборванный output.
3. `output_chunkNNNN.meta.json` не существует — sub-agent не записал
   наблюдения. Может быть валидно (no-op meta), но файл обязан быть.

**Если любое условие не выполнено — retry** тем же промптом + дополнительной
инструкцией:

```
Предыдущая попытка вернула пустой или слишком короткий файл
(<N символов). Переведи чанк полностью, не пропускай содержимое.
Если в чанке есть структурные элементы (эпиграф, сноска) — обработай их
по prompts/phase2_translate/эпиграф_обработка.md.

После завершения — убедись, что оба файла созданы:
- <temp_dir>/process/output_chunkNNNN.md (> 100 символов)
- <temp_dir>/process/output_chunkNNNN.meta.json (может быть пустым по
  содержимому, но файл обязан существовать)
```

Максимум **2 retry на чанк**. После 2 неудач — пометить чанк как `failed`
в `run_state.json` через:
```bash
python3 {baseDir}/scripts/phase2_translate/run_state.py mark-failed "<temp_dir>" chunkNNNN "причина"
```
И продолжить прогон. Не вешать весь батч на один упрямый чанк.

`failed`-чанки отображаются в финальном отчёте (шаг 8.5 post-mortem) и в
post_mortem.py `_note`. Пользователь принимает решение: перевести вручную,
увеличить retry, или принять как есть.

> **Реальный кейс из тестового прогона:** в одном батче 2 чанка из 6
> вернули пустой output (sub-agents упали без видимой причины). Без
> `verify_batch.py` это всплыло бы только на шаге 6 (merge) как malformed
> meta — пришлось бы возвращаться к шагу 5 и пере-переводить. С
> `verify_batch.py` это ловится сразу после батча и фиксится одним retry.

---

## Шаг 6. Мерж наблюдений в глоссарий (после каждого батча)

**Транзакционный мерж через `merge_meta.py`.** Скрипт делает всё
детерминированно — никаких LLM-решений в процессе мержа. Главный агент
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
python3 {baseDir}/scripts/phase2_translate/merge_meta.py prepare-merge "<temp_dir>"
```

Вывод — JSON:
```json
{
  "auto_apply": [
    {"action": "add_entity", "source": "Manhattan", "target": "Манхэттен",
     "category": "place", "evidence_refs": ["chunk0001", "chunk0002"]},
    {"action": "add_alias", "variant": "Wit", "to_source": "Hoid"},
    {"action": "set_attribute", "entity_source": "Tai", "attribute": "gender",
     "value": "male", "evidence_refs": ["chunk0003"]}
  ],
  "decisions_needed": [
    {
      "id": "new_entity_conflict_Taig",
      "kind": "existing_entity_conflict",
      "entity_source": "Taig",
      "current_target": "Тайг",
      "proposed_variants": [{"target_proposal": "Тэйг", "category": "person",
                              "evidence": "...", "evidence_chunks": ["chunk0042"]}],
      "options": ["keep_current", "use_variant_0", "record_in_notes"]
    }
  ],
  "consumed_chunk_ids": ["chunk0001", "chunk0002", "chunk0042"],
  "malformed_meta_chunk_ids": ["chunk0099"]
}
```

**Decision kinds и валидные choices:**

| Kind | Когда возникает | Валидные choices |
|------|----------------|-----------------|
| `alias` | sub-agent предложил, что `variant` — алиас к `candidate_source` | `yes_alias`, `no_separate_entity`, `skip` |
| `conflict` | sub-agent нашёл лучшее соответствие, чем в глоссарии | `keep_current`, `accept_proposed`, `record_in_notes` |
| `existing_entity_conflict` | sub-agent предложил новый target для существующей сущности | `keep_current`, `use_variant_N`, `record_in_notes` |
| `conflicting_new_entity_proposals` | разные sub-agents предложили разные target для новой сущности | `use_variant_N`, `skip` |
| `attribute_low_confidence` | гипотеза о gender/attribute с low confidence | `accept`, `skip` |
| `attribute_conflict` | sub-agent предлагает attribute, отличный от записанного | `keep_current`, `accept_proposed`, `record_in_notes` |

**Правила фильтрации `new_entities`** встроены в `merge_meta.py` и не требуют
ручной проверки — невалидные записи автоматически quarantined.

### 6c. Решить decisions (главный агент)

Прочитать `decisions_needed`. Для каждого:
- прочитать `evidence` и `evidence_chunks` (если нужно — открыть сами чанки)
- выбрать один option из массива `options`
- сформировать decision payload с round-trip полей `id`, `kind` (и `variants`
  для `conflicting_new_entity_proposals`):

```json
{"id": "conflict_Stormfather_target", "kind": "conflict", "choice": "accept_proposed"}
```

**⚠️ КРИТИЧНО для `conflicting_new_entity_proposals`**: decision payload
**обязан** содержать поле `variants` — массив вариантов из соответствующего
решения в `prepare-merge`. Без этого `apply-merge` abort'ится с ошибкой
`"conflicting_new_entity_proposals requires 'variants' array in decision payload"`.

**⚠️ ВАЖНО: имена полей РАЗНЫЕ для разных kind** (частая ошибка в
ad-hoc скриптах):

| Kind | Поле в `prepare-merge` | Поле в decision payload |
|------|------------------------|-------------------------|
| `existing_entity_conflict` | `proposed_variants` | `proposed_variants` (для `use_variant_N` choice) |
| `conflicting_new_entity_proposals` | `variants` | `variants` (обязательно для любого choice) |

Не путай `proposed_variants` и `variants` — это разные поля для разных
kind. Если ad-hoc скрипт обращается к `p["proposed_variants"]` для
`conflicting_new_entity_proposals`, получишь `KeyError` (правильное поле
там — `variants`).

Пример правильного decision для `conflicting_new_entity_proposals`:
```json
{
  "id": "conflicting_new_entity_proposals_42",
  "kind": "conflicting_new_entity_proposals",
  "choice": "use_variant_1",
  "variants": [
    {"target_proposal": "Тайг", "category": "person", "evidence": "...", "evidence_chunks": ["chunk0042"]},
    {"target_proposal": "Тэйг", "category": "person", "evidence": "...", "evidence_chunks": ["chunk0050"]}
  ]
}
```

`variants` копируется дословно из поля `variants` (а НЕ `proposed_variants`!)
соответствующего `decisions_needed`-элемента. `choice` выбирает один из
`use_variant_N`, где `N` — индекс в массиве `variants` (с 0).

**Альтернатива (рекомендуется на Windows):** вместо того, чтобы
собирать payload вручную и передавать через stdin, используй
**штатный template-скрипт** `scripts/shared/apply_merge_template.py` —
скопируй его в `<temp_dir>/process/_apply.py`, отредактируй пути и
список `decisions`, запусти. Скрипт вызывает `apply_merge()` напрямую
через Python import, обходит stdin/CLI, и автоматически вкладывает
`variants`/`proposed_variants` из `prepared` (с авто-детекцией поля).

```bash
cp {baseDir}/scripts/shared/apply_merge_template.py "<temp_dir>/process/_apply.py"
# Отредактируй SKILL_DIR, TEMP_DIR и decisions в _apply.py
python3 "<temp_dir>/process/_apply.py"
```

См. также `references/windows-powershell.md` для деталей.

### 6d. Применить merge (транзакционно)

```bash
# На Linux/macOS:
echo '<JSON payload>' | python3 {baseDir}/scripts/phase2_translate/merge_meta.py apply-merge "<temp_dir>"

# На Windows + PowerShell (НЕ ИСПОЛЬЗУЙ stdin pipe с кириллицей —
# используй прямой вызов apply_merge через _apply.py):
#   см. references/windows-powershell.md → "Проблема 2"
```

Payload:
```json
{
  "auto_apply": [...],
  "decisions": [...],
  "consumed_chunk_ids": [...]
}
```

**Транзакционность:** `apply-merge` — all-or-nothing. Если любое decision
malformed (невалидный choice, missing fields, ссылка на несуществующую
сущность) — **весь batch abort'ится**, глоссарий **не мутирует**, хеши **не
записываются**. На non-zero exit — исправить decision и переотправить.

**При abort:** посмотри stderr, исправь конкретное malformed decision,
**перезапусти `apply-merge` с тем же payload**. Глоссарий не мутировал
(транзакционность), retry безопасен. `consumed_chunk_ids` не записались —
meta-файлы будут пере-сканироваться. Если retry 3 раза abort'ится на одном
decision — пропусти его (`choice: "skip"` для конфликтов, или убери из
`decisions`) и пометь в `--note`.

**Важно для no-op metas:** даже если `auto_apply` и `decisions_needed` пусты,
но `consumed_chunk_ids` не пуст — **обязательно** вызвать `apply-merge` с
пустыми `auto_apply` и `decisions`, но полным `consumed_chunk_ids`. Иначе
no-op metas будут пересканироваться вечно.

### 6e. Observability

```bash
python3 {baseDir}/scripts/phase2_translate/merge_meta.py status "<temp_dir>"
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
их до самого конца прогона (когда уже поздно что-то менять — все
последующие чанки уже использовали этот вариант). Если термин
переведён плохо, ошибка размножается на все следующие чанки.

**Правило:** после каждого `apply-merge` (после шага 6e) оркестратор
**обязан** проверить, появились ли в `glossary.json` новые сущности с
`confidence: "low"` и `notes: "auto-applied from sub-agent meta"`,
которых ещё не было в предыдущих батчах. Если да — сообщить
пользователю:

```
В этом батче в глоссарий добавлено N новых терминов:

  • «Brightsand» → «Яркопесок» (term, встречен в chunk0007)
    evidence: «the brightsand reflects Smedry talent»
  • «Incarnate Wheel» → «Колесо Инкарны» (term, встречен в chunk0012)
    evidence: «the Incarnate Wheel turned once more»

Хочешь отредактировать какой-нибудь из них сейчас, или продолжить
перевод с текущими вариантами? Если продолжить — все варианты будут
использованы в следующих чанках как есть.

Команды:
  • «ОК» / «продолжить» — оставить как есть, идти дальше
  • «поменяй X: Brightsand → Песчаник» — переписать вариант
  • «покажи chunk0007» — вывести контекст чанка для решения
```

**Что делать:**
1. Если пользователь сказал «продолжить» — идти к следующему батчу.
2. Если пользователь предложил другой вариант — вручную поправить
   `glossary.json` (поле `target` у соответствующей сущности, поменять
   `confidence` на `"high"`, добавить `notes: "confirmed by user"`).
   После правки **пересчитать частоты**:
   ```bash
   python3 {baseDir}/scripts/phase1_prepare/glossary.py count-frequencies "<temp_dir>"
   ```

> **⚠️ КРИТИЧНО: не правь `glossary.json` строковой подстановкой!**
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
> `scripts/shared/edit_glossary_template.py` — скопируй его в
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
3. Если пользователь изменил существующий вариант — оркестратор
   может предложить пере-перевести чанки, где этот термин уже
   использовался. Решение за пользователем; в `run_state.py` есть
   `plan --retranslate-untracked` для форсирования пере-перевода
   конкретных чанков.
4. **Не задавать этот вопрос на каждом чанке** — только один раз
   после завершения батча (4–8 чанков). Если в батче не появилось
   новых low-confidence терминов — не беспокоить пользователя.

> **Антипаттерн:** молча проглотить всё, что sub-агенты предложили
> в `new_entities`, и не показать пользователю до самого конца
> прогона. На большой книге это приводит к тому, что половина
> имён переведена неудачно и пере-переводить слишком дорого.

### 6f.1. Проверка на near-duplicates (после каждого merge)

**Проблема:** `merge_meta.py` делает exact-match dedup (case-insensitive).
Это безопасно (fuzzy matching опасен), но пропускает near-duplicates:
- `Tracker's Lens` (singular) vs `Tracker's Lenses` (plural) — разные source
- `Janci` vs `Janci Patterson` — token-subset
- `Al` vs `Alcatraz` (если `Al` — alias) — alias overlap

Субагенты могут добавить `Tracker's Lenses` как new entity, хотя
`Tracker's Lens` уже есть в глоссарии — и merge_meta это пропустит.

**После каждого merge** (особенно после последнего батча) проверяй:

```bash
python3 {baseDir}/scripts/phase1_prepare/glossary.py find-duplicates "<temp_dir>"
```

Скрипт non-destructive — только REPORTS, не mergeит. Найденные пары
объедини вручную через `edit_glossary_template.py` (один source → alias
другого, target оставь тот, что правильнее).

**Если 0 пар** — отлично, продолжай к 6g (точка сжатия).
**Если >5 пар** — возможно, проблема в промпте перевода (субагенты
систематически создают дубликаты). Проверь 2-3 чанка вручную и убедись,
что они видят терм-таблицу.

### 6g. После последнего батча — ТОЧКА СЖАТИЯ КОНТЕКСТА

Когда все чанки переведены и смержены:

1. **Запиши состояние** в `progress.json`:
   ```bash
   python3 {baseDir}/scripts/shared/save_state.py "<temp_dir>" 6 \
       --note "Все чанки переведены и смержены" \
       --append-list completed_milestones="phase-2-done"
   ```
2. **Предложи пользователю сжать контекст:**
   > Все чанки переведены и смержены. Глоссарий обновлён (N terms, M aliases).
   > Дальше — QA и polish, которые работают с файлами на диске через
   > субагентов. **Рекомендую сжать контекст сейчас** — это последняя
   > обязательная точка.
3. После compact (или без него) — переходи к фазе 3: прочитай
   `references/phase-3-finish.md` и начни с шага 7.

Подробнее про точки сжатия: `references/compaction-points.md`.

### 6h. Точка сжатия контекста (после merge каждого долгого батча)

Если батчей было много (≥3) и контекст раздувается — можно предлагать
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

- **Батч = 4-8 чанков.** Не запускай все 30 чанков одновременно —
  упрёшься в rate limit и потеряешь контекст.
- **После каждого батча — `verify_batch.py` + `check_metas.py` + шаг 6 целиком.** Не копи
  несколько батчей в очередь — merge накапливает конфликты, которые
  сложнее решать оптом.
- **`failed`-чанки — не блокатор.** Помечай и продолжай; проверяй через
  `run_state.py status` (не через post_mortem — `post_mortem.py` не читает
  `run_state.json`).
- **Не подгружай чанки в контекст оркестратора.** Если нужно посмотреть
  evidence из решения — открой нужный кусок через bash `head`, не
  read_file в основной поток.
