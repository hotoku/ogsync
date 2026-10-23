import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from ogsync import google_cal, owa, owa_api, sync
from ogsync.browser import DEFAULT_PROFILE, open_context

# Distinct from the generic failure (1) so .cron/run-sync.sh can notify on it.
EXIT_SESSION_EXPIRED = 3


def _print_events(events, as_json: bool) -> None:
    if as_json:
        print(json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=2))
        return
    for e in events:
        start = e.start.astimezone(owa.DEFAULT_TZ)
        end = e.end.astimezone(owa.DEFAULT_TZ)
        span = "終日" if e.is_all_day else f"{start:%H:%M}-{end:%H:%M}"
        flags = " [キャンセル]" if e.is_cancelled else ""
        print(f"{start:%Y-%m-%d (%a)} {span:11} {e.show_as:9} {e.subject}{flags}")
    print(f"-- {len(events)} 件", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ogsync")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--cdp", help="既存ブラウザに接続する CDP URL (例: http://localhost:9222)")
    parser.add_argument("--base-url", default=owa.DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="ブラウザを開いて OWA に手動サインインする")

    p_fetch = sub.add_parser("fetch", help="OWA から予定を取得して表示する")
    p_fetch.add_argument("--start", type=date.fromisoformat, default=date.today())
    p_fetch.add_argument("--days", type=int, default=7)
    p_fetch.add_argument("--dump", type=Path, help="OWA の JSON 応答を保存するディレクトリ")
    p_fetch.add_argument("--headless", action="store_true")
    p_fetch.add_argument("--json", action="store_true")

    p_parse = sub.add_parser("parse", help="保存済みダンプから予定を再抽出する（ブラウザ不要）")
    p_parse.add_argument("dump_dir", type=Path)
    p_parse.add_argument("--start", type=date.fromisoformat, default=date.today())
    p_parse.add_argument("--days", type=int, default=7)
    p_parse.add_argument("--json", action="store_true")

    sub.add_parser("google-auth", help="Google カレンダーへのアクセスを認可する")

    p_record = sub.add_parser("record", help="手動での予定作成・変更・削除時の OWA 通信を記録する")
    p_record.add_argument("--out", type=Path, default=Path("dumps/record"))

    p_sync = sub.add_parser("sync", help="Outlook ⇄ Google を同期する（Outlook→primary、primary の自分の予定→Outlook の BLOCKED）")
    p_sync.add_argument("--past", type=int, default=7, help="今日より前の同期日数")
    p_sync.add_argument("--future", type=int, default=28, help="今日以降の同期日数")
    p_sync.add_argument(
        "--detail",
        choices=sync.DETAIL_LEVELS,
        default="busy",
        help=f"busy=「{sync.GOOGLE_BLOCK_SUBJECT}」のみ（既定） / title=件名 / full=件名+場所",
    )
    p_sync.add_argument("--dry-run", action="store_true", help="変更内容を表示するだけ")
    p_sync.add_argument("--allow-empty", action="store_true")
    p_sync.add_argument("--headless", action="store_true")

    p_test = sub.add_parser("test-write", help="Outlook にテスト予定を作成→確認→削除する")
    p_test.add_argument("--at", type=datetime.fromisoformat, required=True, help="開始日時 (例: 2026-10-24T09:00)")
    p_test.add_argument("--minutes", type=int, default=30)

    for name, help_text in (
        ("created", "ogsync が作った予定（Google のコピーと Outlook の BLOCKED）を一覧表示する"),
        ("purge", "ogsync が作った予定をすべて削除する"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--side", choices=("both", "google", "outlook"), default="both")
        p.add_argument("--past", type=int, default=60, help="Outlook を探す今日より前の日数")
        p.add_argument("--future", type=int, default=180, help="Outlook を探す今日以降の日数")
        p.add_argument("--headless", action="store_true")
        p.add_argument(
            "--calendar",
            action="append",
            default=[],
            metavar="FOLDER_ID",
            help="表示中の予定表に加えて探す Outlook の予定表 ID（複数指定可）",
        )
        if name == "purge":
            p.add_argument("--yes", action="store_true", help="確認せずに削除する")

    args = parser.parse_args()

    if args.command == "parse":
        events = owa.select_events(owa.parse_dump_dir(args.dump_dir), args.start, args.days)
        _print_events(events, args.json)
        return
    if args.command in ("created", "purge") and args.side == "google":
        try:
            _created(None, args, purge=args.command == "purge")
        except google_cal.NotAuthorized as e:
            sys.exit(f"{e}。`ogsync google-auth` を実行してください。")
        return
    if args.command == "google-auth":
        try:
            google_cal.authorize()
        except google_cal.NotAuthorized as e:
            sys.exit(str(e))
        print("Google の認可を保存しました。")
        return

    headless = getattr(args, "headless", False)
    try:
        with open_context(args.profile, headless=headless, cdp_url=args.cdp) as ctx:
            if args.command == "login":
                owa.login(ctx, args.base_url)
                print("サインイン状態を保存しました。")
            elif args.command == "fetch":
                events = owa.fetch_events(
                    ctx,
                    args.start,
                    args.days,
                    base_url=args.base_url,
                    dump_dir=args.dump,
                )
                _print_events(events, args.json)
            elif args.command == "record":
                n = owa.record(ctx, args.out, args.base_url)
                print(f"{n} 件の通信を {args.out} に保存しました。")
            elif args.command == "sync":
                _run_sync(ctx, args)
            elif args.command == "test-write":
                _test_write(ctx, args)
            elif args.command in ("created", "purge"):
                _created(ctx, args, purge=args.command == "purge")
    except owa.SessionExpired as e:
        print(f"セッションが切れています。`ogsync login` を実行してください。({e})", file=sys.stderr)
        sys.exit(EXIT_SESSION_EXPIRED)
    except owa.FetchIncomplete as e:
        sys.exit(f"Outlook の取得が不完全なため中止しました。({e})")
    except google_cal.NotAuthorized as e:
        sys.exit(f"{e}。`ogsync google-auth` を実行してください。")
    except sync.UnsafeSync as e:
        sys.exit(str(e))
    except owa_api.OwaApiError as e:
        sys.exit(f"Outlook の操作に失敗しました: {e}")


def _run_sync(ctx, args) -> None:
    # Authorize Google before the slow OWA fetch so a missing token fails fast.
    svc = google_cal.service()
    start = date.today() - timedelta(days=args.past)
    days = args.past + args.future
    lo, hi = owa.window(start, days)
    events, client = owa.fetch(ctx, start, days, base_url=args.base_url)

    # Plan both directions before writing anything, so a safety check in either
    # direction stops the whole run.
    to_google = sync.plan_to_google(
        events, google_cal.list_synced(svc, lo, hi), args.detail, args.allow_empty
    )
    busy = sync.busy_from_google(google_cal.list_all(svc, lo, hi), lo, hi)
    to_outlook = sync.plan_to_outlook(busy, events, args.allow_empty)

    print(_summary("Outlook → Google", to_google))
    for body in to_google.inserts:
        print(f"  + {_when(body)} {body['summary']}")
    for _, body in to_google.updates:
        print(f"  ~ {_when(body)} {body['summary']}")
    print(_summary("Google → Outlook", to_outlook))
    for block in to_outlook.inserts:
        print(f"  + {_span(block.start, block.end)} {sync.BLOCK_SUBJECT}")
    for e in events:
        if e.source_id in to_outlook.deletes:
            print(f"  - {_span(e.start, e.end)} {e.subject}")

    if args.dry_run:
        return
    if to_google:
        sync.apply_to_google(svc, to_google)
    if to_outlook:
        sync.apply_to_outlook(client, to_outlook)
    print("同期しました。")


def _summary(label: str, p: sync.Plan) -> str:
    return f"{label}: 追加 {len(p.inserts)} / 更新 {len(p.updates)} / 削除 {len(p.deletes)}"


def _span(start: datetime, end: datetime) -> str:
    s, e = start.astimezone(owa.DEFAULT_TZ), end.astimezone(owa.DEFAULT_TZ)
    return f"{s:%Y-%m-%d %H:%M}-{e:%H:%M}"


def _when(body: dict) -> str:
    s = body["start"]
    return s.get("date") or datetime.fromisoformat(s["dateTime"]).astimezone(owa.DEFAULT_TZ).strftime(
        "%Y-%m-%d %H:%M"
    )


def _test_write(ctx, args) -> None:
    """Create one event, confirm it through a fresh read, delete it, confirm it is gone.

    The test event carries the ogsync category, so if anything fails half way, the
    next sync removes it as a stale block.
    """
    subject = "ogsync write test"
    start = args.at.replace(tzinfo=owa.DEFAULT_TZ) if args.at.tzinfo is None else args.at
    end = start + timedelta(minutes=args.minutes)
    day = start.astimezone(owa.DEFAULT_TZ).date()
    client = owa.connect(ctx, args.base_url)

    def find() -> list:
        return [
            e
            for e in owa.read_calendar(client, client.folder_id, day, 1)
            if e.subject == subject and sync.OGSYNC_CATEGORY in e.categories
            and e.start == start and e.end == end
        ]

    client.create_event(subject, start, end, sync.OGSYNC_CATEGORY, owa.DEFAULT_TZ)
    print(f"1. 作成しました: {_span(start, end)} {subject}")
    found = find()
    if not found:
        raise owa_api.OwaApiError("作成した予定が読み取りで見つかりません")
    print(f"2. 読み取りで確認しました（分類「{sync.OGSYNC_CATEGORY}」付き）")
    for e in found:
        client.delete_event(e.source_id)
    print("3. 削除しました")
    if find():
        raise owa_api.OwaApiError("削除した予定がまだ残っています")
    print("4. 削除を確認しました。書き込みは正常に動作しています。")


def _created(ctx, args, purge: bool) -> None:
    """List (and with purge, delete) everything ogsync made, on the chosen sides.

    Outlook is searched in every calendar OWA displays (plus --calendar), not just
    the default one: an earlier version could write blocks to another calendar.
    """
    google_items: list[dict] = []
    outlook: list[tuple[str, owa.Event]] = []  # (calendar label, event)
    svc = client = None

    if args.side in ("both", "google"):
        svc = google_cal.service()
        google_items = google_cal.list_created(svc)
        print(f"Google primary: {len(google_items)} 件")
        for item in google_items:
            print(f"  {_when(item)} {item.get('summary', '')}")

    if args.side in ("both", "outlook"):
        client = owa.connect(ctx, args.base_url)
        start = date.today() - timedelta(days=args.past)
        calendar_ids = dict.fromkeys([*client.shown_calendar_ids, *args.calendar])
        for i, cid in enumerate(calendar_ids):
            label = "既定の予定表" if cid == client.folder_id else f"他の予定表 {i} (…{cid[-8:]})"
            try:
                events = owa.read_calendar(client, cid, start, args.past + args.future)
            except owa.FetchIncomplete as e:
                print(f"Outlook {label}: 読み取れませんでした ({e})")
                continue
            mine = [e for e in events if sync.OGSYNC_CATEGORY in e.categories]
            print(f"Outlook {label}: {len(mine)} 件")
            for e in mine:
                print(f"  {_span(e.start, e.end)} {e.subject}")
            outlook.extend((label, e) for e in mine)

    if not purge or not (google_items or outlook):
        return
    if not args.yes and input(f"これら {len(google_items) + len(outlook)} 件を削除します。よろしいですか？ [y/N]: ").strip().lower() != "y":
        print("中止しました。")
        return
    for item in google_items:
        google_cal.delete(svc, item["id"])
    for _, e in outlook:
        client.delete_event(e.source_id)
    print("削除しました。")
