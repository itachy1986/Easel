"""Bounded JSON-RPC over a private stdio subprocess."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from contextlib import asynccontextmanager

from .base import HarnessError, RunRequest
from .workspace import harness_instructions


_POOLS: dict[asyncio.AbstractEventLoop, dict[str, dict]] = {}
IDLE_SECONDS = 300
MAX_IDLE_PROCESSES = 8
MAX_CLIENT_REQUESTS = 64


async def close_sessions(session_key: str | None = None):
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop, {})
    selected = [(key, entry) for key, entry in list(pool.items()) if session_key is None or key == session_key]
    for key, entry in selected:
        pool.pop(key, None)
        if entry["timer"]:
            entry["timer"].cancel()
    if not pool:
        _POOLS.pop(loop, None)
    for _key, entry in selected:
        await entry["rpc"].__aexit__()


@asynccontextmanager
async def connection(request: RunRequest, command: list[str], notify, respond):
    if not request.session_key:
        async with RPC(request, command, notify, respond) as rpc:
            yield rpc
        return
    loop = asyncio.get_running_loop()
    pool = _POOLS.setdefault(loop, {})
    key = request.session_key
    signature = hashlib.sha256(json.dumps([
        str(request.cwd.resolve()), sorted(request.env.items()), command,
        request.model, request.sandbox, harness_instructions(request),
    ], ensure_ascii=False).encode("utf-8")).hexdigest()
    entry = pool.get(key)
    previous = None
    if entry and entry["busy"]:
        raise HarnessError("此 Codex 会话正在执行，请等待本轮完成")
    if entry and (
        entry["signature"] != signature
        or entry["rpc"].native_id != request.native_id
        or entry["rpc"].reader.done()
        or entry["rpc"].process.returncode is not None
    ):
        previous = entry
        if previous["timer"]:
            previous["timer"].cancel()
        pool.pop(key, None)
        entry = None
    if entry is None:
        rpc = RPC(request, command, notify, respond)
        entry = {"rpc": rpc, "signature": signature, "busy": True, "timer": None}
        pool[key] = entry
        try:
            if previous:
                await previous["rpc"].__aexit__()
            idle = [(old_key, value) for old_key, value in pool.items() if not value["busy"]]
            if len(idle) >= MAX_IDLE_PROCESSES:
                await close_sessions(idle[0][0])
            await rpc.__aenter__()
        except BaseException:
            await close_sessions(key)
            raise
    rpc = entry["rpc"]
    if entry["timer"]:
        entry["timer"].cancel()
        entry["timer"] = None
    entry["busy"] = True
    rpc.notify = notify
    rpc.respond = respond
    rpc.client_error = None
    rpc.client_failed = loop.create_future()
    try:
        yield rpc
    except BaseException:
        await close_sessions(key)
        raise
    else:
        if pool.get(key) is not entry:
            return
        rpc.notify = lambda _method, _params: None

        async def reject_idle(_method, _params):
            raise HarnessError("会话空闲时不允许执行工具")

        rpc.respond = reject_idle

        async def expire():
            await asyncio.sleep(IDLE_SECONDS)
            entry["timer"] = None
            await close_sessions(key)

        entry["timer"] = asyncio.create_task(expire())
        entry["busy"] = False


class RPC:
    def __init__(self, request: RunRequest, command: list[str], notify, respond):
        self.request = request
        self.command = command
        self.notify = notify
        self.respond = respond
        self.pending: dict[object, asyncio.Future] = {}
        self.sequence = 0
        self.tasks: set[asyncio.Task] = set()
        self.process = None
        self.reader = None
        self.stderr = None
        self.write_lock = asyncio.Lock()
        self.client_error = None
        self.client_failed = None
        self.reader_error = None
        self.initialized = None
        self.native_id = ""

    async def __aenter__(self):
        env = {
            **self.request.env,
            "EASEL_ROOT": str(self.request.cwd.resolve()),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.request.cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            raise HarnessError("无法启动 Codex CLI，请检查安装与 PATH") from error
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._drain_stderr())
        self.client_failed = asyncio.get_running_loop().create_future()
        return self

    async def send(self, message: dict):
        async with self.write_lock:
            try:
                self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise HarnessError("Codex 进程已退出，请检查 CLI 登录与模型配置") from error

    async def call(self, method: str, params: dict, timeout=60):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            result = await asyncio.wait_for(future, timeout)
            if self.client_error:
                raise self.client_error
            return result
        except asyncio.TimeoutError as error:
            raise HarnessError(f"Codex 协议请求超时：{method}") from error
        finally:
            self.pending.pop(request_id, None)

    async def _serve(self, message):
        try:
            result = await self.respond(message["method"], message.get("params") or {})
            await self.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            self.client_error = HarnessError("Codex 请求了当前操作不支持的交互或工具")
            if self.client_failed is not None and not self.client_failed.done():
                self.client_failed.set_result(None)
            try:
                await self.send({"id": message["id"], "error": {
                    "code": -32601, "message": "Unsupported or invalid client request",
                }})
            except HarnessError:
                pass

    async def _read(self):
        failure = HarnessError("Codex 协议连接已断开，请检查 CLI 登录、模型与版本")
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError
                except ValueError:
                    raise HarnessError("Codex 返回了无效 JSON-RPC 数据") from None
                if "method" in message:
                    if "id" in message:
                        self.tasks.difference_update(task for task in self.tasks if task.done())
                        if len(self.tasks) >= MAX_CLIENT_REQUESTS:
                            raise HarnessError("Codex 客户端请求并发超过上限，本轮已终止")
                        task = asyncio.create_task(self._serve(message))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    else:
                        self.notify(message["method"], message.get("params") or {})
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            detail = str((message["error"] or {}).get("message", "请求失败"))[:1000]
                            for key, value in self.request.env.items():
                                if any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and len(value) >= 4:
                                    detail = detail.replace(value, "***")
                            future.set_exception(HarnessError(f"Codex 协议错误：{detail}"))
                        else:
                            future.set_result(message.get("result") or {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = error if isinstance(error, HarnessError) else HarnessError("Codex 协议读取失败")
        finally:
            self.reader_error = failure
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)

    async def _drain_stderr(self):
        while await self.process.stderr.read(8192):
            pass

    async def wait_notification(self, future: asyncio.Future):
        finished, _ = await asyncio.wait(
            (future, self.reader, self.client_failed), return_when=asyncio.FIRST_COMPLETED,
        )
        if future in finished:
            result = await future
            if self.client_error:
                raise self.client_error
            return result
        if self.client_failed in finished:
            raise self.client_error or HarnessError("Codex 客户端请求失败")
        raise self.reader_error or HarnessError("Codex 在本轮完成前退出")

    async def __aexit__(self, *_arguments):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.process is not None:
            if os.name == "nt":
                if self.process.returncode is None:
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(self.process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), 5)
                    except asyncio.TimeoutError:
                        killer.kill()
                        await killer.wait()
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    self.process.kill()
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await self.process.wait()
        for task in (self.reader, self.stderr):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (self.reader, self.stderr) if task is not None), return_exceptions=True)
