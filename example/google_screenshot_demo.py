from pathlib import Path

try:
    import nodriver as uc
except (ModuleNotFoundError, ImportError):
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import nodriver as uc


async def main():
    project_root = Path(__file__).resolve().parent.parent
    screenshot_path = project_root / "google_screenshot.png"

    browser = await uc.start()
    try:
        tab = await browser.get("https://google.com")
        await tab.wait(3)
        saved_path = await tab.save_screenshot(screenshot_path, format="png")
        print(f"Saved screenshot to: {saved_path}")
    finally:
        browser.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
