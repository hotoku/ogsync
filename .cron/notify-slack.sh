#!/bin/bash
# Post a message to Slack: .cron/notify-slack.sh "text"
# The Incoming Webhook URL is read from ~/.config/ogsync/slack_webhook_url (kept
# out of the repository, since anyone with the URL can post to the channel).
set -u
url_file="$HOME/.config/ogsync/slack_webhook_url"
if [ ! -r "$url_file" ]; then
    echo "notify-slack: $url_file がないため通知できませんでした" >&2
    exit 1
fi
# Escape for a JSON string: backslashes, quotes, then newlines, tabs and CRs
# (error output may contain any of them).
text=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
text=${text//$'\n'/\\n}
text=${text//$'\t'/\\t}
text=${text//$'\r'/\\r}
curl -sS -m 10 -X POST -H 'Content-type: application/json' \
    --data "{\"text\":\"$text\"}" "$(cat "$url_file")" >/dev/null
