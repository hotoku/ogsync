# ogsync

会社の Outlook（OWA をブラウザ自動操作）と個人の Google カレンダーを同期するツール。

現在の段階: **双方向同期（試作）**

どちらの方向も Google の primary カレンダーを使う。

- Outlook の予定 → primary にコピー（実装済み）
- primary の自分の予定 → Outlook に「BLOCKED」予定（実装済み。`ogsync test-write --at 2026-10-24T09:00` で書き込みを単体確認できる）

## 使い方

```sh
uv sync
uv run playwright install chromium

# 1. 初回のみ: ブラウザが開くので手動でサインイン（MFA 含む）
uv run ogsync login

# 2. 予定を取得（--dump で OWA の JSON 応答を保存。解析の調整用）
uv run ogsync fetch --start 2026-10-26 --days 7 --dump dumps/

# 3. 保存済みダンプから再抽出（ブラウザ不要。パーサ調整用）
uv run ogsync parse dumps/ --start 2026-10-26 --days 7
```

- サインイン状態は `~/.local/share/ogsync/profile` に保存される（`--profile` で変更可）。
- WSL の Chromium が条件付きアクセスで拒否される場合は、Windows 側の Edge を
  `msedge.exe --remote-debugging-port=9222` で起動し、`--cdp http://localhost:9222` を付けて実行する。
- テナントによって URL が異なる場合は `--base-url https://outlook.cloud.microsoft` など。
- `dumps/` には会社の予定データが含まれるため git 管理外（`.gitignore` 済み）。

## Google への同期

### 初回準備
1. Google Cloud Console でプロジェクトを作り、**Google Calendar API** を有効化
2. 「OAuth 同意画面」を作成（ユーザーの種類: 外部）し、公開ステータスを **本番環境** にする
   （「テスト」のままだと 7 日ごとに再認可が必要になる。未審査のままで自分だけ使う分には問題ない）
3. 「認証情報 → OAuth クライアント ID → デスクトップ アプリ」を作成し、JSON を
   `~/.config/ogsync/client_secret.json` に保存
4. `uv run ogsync google-auth` → 表示された URL をブラウザで開いて許可

### 実行

```sh
uv run ogsync sync --dry-run   # 変更内容の確認のみ
uv run ogsync sync             # 過去7日〜28日先を同期
```

- スコープは `calendar.events`。ただし更新・削除するのは、ogsync が作った印
  （`extendedProperties.private.ogsync_source=outlook`）の付いた予定だけで、手動で登録した予定には触れない。
- コピーした予定を Google 側で手動編集しても、次回の同期で Outlook の内容に戻る。
- Google に載せる件名は既定で「AD: BLOCKED」固定。`--detail title` で件名、`--detail full` で件名+場所を載せる。
- 対応関係は Google 予定の `extendedProperties.private` に保存しており、ローカル DB は不要。
- BLOCKED にするのは primary の時間指定の予定のうち、ogsync のコピー・「空き時間」・辞退・キャンセル以外。
  重なっている予定は 1 つの BLOCKED にまとめる。終日予定は対象外。
- ループ防止: Outlook に作る BLOCKED には分類「ogsync」を付けて Google へのコピー対象から外し、
  Google のコピーには上記の印を付けて BLOCKED の対象から外す。
- 安全策: いずれかの週の取得に失敗した場合、または Outlook 側 0 件で Google 側に予定がある場合は中止する。

### ogsync が作った予定の確認・削除

```sh
uv run ogsync created              # Google のコピーと Outlook の BLOCKED を一覧表示
uv run ogsync purge                # 一覧を表示し、確認のうえ全て削除
uv run ogsync purge --side google  # Google 側だけ（outlook も指定可）
```

Outlook 側は OWA に表示中のすべての予定表を探す（既定の予定表以外に書かれたものも見つける）。

## 仕組み

OWA を 1 回開き、OWA 自身が起動時に送る通信から認証ヘッダー・既定の予定表 ID
（startupdata の `DefaultFolderNames` が `calendar` のもの）・表示中の予定表 ID を取得する。
以降は OWA と同じ `/owa/service.svc` の `GetCalendarView` / `CreateCalendarEvent` /
`DeleteCalendarEvent` を、記録した手動操作と同じ形式で送る。
（OWA の URL で週を指定しても今週しか表示されないため、画面遷移による取得はしない。）

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
- Slack 通知には Incoming Webhook の URL を `~/.config/ogsync/slack_webhook_url` に置く
  （リポジトリには入れない）。`.cron/notify-slack.sh "テスト"` で送信を試せる。
