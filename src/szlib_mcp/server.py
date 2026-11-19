"""szlib-mcp 服务端：MCP over streamable-http + 一次性验证页。

一个进程、一个端口同时提供：
  /mcp            → MCP 端点（OWUI 直连）
  /verify/<token> → 用户浏览器里完成登录/验证码
  /healthz        → 健康检查
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from .browser import BrowserManager
from .config import get_config
from .session import AuthRequired
from .sources import duxiu
from .verify import VerificationGate

cfg = get_config()
server = MCPServer("szlib")
browsers = BrowserManager(cfg)
gate = VerificationGate(public_base_url=cfg.public_base_url, port=cfg.verify_port)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


async def _capture_captcha(page) -> bytes | None:
    """截取登录页验证码图片；未显示则返回 None。"""
    for sel in ("img#randImage1", "img[name='randImage']", "#auth1 img"):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return await el.screenshot()
        except Exception:
            continue
    return None


async def _is_logged_in(page, portal: str) -> bool:
    try:
        await page.goto(portal + "/", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        body = await page.content()
        return "欢迎您" in body
    except Exception:
        return False


async def _do_login(sess, creds: dict[str, str]) -> tuple[bool, str]:
    """在会话上下文里完成登录。返回 (成功, 说明)。"""
    page = sess.page
    await page.goto(f"{cfg.portal}/auth/login", timeout=90000,
                    wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    try:
        await page.fill("input#username", creds.get("username", ""))
        await page.fill("input#password", creds.get("password", ""))
        code = (creds.get("code") or "").strip()
        if code:
            try:
                await page.fill("input#code1", code)
            except Exception:
                pass
        await page.click("button[type='submit']")
    except Exception as e:
        return False, f"填写登录表单失败: {type(e).__name__}"

    await page.wait_for_timeout(4000)

    # 验证码区是否仍显示（说明还需要验证码）
    try:
        if await page.is_visible("#auth1") and not (creds.get("code") or "").strip():
            return False, "需要验证码"
    except Exception:
        pass

    for _ in range(10):
        await page.wait_for_timeout(3000)
        try:
            body = await page.content()
            if "欢迎您" in body:
                sess.logged_in = True
                sess.username = creds.get("username", "")
                return True, "登录成功"
        except Exception:
            pass
    return False, "登录后未检测到已登录状态"


async def ensure_session(key: str) -> tuple[Any, dict[str, Any]]:
    """确保该会话键已登录。返回 (sess, 附加信息)。

    需要人工时抛 AuthRequired，其 args[0] 是 dict：
      {"code": "...", "url": "...", "token": "..."}
    """
    sess = await browsers.get(key)

    # 上下文已登录则直接复用
    if sess.logged_in:
        return sess, {}

    if await _is_logged_in(sess.page, cfg.portal):
        sess.logged_in = True
        return sess, {}

    # 有 env 凭据 → 直接尝试
    if cfg.has_credentials:
        ok, msg = await _do_login(
            sess, {"username": cfg.username, "password": cfg.password})
        if ok:
            return sess, {}
        # 失败则走人工
        prompt = f"自动登录未成功（{msg}），请手动登录深圳图书馆"
    else:
        prompt = "请登录深圳图书馆（未配置 SZLIB_USERNAME / SZLIB_PASSWORD）"

    # 预取验证码图片（如果需要）
    image = None
    try:
        await sess.page.goto(f"{cfg.portal}/auth/login", timeout=60000,
                             wait_until="domcontentloaded")
        await sess.page.wait_for_timeout(1500)
        image = await _capture_captcha(sess.page)
    except Exception:
        pass

    qr_url = None
    if image is None:
        # 拿一次登录页，判断是否需要验证码；不需要则用户只填账号密码
        pass

    p = gate.create("login", prompt, image=image)
    raise AuthRequired({"code": "AWAITING_LOGIN", "url": gate.url_for(p),
                        "token": p.token, "expires_in": p.ttl})


async def _finish_login(key: str, token: str) -> tuple[Any, dict[str, Any]]:
    """等待用户在网页提交凭据并完成登录。"""
    values = await gate.wait(token, timeout=cfg.verify_ttl)
    if values is None:
        raise AuthRequired({"code": "LOGIN_TIMEOUT",
                            "message": "登录页已过期，请重新调用 login"})
    sess = await browsers.get(key)
    ok, msg = await _do_login(sess, values)
    if not ok:
        raise AuthRequired({"code": "LOGIN_FAILED", "message": msg})
    return sess, {"username": values.get("username", "")}


# ---------------- MCP 工具 ----------------

@server.tool()
async def search_literature(query: str, limit: int = 20,
                            session: str = "default") -> dict:
    """在读秀学术搜索里检索中文文献，返回题录（类型/标题/作者/出处/年份）。

    首次调用若未登录，会返回 AWAITING_LOGIN 和一个网址；
    请把该网址交给用户打开完成登录，然后用 login_token 再次调用。
    """
    sess = await browsers.get(session)
    if not sess.logged_in:
        await ensure_session(session)

    res = await duxiu.search(sess.page, cfg.portal, query, limit=limit,
                             db_id=cfg.db_ids["duxiu"])
    return res


@server.tool()
async def login(session: str = "default", login_token: str = "") -> dict:
    """完成登录。login_token 为空时返回待验证 URL；带上 token 则等待并完成登录。"""
    if not login_token:
        sess, _ = await ensure_session(session)
        return {"status": "ALREADY_LOGGED_IN" if sess.logged_in else "UNKNOWN"}
    sess, meta = await _finish_login(session, login_token)
    return {"status": "LOGGED_IN", **meta}


@server.tool()
async def login_status(session: str = "default") -> dict:
    """查看某会话的登录状态。"""
    sess = await browsers.get(session)
    return {"session": session, "logged_in": sess.logged_in,
            "username": sess.username, "contexts": browsers.session_count()}


@server.tool()
async def health() -> dict:
    """服务与配置状态（不含任何凭据）。"""
    return {
        "portal": cfg.portal,
        "public_base_url": cfg.public_base_url or "(未配置)",
        "verify_port": cfg.verify_port,
        "headless": cfg.headless,
        "env_credentials": cfg.has_credentials,
        "sessions": browsers.session_count(),
        "db_ids": cfg.db_ids,
    }


# ---------------- HTTP 应用 ----------------

def _allowed_hosts() -> list[str]:
    """MCP 的 DNS 重绑定保护会校验 Host。这里把对外域名与容器内名称都放行。

    - mcp.savorcare.com          （经 Traefik）
    - szlib-mcp / szlib-mcp:8765 （同一 Docker 网络内互调）
    - localhost / 127.0.0.1      （本机调试）
    - SZLIB_ALLOWED_HOSTS 追加    （逗号分隔，便于换域名）
    """
    from urllib.parse import urlparse

    hosts = ["localhost", "127.0.0.1", "szlib-mcp", "szlib-mcp:8765"]
    # 本地调试常用带端口形式
    hosts += [f"localhost:{cfg.verify_port}", f"127.0.0.1:{cfg.verify_port}"]
    if cfg.public_base_url:
        netloc = urlparse(cfg.public_base_url).netloc
        if netloc:
            hosts.append(netloc)
    extra = os.environ.get("SZLIB_ALLOWED_HOSTS", "")
    hosts += [h.strip() for h in extra.split(",") if h.strip()]
    # 去重保序
    seen, out = set(), []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def build_app():
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from starlette.routing import Route

    from urllib.parse import urlparse

    origins = []
    if cfg.public_base_url:
        p = urlparse(cfg.public_base_url)
        if p.scheme and p.netloc:
            origins.append(f"{p.scheme}://{p.netloc}")

    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_hosts(),
            allowed_origins=origins,
        )
    )

    async def verify_get(request):
        token = request.path_params["token"]
        status, body = gate.render_html(token)
        return HTMLResponse(body, status_code=status)

    async def verify_post(request):
        token = request.path_params["token"]
        try:
            form = await request.form()
        except Exception:
            form = {}
        values = {k: str(v) for k, v in (form.items() if form else [])}
        ok = gate.submit(token, values)
        return HTMLResponse(
            "<h2>已提交</h2><p>可以返回对话继续了。</p>" if ok
            else "<h2>无法提交</h2><p>链接已过期或已使用。</p>",
            status_code=200 if ok else 409)

    async def healthz(_request):
        return PlainTextResponse("szlib-mcp ok")

    # ---- 给 paper-search 这类上层工具用的简化 REST 接口 ----
    async def api_search(request):
        """POST {"query": str, "limit": int, "session": str}

        200 → {ok, total, by_type, items}
        202 → {status: "AWAITING_LOGIN", url, login_token}
        400/500 → {error}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        query = (body.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "query required"}, status_code=400)
        limit = int(body.get("limit") or 20)
        session = body.get("session") or "default"

        # 已发起登录但还没完成时，允许带 token 收尾
        login_token = (body.get("login_token") or "").strip()

        try:
            if login_token:
                await _finish_login(session, login_token)
            sess, _ = await ensure_session(session)
            res = await duxiu.search(sess.page, cfg.portal, query,
                                     limit=limit, db_id=cfg.db_ids["duxiu"])
            return JSONResponse(res)
        except AuthRequired as e:
            info = e.args[0] if e.args and isinstance(e.args[0], dict) else {}
            payload = dict(info)
            if "token" in payload:
                payload["login_token"] = payload.pop("token")
            payload.setdefault("status", "AWAITING_LOGIN")
            return JSONResponse(payload, status_code=202)
        except Exception as e:
            return JSONResponse(
                {"error": f"{type(e).__name__}: {str(e)[:200]}"}, status_code=500)

    async def api_status(_request):
        return JSONResponse({
            "sessions": browsers.session_count(),
            "env_credentials": cfg.has_credentials,
            "public_base_url": cfg.public_base_url or None,
        })

    app.routes.append(Route("/verify/{token}", verify_get, methods=["GET"]))
    app.routes.append(Route("/verify/{token}", verify_post, methods=["POST"]))
    app.routes.append(Route("/healthz", healthz, methods=["GET"]))
    app.routes.append(Route("/api/search", api_search, methods=["POST"]))
    app.routes.append(Route("/api/status", api_status, methods=["GET"]))
    return app


def main() -> None:
    import uvicorn

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    gate.bind_loop(loop)

    app = build_app()
    uvicorn.run(app, host="0.0.0.0", port=cfg.verify_port, log_level="info")


if __name__ == "__main__":
    main()
