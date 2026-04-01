import asyncio
from pathlib import Path
import argparse

try:
    import nodriver as uc
except (ModuleNotFoundError, ImportError):
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import nodriver as uc


PROJECT_ROOT = Path(__file__).resolve().parent
PROFILES_DIR = PROJECT_ROOT / "profiles"

URL = "https://www.mybookie.ag/"
SELECTOR = 'a.login-btn-box__login.text-uppercase[href="/login/"]'


async def main():
    profile_dir = PROFILES_DIR / "POM"
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser = await uc.start(
        user_data_dir=profile_dir,
        headless=False,
    )
    await asyncio.sleep(3)
    try:
        tab = await browser.get(URL)

        # Wait until document.readyState is complete
        while True:
            state = await tab.evaluate("document.readyState")
            if state == "complete":
                print("document.readyState is complete")
                break
            await asyncio.sleep(0.05)

        # Wait until the login element exists
        while True:
            el = await tab.select(SELECTOR, timeout=0.2)
            if el:
                print("Login element exists")
                break
            await asyncio.sleep(0.05)

        pos = await el.get_position()
        if not pos:
            raise RuntimeError("Could not determine login element position")

        x, y = pos.center
        print(f"Moving mouse to login element at ({x:.2f}, {y:.2f})")
        await tab.mouse_move(x, y, steps=25, flash=True)
        await asyncio.sleep(0.2)
        print("Clicking login element via tab.mouse_click()")
        await tab.mouse_click(x, y)

        await asyncio.sleep(10)

    finally:
        browser.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
