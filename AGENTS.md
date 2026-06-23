# AGENTS.md
Practical guide for coding agents working in `supysonic`.

## Mission
- Keep changes minimal, safe, and reviewable.
- Base decisions on repository evidence.
- Verify modified behavior with reproducible commands.

## Verified Project Facts
- Language/runtime: Python.
- Build backend: `setuptools.build_meta` (`pyproject.toml`).
- Test framework: `unittest` (primary).
- CI tests: coverage + unittest.
- Docs build: Sphinx via `docs/Makefile`.
- Core stack includes Flask and Peewee.

Evidence files:
- `.github/workflows/tests.yaml`
- `README-en.md`
- `pyproject.toml`
- `docs/Makefile`
- `setup.cfg`
- `setup.py`

## Rule Sources
- Checked and not found: `.cursor/rules/`, `.cursorrules`, `.github/copilot-instructions.md`.
- If any of these are added later, apply the more specific rule first, then treat this file as fallback guidance.

## Setup
Linux/macOS virtualenv:
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r ci-requirements.txt
```
Windows local Conda environment:
```powershell
conda activate supysonic
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r ci-requirements.txt
```
- On this Windows workspace, prefer the existing Conda environment named
  `supysonic` at `C:\Users\jsdfhasuh\.conda\envs\supysonic`.
- If activation is unreliable, call the interpreter directly:
  `C:\Users\jsdfhasuh\.conda\envs\supysonic\python.exe`.
- `ci-requirements.txt` includes `-e .`, `coverage`, and `lxml`.
- Some tests require `lxml`.

## Build Commands
```bash
python -m build
python setup.py sdist
```
- `setup.py sdist` triggers `sphinx-build -q -b man docs man`.
- If packaging changes, verify generated man pages too.

## Docs Commands
Run inside `docs/`:
```bash
make help
make html
sphinx-build -M html . _build
```

## Test Commands
```bash
python -m unittest
python -m unittest tests.net.suite
coverage run -m unittest
coverage run -a -m unittest tests.net.suite
coverage report -m
```

## Single-Test Commands
Use standard `unittest` dotted paths:
```bash
python -m unittest tests.api.test_media
python -m unittest tests.api.test_media.MediaTestCase
python -m unittest tests.api.test_media.MediaTestCase.test_stream
python -m unittest discover -s tests/api -p "test_*.py"
```
- Start with the narrowest test that covers the change.
- Widen to `python -m unittest` if the impact is unclear.

## Run / Dev Commands
Flask dev server on Linux/macOS:
```bash
export FLASK_APP="supysonic.web:create_application()"
export FLASK_ENV=development
flask run
```
Flask dev server on Windows PowerShell:
```bash
conda activate supysonic
$env:FLASK_APP="supysonic.web:create_application()"
$env:FLASK_ENV="development"
flask run
```
Installed entry points:
```bash
supysonic-cli --help
supysonic-server
supysonic-daemon
```

## Windows Local Debugging Scope
- Treat this Windows workspace as frontend display/debug only by default.
- Use the in-app browser and localhost pages to inspect UI rendering,
  interaction behavior, templates, CSS, and static assets.
- Do not proactively debug or change backend runtime behavior, database data,
  schema migrations, library scanning, daemon watcher behavior, transcoding, or
  music-library path migration while working on Windows.
- Do not run commands that mutate the media library or database state, including
  folder scans, path migration scripts, daemon watcher runs, or release upload
  workflows, unless the user explicitly asks for that operation.
- `supysonic.conf` is a private local runtime file and may contain secrets. Do
  not print, commit, or broadly rewrite it. Only adjust local Windows paths such
  as cache, logs, socket, and upload directories when explicitly requested.
- If a frontend issue needs backend context, prefer read-only inspection and
  focused frontend render tests before considering broader backend commands.

## Lint / Formatting Status
- No official config detected for `ruff`, `flake8`, `black`, `isort`, or `mypy`.
- Do not introduce new lint tooling unless the task explicitly asks for it.
- Keep formatting consistent with touched files.

## Code Style Guidelines
### Imports
- Order groups as: stdlib -> third-party -> local modules.
- Avoid new wildcard imports.
- Keep imports explicit and stable.
- Existing package `__init__.py` files may intentionally use wildcard imports; do not mechanically rewrite them.

### Formatting
- Follow PEP 8 baseline in edited files.
- Prefer focused functions and shallow nesting.
- Add comments only for non-obvious logic.
- Do not use opportunistic rewrites as part of unrelated work.

### Types
- Add type hints for new or modified signatures.
- Prefer concrete types over `Any`.
- Use nullable types only where the value can actually be absent.
- Do not turn a small fix into a repository-wide typing pass.

### Naming
- New modules/files: `snake_case.py`.
- Variables/functions: `snake_case`.
- Classes: `PascalCase`.
- Constants: `UPPER_SNAKE_CASE`.
- Preserve legacy names unless rename is required for correctness.

### Error Handling
- Catch specific exceptions where possible.
- Avoid bare `except:` and silent swallow.
- Keep API error behavior compatible with existing callers.
- Include enough context in errors or logs to debug IDs, paths, or key params.

### Logging
- Use module logger: `logger = logging.getLogger(__name__)`.
- Use parameterized log messages for new code.
- Never log secrets, passwords, tokens, or keys.

### Data / DB Changes
- Reuse existing manager/model patterns first.
- Consider race conditions around unique constraints.
- Validate DB-related edits with targeted tests.

## Verification Discipline
- Run at least affected tests after edits.
- If impact is uncertain, run `python -m unittest`.
- If docs, packaging, or entry points are touched, run the relevant command too.
- Report what was verified and what was not.

## Git Safety
- Avoid destructive git operations.
- Do not mix unrelated refactors in one change.
- Do not overwrite user changes you did not create.
- Do not change CI or major dependency versions unless requested.

## Cursor / Copilot Rules Check
Checked and not found:
- `.cursor/rules/`
- `.cursorrules`
- `.github/copilot-instructions.md`
