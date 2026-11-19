"""深图会话管理：状态检测、按需登录、空闲自动停止的保活。

设计要点
--------
1. 保活不是 24 小时不停。空闲超过 IDLE_STOP_MINUTES 就进入"休眠"，
   不再访问网络；等下一次真实请求到来时才恢复。这样账号行为更像真人。
2. 保活间隔取 [MIN, MAX] 的随机值，避免固定节奏。
3. 会话失效时：有凭据则自动重登；重登若要求验证码，抛 AuthRequired，
   由上层决定是否弹有头窗口让人处理。
"""
from __future__ import annotations

import json
import random
import shutil
import sqlite3
import ssl
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config, get_config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")


class AuthRequired(RuntimeError):
    """需要人工介入（验证码 / 凭据缺失 / 登录失败）。"""


@dataclass
class State:
    last_activity: float = 0.0
    last_check: float = 0.0
    session_valid: bool = False
    last_login: float = 0.0


class SessionManager:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.cfg.state_dir / "state.json"
        self.state = self._load_state()

    # ---------- 状态持久化 ----------
    def _load_state(self) -> State:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            return State(**{k: v for k, v in raw.items() if k in State.__annotations__})
        except Exception:
            return State()

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    # ---------- 活跃度 ----------
    def touch(self) -> None:
        """每次真实请求调用，用于判断是否该继续保活。"""
        self.state.last_activity = time.time()
        self._save_state()

    def idle_seconds(self) -> float:
        if not self.state.last_activity:
            return float("inf")
        return time.time() - self.state.last_activity

    def is_dormant(self) -> bool:
        return self.idle_seconds() > self.cfg.idle_stop_minutes * 60

    # ---------- cookie 读取 ----------
    def read_cookies(self) -> dict[str, str]:
        """从持久化 profile 的 cookies.sqlite 读取 szlib 相关 cookie。"""
        src = self.cfg.profile_dir / "cookies.sqlite"
        if not src.exists():
            return {}
        tmpdir = tempfile.mkdtemp()
        tmp = Path(tmpdir) / "c.sqlite"
        try:
            shutil.copy2(src, tmp)
            for suffix in ("-wal", "-shm"):
                s = Path(str(src) + suffix)
                if s.exists():
                    shutil.copy2(s, Path(str(tmp) + suffix))
            con = sqlite3.connect(tmp)
            rows = con.execute(
                "select name, value from moz_cookies where host like '%szlib%'"
            ).fetchall()
            con.close()
            return {n: v for n, v in rows}
        except Exception:
            return {}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.read_cookies().items())

    # ---------- 会话检测 ----------
    def check_http(self, persist: bool = True) -> bool:
        """轻量 HTTP 检测：GET 门户首页，看是否出现「欢迎您」。"""
        ck = self._cookie_header()
        if not ck:
            if persist:
                self.state.session_valid = False
                self.state.last_check = time.time()
                self._save_state()
            return False
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            req = urllib.request.Request(
                self.cfg.portal + "/",
                headers={"User-Agent": UA, "Cookie": ck,
                         "Accept-Language": "zh-CN,zh;q=0.9"})
            body = urllib.request.urlopen(req, timeout=25, context=ctx).read()
            valid = "欢迎您".encode() in body
        except Exception:
            valid = False
        if persist:
            self.state.session_valid = valid
            self.state.last_check = time.time()
            self._save_state()
        return valid

    # ---------- 自动登录 ----------
    async def login(self, headful: bool = False, wait_seconds: int = 180) -> bool:
        """用 env 凭据登录。若出现验证码则抛 AuthRequired。

        返回 True 表示登录成功。
        """
        if not self.cfg.has_credentials:
            raise AuthRequired("未配置 SZLIB_USERNAME / SZLIB_PASSWORD")

        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            ctx = await p.firefox.launch_persistent_context(
                str(self.cfg.profile_dir),
                headless=not headful,
                locale="zh-CN",
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(f"{self.cfg.portal}/auth/login",
                                timeout=90000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)

                if "欢迎您" in await page.content():
                    return True

                await page.fill("input#username", self.cfg.username)
                await page.fill("input#password", self.cfg.password)
                await page.click("button[type='submit']")
                await page.wait_for_timeout(4000)

                # 验证码区是否被显示出来
                captcha_visible = False
                try:
                    captcha_visible = await page.is_visible("#auth1")
                except Exception:
                    pass

                if captcha_visible:
                    raise AuthRequired(
                        "登录要求图形验证码：请在弹出的窗口内手动完成，"
                        "或设置 SZLIB_HEADLESS=false 后重试")

                for _ in range(wait_seconds // 3):
                    await page.wait_for_timeout(3000)
                    try:
                        if "欢迎您" in await page.content():
                            self.state.last_login = time.time()
                            self.state.session_valid = True
                            self._save_state()
                            return True
                    except Exception:
                        pass
                return False
            finally:
                await ctx.close()

    async def ensure(self, headful: bool = False) -> bool:
        """确保会话可用：先 HTTP 检测，失效则尝试登录。"""
        if self.check_http():
            return True
        if not self.cfg.has_credentials:
            raise AuthRequired("会话已失效且未配置凭据，需手动登录一次")
        ok = await self.login(headful=headful)
        if not ok:
            raise AuthRequired("自动登录未成功，需要人工介入")
        return True

    # ---------- 保活 ----------
    async def keepalive_loop(self, sleep=None) -> None:
        """空闲时自动休眠、有活动时低频保活。"""
        import asyncio

        _sleep = sleep or asyncio.sleep
        while True:
            if self.is_dormant():
                # 休眠：不访问网络，只等下一轮判断
                await _sleep(600)
                continue

            valid = self.check_http()
            if not valid:
                if self.cfg.has_credentials:
                    try:
                        await self.login()
                    except AuthRequired:
                        pass
                    except Exception:
                        pass
                # 登录失败也不再空转，交给下一次真实请求触发
                await _sleep(600)
                continue

            interval = random.uniform(self.cfg.keepalive_min_minutes,
                                      self.cfg.keepalive_max_minutes) * 60
            await _sleep(interval)
