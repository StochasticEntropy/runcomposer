#!/bin/sh
# sync.sh — the round trip of the remote-agent kit (DESIGN.md §6.2c):
# compose here → carry the spec there → execute → carry the bundle back →
# ingest here. Five steps, one seam: the transport functions below.
#
#   ./sync.sh                        # the whole loop over the local transport
#   RC_FILTER='Checkout' ./sync.sh   # compose something else
#
# Environment (all optional):
#   RC_CONFIG       composer config (default: config.yaml next to this script)
#   RC_STATE        composer state root (default: state/ next to RC_CONFIG)
#   RC_INBOX        file-drop inbox — must match core.ingestion.inbox
#   RC_REMOTE       stands in for the executing machine's filesystem
#   RC_SUITE_ROOT   the EXECUTING machine's test checkout
#   RC_EXEC_SOURCE  the runcomposer_exec.py to vendor into the payload
#   RC_TRANSPORT    local (default) — see transport_send/transport_receive
#   RC_FILTER       tag filter to compose (default: Payments)
#   RC_TITLE        run title
#   RC_ALLOW_DRIFT  1 = the agent executes the intersection instead of refusing
#   RC_PYTHON       interpreter on the executing machine (default: python3)
#   RUNCOMPOSER     the runcomposer command (default: runcomposer)
#
# No host, user, or path of anybody's infrastructure appears in this file: the
# default transport is a directory copy so the example runs on one machine, in
# CI, and in the test suite.
set -eu

kit_dir=$(cd "$(dirname "$0")" && pwd)
: "${RC_CONFIG:=$kit_dir/config.yaml}"
config_dir=$(cd "$(dirname "$RC_CONFIG")" && pwd)
: "${RC_STATE:=$config_dir/state}"
: "${RC_INBOX:=$RC_STATE/inbox}"
: "${RC_REMOTE:=$RC_STATE/remote}"
: "${RC_SUITE_ROOT:=$kit_dir/../robot-shop/tests}"
: "${RC_EXEC_SOURCE:=$kit_dir/../../src/runcomposer_exec.py}"
: "${RC_TRANSPORT:=local}"
: "${RC_FILTER:=Payments}"
: "${RC_TITLE:=Remote agent round trip}"
: "${RC_ALLOW_DRIFT:=0}"
: "${RC_PYTHON:=python3}"
: "${RUNCOMPOSER:=runcomposer}"

# No `cd` here on purpose. runcomposer resolves every relative path in a config
# file against that file's own directory (DESIGN.md §8), so a config of
# relative paths keeps all state in one place — `state/` next to config.yaml —
# no matter which directory this script is invoked from.

say() { printf '\n=== %s\n' "$*"; }

# -- the transport seam -------------------------------------------------------
# These three functions are the ONLY place the transport appears. Replace their
# bodies and nothing else in the kit changes — that is what "the interface is a
# document" buys. Uncomment the variant you use and set your own destination;
# none is filled in here, because a shipped example has no business knowing
# anybody's hosts.

transport_send() { # $1 = local payload dir, $2 = destination on the other side
    case "$RC_TRANSPORT" in
        local) mkdir -p "$2" && cp -R "$1"/. "$2"/ ;;
        # rsync) rsync -a --delete "$1"/ "$RC_REMOTE_HOST:$2/" ;;
        # scp)   ssh "$RC_REMOTE_HOST" "mkdir -p '$2'" && scp -qr "$1"/. "$RC_REMOTE_HOST:$2/" ;;
        # git)   cp -R "$1"/. "$2"/ && git -C "$2" add -A && git -C "$2" commit -qm "runspec" && git -C "$2" push -q ;;
        *) echo "sync: unknown RC_TRANSPORT '$RC_TRANSPORT'" >&2; exit 2 ;;
    esac
}

transport_receive() { # $1 = bundle on the other side, $2 = local destination
    case "$RC_TRANSPORT" in
        local) rm -rf "$2" && mkdir -p "$2" && cp -R "$1"/. "$2"/ ;;
        # rsync) rsync -a "$RC_REMOTE_HOST:$1/" "$2"/ ;;
        # scp)   mkdir -p "$2" && scp -qr "$RC_REMOTE_HOST:$1/." "$2"/ ;;
        # git)   git -C "$2" pull -q ;;
        *) echo "sync: unknown RC_TRANSPORT '$RC_TRANSPORT'" >&2; exit 2 ;;
    esac
}

remote_run() { # $1 = the command line to run on the executing machine
    case "$RC_TRANSPORT" in
        local) sh -c "$1" ;;
        # rsync|scp) ssh "$RC_REMOTE_HOST" "$1" ;;
        # git) : ;;   # nothing to trigger: that side runs on its own schedule
        #             #  and the next `sync.sh pull` finds the bundle waiting
        *) echo "sync: unknown RC_TRANSPORT '$RC_TRANSPORT'" >&2; exit 2 ;;
    esac
}

# -- 1/5 compose ---------------------------------------------------------------
# The spec is exported, not dispatched: no runner drives this execution. The
# export mints a dispatch (§4) and records the hash of the exact bytes handed
# out, which is what the returned marker is verified against (§5).

say "1/5 compose — build the run spec and mint an export dispatch"
payload=$RC_STATE/outbox
rm -rf "$payload"
mkdir -p "$payload" "$RC_INBOX"
report=$("$RUNCOMPOSER" spec "$RC_FILTER" \
    --title "$RC_TITLE" \
    --label origin=remote-agent \
    --format json -o "$payload/spec.json" \
    --expect-format robot-output-xml \
    --export --config "$RC_CONFIG" 2>&1 >/dev/null)
printf '%s\n' "$report"
run_id=$(printf '%s\n' "$report" | sed -n 's/^run: \([^ ]*\).*/\1/p')
dispatch_id=$(printf '%s\n' "$report" | sed -n 's/^export dispatch: \([^ ]*\).*/\1/p')
[ -n "$run_id" ] || { echo "sync: could not read the run id from the compose report" >&2; exit 1; }

# -- 2/5 push ------------------------------------------------------------------
# What travels out: the spec document, the agent, and the single-file consumer.
# Copying runcomposer_exec.py IS the vendoring step of §6.2c — in a real setup
# it is a release download that lands on the executing machine once, not per
# run. The tests themselves are already over there (RC_SUITE_ROOT); code
# delivery is not this loop's job.

say "2/5 push — spec + agent travel to the executing machine ($RC_TRANSPORT)"
cp "$RC_EXEC_SOURCE" "$payload/runcomposer_exec.py"
cp "$kit_dir/agent/run_agent.sh" "$kit_dir/agent/robot_command.py" "$payload/"
chmod +x "$payload/run_agent.sh"
remote_dir=$RC_REMOTE/$run_id
transport_send "$payload" "$remote_dir"
ls "$remote_dir"

# -- 3/5 execute ---------------------------------------------------------------
# Over there: python3 + robot, no runcomposer. A drift refusal (§3.3) fails
# here, so nothing travels back and the run stays AWAITING_RESULTS.

say "3/5 execute — the agent fulfills the spec with python3 + robot only"
remote_run "RC_SUITE_ROOT='$RC_SUITE_ROOT' RC_PYTHON='$RC_PYTHON' \
RC_DISPATCH='$dispatch_id' RC_SHARD=1 RC_ALLOW_DRIFT='$RC_ALLOW_DRIFT' \
sh '$remote_dir/run_agent.sh' '$remote_dir/spec.json' '$remote_dir/results'"

# -- 4/5 pull ------------------------------------------------------------------
# What travels back: the native output.xml plus the runcomposer_run.json
# marker. Dropping it in the inbox is the whole file-drop transport (§5).

say "4/5 pull — the results bundle returns into the file-drop inbox"
transport_receive "$remote_dir/results" "$RC_INBOX/$run_id"
ls "$RC_INBOX/$run_id"

# -- 5/5 ingest ----------------------------------------------------------------
# Explicit here so the loop is one script. With `runcomposer serve` running,
# the inbox watcher ingests the same drop by itself and this call is only a
# byte-identical re-delivery — a no-op by §5's idempotency rule.

say "5/5 ingest — marker-correlated and idempotent (DESIGN.md §5)"
"$RUNCOMPOSER" ingest "$RC_INBOX/$run_id" --config "$RC_CONFIG"
"$RUNCOMPOSER" runs --config "$RC_CONFIG" --limit 5
