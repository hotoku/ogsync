#!/bin/bash
# Run by cron every 15 minutes (see README). Extra arguments go to `ogsync sync`,
# e.g. `.cron/run-sync.sh --dry-run` to try it by hand.
set -u
cd "$(dirname "$0")/.."

# Skip if the previous run is still going; two runs would share one browser profile.
exec 9>.cron/sync.lock
if ! flock -n 9; then
    echo "=== $(date '+%F %T') previous run still in progress; skipped"
    exit 0
fi

out=$(mktemp)
trap 'rm -f "$out"' EXIT

echo "=== $(date '+%F %T') start"
# Keep a copy of the output for the notification; the log still gets all of it.
"$HOME/.local/bin/uv" run ogsync sync --headless "$@" 2>&1 | tee "$out"
status=${PIPESTATUS[0]}
echo "=== $(date '+%F %T') end (exit $status)"

# Notify when a failure starts or changes, and once when syncing works again,
# instead of on every 15-minute run. The state file holds the current failure:
# "session" for an expired OWA login (exit 3), otherwise a hash of the last
# output line, which carries the error message.
state=.cron/failure
previous=$(cat "$state" 2>/dev/null || true)
if [ "$status" -eq 0 ]; then
    if [ -n "$previous" ]; then
        .cron/notify-slack.sh ":white_check_mark: ogsync: 同期が再開しました。" && rm -f "$state"
    fi
elif [ "$status" -eq 3 ]; then
    if [ "$previous" != "session" ]; then
        msg=$(printf ':warning: ogsync: OWA のセッションが切れたため同期が止まっています。次のコマンドで再ログインしてください。\n```\ncd %s && uv run ogsync login\n```' "$PWD")
        .cron/notify-slack.sh "$msg" && echo session > "$state"
    fi
else
    current="error:$(tail -n 1 "$out" | md5sum | cut -c1-12)"
    if [ "$previous" != "$current" ]; then
        msg=$(printf ':x: ogsync: 同期でエラーが発生しました（exit %s）。\n```\n%s\n```\nログ: %s/cron.log' \
            "$status" "$(tail -n 15 "$out")" "$PWD")
        .cron/notify-slack.sh "$msg" && echo "$current" > "$state"
    fi
fi
exit "$status"
