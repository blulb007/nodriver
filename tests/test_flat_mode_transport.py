import asyncio
import json
import types
import unittest

from nodriver import cdp
from nodriver.core.browser import Browser
from nodriver.core.connection import Connection, ProtocolException


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.close_code = None

    async def send(self, message):
        self.messages.append(json.loads(message))

    async def close(self):
        await asyncio.sleep(0)
        self.close_code = 1000


def make_target(
    target_id: str,
    type_: str = "page",
    url: str = "about:blank",
    attached: bool = False,
):
    return cdp.target.TargetInfo(
        target_id=cdp.target.TargetID(target_id),
        type_=type_,
        title=target_id,
        url=url,
        attached=attached,
        can_access_opener=False,
    )


class FlatModeTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = types.SimpleNamespace(
            targets=[],
            config=types.SimpleNamespace(host="127.0.0.1", port=9222),
        )
        self.root = Connection("ws://127.0.0.1:9222/devtools/browser/test", browser=self.browser)
        self.root._websocket = FakeWebSocket()

    async def _resolve_pending_messages(self, result_map):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        for message in list(self.root.websocket.messages):
            key = (message.get("sessionId"), message["id"])
            tx = self.root.pending.pop(key)
            tx(result=result_map.get(message["id"], {}))

    async def test_global_message_ids_are_unique_across_sessions(self):
        page_one = self.root._upsert_target_connection(
            make_target("page-1"),
            session_id=cdp.target.SessionID("session-1"),
        )
        page_two = self.root._upsert_target_connection(
            make_target("page-2"),
            session_id=cdp.target.SessionID("session-2"),
        )
        self.root.sessions[page_one.session_id] = page_one
        self.root.sessions[page_two.session_id] = page_two

        tasks = [
            asyncio.create_task(page_one.send(cdp.page.enable(), _is_update=True)),
            asyncio.create_task(page_two.send(cdp.page.enable(), _is_update=True)),
            asyncio.create_task(self.root.send(cdp.target.get_targets(), _is_update=True)),
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        sent = self.root.websocket.messages
        self.assertEqual(len(sent), 3)
        ids = [message["id"] for message in sent]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sent[0]["sessionId"], "session-1")
        self.assertEqual(sent[1]["sessionId"], "session-2")
        self.assertNotIn("sessionId", sent[2])

        await self._resolve_pending_messages(
            {sent[2]["id"]: {"targetInfos": []}}
        )
        await asyncio.gather(*tasks)

    async def test_attached_to_target_upserts_without_duplication(self):
        placeholder = self.root._upsert_target_connection(make_target("page-1"))
        self.assertEqual(len(self.browser.targets), 1)

        event = cdp.target.AttachedToTarget(
            session_id=cdp.target.SessionID("session-1"),
            target_info=make_target("page-1", url="https://example.com"),
            waiting_for_debugger=False,
        )
        await self.root._handle_attached_to_target(event)
        await self.root._handle_attached_to_target(event)

        self.assertEqual(len(self.browser.targets), 1)
        self.assertIs(self.root.targets[event.target_info.target_id], placeholder)
        self.assertEqual(placeholder.session_id, event.session_id)
        self.assertEqual(
            self.root.sessions[event.session_id].target.url, "https://example.com"
        )

    async def test_detach_cleans_up_session_routes_and_pending(self):
        page = self.root._upsert_target_connection(
            make_target("page-1"),
            session_id=cdp.target.SessionID("session-1"),
        )
        self.root.sessions[page.session_id] = page

        pending = asyncio.create_task(page.send(cdp.page.enable(), _is_update=True))
        await asyncio.sleep(0)

        await self.root._handle_detached_from_target(
            cdp.target.DetachedFromTarget(
                session_id=cdp.target.SessionID("session-1"),
                target_id=page.target_id,
            )
        )

        with self.assertRaises(ProtocolException):
            await pending
        self.assertIsNone(page.session_id)
        self.assertNotIn(cdp.target.SessionID("session-1"), self.root.sessions)

    async def test_attach_target_deduplicates_concurrent_attach_attempts(self):
        target = make_target("page-1")
        send_calls = []
        release_send = asyncio.Event()

        async def fake_send(cdp_obj, _is_update=False):
            send_calls.append(cdp_obj)
            await release_send.wait()
            return cdp.target.SessionID("session-1")

        self.root.send = fake_send

        task_one = asyncio.create_task(self.root.attach_target(target))
        task_two = asyncio.create_task(self.root.attach_target(target))
        await asyncio.sleep(0)

        await self.root._handle_attached_to_target(
            cdp.target.AttachedToTarget(
                session_id=cdp.target.SessionID("session-1"),
                target_info=target,
                waiting_for_debugger=False,
            )
        )
        release_send.set()

        result_one, result_two = await asyncio.gather(task_one, task_two)
        self.assertIs(result_one, result_two)
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(len(self.browser.targets), 1)

    async def test_tab_browser_level_commands_omit_session_id(self):
        page = self.root._upsert_target_connection(
            make_target("page-1"),
            session_id=cdp.target.SessionID("session-1"),
        )
        self.root.sessions[page.session_id] = page

        task = asyncio.create_task(page.send(cdp.target.close_target(page.target_id), _is_update=True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        sent = self.root.websocket.messages[-1]
        self.assertEqual(sent["method"], "Target.closeTarget")
        self.assertNotIn("sessionId", sent)

        tx = self.root.pending.pop((None, sent["id"]))
        tx(result={"success": True})
        await task

    async def test_target_crash_cleans_up_only_that_session(self):
        page_one = self.root._upsert_target_connection(
            make_target("page-1"),
            session_id=cdp.target.SessionID("session-1"),
        )
        page_two = self.root._upsert_target_connection(
            make_target("page-2"),
            session_id=cdp.target.SessionID("session-2"),
        )
        self.root.sessions[page_one.session_id] = page_one
        self.root.sessions[page_two.session_id] = page_two

        browser = Browser.__new__(Browser)
        browser.targets = self.browser.targets
        browser.connection = self.root
        browser.config = self.browser.config
        async def _noop():
            return None
        browser.update_targets = _noop

        pending = asyncio.create_task(page_one.send(cdp.page.enable(), _is_update=True))
        await asyncio.sleep(0)

        browser._handle_target_update(
            cdp.target.TargetCrashed(
                target_id=page_one.target_id,
                status="crashed",
                error_code=1,
            )
        )
        await asyncio.sleep(0)

        with self.assertRaises(ProtocolException):
            await pending
        self.assertTrue(page_one.crashed)
        self.assertIsNone(page_one.session_id)
        self.assertIn(page_one.target_id, self.root.targets)
        self.assertIn(page_two.session_id, self.root.sessions)

    async def test_disconnect_from_listener_cleans_pending_and_sessions(self):
        page = self.root._upsert_target_connection(
            make_target("page-1", attached=True),
            session_id=cdp.target.SessionID("session-1"),
        )
        self.root.sessions[page.session_id] = page
        page.enabled_domains.append(cdp.page)

        pending = asyncio.create_task(page.send(cdp.page.enable(), _is_update=True))
        await asyncio.sleep(0)

        self.root._listener_task = asyncio.current_task()
        await self.root.disconnect(reason="socket closed")

        with self.assertRaises(ProtocolException):
            await pending
        self.assertEqual(self.root.sessions, {})
        self.assertEqual(self.root.pending, {})
        self.assertIsNone(page.session_id)
        self.assertEqual(page.enabled_domains, [])
        self.assertFalse(page.target.attached)

    async def test_listener_crash_done_callback_forces_disconnect(self):
        page = self.root._upsert_target_connection(
            make_target("page-1"),
            session_id=cdp.target.SessionID("session-1"),
        )
        self.root.sessions[page.session_id] = page
        pending = asyncio.create_task(page.send(cdp.page.enable(), _is_update=True))
        await asyncio.sleep(0)

        async def boom():
            raise ValueError("bad frame")

        task = asyncio.create_task(boom())
        self.root._listener_task = task
        task.add_done_callback(self.root._on_listener_done)

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        with self.assertRaises(ProtocolException):
            await pending
        self.assertEqual(self.root.pending, {})
        self.assertEqual(self.root.sessions, {})
        self.assertTrue(self.root.websocket.close_code)

    async def test_attach_target_refuses_browser_reported_attached_targets(self):
        target = make_target("page-1", attached=True)
        self.root._upsert_target_connection(target)

        with self.assertRaises(ProtocolException):
            await self.root.attach_target(target)

    async def test_update_targets_is_inventory_only(self):
        browser = Browser.__new__(Browser)
        browser.targets = self.browser.targets
        browser.connection = self.root
        browser.config = self.browser.config
        browser._stopping = False

        async def fake_get_targets():
            return [make_target("page-1")]

        async def forbidden_attach(_target):
            raise AssertionError("update_targets should not attach targets")

        browser._get_targets = fake_get_targets
        self.root.attach_target = forbidden_attach

        await browser.update_targets()
        self.assertIn(cdp.target.TargetID("page-1"), self.root.targets)

    async def test_remove_handler_disables_last_domain_listener(self):
        page = self.root._upsert_target_connection(
            make_target("page-1"),
            session_id=cdp.target.SessionID("session-1"),
        )
        self.root.sessions[page.session_id] = page
        handler = lambda event: None
        page.handlers[cdp.page.FrameStoppedLoading] = [handler]
        page.enabled_domains.append(cdp.page)

        page.remove_handler(cdp.page.FrameStoppedLoading, handler)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        sent = self.root.websocket.messages[-1]
        self.assertEqual(sent["method"], "Page.disable")
        self.assertEqual(sent["sessionId"], "session-1")

        tx = self.root.pending.pop((page.session_id, sent["id"]))
        tx(result={})
        await asyncio.sleep(0)
        self.assertNotIn(cdp.page, page.enabled_domains)


if __name__ == "__main__":
    unittest.main()
