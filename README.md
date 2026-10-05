# JABS Snapshot Agent

A JABS (Just Another Backup Script) agent that runs [restic](https://restic.net/)
backups on a cron schedule, reports progress/results to the JABS dashboard,
and sends immediate email alerts on failure/completion. Restores are handled
by [Restic Browser](https://github.com/emuell/restic-browser), a third-party
GUI, installable via `./jabs-agent.sh install-browser`.

## Design

- **One restic repository per machine.** Each machine running this agent
  backs up into its own restic repo (typically on local disk or a mounted
  NAS share). Multiple jobs on the same machine can share one repo — restic
  snapshots are tagged per job so retention (`forget --prune`) and restores
  can be scoped to a single job's tag without touching another job's history.
- **No local database.** restic's own snapshot index is the source of truth;
  the agent only keeps a `portalocker`-based lock file per job to prevent
  overlapping runs, plus rotated log files.
- **Local/NAS backend only for v1.** S3-backed repos are not yet supported
  (restic supports it natively; this can be added later without changing the
  agent's architecture).
- **restic binary must be pre-installed** and on `PATH` (or point `RESTIC_BIN`
  at it in `.env`) — this agent never downloads or manages the restic binary
  itself.
- **Live progress reporting.** While a backup runs, restic's own `--json`
  `status` lines (`percent_done`, `bytes_done`, `seconds_remaining`) are
  parsed and posted to the dashboard every ~5s so the Agent Detail page can
  show a live progress bar; the local log only records every 10% crossed.

## Installing restic

Install restic using whichever method suits your machine — the agent doesn't
care how it got there, only that the binary is reachable. See the
[official installation docs](https://restic.readthedocs.io/en/stable/020_installation.html)
for the full list (package manager, static binary download, `restic
self-update`, etc.).

- **Installed via a package manager, or otherwise already on `PATH`**
  (e.g. `apt install restic`, `dnf install restic`, Homebrew, a symlink into
  `/usr/local/bin`): no configuration needed. Leave `RESTIC_BIN` unset/empty
  in `.env` — the agent will call plain `restic` and find it on `PATH`.
  Verify with:
  ```bash
  command -v restic && restic version
  ```
- **Installed somewhere NOT on `PATH`** (e.g. a static binary extracted to a
  private directory): set `RESTIC_BIN` in `.env` to the full path, e.g.:
  ```bash
  RESTIC_BIN="/opt/restic/restic"
  ```
  `./jabs-agent.sh setup` and `./jabs-agent.sh help` both read this override
  (via `.env`) when checking for/reporting the restic binary, and
  `backup.py`/`scheduler.py` use it for every restic call via
  `restic_client.py`.

## Requirements

- Python 3.8+
- [restic](https://restic.readthedocs.io/en/stable/020_installation.html)
  installed and on `PATH`

## Setup

```bash
./jabs-agent.sh setup
```

This creates a venv, installs Python requirements, and copies
`config/global-example.yaml` → `config/global.yaml`, `.env.example` → `.env`,
and a starter job from `config/templates/job.yaml` into `config/jobs/`.

Then:

1. Edit `.env` — set `RESTIC_PASSWORD` (or `RESTIC_PASSWORD_FILE`),
   `JABS_AGENT_KEY`, `JABS_DASHBOARD_URL`, and SMTP credentials if you want
   email alerts.
2. Edit `config/global.yaml` — set `repo_path` and the default retention
   policy.
3. Edit/add job files under `config/jobs/` (see `config/templates/job.yaml`).
4. Initialize the repo and run a first backup:
   ```bash
   venv/bin/python backup.py --job example --init
   ```
5. Add a cron entry to run the scheduler periodically:
   ```
   */15 * * * * /path/to/snapshot_agent/venv/bin/python /path/to/snapshot_agent/scheduler.py > /dev/null 2>&1
   ```

Run `./jabs-agent.sh help` for ready-to-paste commands built from this
machine's actual paths.

## Usage

```bash
# Run one job by name (config/jobs/<name>.yaml)
venv/bin/python backup.py --job example

# Initialize the repo if it doesn't exist yet
venv/bin/python backup.py --job example --init

# Run forget/prune after the backup
venv/bin/python backup.py --job example --prune

# Dry run (passed through to restic; still reports to the dashboard)
venv/bin/python backup.py --job example --dry-run

# Check all jobs' schedules and run any that are due (invoked by cron)
venv/bin/python scheduler.py
```

## Restoring

Restores are done with [Restic Browser](https://github.com/emuell/restic-browser),
a third-party GUI that browses snapshots and restores files/folders while
preserving their original timestamps (unlike some other restore paths). It is
not bundled with this agent and is never invoked by cron.

Install or update it with:

```bash
./jabs-agent.sh install-browser
```

This downloads the latest Linux AppImage release into `restic-browser/` and
makes it executable. Launch it with:

```bash
restic-browser/Restic-Browser.AppImage
```

(AppImages need `libfuse2` on some distros, e.g. `apt install libfuse2`.) Point
it at your `repo_path` from `config/global.yaml` and the `RESTIC_PASSWORD`/
`RESTIC_PASSWORD_FILE` from `.env`.

### Finding a file

Restic Browser doesn't offer a "search all snapshots by partial name" view, so
`scripts/find_file.py` fills that gap:

```bash
venv/bin/python scripts/find_file.py "partial-name"
```

Matches by partial, case-insensitive filename/path fragment (auto-wrapped in
`*...*` wildcards) across every snapshot in the repo, and prints a
deduplicated list of matching paths — paginated 30 at a time if the list is
longer. Read-only; it never restores anything itself (use Restic Browser for
that once you know the path).

## Integrity checks

A cheap `restic check` (metadata only — no data is read back) runs
automatically after every backup to catch repository-structure problems
early. It never fails the backup itself; a failure is logged and emailed
separately (`email.notify_on.error`). Disable it with `verify_after_backup:
false` in `global.yaml` or a job config if you'd rather run checks purely on
your own schedule.

For a real data-integrity check (reads the actual backed-up data, not just
repository structure), run one manually — this is slow and I/O-heavy, so it's
never run automatically:

```bash
./jabs-agent.sh check          # quick check, same as the automatic post-backup one
./jabs-agent.sh check-deep     # slow: reads every byte in the repo
./jabs-agent.sh check-deep --subset 5%   # slow: reads only a 5% sample
```

## Configuration

See `config/global-example.yaml` and `config/templates/job.yaml` for the full,
commented set of options. Job-level `retention`/`exclude` override the global
config when present.

## Launcher

`jabs-agent.sh` handles `setup`, `logs`, `reset`, `install-browser`, `check`,
`check-deep`, and `help`. `reset` only clears local logs/locks — it never
touches your restic repository or config.
