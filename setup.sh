#!/usr/bin/env bash
# First-time setup: venv, dependencies, config files from the examples.
# Safe to re-run; never overwrites existing files.
# Usage: ./setup.sh [--dev]     (--dev also installs the test dependencies)
#        PYTHON=python3.11 ./setup.sh   (use another interpreter than python3)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
DEV=0
[[ ${1:-} == "--dev" ]] && DEV=1

die() { echo "error: $*" >&2; exit 1; }

PYTHON=${PYTHON:-python3}
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON was not found"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required (found $("$PYTHON" --version 2>&1); set PYTHON=python3.11 to choose another)"
"$PYTHON" -c 'import ensurepip, venv' 2>/dev/null \
    || die "the venv module is missing for $PYTHON (sudo apt install ${PYTHON}-venv)"
command -v restic >/dev/null 2>&1 || [[ -n ${RESTIC_BIN:-} ]] \
    || echo "warning: restic was not found on PATH; install it or set RESTIC_BIN in .env" >&2

[[ -d .venv ]] || "$PYTHON" -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
if ((DEV)); then
    .venv/bin/pip install --quiet -r requirements-dev.txt
fi

copy_if_missing() {  # copy_if_missing SRC DEST
    if [[ ! -e $2 ]]; then
        cp "$1" "$2"
        echo "created $2 from $1"
    fi
}
if [[ ! -e config/config.yaml && -e config/global.yaml ]]; then
    cp config/global.yaml config/config.yaml
    echo "created config/config.yaml from the old config/global.yaml"
fi
copy_if_missing config/config.example.yaml config/config.yaml
copy_if_missing .env.example .env
chmod 600 .env

mkdir -p config/jobs data
if [[ -z $(ls -A config/jobs 2>/dev/null) ]]; then
    copy_if_missing config/templates/job.yaml config/jobs/example.yaml
fi

# Job files from before job ids (0.6.0), and the example copied above, get their id here.
.venv/bin/python -m app job adopt || true

.venv/bin/python -m app config

cat <<EOT

Next steps:
  1. Edit config/config.yaml (repo_path, retention, email) and .env (restic password, optional dashboard URL and key).
  2. Add jobs:               .venv/bin/python -m app job new NAME   (then edit config/jobs/NAME.yaml and enable a schedule)
  3. Create the repository:  .venv/bin/python -m app init-repo
  4. Try a job:              .venv/bin/python -m app run JOB --dry-run
  5. Install the timer:      sudo deploy/install.sh        (add --read-all to back up files you do not own)
EOT
