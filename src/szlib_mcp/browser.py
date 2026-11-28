"""浏览器管理：单实例多 Context，实现按用户/会话隔离。

设计
----
- 整个进程只启动一个 Firefox（省内存）
- 每个会话键（session key）一个独立 BrowserContext —— cookie/存储物理隔离
- Context 空闲超过 TTL 自动关闭
- 凭据只存在于对应 Context 的内存与 cookie jar 中，不写全局 profile
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import Config, get_config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")

# 会话上下文空闲多久回收（秒）。取配置值，保证与 idle_stop_minutes 对齐：
# 否则 context 先于休眠被回收，登录态丢失，"维护几小时后自然休息"就名存实亡。
CONTEXT_TTL = 3600


@dataclass
class SessionContext:
    key: str
    ctx: object
    page: object
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    logged_in: bool = False
    username: str = ""

    def touch(self) -> None:
        self.last_used = time.time()


class BrowserManager:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self._pw = None
        self._browser = None
        self._sessions: dict[str, SessionContext] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.firefox.launch(headless=self.cfg.headless)

    async def stop(self) -> None:
        async with self._lock:
            for s in list(self._sessions.values()):
                try:
                    await s.ctx.close()
                except Exception:
                    pass
            self._sessions.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def get(self, key: str = "default") -> SessionContext:
        """取得（或创建）某会话键的隔离上下文。"""
        async with self._lock:
            self._sweep_locked()
            s = self._sessions.get(key)
            if s is not None:
                s.touch()
                return s
            await self.start()
            ctx = await self._browser.new_context(
                user_agent=UA, locale="zh-CN",
                viewport={"width": 1400, "height": 950})
            page = await ctx.new_page()
            s = SessionContext(key=key, ctx=ctx, page=page)
            self._sessions[key] = s
            return s

    def _sweep_locked(self) -> None:
        now = time.time()
        for k, s in list(self._sessions.items()):
            ttl = self.cfg.context_ttl_minutes * 60
            if now - s.last_used > ttl:
                self._sessions.pop(k, None)
                try:
                    asyncio.create_task(s.ctx.close())
                except Exception:
                    pass

    async def reset(self, key: str = "default") -> None:
        """丢弃某会话的上下文（重新登录时用）。"""
        async with self._lock:
            s = self._sessions.pop(key, None)
        if s is not None:
            try:
                await s.ctx.close()
            except Exception:
                pass

    def session_count(self) -> int:
        return len(self._sessions)
