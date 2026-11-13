"""Command line: python -m app <command>."""

import argparse
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, mailer, restic, runner, schedule, search, settings, state, storage, tick
from .config import Config, ConfigError
from .dashboard import Client
from .restic import ResticError

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def _credentials() -> tuple:
    password, password_file = settings.restic_password()
    if not password and not password_file:
        raise ConfigError("neither RESTIC_PASSWORD nor RESTIC_PASSWORD_FILE is set (put one in .env)")
    return password, password_file


def _repo(cfg: Config) -> str:
    if not cfg.repo_path:
        raise ConfigError(f"no repo_path in {settings.CONFIG_PATH}")
    return cfg.repo_path


def _cmd_config(_args: argparse.Namespace, cfg: Config, client: Client) -> int:
    jobs, errors = config.load_jobs(cfg)
    print(f"{settings.CONFIG_PATH}: OK")
    print(f"repo: {cfg.repo_path or '(not set)'}")
    print(f"dashboard: {client.describe()}")
    print(f"{settings.JOBS_DIR}: {len(jobs)} job(s)")
    for job in jobs:
        crons = ", ".join(job.enabled_crons()) or "no schedule enabled"
        print(f"  {job.name}: {crons}")
    for message in errors:
        print(message, file=sys.stderr)
    return 1 if errors else 0


def _cmd_tick(_args: argparse.Namespace, cfg: Config, client: Client) -> int:
    tick.run_once(cfg, client)
    return 0


def _cmd_run(args: argparse.Namespace, cfg: Config, client: Client) -> int:
    job = config.find_job(args.job, cfg)
    ok = runner.run_job(job, cfg, client, do_init=args.init, do_prune=args.prune, dry_run=args.dry_run)
    return 0 if ok else 1


def _cmd_init_repo(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    repo, (password, password_file) = _repo(cfg), _credentials()
    global_args = restic.global_args_from(cfg.restic_options)
    if restic.repo_exists(repo, password, password_file, global_args=global_args):
        print(f"Repository already exists: {repo}")
        return 0
    print(f"Initializing new restic repository: {repo}")
    restic.init_repo(repo, password, password_file, global_args=global_args)
    print("Repository initialized.")
    return 0


def _cmd_check(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    repo, (password, password_file) = _repo(cfg), _credentials()
    deep = args.deep or bool(args.subset)
    label = (f"partial data check (subset={args.subset})" if args.subset
             else "full data check" if deep else "quick check (metadata only)")
    print(f"Running {label} against {repo} ...")
    try:
        restic.check(repo, password, password_file, read_data=deep and not args.subset,
                     read_data_subset=args.subset, global_args=restic.global_args_from(cfg.restic_options))
    except ResticError as exc:
        print(f"Repository check FAILED: {exc}", file=sys.stderr)
        return 1
    print("Repository check passed.")
    return 0


def _cmd_snapshots(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    repo, (password, password_file) = _repo(cfg), _credentials()
    tags = config.find_job(args.job, cfg).tags if args.job else None
    snapshots = restic.list_snapshots(repo, tags=tags, password=password, password_file=password_file,
                                      global_args=restic.global_args_from(cfg.restic_options))
    if not snapshots:
        print("no snapshots")
    for snap in snapshots:
        stamp = (snap.get("time") or "")[:19].replace("T", " ")
        print(f"{snap.get('short_id', snap.get('id', '')[:8])}  {stamp}  {snap.get('hostname', '')}  "
              f"{','.join(snap.get('tags') or []) or '-'}  {' '.join(snap.get('paths') or [])}")
    return 0


def _cmd_ls(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    repo, (password, password_file) = _repo(cfg), _credentials()
    entries = restic.list_files(repo, args.snapshot, path=args.path, password=password, password_file=password_file,
                                global_args=restic.global_args_from(cfg.restic_options))
    search.page_output([entry["path"] for entry in entries if entry.get("path")])
    return 0


def _cmd_restore(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    repo, (password, password_file) = _repo(cfg), _credentials()
    print(f"Restoring {args.snapshot}{' ' + args.path if args.path else ''} to {args.target} ...")
    restic.restore(repo, args.snapshot, args.target, include_path=args.path, password=password,
                   password_file=password_file, global_args=restic.global_args_from(cfg.restic_options))
    print("Restore finished.")
    return 0


def _cmd_find(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    repo, (password, password_file) = _repo(cfg), _credentials()
    query = args.query
    if not query:
        try:
            query = input("Enter a filename or path fragment to search for: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
    if not query:
        print("No search term given.", file=sys.stderr)
        return 1
    print(f"Searching repo: {repo}")
    paths = search.find_paths(repo, query, password, password_file, restic.global_args_from(cfg.restic_options))
    if not paths:
        print(f"No matches found for '{query}'.")
        return 0
    print(f"{len(paths)} match(es) for '{query}':\n")
    search.page_output(paths)
    return 0


def _fmt_ts(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _cmd_jobs(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    jobs, errors = config.load_jobs(cfg)
    data = state.read()
    now = schedule.local_now()
    for job in jobs:
        nxt = schedule.next_run(job, now)
        last = data.get("jobs", {}).get(job.name, {}).get("last_run")
        outcome = "-" if not last else f"{'ok' if last.get('success') else 'FAILED'} {_fmt_ts(last.get('finished_at'))}"
        print(f"{job.name}\tnext {nxt.strftime('%Y-%m-%d %H:%M') if nxt else '-'}\tlast {outcome}")
    for message in errors:
        print(message, file=sys.stderr)
    return 1 if errors else 0


def _cmd_testmail(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    if not mailer.is_configured(cfg.email):
        print("email is not configured: set email.smtp_server and email.to_addrs in config.yaml", file=sys.stderr)
        return 1
    body = (f"This is a test message from the JABS snapshot agent on {settings.machine_hostname(cfg.hostname)}.\n"
            f"Server: {cfg.email.smtp_server}:{cfg.email.smtp_port}\n")
    if not mailer.send(cfg.email, "JABS snapshot agent test message", body):
        print("sending failed; see the error above", file=sys.stderr)
        return 1
    print(f"test message sent to {', '.join(cfg.email.to_addrs)}")
    return 0


def _cmd_report_storage(args: argparse.Namespace, cfg: Config, client: Client) -> int:
    jobs, _ = config.load_jobs(cfg)
    repo_paths = sorted({job.repo_path for job in jobs} or ({cfg.repo_path} if cfg.repo_path else set()))
    samples = storage.collect_scheduled(cfg, repo_paths, force=True)
    if cfg.storage.source_size:
        samples += [s for s in (storage.source_size_sample(cfg, job) for job in jobs) if s]
    print(storage.describe(samples))
    if args.print or not client.enabled:
        return 0
    return 0 if client.post_storage(samples) else 1


def _cmd_paths(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    """Directories the agent must be able to write; deploy/install.sh turns them into ReadWritePaths."""
    jobs, _ = config.load_jobs(cfg)
    wanted = {str(settings.DATA_DIR)}
    for repo in {job.repo_path for job in jobs} | ({cfg.repo_path} if cfg.repo_path else set()):
        if storage.is_local_repo(repo):
            wanted.add(repo)
    caches = {cfg.restic_options.cache_dir} | {job.restic_options.cache_dir for job in jobs}
    wanted |= {path for path in caches if path}
    if not any(job.restic_options.no_cache for job in jobs) and not cfg.restic_options.no_cache:
        wanted.add(str(Path.home() / ".cache" / "restic"))
    print("\n".join(sorted(wanted)))
    return 0


def _cmd_reset(args: argparse.Namespace, _cfg: Config, _client: Client) -> int:
    if not args.yes:
        print("reset deletes lock files and the schedule state; re-run with --yes to confirm", file=sys.stderr)
        return 2
    removed = 0
    for path in [*settings.LOCK_DIR.glob("*.lock"), *settings.STATE_PATH.parent.glob("state.*")]:
        path.unlink()
        removed += 1
    if settings.LOCK_DIR.is_dir() and not any(settings.LOCK_DIR.iterdir()):
        shutil.rmtree(settings.LOCK_DIR, ignore_errors=True)
    print(f"reset: removed {removed} file(s); the restic repository and your config are untouched")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="JABS snapshot agent (restic)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=func)
        return command

    add("config", _cmd_config, "validate config.yaml and the job files")
    add("tick", _cmd_tick, "run the periodic work once (driven by the systemd timer)")
    run = add("run", _cmd_run, "run one backup job now")
    run.add_argument("job", help="job name (config/jobs/NAME.yaml) or a path to a job file")
    run.add_argument("--init", action="store_true", help="create the repository first if it does not exist")
    run.add_argument("--prune", action="store_true", help="forget and prune old snapshots afterwards")
    run.add_argument("--dry-run", action="store_true", help="pass --dry-run to restic")
    add("init-repo", _cmd_init_repo, "create the restic repository if it does not exist")
    check = add("check", _cmd_check, "verify the repository (metadata only unless --deep or --subset)")
    check.add_argument("--deep", action="store_true", help="read all data back (slow)")
    check.add_argument("--subset", metavar="PERCENT_OR_SIZE", help="read only this part of the data, e.g. 5%% or 500M")
    snaps = add("snapshots", _cmd_snapshots, "list snapshots")
    snaps.add_argument("--job", help="only this job's snapshots")
    ls = add("ls", _cmd_ls, "list the files in a snapshot")
    ls.add_argument("snapshot")
    ls.add_argument("path", nargs="?")
    restore = add("restore", _cmd_restore, "restore a snapshot, or one path of it, into a directory")
    restore.add_argument("snapshot")
    restore.add_argument("target", help="directory to restore into")
    restore.add_argument("--path", help="restore only this path inside the snapshot")
    find = add("find", _cmd_find, "search all snapshots for a file by partial name")
    find.add_argument("query", nargs="?")
    add("jobs", _cmd_jobs, "list jobs with their next and last run")
    add("testmail", _cmd_testmail, "send a test message with the email settings")
    report = add("report-storage", _cmd_report_storage, "measure and report storage now")
    report.add_argument("--print", action="store_true", help="only print, do not send to the dashboard")
    add("paths", _cmd_paths, "print the directories the agent must be able to write")
    add("reset", _cmd_reset, "delete lock files and schedule state (never the repository)").add_argument(
        "--yes", action="store_true", help="confirm")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    settings.load_env()
    level = logging.DEBUG if settings.env_mode() == "development" else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger().setLevel(level)
    try:
        cfg = config.load_config()
        client = Client.from_env(cfg)
        return args.func(args, cfg, client)
    except (ConfigError, ResticError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
