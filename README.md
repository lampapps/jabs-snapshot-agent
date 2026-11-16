# JABS Snapshot Agent

A JABS (Just Another Backup Script) agent that runs [restic](https://restic.net/) backups on a schedule, emails failures, and can report to a JABS dashboard.

- **Command line and config files only.** There is no GUI on the agent; everything you watch lives on the dashboard.
- **Runs standalone.** Without a dashboard it backs up, prunes, checks and emails exactly the same. The dashboard is optional.
- **Scheduled by a systemd timer**, not cron. A timer runs `python -m app tick` every 5 minutes; the tick decides which jobs are due.

## Requirements

- Python 3.11 or newer with `python3-venv`
- [restic](https://restic.readthedocs.io/en/stable/020_installation.html)
- systemd, for the timer (without it, run `python -m app tick` from anything that can run a command every few minutes)
- Optional: a JABS dashboard (`dashboardv2`) to report to; storage reporting needs its `/api/v2/storage` endpoint

## Install

Do these steps as the user that will own the install, in the install directory.

1. **Install.**

   ```bash
   ./setup.sh
   ```

   It creates the virtual environment, installs the dependencies, copies the example `config/config.yaml`, `.env` and an example job (never overwriting existing files), and copies the old `config/global.yaml` to `config/config.yaml` once if it finds one. If `python3` is older than 3.11, choose another interpreter: `PYTHON=python3.11 ./setup.sh` (needs `python3.11-venv`). Do not run it with `sudo`.

2. **Open the admin console.** The next steps use it; the [Admin console](#admin-console) section lists every tab.

   ```bash
   .venv/bin/python -m app admin
   ```

3. **Configure** (tab `3` Files). Select `.env` and press `e` to edit it in your system editor: set `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE` and, if you want them, the SMTP login, `JABS_DASHBOARD_URL` and `JABS_AGENT_KEY` (register the agent on the dashboard host with `.venv/bin/python -m app agent add NAME`). Do the same for `config.yaml`: `repo_path`, retention and email. The files document themselves, so leave what you do not need. The console checks each file when you close the editor and offers to revert a mistake.

4. **Add your jobs** (tab `1` Jobs). Press `n` to create one from the template and open it in the editor, or `e` to edit the example job. Set the paths to back up and enable a schedule.

5. **Create the repository** (tab `2` Repository): press `i`.

6. **Test email** (tab `4` Diagnostics): press `m` to send a test message. With a dashboard configured, `h` sends one heartbeat to test the link.

7. **Try a job** (tab `1` Jobs): select it and press `d` for a dry run. restic's output streams in a window.

8. **Install the timer** (quit the console first, or use another terminal):

   ```bash
   sudo deploy/install.sh
   ```

9. **Check it.** In the console, tab `5` Service shows the service and timer and `l` follows the journal; tab `4` Diagnostics `t` runs a tick now.

Every step can also be done on the command line (`.venv/bin/python -m app config`, `init-repo`, `run JOB --dry-run`; see [Commands](#commands)), and `.venv/bin/python -m app config` checks everything at once.

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

## Configuration

`config/config.yaml` (from `config/config.example.yaml`) holds the settings shared by all jobs; `config/jobs/*.yaml` can override retention, restic options, `repo_path` and `verify_after_backup` per job. `config/config.example.yaml` documents every global key, and `config/templates/job.yaml` lists every job setting with comments. Secrets live only in `.env`: `RESTIC_PASSWORD` or `RESTIC_PASSWORD_FILE`, cloud repository credentials (see `.env.example`), `JABS_SMTP_USERNAME` and `JABS_SMTP_PASSWORD`, `JABS_DASHBOARD_URL`, `JABS_AGENT_KEY`, `UPTIME_KUMA_URL`, `RESTIC_BIN`, `ENV_MODE`.

Blank values in YAML (`retention:`) mean "use the default".

An optional Uptime Kuma push monitor gets an `up` or `down` ping after each run (not on dry runs).

## Usage

### Scheduling

Each job lists schedules as 5-field cron expressions (`schedules: [{cron: "0 3 * * *", enabled: true}]`). They are evaluated by the agent, and no cron daemon is involved.

- The agent remembers, per job and expression, the latest scheduled time it has served. A newer slot is due.
- **A missed run happens once at the next tick**, for example after the machine was off or while another backup was still running. Several missed slots still mean one run.
- A job seen for the first time only gets a baseline, so installing never starts a surprise backup.
- A failed backup is not retried until its next slot.
- Several schedules of one job that are due together run it once.
- Jobs run one after another within a tick, and a job already running (a manual `run`) is skipped.

The tick every 5 minutes means a job starts up to 5 minutes after its scheduled time. For tighter timing change `OnCalendar` in `deploy/agent.timer` (for example `minutely`) and run `deploy/install.sh` again.

### Commands

Run as `.venv/bin/python -m app <command>`.

| Command | Purpose |
| --- | --- |
| `config` | Validate `config.yaml` and the job files; shows whether the dashboard is in use |
| `admin [--unit NAME]` | Open the interactive [admin console](#admin-console) |
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

### Admin console

`.venv/bin/python -m app admin` opens a full-screen console for the person administering the agent: everything the commands above do, plus running jobs. Switch tabs with `1` to `6` or the mouse; the footer lists the keys for the current tab; `r` refreshes and `q` quits. It starts even when `config.yaml` is broken, so you can fix it there.

| Tab | What you can do |
| --- | --- |
| 1 Jobs | Each job's schedule, next run, last result and whether it is running now. **Dry run** (`d`) or **Back up now** (`b`) runs the job in a window that streams restic's output, with a Stop button (interrupts so the run cleans up). Options: create the repository first, prune afterwards. Edit a job file (`e`), create one from the template (`n`) or delete one (`x`: you type its name to confirm; its snapshots stay in the repository) |
| 2 Repository | Snapshots (filter by job), list a snapshot's files, restore a snapshot or one path, find a file across all snapshots, check the repository (quick, sample or deep) and create it |
| 3 Files | Check `config.yaml`, `.env`, `common_exclude.yaml` and every job file, and edit any of them in your system editor (`$VISUAL`, `$EDITOR`, else the system default such as nano). A file is validated when you leave the editor: edit again, revert or keep. The agent reads its files at the start of every run, so a valid edit applies at the next tick; if the directories the sandboxed service may write change, it offers to reinstall the units |
| 4 Diagnostics | Run the tick now, send a test mail, measure storage, test the dashboard link (sends one heartbeat), show the schedule state, list the writable directories, reset the schedule state (typed confirmation) |
| 5 Service | State of the service and timer; start (runs a tick now), stop, enable or disable the timer, reinstall the units with the options already in use (all through `sudo`), follow the journal |
| 6 Docs | Read the install's Markdown files; links between documents work and `b` goes back |

A run started from the console is the same `python -m app run JOB` the timer starts, so it takes the same job lock (a job that is already running is refused), reports to the dashboard and emails on failure. A dry run is still reported to the dashboard, marked `[DRY RUN]`. Closing the console stops a run it started.

Run it as the user that owns the install; it refuses to start as root when the install belongs to someone else, because root-owned state files would stop the service from writing its own data. Units are found by their working directory; if that fails, pass `--unit jabs-snapshot-agent`. Administrative actions are recorded in the journal as `jabs-admin`; passwords are never logged. It needs the `textual` package, installed by `./setup.sh`.

### Restoring and finding files

```bash
.venv/bin/python -m app snapshots --job docs
.venv/bin/python -m app ls SNAPSHOT_ID /home/jim/docs
.venv/bin/python -m app restore SNAPSHOT_ID /tmp/restore --path /home/jim/docs/report.txt
.venv/bin/python -m app find report
```

## How it works

- **One restic repository per machine.** Jobs on the machine share it and are told apart by restic tags, so retention (`forget --prune`) and restores can be scoped to one job.
- **No database.** Restic's snapshot index is the source of truth. The agent keeps a lock file per job and a small `data/state.json` (which schedule slots were served, each job's last result, when storage was last reported).
- **restic must be installed** and on `PATH` (or set `RESTIC_BIN` in `.env`). The agent never downloads it.
- **Local or remote repositories.** Anything restic accepts as `repo_path` works, including S3, B2, SFTP, REST servers and rclone remotes; the address forms are in `config/templates/job.yaml` and the credentials go in `.env`. The agent passes `repo_path` and the environment straight to restic, so free-space reporting and the service's write access (`deploy/install.sh`) apply to local paths only.

## Dashboard integration (optional)

Set `JABS_DASHBOARD_URL` and `JABS_AGENT_KEY` in `.env` (register the agent on the dashboard host with `.venv/bin/python -m app agent add NAME`). Leave them empty, or set `dashboard.enabled: false` in `config.yaml`, to run standalone; `.venv/bin/python -m app config` says which mode is active.

What the dashboard receives:

- **Events** for every run (start, stages, progress about every 5 seconds, completion, check result, snapshots removed by pruning) and a heartbeat with each job's schedule on every tick. Every request carries the machine's **hostname** (`hostname:` in `config.yaml`, else the system name) so jobs on different machines with the same name stay apart.
- **Storage** (`storage:` in `config.yaml`): free and used space of the volume holding a local repository (every 15 minutes), the repository's size (every 12 hours; `restic stats` can be slow on big repositories), and the size of each job's latest snapshot after each backup (`source_size: false` turns it off).

Dashboard problems are logged and never change a backup's result.

## Logs and troubleshooting

The agent logs to stderr, which systemd captures in the journal. It writes no log files and sends none to the dashboard, so read them on the machine the agent runs on. The dashboard's job page shows each run's events (click a run's date), but not log lines.

```bash
journalctl -u jabs-snapshot-agent -f                  # follow live
journalctl -u jabs-snapshot-agent -n 200 --no-pager   # last 200 lines
journalctl -u jabs-snapshot-agent --since today       # today only
journalctl -u jabs-snapshot-agent -p warning          # warnings and errors only
journalctl -u jabs-snapshot-agent -o cat > agent.log  # plain text for sharing
```

- Use the unit name you gave `--name` if it is not `jabs-snapshot-agent`.
- The timer starts a short-lived service every 5 minutes, so each run is a separate batch of lines. `systemctl list-timers jabs-snapshot-agent.timer` shows when the next one starts.
- A non-root user may need to be in the `systemd-journal` group (or use `sudo`) to read the journal.
- Run by hand (`.venv/bin/python -m app tick` or `run JOB`), the lines print to your terminal instead.
- Set `ENV_MODE=development` in `.env` for DEBUG level (the default is INFO). It is read at the start of every run, so no restart is needed.
- The journal's size and retention are set by systemd (`journald.conf`), not by the agent. Passwords and the repository secret are never logged.

## Development

```bash
./setup.sh --dev
.venv/bin/python -m pytest -q
```

The tests that use a real restic repository are skipped when restic is not installed. See [AGENTS.md](AGENTS.md) for the layout and conventions.

## License

MIT, see [LICENSE.md](LICENSE.md).
