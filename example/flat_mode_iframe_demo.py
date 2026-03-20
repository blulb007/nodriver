import asyncio
import os
import sys
import urllib.parse


try:
    import nodriver as uc
except (ModuleNotFoundError, ImportError):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import nodriver as uc

from nodriver import cdp


async def wait_for_iframe_session(browser, timeout=15):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for target in browser.connection.targets.values():
            if target.type_ == "iframe" and target.session_id:
                return target
        await browser.sleep(0.25)
    raise TimeoutError("no iframe session was attached within the timeout")


async def main():
    browser = await uc.start()
    try:
        iframe_src = "https://example.com/"
        page_html = f"""
        <!doctype html>
        <html>
          <body>
            <h1>flat mode demo</h1>
            <iframe id="child" src="{iframe_src}" width="800" height="400"></iframe>
          </body>
        </html>
        """
        page_url = "data:text/html;charset=utf-8," + urllib.parse.quote(page_html)

        main_tab = await browser.get(page_url)
        second_tab = await browser.get("https://example.org/", new_tab=True)
        iframe_target = await wait_for_iframe_session(browser)

        iframe_result, iframe_error = await iframe_target.send(
            cdp.runtime.evaluate(
                expression="document.location.href",
                return_by_value=True,
            )
        )
        second_result, second_error = await second_tab.send(
            cdp.runtime.evaluate(
                expression="document.location.href",
                return_by_value=True,
            )
        )

        if iframe_error:
            raise RuntimeError(f"iframe evaluate failed: {iframe_error}")
        if second_error:
            raise RuntimeError(f"second tab evaluate failed: {second_error}")

        print("Main tab target:", main_tab.target_id, main_tab.session_id)
        print("Second tab target:", second_tab.target_id, second_tab.session_id)
        print("Iframe target:", iframe_target.target_id, iframe_target.session_id)
        print("Iframe href:", iframe_result.value)
        print("Second tab href:", second_result.value)
        print("Root websocket:", browser.connection.websocket_url)
        print("Known routed sessions:", sorted(str(k) for k in browser.connection.sessions))
    finally:
        browser.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
