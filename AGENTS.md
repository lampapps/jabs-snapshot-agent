# JABS Snapshot Agent

Runs restic backups on a systemd timer; optionally reports to a JABS dashboard. Standalone-capable: everything works without a dashboard. Command line and config files only, no GUI.

## Rules

- The dashboard is optional. `app/dashboard.py` is the only module that talks to it, and every method is a no-op when `JABS_DASHBOARD_URL` or `JABS_AGENT_KEY` is unset (or `dashboard.enabled: false`). A dashboard failure never changes a backup's result and never raises.
- `app/restic.py` is the only module that runs the `restic` binary. The repository and password reach it through environment variables, never as arguments, and must never be logged.
- API contract with the dashboard (`../dashboardv2/docs/AGENTS_API_GUIDE.md`): `POST /api/v2/heartbeat`, `/api/v2/events`, `/api/v2/purge`, `/api/v2/jobs` (each job's schedules, `paths` and `repo_path`, password removed from URLs) and `/api/v2/storage`. Every payload carries `hostname`. Identity is by id: every event, purge and jobs entry carries the job's `job_id`, and the single target's `target_id` is the same id; `job_name` and `target_label` are labels only. State, lock files and the default restic tag are keyed by `job_id`, never by a name. A job file must have a valid, unique `job_id`: the loader rejects one without (naming `job adopt`) and the template's nil-UUID placeholder.
- Scheduling is by systemd timer running `python -m app tick`; there is no cron and no daemon. Due-ness is state based (`app/schedule.py`), so it is correct at any tick interval.

## Stack and layout

- Python 3.11+, pydantic (config only), PyYAML, croniter, portalocker, requests, python-dotenv, textual (admin console only). Do not add dependencies without asking.
- `app/__main__.py` is the CLI (`python -m app <command>`); prefer a new subcommand over a shell script. Shell is only `setup.sh` and `deploy/install.sh`.
- `app/admin/` is the Textual admin console (`python -m app admin`). Every CLI subcommand must also be doable there: add it to `admin.COVERAGE` and register the action id in a tab's `ACTIONS` (`TestParity` in `tests/test_admin_support.py` fails otherwise). Keep logic in `app/ops.py` so the CLI and console share it; repository operations and runs go through `python -m app ...` child processes so they behave exactly like the CLI. Textual is imported lazily so `tick` and `run` never load it.
- `app/config.py`: pydantic models for `config/config.yaml` and `config/jobs/*.yaml`; `settings.py` holds paths and env access, read at call time (nothing is loaded at import).
- `app/runner.py` runs one job; `app/tick.py` is the periodic work (isolated tasks); `app/state.py` is `data/state.json`; `app/storage.py` builds storage samples; `app/mailer.py`, `app/kuma.py`, `app/locking.py`.
- Systemd units are templates in `deploy/`, rendered by `deploy/install.sh`.

## Conventions

- PEP8, `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`, type hints on signatures.
- Narrow exceptions only; a broad `except Exception` needs `# pylint: disable=broad-except` and a reason. No `sys.exit()` outside `__main__`.
- Logging goes to stderr (journald); do not add log files. Level is DEBUG when `ENV_MODE=development`. Use `logging.getLogger(__name__)`.
- Secrets only via `.env`.
- The agent's version is the `MAJOR.MINOR.PATCH` in the root `VERSION` file (read by `app/settings.py`); never write it anywhere else. Every JABS component follows this. Every commit bumps it and starts its message with the new number (see `../dashboardv2/AGENTS.md`).

## Testing

- pytest (`requirements-dev.txt`): `.venv/bin/python -m pytest -q`. Tests use the `home` fixture so the real config, `.env` and state are never touched; `tests/test_integration.py` runs against a real restic repository and is skipped when restic is missing.
- Add or update tests with every non-trivial change.
