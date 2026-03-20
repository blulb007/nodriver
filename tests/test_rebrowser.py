import asyncio
import nodriver as uc


async def test_flat_mode_stealth():
    print("Launching FLAT MODE browser session...")
    browser = await uc.start(headless=False)

    page = await browser.get('https://bot-detector.rebrowser.net')
    await asyncio.sleep(2)

    print("Triggering active tests...")
    await page.evaluate("if (window.dummyFn) window.dummyFn();")
    await page.evaluate("document.getElementById('detections-json');")
    await page.evaluate("document.getElementsByClassName('div');")

    print("Check the browser for leaks. Press Ctrl+C to close.")
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(test_flat_mode_stealth())