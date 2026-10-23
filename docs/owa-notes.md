# OWA の癖と設計判断

ogsync の開発中に分かった、コードを読むだけでは分からない事実と、その上で決めたことの理由。
（2026-10 時点。OWA は予告なく変わるので、挙動がおかしいときは「新しい操作を調べる手順」で確かめ直す）

## OWA の癖

### 読み取り

- **URL で週を指定しても今週しか表示されない。** `/calendar/view/week/2026/11/2` を開いても、OWA が
  読み込むのは今週分だけ。最初の実装はこれに頼っていて、来週以降が一度も同期されていなかった。
  そのため画面遷移はやめ、`GetCalendarView` を日付範囲を指定して自分で送っている（14 日ずつ）。
- **OWA は既定の予定表以外も読み込むことがある。** ユーザーのメールボックスには、以前別の Google
  アカウントからエクスポートした予定表があり（件名「CAT:BLOCK」など）、OWA の画面でオフにしていても
  そちらの `GetCalendarView` が飛ぶことがあった。応答の順番で予定表を選ぶと間違える。
  既定の予定表は、OWA 起動時の `startupdata` 応答の `owaUserConfig.SessionSettings` で、
  `DefaultFolderNames` が `calendar` の位置にある `DefaultFolderIds` で特定する。
- エクスポートした予定表の ID は形式が違い（`AAMk...`）、`GetCalendarView` に渡すと
  `ErrorItemNotFound` になる。ユーザーの判断で対象外にしたので、対応していない。
- `GetCalendarView` の応答は EWS 形式の JSON（`Body.Items[]` に `ItemId.Id`、`Start`、`End`、
  `Subject`、`Categories`、`FreeBusyType` など）。日時はオフセット付き（`+09:00`）。
  ID は `Prefer: IdType="ImmutableId"` によるもので、作成時に返る ID や削除に使う ID と同じ形式。
- `startupdata` は、ページ単位の `page.on("response")` では捕まらなかった（`record` のように
  ブラウザコンテキスト単位で受け取ると捕まった）。Service Worker 経由の可能性があるが未確認。
  そのため `connect` はコンテキスト単位でイベントを受け取る。

### 書き込み

- **作成（`CreateCalendarEvent`）は、手動操作の記録から項目を省くと `ErrorInternalServerError` になる。**
  空の配列（`Attachments: []` など）や空のオブジェクト（`OnlineMeeting: {}`）、`localevent:<UUID>` 形式の
  `ItemId` / `ClientSeriesId` / `id` を残す必要がある。`null` の項目は省いても問題ない。
  記録にない `Categories` を足しても問題なかった。
- 書き込み先は `ParentFolderId` と `SavedItemFolderId` で既定の予定表を指定する。
- 招待を送らない指定は `SendMeetingInvitations: "SendToNone"`。参加者も入れない。
- **削除（`DeleteCalendarEvent`）と読み取り（`GetCalendarView`）は、本文が空で、内容を URL エンコード
  した JSON を `x-owa-urlpostdata` ヘッダーに入れて送る。** 作成は本文に JSON を入れる。
- 変更（`UpdateCalendarEvent`）の形式も記録済み（変えた項目だけを `SetItemField` で送る）だが、
  BLOCKED は削除と作成で済むので使っていない。
- OWA で予定を削除すると「元に戻す」が数秒表示され、実際の削除の通信はその後に送られる。
  記録するときは、表示が消えるまで待ってから終える。

### 認証・接続

- 認証ヘッダー（`authorization`、`x-anchormailbox`、`x-owa-sessionid` など）は、OWA 自身が送った
  `service.svc` の通信から借りる。トークンは保存しない。Cookie はブラウザコンテキストが持っている。
- OWA のドメインが `outlook.office.com` から `outlook.cloud.microsoft` に移ることがある。
  送り先は、借りた通信のドメインに合わせる。
- 未ログインだと `login.microsoftonline.com` にリダイレクトされる。これを検知して終了コード 3 で終わる。
- OWA は通信が止まらないので `networkidle` は来ない。必要な通信が揃うまで短い間隔で待つ。
- 画面なし（headless）の Chromium でも OWA は問題なく動く。

### Playwright

- **同期 API は、Playwright のメソッドを呼んでいる間しかイベントを受け取らない。** `input()` で
  ユーザーを待つ間はイベントが届かず、`record` の記録が 0 件になった。待つ間は別スレッドで `input()`
  し、メインスレッドで `page.wait_for_timeout()` を回す（`owa._wait_for_enter`）。
- 応答の本文は時間が経つと読めなくなることがあるので、届いたらすぐ読む。

## 設計判断

- **Google の権限は `calendar.events`。** 最初は専用カレンダー＋`calendar.app.created` と
  `calendar.freebusy`（空き時間だけ）だったが、ユーザーの希望でどちらの方向も primary を使うことに
  なった。primary にコピーを書くと、空き時間の情報にはコピーも含まれ、会社の予定が BLOCKED として
  Outlook に戻ってしまう。予定そのものを読んでコピーを除く必要があるため、権限を広げた。
  代わりに、更新・削除はコードで印の付いた予定だけに限っている。
- **ループ防止は 2 つの印で行う。** Google のコピーには `ogsync_source=outlook` を付けて BLOCKED の
  対象から外し、Outlook の BLOCKED には分類「ogsync」を付けて Google へのコピー対象から外す。
- **状態はローカルに持たない。** Google のコピーには元の予定の ID のハッシュと内容のハッシュを持たせ、
  差分は毎回計算する。Outlook の BLOCKED は開始・終了時刻をキーにするので、作り直しで済む。
- **Google 側の重なった予定は 1 つの BLOCKED にまとめる。** 前後 1 日広く読んでからまとめ、同期範囲の
  端で BLOCKED の時刻が毎日ずれて作り直しが起きないようにしている。
- **安全策**: 両方向の差分を計算してから書き込む。Outlook の読み取りが一部でも失敗したら中止する
  （取得漏れは「消えた予定」に見えるため）。元の予定が 0 件で同期済みの予定が残っていたら中止する。
- Google のコピーの件名は「AD: BLOCKED」固定（ユーザーの希望。会社の予定の件名を個人の Google に
  載せない）。Outlook の BLOCKED の件名は「BLOCKED」で、同僚にも見える。

## 新しい操作を調べる手順

1. `uv run ogsync record` を実行し、開いた OWA で対象の操作を手動で行う（参加者は入れない）。
   認証ヘッダーの値は `<redacted>` で保存される。保存先は `dumps/record/`。
2. 記録を読むときは、メールアドレスを伏せ、構造（キー・型）を中心に見る。内容がヘッダーにある通信は
   `x-owa-urlpostdata` を URL デコードする。
3. 手動操作を 2 回記録して比べると、毎回変わる項目（時刻、`localevent:` の ID）と固定の項目が分かる。
4. 実装したら、本番の前に `test-write` と同じ流れ（作成 → 読み取りで確認 → 削除 → 消えたことを確認）で
   試す。テスト用の予定にも分類「ogsync」を付けておけば、失敗して残っても次の同期で消える。
