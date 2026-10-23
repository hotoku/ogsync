from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

# Holds session cookies; kept outside the project so it is never committed.
DEFAULT_PROFILE = Path.home() / ".local/share/ogsync/profile"


@contextmanager
def open_context(
    profile_dir: Path = DEFAULT_PROFILE,
    headless: bool = False,
    cdp_url: str | None = None,
) -> Iterator[BrowserContext]:
    """Yield a browser context that keeps the OWA login across runs.

    With cdp_url, attach to an already running Chrome/Edge (e.g. Windows-side Edge
    started with --remote-debugging-port=9222). Use this when Conditional Access
    rejects the bundled Chromium on WSL.
    """
    with sync_playwright() as p:
        if cdp_url:
            browser = p.chromium.connect_over_cdp(cdp_url)
            # Leave the user's own browser running.
            yield browser.contexts[0]
            return
        profile_dir.mkdir(parents=True, exist_ok=True)
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1400, "height": 900},
        )
        try:
            yield ctx
        finally:
            ctx.close()
