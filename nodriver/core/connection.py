# Copyright 2024 by UltrafunkAmsterdam (https://github.com/UltrafunkAmsterdam)
# All rights reserved.
# This file is part of the nodriver package.
# and is released under the "GNU AFFERO GENERAL PUBLIC LICENSE".
# Please see the LICENSE.txt file that should have been included as part of this package.

from __future__ import annotations

import asyncio
import collections
import inspect
import itertools
import json
import logging
import types
from asyncio import iscoroutine, iscoroutinefunction
from typing import Any, Awaitable, Callable, Generator, List, Optional, TypeVar, Union

import websockets.asyncio.client
import websockets.exceptions

from .. import cdp
from . import browser as _browser
from . import util

T = TypeVar("T")

GLOBAL_DELAY = 0.005
MAX_SIZE: int = 2**28
PING_TIMEOUT: int = 900

TargetType = Union[cdp.target.TargetInfo, cdp.target.TargetID]

logger = logging.getLogger(__name__)

BROWSER_LEVEL_METHOD_PREFIXES = ("Browser.", "Target.")


class ProtocolException(Exception):
    def __init__(self, *args, **kwargs):
        self.message = None
        self.code = None
        self.args = args
        if args and isinstance(args[0], dict):
            self.message = args[0].get("message", None)
            self.code = args[0].get("code", None)
        elif args and hasattr(args[0], "to_json"):

            def serialize(obj, _d=0):
                res = "\n"
                for k, v in obj.items():
                    space = "\t" * _d
                    if isinstance(v, dict):
                        res += f"{space}{k}: {serialize(v, _d + 1)}\n"
                    else:
                        res += f"{space}{k}: {v}\n"
                return res

            self.message = serialize(args[0].to_json())
        else:
            self.message = "| ".join(str(x) for x in args)

    def __str__(self):
        return f"{self.message} [code: {self.code}]" if self.code else f"{self.message}"


class SettingClassVarNotAllowedException(PermissionError):
    pass


class Transaction(asyncio.Future):
    __cdp_obj__: Generator = None
    method: str = None
    params: dict = None
    id: int = None
    session_id: Optional[cdp.target.SessionID] = None

    def __init__(
        self,
        cdp_obj: Generator,
        *,
        session_id: Optional[cdp.target.SessionID] = None,
    ):
        super().__init__()
        self.__cdp_obj__ = cdp_obj
        self.connection = None
        self.session_id = session_id

        self.method, *params = next(self.__cdp_obj__).values()
        if params:
            params = params.pop()
        self.params = params

    @property
    def message(self):
        payload = {"method": self.method, "params": self.params, "id": self.id}
        if self.session_id is not None:
            payload["sessionId"] = self.session_id
        return json.dumps(payload)

    @property
    def has_exception(self):
        try:
            if self.exception():
                return True
        except asyncio.InvalidStateError as e:
            if "not set" in e.args:
                return False
        except Exception:
            return True
        return False

    def __call__(self, **response: dict):
        if "error" in response:
            return self.set_exception(ProtocolException(response["error"]))
        try:
            self.__cdp_obj__.send(response["result"])
        except KeyError as e:
            raise KeyError(f"key '{e.args}' not found in message: {response['result']}")
        except StopIteration as e:
            self.set_result(e.value)

    def __repr__(self):
        success = False if (self.done() and self.has_exception) else True
        status = "finished" if self.done() else "pending"
        fmt = (
            f"<{self.__class__.__name__}\n\t"
            f"method: {self.method}\n\t"
            f"status: {status}\n\t"
            f"success: {success}>"
        )
        return fmt


class EventTransaction(Transaction):
    event = None
    value = None

    def __init__(self, event_object):
        try:
            super().__init__(None)
        except Exception:
            pass
        self.set_result(event_object)
        self.event = self.value = self.result()

    def __repr__(self):
        status = "finished"
        success = False if self.exception() else True
        event_object = self.result()
        fmt = (
            f"{self.__class__.__name__}\n\t"
            f"event: {event_object.__class__.__module__}.{event_object.__class__.__name__}\n\t"
            f"status: {status}\n\t"
            f"success: {success}>"
        )
        return fmt


class CantTouchThis(type):
    def __setattr__(cls, attr, value):
        if attr == "__annotations__":
            return super().__setattr__(attr, value)
        raise SettingClassVarNotAllowedException(
            "\n".join(
                (
                    "don't set '%s' on the %s class directly, as those are shared with other objects.",
                    "use `my_object.%s = %s`  instead",
                )
            )
            % (attr, cls.__name__, attr, value)
        )


class Connection(metaclass=CantTouchThis):
    attached: bool = None

    @property
    def browser(self) -> _browser.Browser:
        return self._browser

    @property
    def websocket(self) -> websockets.asyncio.client.ClientConnection:
        return self.root._websocket

    @property
    def target(self) -> cdp.target.TargetInfo:
        return self._target

    @property
    def root(self) -> "Connection":
        return self._root if self._root is not None else self

    @property
    def is_root(self) -> bool:
        return self.root is self

    @property
    def session_id(self) -> Optional[cdp.target.SessionID]:
        return self._session_id

    @session_id.setter
    def session_id(self, value: Optional[cdp.target.SessionID]):
        self._session_id = value

    def __init__(
        self,
        websocket_url: str,
        target: cdp.target.TargetInfo = None,
        browser: _browser.Browser = None,
        *,
        root: "Connection" = None,
        session_id: Optional[cdp.target.SessionID] = None,
        **kwargs,
    ):
        super().__init__()
        self.websocket_url: str = websocket_url
        self.mapper = {}
        self.handlers = collections.defaultdict(list)
        self.enabled_domains = []
        self._target = target
        self._browser = browser
        self._websocket = None
        self._listener_task = None
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._root = root
        self._session_id = session_id
        self._is_closed = False
        self._is_crashed = False
        self._flatten_auto_attach_registered = False
        self._discovery_registered = False
        self._disconnecting = None
        self._disconnecting_owner = None
        self.__dict__.update(**kwargs)

        if self.is_root:
            self.pending = {}
            self.sessions = {}
            self.targets = {}
            self.attach_inflight = {}
            self.__count__ = itertools.count(0)
        else:
            self.pending = self.root.pending
            self.sessions = self.root.sessions
            self.targets = self.root.targets
            self.attach_inflight = self.root.attach_inflight

    @property
    def closed(self):
        if not self.is_root:
            return self.root.closed or self._is_closed
        if self._disconnecting and not self._disconnecting.done():
            return True
        if not self._websocket:
            return True
        return bool(self._websocket.close_code)

    @property
    def crashed(self):
        if self.is_root:
            return False
        return self._is_crashed

    def add_handler(
        self,
        event_type_or_domain: Union[type, types.ModuleType, List[type]],
        handler: Union[Callable, Awaitable],
    ):
        if not isinstance(event_type_or_domain, list):
            event_type_or_domain = [event_type_or_domain]

        for evt_dom in event_type_or_domain:
            if isinstance(evt_dom, types.ModuleType):
                for name, obj in inspect.getmembers_static(evt_dom):
                    if name.isupper():
                        continue
                    if not name[0].isupper():
                        continue
                    if type(obj) != type:
                        continue
                    if inspect.isbuiltin(obj):
                        continue
                    self.handlers[obj].append(handler)
                return
            self.handlers[evt_dom].append(handler)

    def remove_handler(
        self,
        event_type_or_domain: Union[type, types.ModuleType, List[type]],
        handler: Union[Callable, Awaitable] = None,
    ):
        if not isinstance(event_type_or_domain, list):
            event_type_or_domain = [event_type_or_domain]

        disabled_domains = set()
        for evt_dom in event_type_or_domain:
            if isinstance(evt_dom, types.ModuleType):
                for name, obj in inspect.getmembers_static(evt_dom):
                    if name.isupper():
                        continue
                    if not name[0].isupper():
                        continue
                    if type(obj) != type:
                        continue
                    if inspect.isbuiltin(obj):
                        continue
                    callbacks = self.handlers.get(obj, [])
                    if handler is not None:
                        self.handlers[obj] = [cb for cb in callbacks if cb != handler]
                        if not self.handlers[obj]:
                            self.handlers.pop(obj, None)
                    else:
                        self.handlers.pop(obj, None)
                if not self._domain_has_handlers(evt_dom):
                    disabled_domains.add(evt_dom)
                continue
            callbacks = self.handlers.get(evt_dom, [])
            if handler is not None:
                self.handlers[evt_dom] = [cb for cb in callbacks if cb != handler]
                if not self.handlers[evt_dom]:
                    self.handlers.pop(evt_dom, None)
            else:
                self.handlers.pop(evt_dom, None)
            if isinstance(evt_dom, type):
                domain_mod = util.cdp_get_module(evt_dom.__module__)
                if not self._domain_has_handlers(domain_mod):
                    disabled_domains.add(domain_mod)

        for domain_mod in disabled_domains:
            if domain_mod not in self.enabled_domains:
                continue
            try:
                self._create_background_task(
                    self._disable_domain(domain_mod),
                    label=f"disable domain {domain_mod.__name__}",
                )
            except RuntimeError:
                # No running loop; just drop the local bookkeeping.
                try:
                    self.enabled_domains.remove(domain_mod)
                except ValueError:
                    pass

    async def connect(self, **kw):
        if not self.is_root:
            await self.root.connect(**kw)
            return
        if self._disconnecting and not self._disconnecting.done():
            await asyncio.shield(self._disconnecting)
        if not self._websocket or bool(self._websocket.close_code):
            try:
                self._websocket = await websockets.connect(
                    self.websocket_url,
                    ping_timeout=PING_TIMEOUT,
                    max_size=MAX_SIZE,
                )
                self._listener_task = asyncio.create_task(self._listener())
                self._listener_task.add_done_callback(self._on_listener_done)
            except Exception as e:
                logger.debug("exception during opening of websocket : %s", e)
                raise

            if self._discovery_registered:
                await self.send(cdp.target.set_discover_targets(discover=True), _is_update=True)
            if self._flatten_auto_attach_registered:
                await self.send(
                    cdp.target.set_auto_attach(
                        auto_attach=True,
                        wait_for_debugger_on_start=False,
                        flatten=True,
                    ),
                    _is_update=True,
                )
            await self._register_handlers()

    async def disconnect(self, *, reason: str = "connection closed"):
        if not self.is_root:
            await self.root.disconnect(reason=reason)
            return
        current_task = asyncio.current_task()
        if self._disconnecting and not self._disconnecting.done():
            if self._disconnecting_owner is current_task:
                return
            await asyncio.shield(self._disconnecting)
            return

        loop = asyncio.get_running_loop()
        self._disconnecting = loop.create_future()
        self._disconnecting_owner = current_task
        listener_task = self._listener_task
        exc = ProtocolException({"message": reason})
        try:
            self._invalidate_session_state(exc)
            if listener_task and listener_task is not current_task and not listener_task.done():
                listener_task.cancel()
            if self._websocket and not bool(self._websocket.close_code):
                try:
                    await self._websocket.close()
                    logger.debug("\nclosed websocket connection to %s", self.websocket_url)
                except Exception:
                    logger.debug("error while closing websocket", exc_info=True)
            if listener_task and listener_task is not current_task and not listener_task.done():
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("listener task failed during disconnect", exc_info=True)
        finally:
            self._listener_task = None
            self._disconnecting_owner = None
            if self._disconnecting and not self._disconnecting.done():
                self._disconnecting.set_result(None)

    def __getattr__(self, item):
        try:
            return getattr(self.target, item)
        except AttributeError:
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def _register_handlers(self):
        seen = []
        enabled_domains = self.enabled_domains.copy()
        for event_type in self.handlers.copy():
            domain_mod = None
            if len(self.handlers[event_type]) == 0:
                self.handlers.pop(event_type)
                continue
            if isinstance(event_type, type):
                domain_mod = util.cdp_get_module(event_type.__module__)
            if domain_mod in self.enabled_domains:
                if domain_mod in enabled_domains:
                    enabled_domains.remove(domain_mod)
                continue
            if domain_mod not in self.enabled_domains:
                if domain_mod in (cdp.target, cdp.storage, cdp.input_):
                    continue
                try:
                    logger.debug("registered %s", domain_mod)
                    self.enabled_domains.append(domain_mod)
                    await self.send(domain_mod.enable(), _is_update=True)
                except Exception:
                    logger.debug("", exc_info=True)
                    try:
                        self.enabled_domains.remove(domain_mod)
                    except Exception:
                        logger.debug("NOT GOOD", exc_info=True)
                        continue
                finally:
                    continue
        for ed in enabled_domains:
            await self._disable_domain(ed)

    def _next_id(self) -> int:
        return next(self.root.__count__)

    def _pending_key(
        self, session_id: Optional[cdp.target.SessionID], message_id: int
    ) -> tuple[Optional[cdp.target.SessionID], int]:
        return (session_id, message_id)

    def _get_transaction_owner(
        self, message: dict[str, Any]
    ) -> tuple["Connection", Optional[cdp.target.SessionID]]:
        session_id = message.get("sessionId")
        if session_id is not None:
            session_id = cdp.target.SessionID.from_json(session_id)
            return self.root.sessions.get(session_id, self.root), session_id
        return self.root, None

    def _upsert_target_connection(
        self,
        target_info: cdp.target.TargetInfo,
        *,
        session_id: Optional[cdp.target.SessionID] = None,
        connection_cls: type["Connection"] | None = None,
    ) -> "Connection":
        if connection_cls is None:
            from .tab import Tab

            connection_cls = Tab

        existing = self.root.targets.get(target_info.target_id)
        if existing is None:
            existing = connection_cls(
                self.root.websocket_url,
                target=target_info,
                browser=self.browser,
                root=self.root,
                session_id=session_id,
            )
            self.root.targets[target_info.target_id] = existing
            if self.browser and existing not in self.browser.targets:
                self.browser.targets.append(existing)
        else:
            existing._target = target_info
            existing._browser = self.browser
            if session_id is not None:
                existing.session_id = session_id
        return existing

    async def attach_target(
        self,
        target: Union[cdp.target.TargetInfo, cdp.target.TargetID],
        *,
        connection_cls: type["Connection"] | None = None,
    ) -> "Connection":
        if isinstance(target, cdp.target.TargetID):
            existing = self.root.targets.get(target)
            if existing and existing.target:
                target_info = existing.target
            else:
                target_info = await self.root.send(cdp.target.get_target_info(target))
        else:
            target_info = target

        if target_info.target_id in self.root.attach_inflight:
            return await asyncio.shield(self.root.attach_inflight[target_info.target_id])

        existing = self.root.targets.get(target_info.target_id)
        if existing and existing.session_id:
            return existing
        target_attached = bool(getattr(target_info, "attached", False))
        if existing and existing.target is not None:
            target_attached = target_attached or bool(getattr(existing.target, "attached", False))
        if target_attached:
            if existing is None:
                existing = self._upsert_target_connection(
                    target_info, connection_cls=connection_cls
                )
            raise ProtocolException(
                {
                    "message": (
                        "target already attached via auto-attach: "
                        f"{target_info.target_id}"
                    )
                }
            )

        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self.root.attach_inflight[target_info.target_id] = waiter
        existing = self._upsert_target_connection(
            target_info, connection_cls=connection_cls
        )
        try:
            session_id = await self.root.send(
                cdp.target.attach_to_target(target_info.target_id, flatten=True),
                _is_update=True,
            )
            if not waiter.done():
                existing.session_id = session_id
                self.root.sessions[session_id] = existing
                waiter.set_result(existing)
            return await asyncio.shield(waiter)
        except Exception as exc:
            if not waiter.done():
                waiter.set_exception(exc)
            raise
        finally:
            current = self.root.attach_inflight.get(target_info.target_id)
            if current is waiter:
                self.root.attach_inflight.pop(target_info.target_id, None)

    async def _handle_attached_to_target(
        self, event: cdp.target.AttachedToTarget, *_args
    ):
        owner = self._upsert_target_connection(
            event.target_info,
            session_id=event.session_id,
        )
        owner.session_id = event.session_id
        owner._is_closed = False
        owner._is_crashed = False
        owner.enabled_domains.clear()
        if owner.target is not None:
            owner.target.attached = True
        self.root.sessions[event.session_id] = owner

        waiter = self.root.attach_inflight.pop(event.target_info.target_id, None)
        if waiter and not waiter.done():
            waiter.set_result(owner)

        if event.waiting_for_debugger:
            try:
                await owner.send(cdp.runtime.run_if_waiting_for_debugger(), _is_update=True)
            except Exception:
                logger.debug("failed to resume waiting target %s", owner, exc_info=True)

    async def _handle_detached_from_target(
        self, event: cdp.target.DetachedFromTarget, *_args
    ):
        owner = self.root.sessions.pop(event.session_id, None)
        if owner and owner.session_id == event.session_id:
            owner.session_id = None
            owner.enabled_domains.clear()
            if owner.target is not None:
                owner.target.attached = False
        for target_id, waiter in list(self.root.attach_inflight.items()):
            if owner and owner.target_id == target_id:
                self.root.attach_inflight.pop(target_id, None)
                if not waiter.done():
                    waiter.set_exception(
                        ProtocolException(
                            {"message": f"target session detached: {event.session_id}"}
                        )
                    )
        self._fail_pending_for_session(
            event.session_id,
            ProtocolException(
                {"message": f"target session detached: {event.session_id}"}
            ),
        )

    def _cleanup_target(
        self,
        target: Optional["Connection"],
        *,
        reason: str,
        remove_target: bool = False,
        mark_closed: bool = False,
        mark_crashed: bool = False,
    ):
        if not target:
            return

        session_id = target.session_id
        if session_id is not None:
            self.root.sessions.pop(session_id, None)
            self._fail_pending_for_session(
                session_id,
                ProtocolException({"message": reason}),
            )
            target.session_id = None
        target.enabled_domains.clear()
        if target.target is not None:
            target.target.attached = False

        waiter = self.root.attach_inflight.pop(target.target_id, None)
        if waiter and not waiter.done():
            waiter.set_exception(ProtocolException({"message": reason}))

        if mark_closed:
            target._is_closed = True
        if mark_crashed:
            target._is_crashed = True

        if remove_target:
            self.root.targets.pop(target.target_id, None)
            if self.browser and target in self.browser.targets:
                self.browser.targets.remove(target)

    def _is_browser_level_method(self, method: str) -> bool:
        return method.startswith(BROWSER_LEVEL_METHOD_PREFIXES)

    def _fail_pending_for_session(
        self,
        session_id: Optional[cdp.target.SessionID],
        exc: Exception,
    ):
        to_fail = [
            key for key in list(self.root.pending.keys()) if key[0] == session_id
        ]
        for key in to_fail:
            tx = self.root.pending.pop(key, None)
            if tx and not tx.done():
                tx.set_exception(exc)

    def _fail_all_pending(self, exc: Exception):
        for key, tx in list(self.root.pending.items()):
            self.root.pending.pop(key, None)
            if tx and not tx.done():
                tx.set_exception(exc)

    def _track_background_task(
        self,
        task: asyncio.Task,
        *,
        label: str,
        disconnect_on_error: bool = False,
    ) -> asyncio.Task:
        def _consume(task_: asyncio.Task):
            try:
                exc = task_.exception()
            except asyncio.CancelledError:
                return

            if exc is None:
                return

            logger.debug(
                "%s failed", label, exc_info=(type(exc), exc, exc.__traceback__)
            )
            if not disconnect_on_error or not self.is_root:
                return
            if self._disconnecting and not self._disconnecting.done():
                return

            follow_up = task_.get_loop().create_task(
                self.disconnect(reason=f"{label} failed: {exc}")
            )
            self._track_background_task(
                follow_up,
                label=f"disconnect after {label}",
            )

        task.add_done_callback(_consume)
        return task

    def _create_background_task(
        self,
        coro: Awaitable[Any],
        *,
        label: str,
        disconnect_on_error: bool = False,
    ) -> asyncio.Task:
        task = asyncio.create_task(coro)
        return self._track_background_task(
            task,
            label=label,
            disconnect_on_error=disconnect_on_error,
        )

    def _on_listener_done(self, task: asyncio.Task):
        if self._listener_task is not task:
            return
        self._listener_task = None

        if task.cancelled():
            return

        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return

        if exc is None:
            if self.closed or (self._disconnecting and not self._disconnecting.done()):
                return
            reason = "listener exited unexpectedly"
            logger.error(reason)
        else:
            reason = f"listener crashed: {exc}"
            logger.error("listener crashed", exc_info=(type(exc), exc, exc.__traceback__))

        if self._disconnecting and not self._disconnecting.done():
            return

        follow_up = task.get_loop().create_task(self.disconnect(reason=reason))
        self._track_background_task(
            follow_up,
            label="disconnect after listener exit",
        )

    def _domain_has_handlers(self, domain_mod: types.ModuleType) -> bool:
        for event_type, callbacks in self.handlers.items():
            if not callbacks:
                continue
            if not isinstance(event_type, type):
                continue
            if util.cdp_get_module(event_type.__module__) == domain_mod:
                return True
        return False

    async def _disable_domain(self, domain_mod: types.ModuleType):
        if domain_mod not in self.enabled_domains:
            return

        try:
            disable = getattr(domain_mod, "disable")
        except AttributeError:
            disable = None

        try:
            if disable is not None and not self.root.closed:
                if self.is_root or self.session_id is not None:
                    await self.send(disable(), _is_update=True)
        except Exception:
            logger.debug("failed to disable %s", domain_mod, exc_info=True)
        finally:
            try:
                self.enabled_domains.remove(domain_mod)
            except ValueError:
                pass

    def _invalidate_session_state(self, exc: Exception):
        self.enabled_domains.clear()
        self._fail_all_pending(exc)

        for waiter in list(self.root.attach_inflight.values()):
            if waiter and not waiter.done():
                waiter.set_exception(exc)
        self.root.attach_inflight.clear()

        for target in list(self.root.targets.values()):
            target.enabled_domains.clear()
            if target.target is not None:
                target.target.attached = False
            target.session_id = None

        self.root.sessions.clear()

    async def _listener(self):
        while True:
            try:
                async with self._lock:
                    raw = await self._websocket.recv()
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosedOK:
                await self.disconnect()
                break
            except websockets.exceptions.ConnectionClosed:
                await self.disconnect()
                break
            except Exception as e:
                logger.info(
                    "error when receiving websocket response: %s" % e, exc_info=True
                )
                raise

            message = json.loads(raw)
            session_id = message.get("sessionId")
            if session_id is not None:
                session_id = cdp.target.SessionID.from_json(session_id)

            if "id" in message:
                key = self._pending_key(session_id, message["id"])
                tx: Transaction = self.root.pending.pop(key, None)
                if tx is None and session_id is not None:
                    key = self._pending_key(None, message["id"])
                    tx = self.root.pending.pop(key, None)
                if tx is None:
                    logger.debug("orphan response received: %s", message)
                    continue
                tx(**message)
                logger.debug(
                    "got answer for (session_id:%s message_id:%d) => %s",
                    session_id,
                    tx.id,
                    message,
                )
                continue

            try:
                event = cdp.util.parse_json_event(message)
            except Exception as e:
                logger.info(
                    "%s: %s  during parsing of json from event : %s"
                    % (type(e).__name__, e.args, message),
                    exc_info=True,
                )
                continue

            owner = self.root.sessions.get(session_id, self.root) if session_id else self.root
            await self._dispatch_event(event, owner)

    async def _dispatch_event(self, event, owner: "Connection"):
        callbacks = owner.handlers.get(type(event), [])
        if not callbacks:
            return
        for callback in callbacks:
            try:
                try:
                    result = callback(event, owner)
                except TypeError:
                    result = callback(event)

                if inspect.isawaitable(result):
                    if self._dispatch_inline(event, callback):
                        await result
                    else:
                        self._create_background_task(
                            result,
                            label=f"event handler {callback}",
                        )
            except Exception as e:
                logger.warning(
                    "exception in callback %s for event %s => %s",
                    callback,
                    event.__class__.__name__,
                    e,
                    exc_info=True,
                )

    async def send(
        self, cdp_obj: Generator[dict[str, Any], dict[str, Any], Any], _is_update=False
    ) -> Any:
        if self.root._disconnecting and not self.root._disconnecting.done():
            await asyncio.shield(self.root._disconnecting)
        if self.root.closed:
            await self.root.connect()
        if not self.is_root and self.session_id is None and self.target is not None:
            await self.root.attach_target(self.target)
        if not _is_update:
            await self._register_handlers()

        tx = Transaction(cdp_obj)
        if not self.is_root and not self._is_browser_level_method(tx.method):
            tx.session_id = self.session_id
        tx.id = self.root._next_id()
        tx.connection = self
        key = self.root._pending_key(tx.session_id, tx.id)
        self.root.pending[key] = tx
        try:
            await self.root.websocket.send(tx.message)
        except Exception as exc:
            self.root.pending.pop(key, None)
            if not tx.done():
                tx.set_exception(
                    ProtocolException(
                        {"message": "connection closed" if self.root.closed else str(exc)}
                    )
                )
        return await tx

    async def _send_oneshot(self, cdp_obj):
        return await self.send(cdp_obj, _is_update=True)

    def _dispatch_inline(self, event, callback: Union[Callable, Awaitable]) -> bool:
        if type(event) not in (
            cdp.target.AttachedToTarget,
            cdp.target.DetachedFromTarget,
        ):
            return False

        callback_self = getattr(callback, "__self__", None)
        callback_func = getattr(callback, "__func__", None)
        return callback_self is self.root and callback_func in (
            Connection._handle_attached_to_target,
            Connection._handle_detached_from_target,
        )
