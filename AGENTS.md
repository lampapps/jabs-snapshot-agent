# JABS Snapshot Agent

Runs restic backups on a systemd timer; optionally reports to a JABS dashboard. Standalone-capable: everything works without a dashboard. Command line and config files only, no GUI.

## Rules

- The dashboard is optional. `app/dashboard.py` is the only module that talks to it, and every method is a no-op when `JABS_DASHBOARD_URL` or `JABS_AGENT_KEY` is unset (or `dashboard.enabled: false`). A dashboard failure never changes a backup's result and never raises.
- `app/restic.py` is the only module that runs the `restic` binary. The repository and password reach it through environment variables, never as arguments, and must never be logged.
- API contract with the dashboard: v1 endpoints `/api/monitoring/events` and `/api/monitoring/target-purged`, plus `POST /api/v2/logs` and `POST /api/v2/storage`. Every payload carries `hostname`. See `../dashboardv2/README.md`.
- Scheduling is by systemd timer running `python -m app tick`; there is no cron and no daemon. Due-ness is state based (`app/schedule.py`), so it is correct at any tick interval.

## Stack and layout

- Python 3.11+, pydantic (config only), PyYAML, croniter, portalocker, requests, python-dotenv. Do not add dependencies without asking.
- `app/__main__.py` is the CLI (`python -m app <command>`); prefer a new subcommand over a shell script. Shell is only `setup.sh` and `deploy/install.sh`.
- `app/config.py`: pydantic models for `config/config.yaml` and `config/jobs/*.yaml`; `settings.py` holds paths and env access, read at call time (nothing is loaded at import).
- `app/runner.py` runs one job; `app/tick.py` is the periodic work (isolated tasks); `app/state.py` is `data/state.json`; `app/storage.py` builds storage samples; `app/logship.py` ships log lines to the dashboard; `app/mailer.py`, `app/kuma.py`, `app/locking.py`.
- Systemd units are templates in `deploy/`, rendered by `deploy/install.sh`.

## Conventions

- PEP8, `snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`, type hints on signatures.
- Narrow exceptions only; a broad `except Exception` needs `# pylint: disable=broad-except` and a reason. No `sys.exit()` outside `__main__`.
- Logging goes to stderr (journald); do not add log files. Level is DEBUG when `ENV_MODE=development`. Use `logging.getLogger(__name__)`; lines from `app.tick` ship to the dashboard's `tick` log, everything else to `backup`.
- Secrets only via `.env`.

## Testing

- pytest (`requirements-dev.txt`): `.venv/bin/python -m pytest -q`. Tests use the `home` fixture so the real config, `.env` and state are never touched; `tests/test_integration.py` runs against a real restic repository and is skipped when restic is missing.
- Add or update tests with every non-trivial change.
