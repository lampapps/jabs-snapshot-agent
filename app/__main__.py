"""Command line: python -m app <command>."""

import argparse
import logging
import sys
from typing import Optional

from . import config, mailer, ops, runner, search, settings, storage, tick
from .config import Config, ConfigError
from .dashboard import Client
from .restic import ResticError

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


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
    repo = ops.repo_path(cfg)
    if not ops.init_repository(cfg):
        print(f"Repository already exists: {repo}")
        return 0
    print(f"Initialized new restic repository: {repo}")
    return 0


def _cmd_check(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    print(f"Running {ops.check_label(args.deep, args.subset)} against {ops.repo_path(cfg)} ...")
    try:
        ops.check_repository(cfg, args.deep, args.subset)
    except ResticError as exc:
        print(f"Repository check FAILED: {exc}", file=sys.stderr)
        return 1
    print("Repository check passed.")
    return 0


def _cmd_snapshots(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    snapshots = ops.snapshot_list(cfg, args.job)
    if not snapshots:
        print("no snapshots")
    for snap in snapshots:
        print("  ".join(ops.snapshot_fields(snap)))
    return 0


def _cmd_ls(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    search.page_output(ops.snapshot_files(cfg, args.snapshot, args.path))
    return 0


def _cmd_restore(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    ops.repo_path(cfg), ops.credentials()
    print(f"Restoring {args.snapshot}{' ' + args.path if args.path else ''} to {args.target} ...")
    ops.restore_snapshot(cfg, args.snapshot, args.target, args.path)
    print("Restore finished.")
    return 0


def _cmd_find(args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    ops.repo_path(cfg), ops.credentials()
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
    print(f"Searching repo: {cfg.repo_path}")
    paths = ops.find_matches(cfg, query)
    if not paths:
        print(f"No matches found for '{query}'.")
        return 0
    print(f"{len(paths)} match(es) for '{query}':\n")
    search.page_output(paths)
    return 0


def _cmd_jobs(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    rows, errors = ops.jobs_overview(cfg)
    for row in rows:
        nxt = row["next"]
        print(f"{row['job'].name}\tnext {nxt.strftime('%Y-%m-%d %H:%M') if nxt else '-'}\tlast {ops.format_last(row['last'])}")
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
    samples = ops.storage_samples(cfg)
    print(storage.describe(samples))
    if args.print or not client.enabled:
        return 0
    return 0 if client.post_storage(samples) else 1


def _cmd_paths(_args: argparse.Namespace, cfg: Config, _client: Client) -> int:
    print("\n".join(ops.writable_paths(cfg)))
    return 0


def _cmd_reset(args: argparse.Namespace, _cfg: Config, _client: Client) -> int:
    if not args.yes:
        print("reset deletes lock files and the schedule state; re-run with --yes to confirm", file=sys.stderr)
        return 2
    removed = ops.reset_state()
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
    add("admin", None, "open the interactive administration console").add_argument(
        "--unit", help="systemd unit name of this install, if it cannot be detected (e.g. jabs-snapshot-agent)")
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
    if args.command == "admin":  # must start even when config.yaml is broken: that is when it is needed
        from . import admin

        return admin.run(args.unit)
    try:
        cfg = config.load_config()
        client = Client.from_env(cfg)
        return args.func(args, cfg, client)
    except (ConfigError, ResticError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
