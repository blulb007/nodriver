import asyncio
from pathlib import Path

try:
    import nodriver as uc
except (ModuleNotFoundError, ImportError):
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import nodriver as uc


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = PROJECT_ROOT / "profiles"
PROFILE_DIR = PROFILES_DIR / "cookie-monitor"
START_URL = "https://example.com"
POLL_INTERVAL_SECONDS = 2.0


def cookie_sort_key(cookie):
    return (cookie.domain, cookie.path, cookie.name)


def cookie_snapshot(cookie):
    return (
        cookie.name,
        cookie.value,
        cookie.domain,
        cookie.path,
        cookie.secure,
        cookie.http_only,
        cookie.session,
        cookie.same_site.value if cookie.same_site else None,
        cookie.expires,
    )


def print_cookies(cookies):
    print(f"\nSaved cookies: {len(cookies)}")
    if not cookies:
        print("  (none)")
        return

    for cookie in sorted(cookies, key=cookie_sort_key):
        same_site = cookie.same_site.value if cookie.same_site else "-"
        expires = cookie.expires if cookie.expires is not None else "session"
        print(
            f"  {cookie.domain}{cookie.path} | {cookie.name}={cookie.value} "
            f"| secure={cookie.secure} http_only={cookie.http_only} "
            f"| same_site={same_site} | expires={expires}"
        )


async def monitor_cookies(browser):
    previous_snapshot = None

    while not browser.stopped:
        cookies = await browser.cookies.get_all()
        current_snapshot = tuple(cookie_snapshot(cookie) for cookie in cookies)

        if current_snapshot != previous_snapshot:
            print_cookies(cookies)
            previous_snapshot = current_snapshot

        await browser.wait(POLL_INTERVAL_SECONDS)


async def main():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    browser = await uc.start(
        user_data_dir=PROFILE_DIR,
        headless=False,
    )

    try:
        tab = await browser.get(START_URL)
        print(f"Using profile: {PROFILE_DIR}")
        print(f"Opened: {tab.url}")
        print("Browse manually in the window. Cookie changes will be printed here.")
        print("Close the browser window or press Ctrl+C to stop.")
        await monitor_cookies(browser)
    finally:
        browser.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
