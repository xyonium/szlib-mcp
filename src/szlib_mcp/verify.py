"""远程验证门：把验证码/登录表单变成一次性 URL，让用户在自己浏览器完成。

支持三种形态
------------
- kind="code"   单字段（验证码）
- kind="login"  多字段（读者证号 / 密码 / 验证码），凭据只留在服务端内存
- kind="slider" 滑块：提示用远程桌面完成（预留）

安全
----
- token 随机、一次性、默认 5 分钟过期
- 页面不写入日志、不缓存、不暴露任何已存凭据
- 单次提交；重复提交 409
"""
from __future__ import annotations

import asyncio
import base64
import html
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote_plus

DEFAULT_TTL = 300

# 每种 kind 要展示的字段：(name, label, type, placeholder)
FIELDS: dict[str, list[tuple[str, str, str, str]]] = {
    "code": [("code", "验证码", "text", "输入图中字符")],
    "login": [
        ("username", "读者证号", "text", "读者证号或身份证号"),
        ("password", "密码", "password", "网上登录强密码"),
        ("code", "验证码", "text", "输入图中字符（若显示）"),
    ],
    "slider": [("code", "", "hidden", "")],
}


@dataclass
class Pending:
    token: str
    kind: str
    prompt: str
    image: bytes | None = None
    created: float = field(default_factory=time.time)
    ttl: int = DEFAULT_TTL
    result: dict[str, str] | None = None
    event: asyncio.Event | None = None

    @property
    def expired(self) -> bool:
        return time.time() - self.created > self.ttl


class VerificationGate:
    def __init__(self, public_base_url: str = "", port: int = 8765):
        self.public_base_url = public_base_url.rstrip("/")
        self.port = port
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------- 生命周期 ----------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._server is not None:
            return
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                gate._handle_get(self)

            def do_POST(self):
                gate._handle_post(self)

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None

    # ---------- 创建 / 提交 ----------
    def create(self, kind: str, prompt: str, image: bytes | None = None,
               ttl: int = DEFAULT_TTL) -> Pending:
        token = secrets.token_urlsafe(24)
        ev = asyncio.Event() if self._loop is not None else None
        p = Pending(token=token, kind=kind, prompt=prompt, image=image,
                    ttl=ttl, event=ev)
        with self._lock:
            self._pending[token] = p
        return p

    def url_for(self, p: Pending) -> str:
        path = f"/verify/{p.token}"
        return f"{self.public_base_url}{path}" if self.public_base_url else path

    def submit(self, token: str, values: dict[str, str]) -> bool:
        with self._lock:
            p = self._pending.get(token)
            if not p or p.expired or p.result is not None:
                return False
            p.result = values
            ev = p.event
        if ev is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(ev.set)
            except Exception:
                pass
        return True

    async def wait(self, token: str, timeout: float | None = None) -> dict[str, str] | None:
        with self._lock:
            p = self._pending.get(token)
        if not p:
            return None
        if p.result is not None:
            self._pending.pop(token, None)
            return p.result
        if p.event is None:
            return None
        try:
            await asyncio.wait_for(p.event.wait(), timeout=timeout or p.ttl)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._lock:
                self._pending.pop(token, None)
        return p.result

    def cancel(self, token: str) -> None:
        with self._lock:
            self._pending.pop(token, None)

    def sweep(self) -> int:
        n = 0
        with self._lock:
            for t, p in list(self._pending.items()):
                if p.expired:
                    self._pending.pop(t, None)
                    n += 1
        return n

    # ---------- HTTP ----------
    def render_html(self, token: str) -> tuple[int, str]:
        """渲染验证页。返回 (状态码, HTML)。供内部 HTTP 与 Starlette 复用。"""
        with self._lock:
            p = self._pending.get(token)
        if not p:
            return 404, "<h2>验证请求不存在或已完成</h2>"
        if p.expired:
            return 410, "<h2>验证已过期，请重新发起请求</h2>"
        if p.result is not None:
            return 200, "<h2>已提交</h2><p>可以返回对话继续了。</p>"

        img_html = ""
        if p.image:
            b64 = base64.b64encode(p.image).decode()
            img_html = (f'<div class="cap"><img src="data:image/png;base64,{b64}" '
                        f'alt="验证码"></div>')
        elif p.kind == "slider":
            img_html = ('<p class="hint">该验证需要在远程桌面中拖动滑块完成。</p>')

        rows = []
        for name, label, ftype, ph in FIELDS.get(p.kind, FIELDS["code"]):
            if ftype == "hidden":
                continue
            req = ' required' if name != "code" or p.kind == "code" else ''
            rows.append(
                f'<label>{html.escape(label)}'
                f'<input type="{ftype}" name="{name}" placeholder="{html.escape(ph)}"'
                f' autocomplete="off"{req}></label>')
        form_fields = "\n".join(rows)

        body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>需要验证 · szlib</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;max-width:520px;margin:56px auto;
      padding:0 20px;color:#111}}
 .card{{border:1px solid #e5e7eb;border-radius:14px;padding:26px;
       box-shadow:0 1px 3px rgba(0,0,0,.05)}}
 label{{display:block;margin:14px 0;font-size:14px;color:#374151}}
 input{{display:block;width:100%;box-sizing:border-box;margin-top:6px;
       font-size:17px;padding:10px 12px;border:1px solid #d1d5db;border-radius:9px}}
 button{{margin-top:8px;font-size:16px;padding:11px 22px;border:0;border-radius:9px;
        background:#2563eb;color:#fff;cursor:pointer;width:100%}}
 .cap img{{width:240px;height:80px;image-rendering:pixelated;border:1px solid #d1d5db;
          border-radius:8px;background:#fff}}
 .hint{{color:#6b7280;font-size:13px;margin-top:16px}}
</style></head><body>
<div class="card">
  <h2 style="margin:0 0 6px">需要完成验证</h2>
  <p style="color:#4b5563;margin:0 0 8px">{html.escape(p.prompt)}</p>
  {img_html}
  <form method="POST" action="/verify/{token}">
    {form_fields}
    <button type="submit">提交</button>
  </form>
  <p class="hint">本页一次性使用，{p.ttl // 60} 分钟内有效。
     凭据仅用于本次登录，不落盘、不记日志。</p>
</div></body></html>"""
        return 200, body

    def status_of(self, token: str) -> str:
        with self._lock:
            p = self._pending.get(token)
        if not p:
            return "missing"
        if p.expired:
            return "expired"
        if p.result is not None:
            return "submitted"
        return "pending"

    def _handle_get(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path.split("?")[0]
        if not path.startswith("/verify/"):
            if path in ("/healthz", "/"):
                self._send(h, 200, "text/plain; charset=utf-8", b"szlib-mcp ok")
            else:
                self._send(h, 404, "text/plain; charset=utf-8", b"not found")
            return
        token = path[len("/verify/"):].strip("/")
        status, body = self.render_html(token)
        self._send_html(h, status, body)

    def _handle_post(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path.split("?")[0]
        if not path.startswith("/verify/"):
            self._send(h, 404, "text/plain; charset=utf-8", b"not found")
            return
        token = path[len("/verify/"):].strip("/")
        try:
            length = int(h.headers.get("Content-Length", "0"))
            raw = h.rfile.read(length).decode("utf-8", "ignore")
        except Exception:
            raw = ""
        parsed = parse_qs(raw)
        values = {k: unquote_plus(v[0]) for k, v in parsed.items() if v}
        ok = self.submit(token, values)
        self._send_html(h, 200 if ok else 409,
                        "<h2>已提交</h2><p>可以返回对话继续了。</p>" if ok
                        else "<h2>无法提交</h2><p>链接已过期或已使用。</p>")

    @staticmethod
    def _send(h, status: int, ctype: str, body: bytes) -> None:
        try:
            h.send_response(status)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Cache-Control", "no-store")
            h.send_header("Referrer-Policy", "no-referrer")
            h.end_headers()
            h.wfile.write(body)
        except Exception:
            pass

    def _send_html(self, h, status: int, body: str) -> None:
        self._send(h, status, "text/html; charset=utf-8", body.encode("utf-8"))
