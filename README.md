# JABS Snapshot Agent

A JABS (Just Another Backup Script) agent that runs [restic](https://restic.net/) backups on a schedule, emails failures, and can report to a JABS dashboard.

- **Command line and config files only.** There is no GUI on the agent; everything you watch lives on the dashboard.
- **Runs standalone.** Without a dashboard it backs up, prunes, checks and emails exactly the same. The dashboard is optional.
- **Scheduled by a systemd timer**, not cron. A timer runs `python -m app tick` every 5 minutes; the tick decides which jobs are due.

## Design

- **One restic repository per machine.** Jobs on the machine share it and are told apart by restic tags, so retention (`forget --prune`) and restores can be scoped to one job.
- **No database.** Restic's snapshot index is the source of truth. The agent keeps a lock file per job and a small `data/state.json` (which schedule slots were served, each job's last result, when storage was last reported).
- **restic must be installed** and on `PATH` (or set `RESTIC_BIN` in `.env`). The agent never downloads it.
- **Local or remote repositories.** Anything restic accepts as `repo_path` works; free-space reporting only applies to local paths.

## Requirements

- Python 3.11 or newer with `python3-venv`
- [restic](https://restic.readthedocs.io/en/stable/020_installation.html)
- systemd, for the timer (without it, run `python -m app tick` from anything that can run a command every few minutes)

## Setup

```bash
./setup.sh            # venv, dependencies, config/config.yaml, .env, an example job
```

`./setup.sh` never overwrites existing files. If it finds the old `config/global.yaml` it copies it to `config/config.yaml` once.

Then:

1. Edit `.env`: `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE`, and optionally SMTP login and the dashboard URL and key.
2. Edit `config/config.yaml`: `repo_path`, retention, email.
3. Add jobs in `config/jobs/*.yaml` (copy `config/templates/job.yaml`) and enable a schedule.
4. Create the repository and try a job:

   ```bash
   .venv/bin/python -m app init-repo
   .venv/bin/python -m app run JOB --dry-run
   ```

5. Install the timer:

   ```bash
   sudo deploy/install.sh
   ```

Check everything with `.venv/bin/python -m app config`.

### What `deploy/install.sh` does

It installs a oneshot service and a timer (`jabs-snapshot-agent.service` and `.timer`), runs the config check as the service user, and starts the timer. The service is hardened (`ProtectSystem=strict`, `NoNewPrivileges`, private `/tmp`) and may write only to `data/`, a local repository directory and the restic cache, which it reads from your config with `python -m app paths`. **Run the script again after changing `repo_path` or a cache directory.**

| Option | Meaning |
| --- | --- |
| `--user NAME` | Run as this user (default: the user who ran `sudo`). `root` is allowed, with a warning. |
| `--read-all` | Let a non-root user read files it does not own (`CAP_DAC_READ_SEARCH`), to back up system directories or other users' homes. |
| `--name UNIT` | Unit name (default `jabs-snapshot-agent`). |
| `--no-start` | Install and enable the timer without starting it. |
| `--render-only DIR` | Write the unit files into DIR and change nothing on the system. |

```bash
systemctl status jabs-snapshot-agent.timer
journalctl -u jabs-snapshot-agent -f          # logs
sudo systemctl start jabs-snapshot-agent      # run a tick now
```

Logs go to the journal; the agent writes no log files. If you used the old cron-based scheduler, the script warns when a `scheduler.py` line is still in a crontab. Remove it, or jobs will run twice.

## Scheduling

Each job lists schedules as 5-field cron expressions (`schedules: [{cron: "0 3 * * *", enabled: true}]`). They are evaluated by the agent, and no cron daemon is involved.

- The agent remembers, per job and expression, the latest scheduled time it has served. A newer slot is due.
- **A missed run happens once at the next tick**, for example after the machine was off or while another backup was still running. Several missed slots still mean one run.
- A job seen for the first time only gets a baseline, so installing never starts a surprise backup.
- A failed backup is not retried until its next slot.
- Several schedules of one job that are due together run it once.
- Jobs run one after another within a tick, and a job already running (a manual `run`) is skipped.

The tick every 5 minutes means a job starts up to 5 minutes after its scheduled time. For tighter timing change `OnCalendar` in `deploy/agent.timer` (for example `minutely`) and run `deploy/install.sh` again.

## Commands

Run as `.venv/bin/python -m app <command>`.

| Command | Purpose |
| --- | --- |
| `config` | Validate `config.yaml` and the job files; shows whether the dashboard is in use |
| `tick` | Run what is due, report to the dashboard (the timer does this) |
| `run JOB [--init] [--prune] [--dry-run]` | Run one job now; `--dry-run` still reports to the dashboard |
| `init-repo` | Create the repository if it does not exist |
| `check [--deep] [--subset 5%]` | Verify the repository; metadata only unless `--deep` or `--subset` (slow, reads data) |
| `snapshots [--job NAME]` | List snapshots |
| `ls SNAPSHOT [PATH]` | List the files in a snapshot |
| `restore SNAPSHOT TARGET [--path P]` | Restore a snapshot, or one path of it, into TARGET |
| `find [QUERY]` | Search all snapshots for a file by partial, case-insensitive name |
| `jobs` | Each job's next scheduled run and last result |
| `testmail` | Send a test email with the SMTP settings |
| `report-storage [--print]` | Measure and report storage now |
| `reset --yes` | Delete lock files and the schedule state (never the repository or your config) |
| `paths` | Print the directories the agent must be able to write |

A cheap `restic check` (metadata only) runs after every backup unless `verify_after_backup: false`. A failing check is reported and emailed but does not fail the backup.

## Dashboard (optional)

Set `JABS_DASHBOARD_URL` and `JABS_AGENT_KEY` in `.env` (register the agent on the dashboard host with `python -m app agent add NAME`). Leave them empty, or set `dashboard.enabled: false` in `config.yaml`, to run standalone; `python -m app config` says which mode is active.

What the dashboard receives:

- **Events** for every run (start, stages, progress about every 5 seconds, completion, check result, snapshots removed by pruning) and a heartbeat with each job's schedule on every tick. Every request carries the machine's **hostname** (`hostname:` in `config.yaml`, else the system name) so jobs on different machines with the same name stay apart.
- **Logs**, shipped as two logs: `tick` and `backup` (job name in each line). A background thread sends batches; if the dashboard is down they are kept in a bounded buffer and retried, and a down dashboard never slows a backup.
- **Storage** (`storage:` in `config.yaml`): free and used space of the volume holding a local repository (every 15 minutes), the repository's size (every 12 hours; `restic stats` can be slow on big repositories), and the size of each job's latest snapshot after each backup (`source_size: false` turns it off).

Dashboard problems are logged and never change a backup's result.

## Restoring and finding files

```bash
.venv/bin/python -m app snapshots --job docs
.venv/bin/python -m app ls SNAPSHOT_ID /home/jim/docs
.venv/bin/python -m app restore SNAPSHOT_ID /tmp/restore --path /home/jim/docs/report.txt
.venv/bin/python -m app find report
```

## Configuration

`config/config.yaml` (from `config/config.example.yaml`) holds the settings shared by all jobs; `config/jobs/*.yaml` can override retention, restic options, `repo_path` and `verify_after_backup` per job. Every key is documented in the example files. Secrets live only in `.env`: `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE`, `JABS_SMTP_USER` and `JABS_SMTP_PASS`, `JABS_DASHBOARD_URL`, `JABS_AGENT_KEY`, `UPTIME_KUMA_URL`, `RESTIC_BIN`, `ENV_MODE`.

Blank values in YAML (`retention:`) mean "use the default".

An optional Uptime Kuma push monitor gets an `up` or `down` ping after each run (not on dry runs).

## Development

```bash
./setup.sh --dev
.venv/bin/python -m pytest -q
```

The tests that use a real restic repository are skipped when restic is not installed. See [AGENTS.md](AGENTS.md) for the layout and conventions.

## License

MIT, see [LICENSE.md](LICENSE.md).
