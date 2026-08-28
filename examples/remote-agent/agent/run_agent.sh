#!/bin/sh
# The remote-side run agent (DESIGN.md §6.2c) — everything that has to exist
# on the executing machine, and nothing else. It needs `python3` and `robot`;
# it does NOT need runcomposer installed, which is the whole point of the
# vendored single-file consumer sitting next to this script.
#
#   ./run_agent.sh <spec.json> [<out_dir>]
#
# POSIX sh on purpose: a remote that only just clears "python3 + robot" is not
# a machine to make bash assumptions about.
#
# Environment:
#   RC_SUITE_ROOT   the executing machine's own test checkout (required)
#   RC_EXEC         vendored runcomposer_exec.py (default: next to this script)
#   RC_PYTHON       interpreter for both the consumer and the adapter (python3)
#   RC_DISPATCH     dispatch id to record in the marker (§4 identity layering)
#   RC_SHARD        shard label to record in the marker (§4/§5)
#   RC_ALLOW_DRIFT  1 = execute the intersection instead of refusing (§3.3);
#                   inherited straight through to robot_command.py
#
# On success the out dir is a complete results bundle: the native output.xml
# plus the runcomposer_run.json marker that carries correlation home. Whatever
# transport brought the spec here takes that directory back.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
spec=${1:?"usage: run_agent.sh <spec.json> [<out_dir>]"}
out_dir=${2:-$(dirname "$spec")/results}

python_bin=${RC_PYTHON:-python3}
exec_file=${RC_EXEC:-$here/runcomposer_exec.py}
suite_root=${RC_SUITE_ROOT:?"must name the test checkout on this machine"}

if [ ! -f "$exec_file" ]; then
    echo "run_agent: vendored consumer not found: $exec_file" >&2
    echo "run_agent: copy src/runcomposer_exec.py (or a release download) next to this script" >&2
    exit 2
fi

# Marker fields are optional; build the argument list without empty words.
set -- "$spec" --out "$out_dir"
if [ -n "${RC_DISPATCH:-}" ]; then set -- "$@" --dispatch "$RC_DISPATCH"; fi
if [ -n "${RC_SHARD:-}" ]; then set -- "$@" --shard "$RC_SHARD"; fi

# The consumer writes {ids_file} — exactly selection.materialized.item_ids,
# obligation 1 (§3.3) — and the adapter turns it into a robot invocation. The
# placeholders are quoted inside the template so paths with spaces survive the
# shell the consumer runs the command in.
"$python_bin" "$exec_file" "$@" \
    --command "'$python_bin' '$here/robot_command.py' '{ids_file}' '{out_dir}' '$suite_root'"
