# Схемы JSON-артефактов: `glossary.json` и `meta.json`

Этот файл — **единственный источник истины** по схемам обоих JSON-артефактов
пайплайна. Инструкции субагенту (и оркестратору), который записывает
`glossary.json` или `meta.json`, берутся **отсюда** — не из памяти.

---

# Часть 1. Схема `glossary.json` (v2)

`<temp_dir>/glossary.json` — human-facing файл: его правит пользователь,
дописывает `merge_meta.py`, читают `print-terms-for-chunk`, `run_state.py`
и `pronoun_check.py`.

## Полный пример (минимально валидный файл)

```json
{
  "version": 2,
  "high_frequency_top_n": 20,
  "applied_meta_hashes": {},
  "terms": [
    {
      "id": "sing_sing",
      "source": "Sing Sing",
      "target": "Синг-Синг",
      "aliases": ["Sing"],
      "category": "person",
      "gender": "male",
      "confidence": "high",
      "frequency": 0,
      "evidence_refs": [],
      "notes": "антрополог, кузен рассказчика"
    }
  ]
}
```

## Обязательные поля

| Поле | Уровень | Обязательно | Что будет, если пропустить |
|---|---|---|---|
| `version` | top-level | **ДА, ровно `2`** | без него файл — не глоссарий v2; скрипты **останавливаются с ошибкой** (раньше молча подставлялся пустой глоссарий и данные терялись) |
| `terms` | top-level | **ДА, массив** | то же — hard error |
| `high_frequency_top_n` | top-level | нет (дефолт из `config.toml`) | подставится дефолт |
| `applied_meta_hashes` | top-level | нет (создастся при merge) | создастся пустым |
| `id` | term | **ДА** — но **генерируется скриптом** | `validate-glossary --fix` проставит; `run_state.py` использует id как ключ хешей |
| `source` | term | **ДА** (английский оригинал) | error |
| `target` | term | **ДА** (русский перевод) | error |
| `aliases` | term | нет (`[]`) | warning |
| `category` | term | нет (`person`/`place`/`org`/`term`/`other`) | warning |
| `gender` | term | нет (`male`/`female`/`neutral`/`unknown`) | warning |
| `confidence` | term | нет (`high`/`medium`/`low`) | warning |
| `frequency` | term | нет — считается `count-frequencies` | warning |
| `evidence_refs`, `notes` | term | нет | — |

> **`confidence` и обратная связь пользователя.** Агенты добавляют
> новые термины с `confidence: "low"`. Когда пользователь подтверждает
> вариант («ОК», «верно»), подними `confidence` до `"high"` и допиши в
> `notes` «confirmed by user». Делай это штатной командой, а не
> ручной правкой JSON:
> ```bash
> python3 {baseDir}/scripts/phase1_prepare/glossary.py confirm-terms "<temp_dir>" --all
> python3 {baseDir}/scripts/phase1_prepare/glossary.py confirm-terms "<temp_dir>" --source "Brightsand"
> python3 {baseDir}/scripts/phase1_prepare/glossary.py confirm-terms "<temp_dir>" --id brightsand
> ```
> Команда пропускает уже подтверждённые (`high`) термины (кроме
> `--force`) и не записывает файл, если ни один термин не выбран.

> **`"version": 2` — самое частое место, где ломается глоссарий.**
> Если ты (или субагент) пишешь `glossary.json` целиком — скопируй
> структуру из примера выше вместе с ключом `version`.

## `id` генерируется автоматически — не придумывай его

`id` выводится из английского `source` детерминированно
(`scripts/shared/common.py → make_term_id`): нижний регистр, апострофы
выбрасываются, всё остальное не-буквенно-цифровое → `_`.

| `source` | `id` |
|---|---|
| `Sing Sing` | `sing_sing` |
| `Tracker's Lenses` | `trackers_lenses` |
| `Order of the Broken Lens` | `order_of_the_broken_lens` |

Коллизии получают суффикс `_2`, `_3`. Проставить недостающие id:

```bash
python3 {baseDir}/scripts/phase1_prepare/glossary.py validate-glossary "<temp_dir>" --fix
```

Уже существующие id **никогда не переписываются**: `run_state.json`
хранит хеши терминов по id, и переименование id заставило бы пайплайн
считать все переведённые чанки «устаревшими».

## Что НЕ должно попадать в глоссарий

Глоссарий — про **повторяющуюся** терминологию, которую нужно держать
единой по всей книге. Одноразовое имя стоит перевести по месту, в тексте
чанка, а не заносить в словарь.

**Не заноси:**
- имена из **благодарностей, посвящения, вступления/послесловия автора**:
  редакторы, агенты, издатели, семья автора, друзья, бета-ридеры;
- реальных людей и компании, упомянутые один раз (Tor Books, Amazon,
  Kickstarter, имена реальных писателей в эпиграфе-цитате);
- названия книг, серий, цитаты, продуктовые описания
  (`"Blenderbuss 3000 Exploding Edition"`);
- эпизодических персонажей и локации, встречающиеся ровно один раз, если
  их перевод очевиден и не влияет на согласованность;
- служебные строки вёрстки («Contents», «Copyright», «About the Author»).

**Заноси:** сквозных персонажей, географию и организации мира, магические
системы и термины мира, повторяющиеся прозвища и обращения.

Практический критерий: `frequency` ≥ 2 (термин встречается минимум в двух
чанках) **или** термин важен для сюжета/серии. `validate-glossary`
отдельно подсвечивает термины с `frequency` 0 и 1 — это кандидаты на
удаление.

## Протокол записи `glossary.json` (обязателен)

1. **Никогда** не правь `glossary.json` строковой подстановкой
   (`str.replace`, sed, ручной edit фрагмента). Только
   `json.loads` → правка → `json.dumps`; готовый шаблон —
   `scripts/shared/edit_glossary_template.py`.
2. **После любой записи** (особенно если писал субагент) — прогони шлюз:
   ```bash
   python3 {baseDir}/scripts/phase1_prepare/glossary.py validate-glossary "<temp_dir>"
   ```
   Только после `GLOSSARY OK` запускай `count-frequencies` и переходи
   дальше.
3. **Отчёт субагента — не доказательство.** «Записал 78 терминов и
   проверил» ничего не значит, пока `validate-glossary` не подтвердил это
   на диске. Проверяй сам.

## Инцидент, из-за которого появились эти правила

Субагент дважды записал `glossary.json` без `"version": 2`.
`load_glossary()` тогда молча возвращал пустой глоссарий, а
`count-frequencies` записывал этот пустой результат **поверх** файла —
78 собранных терминов уничтожались молча.

Что изменено в скриптах:
- `load_glossary()` больше **никогда** не подставляет пустой дефолт для
  существующего файла: либо мигрирует (если есть `terms`, но нет
  `version`), либо падает с ненулевым кодом;
- `save_glossary()` / `save_glossary_atomic()` **отказываются** писать
  пустой список терминов поверх непустого файла;
- `count-frequencies` сверяет число терминов на диске до загрузки и
  прерывается, если загрузилось 0, а на диске было больше;
- добавлена команда `validate-glossary [--fix]` — шлюз после любой записи.

---

# Часть 2. Схема meta.json (v2) — детальная справка

`output_chunkNNNN.meta.json` — наблюдения субагента-переводчика. Этот
файл **обязателен** (даже если пустой по содержимому), его наличие
трекается в `applied_meta_hashes`. Схема v2:

```json
{
  "schema_version": 2,
  "new_entities": [
    {"source": "EnglishName", "target_proposal": "Русский", "category": "person",
     "evidence": "≤200 chars from chunk"}
  ],
  "alias_hypotheses": [
    {"variant": "Alt", "may_be_alias_of_source": "Main", "evidence": "≤200 chars"}
  ],
  "attribute_hypotheses": [
    {"entity_source": "X", "attribute": "gender", "value": "male",
     "confidence": "high", "evidence": "≤200 chars"}
  ],
  "narrator_identification": {
    "name": "Kaladin|Shallan|Dalinar|Wit|unknown|omniscient|dialogue_only|multiple",
    "confidence": "high|medium|low",
    "evidence": "≤300 chars обоснование",
    "gender_of_narrator": "male|female|neutral|unknown",
    "voice_markers": ["lex1", "lex2", "syntax_pattern"],
    "is_pov_change_inside_chapter": false
  },
  "epigraph_translation": {
    "structural_id": "epigraph_before_line_42",
    "strategy": "kept_as_is|adapted_with_paren|transliterated|swapped_for_russian",
    "translated_text": "...",
    "attribution_strategy": "transliterated|glossary|real_name|kept_as_is"
  },
  "used_term_sources": ["id1", "id2"],
  "conflicts": [
    {"entity_source": "X", "field": "target", "injected": "Старый",
     "observed_better": "Новый", "evidence": "≤200 chars"}
  ]
}
```

---

## Имена полей — критично

| Поле в meta.json | Тип | Назначение |
|---|---|---|
| `new_entities[].source` | string (ASCII) | Английское имя собственное |
| `new_entities[].target_proposal` | string | Предлагаемый русский перевод |
| `new_entities[].category` | string | person/place/org/term/other |

**Критично:** в `new_entities` поле называется **`target_proposal`** (с суффиксом
`_proposal`), а НЕ `target`. В глоссарии (после merge) оно станет `target`, но
в meta.json — именно `target_proposal`. Это разные имена для одного поля в
режиме записи (sub-agent) и чтения (merge) — не путай.

Если записать `target` вместо `target_proposal` — `merge_meta.py` пометит
meta как malformed (missing `target_proposal`) и observations будут потеряны.

---

## Валидация meta.json — что считается malformed

### 1. Не указывай `chunk_id` в meta.json

`chunk_id` выводится из имени файла. Указание `chunk_id` в payload —
валидационная ошибка, meta будет quarantined как malformed.

### 2. Не выдумывай сущности для «галочки»

Пустой meta (все массивы пусты) — валидный вывод. Не добавляй сущности,
алиасы, гипотезы, если нет реальных наблюдений.

### 3. `new_entities.source` — правила валидации

`merge_meta.py` валидирует source через regex `^[A-Za-z][A-Za-z\s'\-]*$`:
- **Только ASCII-буквы** (A-Z, a-z). **Цифры запрещены** — поэтому
  `"Blenderbuss 3000 Exploding Edition"` отбрасывается (из-за `3000`).
- Пробелы, апострофы (`'`), дефисы (`-`) **разрешены** — для multi-word
  имён собственных (`"Tracker's Lenses"`, `"Dark Talent"`, `"Sing-Sing"`).
- Non-ASCII (кириллица, акценты) **запрещён** — галлюцинация субагента.

**Правила для source:**
- **Не используй source с цифрами.** Если термин содержит цифру
  (`"Blenderbuss 3000"`) — выбери ключевое слово без цифры
  (`"Blenderbuss"`), а полную форму и описание — в `evidence`.
- **Не используй названия книг, цитаты или продуктовые описания** как
  `source`. `"Alcatraz vs. the Evil Librarians"` → отброшен. Название
  книги — не термин глоссария.
- **Multi-word source допустим** только если это **устоявшееся имя
  собственное** (артефакт, место, организация):
  - OK: `"Tracker's Lenses"`, `"Order of the Broken Lens"`, `"Dark Talent"`
  - НЕ OK: `"Blenderbuss 3000 Exploding Edition"` (продуктовое описание),
    `"the evil librarians who stole the book"` (описание, не имя)
- **Если сомневаешься** — выбери короткое ключевое слово.

### 3a. Не предлагай одноразовые имена (КРИТИЧНО)

`new_entities` — это **кандидаты в глоссарий**, а глоссарий нужен только
для терминологии, которая повторяется по книге. Не предлагай:

- имена из благодарностей, посвящения, вступления/послесловия автора
  (редакторы, агенты, издатели, семья, бета-ридеры);
- реальных людей и компании, упомянутых один раз;
- названия книг и серий, цитаты, продуктовые описания;
- эпизодических персонажей/локации, встречающихся ровно один раз, если
  перевод очевиден.

Такие имена переводи **по месту, в тексте чанка**, и не упоминай в
`new_entities`. Если чанк целиком — благодарности, посвящение,
copyright или «об авторе», то нормальный `meta.json` для него —
**пустой** (все массивы пусты).

Проверочный вопрос перед добавлением: «встретится ли этот термин ещё в
других главах, и испортит ли разнобой перевода читательский опыт?»
Если нет — не добавляй.

### 3b. Имена персонажей: краткая vs полная форма (КРИТИЧНО)

`glossary.py print-terms-for-chunk` сравнивает по surface-формам (source
+ aliases). Если в чанке встречается только **имя** («Attica», «Sing»),
а в глоссарии лежит **полное имя** («Attica Smedry», «Sing Sing»),
surface-форма НЕ совпадёт → термин не попадёт в таблицу → субагент
честно предложит «Attica» как new_entity → **дубликат**.

**Реальный кейс (Bastille vs. the Evil Librarians):**
| Уже в глоссарии (high) | Предложено субагентом (low) | Перевод совпадает? |
|---|---|---|
| `Attica Smedry` → «Аттика Смедри» | `Attica` → «Аттика» | ✅ (корень) |
| `Sing Sing` → «Синг-Синг» | `Sing` → «Син» | ❌ **НЕТ** |
| `Sing Sing` → «Синг-Синг» | `Sing Smedry` → «Синг Смедри» | ❌ **НЕТ** |

Результат: персонаж Sing переведён 3 разными способами в разных главах.

**Правила для субагента (см. translate_chunk.md, секция "Имена персонажей"):**
1. Если видишь краткое имя, а в терм-таблице есть полное (отличается
   только фамилией) — **НЕ добавляй как new_entity**. Используй target
   из полной записи, или предложи краткую форму как `alias_hypothesis`:
   `{"variant": "Attica", "may_be_alias_of_source": "Attica Smedry", "evidence": "..."}`.
2. **Исключение:** если краткая форма — это ДРУГОЙ персонаж, добавляй
   как new_entity, но в `evidence` укажи, чем он отличается от
   полного имени в глоссарии.

**Рекомендация для оркестратора (при сборке глоссария на шаге 3):**
Предпочитайте **отдельные термины для имени и фамилии**, а не одно
полное имя. Это позволяет:
- Переводить «Sing» и «Smedry» независимо (как они встречаются в тексте)
- Избегать дубликатов, когда субагент видит только краткую форму
- Сохранять консистентность: «Sing» всегда → «Син», «Smedry» всегда → «Смедри»

Пример (`id` проставит `validate-glossary --fix` — вручную не пиши):
```json
{"id": "sing", "source": "Sing", "target": "Син", "aliases": [], "category": "person",
 "notes": "first name; see also 'Smedry' (family name)"},
{"id": "smedry", "source": "Smedry", "target": "Смедри", "aliases": [], "category": "person",
 "notes": "family name; appears as 'X Smedry' in text, see also separate terms for each family member"}
```

Если встречается «Sing Smedry» — это составная форма, переводится как
«Син Смедри» из двух отдельных терминов.

**Альтернатива (если пользователь предпочитает полные имена):**
Одна запись с aliases:
```json
{"id": "sing_sing", "source": "Sing Sing", "target": "Син-Син",
 "aliases": ["Sing", "SingSmedry", "Sing Smedry"], "category": "person"}
```
Но тогда `print-terms-for-chunk` найдёт термин только если в чанке
встретится одна из surface-форм (включая aliases).

### 4. Все строки в meta.json — валидный JSON

Это означает:
- **Не используй неэкранированные `"` внутри строк.** Если нужна кавычка
  внутри `evidence` или `voice_markers` — используй русские ёлочки
  «...» или экранируй как `\"`. Например:
  - `"voice_markers": ["обращение «вы» к читателю"]` — правильно;
  - `["вы" к читателю]` — сломает JSON.
- **Не вставляй сырые переводы строк** внутри строковых значений —
  используй `\n` или сокращай.
- **Если сомневаешься, что JSON валиден** — лучше выдай пустой meta
  (`{"new_entities": [], "alias_hypotheses": [], ...}`), чем сломанный.

### 5. `narrator_identification` — твоё решение, не скрипта

Если в чанке один очевидный POV — `confidence: high`. Если стык или
неясно — `medium`/`low`. Не «угадывай» по одной фразе.

### 6. `epigraph_translation` — только если в чанке есть эпиграф

Заполняется только если в чанке есть эпиграф (см. `structural_units.json`).
Иначе поле опускается.

### 7. Если терм-таблица пуста — правило 2 (translate_chunk.md) не добавлять

### 8. Если neighbour context пуст — правило 3 пропустить

---

## Post-validate meta после шага 5

После завершения каждого батча перевода, перед merge (шаг 6), рекомендуется
прогнать быструю проверку всех meta-файлов на валидность JSON. Если meta
malformed — merge_meta.py quarantines её, но лучше поймать это раньше и
попросить субагента пере-создать meta.

**Штатный скрипт:**
```bash
python3 {baseDir}/scripts/shared/check_metas.py "<temp_dir>"
```

Проверяет все `output_chunk*.meta.json` на валидность JSON, печатает
имена и ошибки malformed-файлов.

Если найдены malformed metas — у субагента есть 2 пути:
1. Попросить субагента пере-создать meta.json (с явной инструкцией про
   валидный JSON и экранирование кавычек).
2. Исправить вручную (если ошибка простая — например, замена `"` на `«...»`
   внутри `voice_markers`).

---

## Типичные malformed-кейсы из реальных прогонов

- **chunk0006 (Alcatraz book 4):** в `voice_markers` были неэкранированные
  кавычки (`["вы" к читателю, ...]`) → сломанный JSON. Исправлено на
  `["обращение «вы» к читателю", ...]`.
- **chunk0029 (Alcatraz book 4):** `new_entities` содержал
  `source: "Alcatraz vs. the Evil Librarians"` (название книги с пробелами,
  не ASCII-токен) → validate_meta пометил malformed. Удалена эта запись
  (название книги не термин глоссария).
- **chunk0006 (Bastille book):** `new_entities` содержал
  `source: "Blenderbuss 3000 Exploding Edition"` — содержит цифры `3000`,
  regex `^[A-Za-z][A-Za-z\s'\-]*$` не разрешает цифры → malformed.
  Удалена запись; перевод «Блендербасс 3000, взрывное издание» инлайнен
  в тексте чанка (продукт одноразовый, не термин глоссария).

Все правки сделаны вручную, после чего повторный merge принял чанки.

> **Урок:** если субагент упорно предлагает source с цифрами или
> продуктовым описанием — это сигнал, что термин не глоссарийный.
> Удали запись из meta.json и инлайнь перевод в текст чанка.
