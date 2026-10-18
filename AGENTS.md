# AGENTS.md - book-translate-ru

## Purpose

- This repository contains a skill for the translation of books from English to Russian.
- The skill uses the Agent Skills specification.
- The skill is not an application. It contains instructions, prompts, and Python scripts.
- To install the skill, copy the folder into the skills directory of your platform.
- Example paths: `~/.claude/skills/` and `~/.kilo/skills/`.
- IMPORTANT: A change in this repository changes all future translations.
- After a change, check that `SKILL.md`, `references/`, and `config.toml` agree.

## Languages

- Write all documents in Russian. The documents are `SKILL.md`, `README.md`, `references/`, `prompts/`, and `templates/`.
- Write the comments in `config.toml` and the git commit messages in Russian.
- Write the code, the command-line arguments, the JSON keys, and the docstrings in English.
- Do not mix the two languages in one file.

## Architecture

The structure of the project is not clear from the file names. Read this section.

- `SKILL.md` is a small orchestrator file for the translation agent.
- The detailed procedure of each step is in `references/phase-{1,2,3}-*.md`.
- The agent reads these files during the pipeline. Thus the files survive context compaction.
- To add a pipeline rule, write the rule in `references/`. Refer to the rule from `SKILL.md`. Do not add the rule to a script only.
- The pipeline has nine steps in three phases.
- The state is in `progress.json` in `<book>_temp/`.
- Use `scripts/shared/load_state.py` to read the state. Use `scripts/shared/save_state.py` to write the state.
- `scripts/shared/state_map.py` is the only source of the step-to-phase map. Do not copy this map to other files.

### Work split

- The orchestrator does not do LLM work.
- Sub-agents do the translation, the quality checks, the polish, and the narrator decisions.
- Sub-agents use the prompts in `prompts/`.
- The orchestrator only starts scripts, starts sub-agents, and merges the results.
- Exception: the orchestrator can select an option in `decisions_needed` of `merge_meta.py`.
- Exception: the orchestrator can write `голос_книги.md` and the first `glossary.json` at step 3.

### Step 3

- Step 3 is a hard stop.
- After you build the glossary, wait for the user to confirm it.
- "ok" and general words are not a confirmation.

### Data

- The schemas of `glossary.json` and `meta.json` are in `references/meta-json-schema.md`.
- `glossary.json` must contain `"version": 2`.
- `common.py` makes each term id from the English term. The function is `make_term_id`.
- Change `glossary.json` only with `scripts/shared/glossary_io.py` or `scripts/shared/edit_glossary_template.py`.
- Never use `str.replace` or sed on `glossary.json`.

### Tools

- Use Pandoc for step 1 only. Step 1 converts the source to Markdown.
- The make of the FB2 file in step 9 uses `lxml` and XSD validation.
- The script is `scripts/phase3_finish/fb2_builder.py`.
- Do not use Pandoc for the FB2 file.

## Commands and Checks

- Use Python 3.11 or a later version.
- The configuration loader uses the standard library module `tomllib`. There is no other parser.

```bash
pip install -r requirements.txt
python3 {baseDir}/scripts/shared/config.py
```

- `requirements.txt` contains `beautifulsoup4` and `lxml` only.
- Do not add large dependencies.
- The command `config.py` shows the default values.

The command structure of the scripts:

```bash
python3 {baseDir}/scripts/<phase>/<script>.py "<temp_dir>"
```

- `{baseDir}` is the skill root directory. Replace it with the correct path.
- `<temp_dir>` is the work directory of the book.
- Start the scripts from the work directory of the user. The `<book>_temp/` directory is there.
- The scripts find `config.toml` in this order: `cwd/config.toml`, then `cwd/*_temp/config.toml`, then the skill root.

- This repository has no tests, no linter, and no CI.
- To check a script, use a real book.
- Make a `<book>_temp/` directory. Then start the scripts step by step.
- Many scripts have a smoke test in the `__main__` block.
- After each write to the glossary, start `glossary.py validate-glossary`.

## Script Conventions

- The shared modules have no `__init__.py` file.
- Add this line at the start of a script:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
```

- Then import the modules with `from common import ...` and `from config import ...`.
- Example: `scripts/phase2_translate/merge_meta.py`.
- Use the functions in `common.py`: `process_dir`, `run_cmd`, `sha256_file`, `stable_hash`, `make_term_id`, `ensure_term_ids`, `find_english_leaks`.
- Use the functions in `config.py`: `atomic_write_text`, `atomic_write_json`, `read_json_safe`, `sanitize_for_json`, `read_text_safe`.
- Do not copy these functions into a new script.
- Read and write the glossary only with `scripts/shared/glossary_io.py`: `load_glossary`, `save_glossary`, `GlossaryError`.
- If the glossary is damaged, the script stops. The script does not replace the glossary with an empty default.
- `config.py` has the `DEFAULTS` dictionary. `config.toml` has the same values.
- If you change one value, change the other value too.
- Example: `[parallelism] batch_size` is 8 in `DEFAULTS` and 4 in `config.toml`.
- The scripts do not read `batch_size`. It is a recommendation for the orchestrator.
- The scripts write to `sys.stdout` in UTF-8. The import of `common.py` or `config.py` sets UTF-8.
- The scripts write files with `atomic_write_text` or `atomic_write_json`. These functions are safe on Windows.

## Windows and PowerShell

- The developers use Windows.
- Read `references/windows-powershell.md` before you start the scripts.

- A `python3 -c` command with a loop or a generator gives no output. The command fails without a message.
- Write your Python code in a temporary `.py` file.
- Put the temporary file in `<temp_dir>/process/`.
- Start the name of the file with an underscore.
- The redirect symbol `<` does not work in PowerShell.
- A path with Cyrillic letters can fail. Change the directory to the parent directory first. Then use a relative path.
- A `WinError 5` error or a `PermissionError` error during an atomic write is a normal event. The script corrects the write.
- A `UnicodeEncodeError` error with a surrogate character is a normal event. The script removes the bad characters.
- Do not write a workaround script for these two errors.

## Git

- The `.gitignore` file ignores all `*_temp/` directories and all `__pycache__/` directories.
- Do not commit test translations.
- The `.gitignore` file also ignores the hidden root directories, for example `.vscode/` and `.idea/`.
- The exception is `!.github/`.
- Use conventional commit messages in Russian. Example: `feat(glossary): ...` and `fix(...): ...`.
