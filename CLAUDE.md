# ogsync — 作業メモ（Claude 向け）

会社の Outlook と個人の Google カレンダー（primary）を双方向同期するツール。使い方は README.md、
OWA の癖と設計判断の理由は docs/owa-notes.md にある。OWA まわりを触る前に必ず owa-notes を読むこと。

## 前提

- 会社の Microsoft 365 は外部ツールに OAuth を発行できない。そのため Playwright で OWA を開き、
  OWA 自身と同じ `/owa/service.svc` の通信を送って読み書きしている（Graph API は使えない）。
- 同期対象は **Outlook の既定の予定表** と **Google の primary** だけ。
  Outlook には別の予定表（以前エクスポートで追加したもの）もあるが、対象外。
- cron が 15 分ごとに `.cron/run-sync.sh` を実行している（本番稼働中）。失敗は Slack に通知される。

## 守ること

- **会社の Outlook や Google に書き込む操作は、実行前にユーザーに確認する。**
  対象: `sync`（`--dry-run` なし）、`test-write`、`purge`（`--yes` を含む）、`login`、`record`。
  確認なしでよいのは読み取りだけ: `sync --dry-run`、`created`、`fetch`、`parse`、`pytest`。
- 削除してよいのは ogsync の印が付いた予定だけ（Google: `extendedProperties.private.ogsync_source=outlook`、
  Outlook: 分類「ogsync」）。手動で登録された予定を消すコードを書かない。
- `dumps/` と `cron.log` には会社の予定（件名・社外の取引先名など）が入っている。
  調べるときは件数・日時・キーの構造など必要な情報だけを出し、件名はユーザーに求められたときだけ表示する。
  認証ヘッダーやトークンの値は出力しない。
- `~/.config/ogsync/` の秘密情報（Google トークン、Slack Webhook URL）を表示・コミットしない。
- ユーザーへの返答は日本語で書く。

## 開発

```sh
uv run pytest                     # テスト（ネットワーク不要）
uvx ruff check src tests --select F
# cron と同じ最小環境で、書き込みなしで動作確認する
env -i HOME=$HOME LOGNAME=$LOGNAME PATH=/usr/bin:/bin SHELL=/bin/sh .cron/run-sync.sh --dry-run
```

- 手動実行と cron が重なるとブラウザのプロファイルを取り合う。手動で試すときは `.cron/run-sync.sh`
  経由にすると `flock` で安全にスキップされる。
- 終了コード: 0 成功 / 1 その他のエラー / 3 OWA のセッション切れ（`.cron/run-sync.sh` が通知の判定に使う）。
- エディタの Pyright が `playwright` などを解決できないと警告するのは、`.venv` を見ていないだけ。

## コードの地図

| ファイル | 役割 |
|---|---|
| `src/ogsync/owa.py` | OWA への接続（`connect`）、予定表の読み取り（`read_calendar`）、応答の解析、`record` |
| `src/ogsync/owa_api.py` | `service.svc` への通信（読み取り・作成・削除）。送る内容は記録した手動操作と同じ形式 |
| `src/ogsync/google_cal.py` | Google Calendar API（primary の読み書き、認可） |
| `src/ogsync/sync.py` | 両方向の差分計算（純粋関数）と書き込み。ループ防止と安全策もここ |
| `src/ogsync/cli.py` | コマンド |
| `.cron/` | cron 用スクリプト（実行・Slack 通知）と logrotate 設定 |
