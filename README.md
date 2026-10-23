# ogsync

会社の Outlook（OWA をブラウザ自動操作）と個人の Google カレンダー（primary）を双方向に同期するツール。
会社側は OAuth を外部ツールに発行できないため、Playwright で OWA を開き、OWA 自身と同じ通信を送って読み書きする。

| 方向 | 読む | 書く |
|---|---|---|
| Outlook → Google | Outlook の既定の予定表 | Google primary に「AD: BLOCKED」としてコピー |
| Google → Outlook | Google primary の自分の予定 | Outlook の既定の予定表に「BLOCKED」 |

cron で 15 分ごとに実行し、失敗したら Slack に通知する。

## セットアップ

```sh
uv sync
uv run playwright install chromium
```

### Outlook（OWA）

```sh
uv run ogsync login   # ブラウザが開くので手動でサインイン（MFA 含む）
```

- サインイン状態は `~/.local/share/ogsync/profile` に保存される（`--profile` で変更可）。
- WSL の Chromium が条件付きアクセスで拒否される場合は、Windows 側の Edge を
  `msedge.exe --remote-debugging-port=9222` で起動し、`--cdp http://localhost:9222` を付けて実行する。

### Google

1. Google Cloud Console でプロジェクトを作り、**Google Calendar API** を有効化
2. 「OAuth 同意画面」を作成（ユーザーの種類: 外部）し、公開ステータスを **本番環境** にする
   （「テスト」のままだと 7 日ごとに再認可が必要になる。未審査のままで自分だけ使う分には問題ない）
3. 「認証情報 → OAuth クライアント ID → デスクトップ アプリ」を作成し、JSON を
   `~/.config/ogsync/client_secret.json` に保存
4. `uv run ogsync google-auth` → 表示された URL をブラウザで開いて許可

必要な権限（スコープ）が変わったときは、実行時に再認可を求められるので `google-auth` をやり直す。

### Slack 通知

Incoming Webhook の URL を `~/.config/ogsync/slack_webhook_url` に保存する（リポジトリには入れない）。

```sh
printf '%s' 'https://hooks.slack.com/services/...' > ~/.config/ogsync/slack_webhook_url
chmod 600 ~/.config/ogsync/slack_webhook_url
.cron/notify-slack.sh "テスト"   # 送信を試す
```

## 使い方

```sh
uv run ogsync sync --dry-run   # 両方向の変更内容を表示するだけ
uv run ogsync sync             # 過去 7 日〜28 日先を同期（--past / --future で変更）
```

### ogsync が作った予定の確認・削除

```sh
uv run ogsync created              # Google のコピーと Outlook の BLOCKED を一覧表示（削除しない）
uv run ogsync purge                # 一覧を表示し、y と答えたら全て削除
uv run ogsync purge --side google  # Google 側だけ（outlook も指定可）
```

- Google は、ogsync の印が付いた primary の予定を日付に関係なく探す。
- Outlook は、分類「ogsync」が付いた予定を、OWA が表示している予定表から探す（過去 60 日〜180 日先）。
  ほかの予定表も探すときは `--calendar <予定表 ID>` を付ける（複数指定可）。

### その他のコマンド

| コマンド | 用途 |
|---|---|
| `fetch [--start 日付] [--days N] [--dump DIR]` | Outlook の既定の予定表の予定を表示する。`--dump` で OWA の応答を保存 |
| `parse DIR` | 保存した応答から予定を抽出し直す（ブラウザ不要。解析の調整用） |
| `test-write --at 2026-10-24T09:00` | Outlook にテスト予定を作成 → 読み取りで確認 → 削除 → 消えたことを確認 |
| `record` | 手動で予定を操作したときの OWA の通信を `dumps/record/` に記録する（認証ヘッダーの値は伏せる） |

`dumps/` には会社の予定データが含まれるため git 管理外。

## 同期のルール

### Outlook → Google

- Outlook の **既定の予定表だけ** を読む（エクスポートで追加した予定表などは対象外）。
- キャンセル済みの予定と、ogsync が作った BLOCKED（分類「ogsync」）はコピーしない。
- 件名は既定で「AD: BLOCKED」固定。`--detail title` で件名、`--detail full` で件名＋場所を載せる。
- Outlook で「空き時間」の予定は Google でも空き扱い。リマインダーは付けない。
- コピーには `extendedProperties.private` に印（`ogsync_source=outlook`）と元の予定の ID・内容のハッシュを
  付ける。更新・削除するのはこの印の付いた予定だけで、手動で登録した予定には触れない。
  コピーを Google 側で手動編集しても、次回の同期で Outlook の内容に戻る。

### Google → Outlook

- primary の時間指定の予定のうち、ogsync のコピー・「空き時間」・辞退・キャンセル以外を対象にする。
  終日予定は対象外。重なっている予定は 1 つにまとめる。
- Outlook の既定の予定表に、件名「BLOCKED」・予定あり・リマインダーなし・分類「ogsync」で作る。
  参加者は入れず、招待も送らない。
- BLOCKED は開始・終了時刻で識別し、時間が変わったら古いものを削除して作り直す。
  削除するのは分類「ogsync」の付いた予定だけ。

### 安全策

- 書き込む前に両方向の変更を計算し、どちらかで問題があれば何も書かずに中止する。
- Outlook の読み取りが一部でも失敗したら中止する（取得漏れを「削除された」と誤認しないため）。
- 読み取った元の予定が 0 件なのに、同期済みの予定が残っている場合は中止する（本当に空なら `--allow-empty`）。

## 定期実行（cron）

15 分ごとに `.cron/run-sync.sh`（`ogsync sync --headless`）を実行し、出力を `cron.log` に追記する。
前回の実行が終わっていなければその回はスキップする（`flock`）。

```cron
*/15 * * * * /home/hotoku/projects/hotoku/ogsync/.cron/run-sync.sh >> /home/hotoku/projects/hotoku/ogsync/cron.log 2>&1
*/15 * * * * /usr/sbin/logrotate -s /home/hotoku/projects/hotoku/ogsync/.cron/logrotate.state /home/hotoku/projects/hotoku/ogsync/.cron/logrotate-ogsync.conf >/dev/null 2>&1
```

- ログは 10MB を超えたらローテーション、14 世代保持（`.cron/logrotate-ogsync.conf`）。
- 手動で試すとき: `.cron/run-sync.sh --dry-run`
- 失敗すると Slack に通知する。
  - OWA のセッション切れ（終了コード 3）: 再ログインのコマンドを付けて通知する。
  - その他のエラー: 出力の最後の 15 行を付けて通知する。
  - 同じ失敗が続く間は再通知しない（失敗の種類を `.cron/failure` に記録し、エラーは出力の
    最後の行で同じかどうかを判定する）。同期が再開したときに 1 回通知する。

## 仕組み

OWA を 1 回開き、OWA 自身が起動時に送る通信から次の 3 つを取得する。

- 認証ヘッダー（トークンはどこにも保存しない）
- 既定の予定表の ID（startupdata の `DefaultFolderNames` が `calendar` のもの）
- OWA が表示している予定表の ID（`created` / `purge` で使う）

以降は OWA と同じ `/owa/service.svc` の `GetCalendarView`（14 日ずつ）/ `CreateCalendarEvent` /
`DeleteCalendarEvent` を、`record` で記録した手動操作と同じ形式で送る。
OWA の URL で週を指定しても今週しか表示されないため、画面遷移による取得はしない。

## ファイル

| 場所 | 内容 |
|---|---|
| `~/.local/share/ogsync/profile/` | OWA のサインイン状態（ブラウザのプロファイル） |
| `~/.config/ogsync/client_secret.json` | Google の OAuth クライアント |
| `~/.config/ogsync/google_token.json` | Google の認可トークン |
| `~/.config/ogsync/slack_webhook_url` | Slack の Incoming Webhook URL |
| `cron.log` | 定期実行のログ（git 管理外） |
| `.cron/` | cron 用スクリプト・logrotate 設定・実行時の状態ファイル |
