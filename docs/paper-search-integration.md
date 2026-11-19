# 把 szlib 接入 paper-search 作为一个源

szlib-mcp 跑在独立容器里（需要 Firefox），paper-search 跑在 mcpo 的 uvx 子进程里（无浏览器）。
两者通过一个**极简 REST 接口**对接，paper-search 侧只增加一个 HTTP 客户端，零浏览器依赖。

## 接口

```
POST {SZLIB_URL}/api/search
  {"query": "...", "limit": 20, "session": "<可选，默认 default>"}

200 → {"ok":true,"source":"duxiu","total":144829,
       "by_type":{"期刊":321789,"学位论文":171824},
       "items":[{"type":"期刊","title":"…","author":"…","source":"…","year":"2023"}]}

202 → {"status":"AWAITING_LOGIN","url":"https://mcp.example.com/szlib/verify/xxx",
       "login_token":"xxx","expires_in":300}

400 → {"error":"query required"}
500 → {"error":"…"}
```

**202 是关键**：说明需要用户在浏览器里登录一次。上层工具应把 `url` 原样返回给用户，
用户完成后带 `login_token` 再调一次。

## paper-search 侧源码

放到 `src/paper_search_mcp/sources/szlib.py`：

```python
"""深圳图书馆 / 读秀 —— 通过 szlib-mcp 的 REST 接口。"""
from __future__ import annotations

import os

import httpx

SZLIB_URL = os.environ.get("SZLIB_URL", "http://szlib-mcp:8765").rstrip("/")
TIMEOUT = 120


class SzlibLoginRequired(RuntimeError):
    """需要用户打开 url 完成登录。"""

    def __init__(self, url: str, login_token: str, expires_in: int):
        self.url = url
        self.login_token = login_token
        self.expires_in = expires_in
        super().__init__(f"需要登录: {url}")


async def search(query: str, limit: int = 20, session: str = "default",
                 login_token: str = "") -> dict:
    payload = {"query": query, "limit": limit, "session": session}
    if login_token:
        payload["login_token"] = login_token

    async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
        r = await cli.post(f"{SZLIB_URL}/api/search", json=payload)

    if r.status_code == 202:
        d = r.json()
        raise SzlibLoginRequired(
            d.get("url", ""), d.get("login_token", ""), d.get("expires_in", 300))
    if r.status_code != 200:
        raise RuntimeError(f"szlib API {r.status_code}: {r.text[:200]}")
    return r.json()
```

## 在论文检索工具里包装

paper-search 的入口工具（OWUI 那个 `tool.py` 或 MCP 的 `search` 工具）里加一段：

```python
from .sources import szlib

async def _search_szlib(query: str, limit: int, session: str) -> dict:
    try:
        return await szlib.search(query, limit, session)
    except szlib.SzlibLoginRequired as e:
        return {
            "source": "szlib",
            "ok": False,
            "notice": (
                f"深圳图书馆需要登录，请打开下面链接完成（{e.expires_in // 60} 分钟内有效）：\n"
                f"{e.url}\n完成后重新发起同样的检索即可。"
            ),
            "login_url": e.url,
            "login_token": e.login_token,
        }
```

## 部署要点

- paper-search 的容器需要能访问 `http://szlib-mcp:8765`（同一 Docker 网络）
- 加环境变量 `SZLIB_URL=http://szlib-mcp:8765`
- **不要把 szlib 放进 mcpo 的 `config.json`**：它不是 stdio 服务，走 HTTP

## 收益

| | 独立调用 | 接入 paper-search |
|---|---|---|
| 中文覆盖 | 有 | 有 |
| 与英文源合并去重 | ❌ | ✅ |
| 用户只看到一套参数 | ❌ | ✅ |
| 维护量 | 两个工具面 | 一个工具面 + 一个适配器（约 60 行） |

---

## 实现状态（已接入 paper-search v2.9.13，2026-11-19）

上面"paper-search 侧源码"那节是按**已退役的 paper-search-mcp** 布局写的（`src/paper_search_mcp/sources/`）。
现架构是单文件 OWUI 工具 `tool.py` + 自托管 papers-service，实际接入落在 **`tool.py` 的直连源**里：

| 项 | 实现 |
|---|---|
| 适配器 | `Tools._szlib_search(query, limit, session, login_token, __user__)`，`requests.post` 调 `{base}/api/search` |
| 地址 | Valves `szlib_url`（默认 `http://szlib-mcp:8765`）→ UserValves → 环境变量 `SZLIB_URL` |
| 源注册 | `DIRECT_SOURCES` 增加 `"szlib"`；**只认显式 `sources="szlib"`**，不进 `all`、不进 `default_sources` |
| 202 处理 | 三种 `code`：`AWAITING_LOGIN`（带 url/token）→ 返回 `login_required.login_url`；`LOGIN_TIMEOUT`/`LOGIN_FAILED`（无 url）→ 提示"不带 token 重新发起拿新链接" |
| 相对 url | 未配 `SZLIB_PUBLIC_BASE_URL` 时服务端回 `/verify/xxx`，客户端自动补 `szlib_url` 的 host |
| 长轮询 | 带 `login_token` 的请求服务端阻塞等验证（默认 300s）→ 客户端超时设 **330s**；不带 token 设 180s |
| 200 + error | 如 `SEARCH_BOX_NOT_FOUND` 视为上游页面漂移 → 进 `errors` 与 `admin_alert`，不吞成 0 命中 |
| 字段映射 | `items[].title → title`；`items[].year → published_date`；`type`+`context → abstract`（`"[期刊] 某学报, 2023, (4)"`）；`paper_id = "szlib:" + sha1(title|context|year)[:16]`；无 DOI/全文 |
| 重试参数 | `szlib_session`（默认 `default`）、`szlib_login_token`（一次性，登录后回传） |
| 测试 | `paper-search/tests/test_szlib.py`（10 例：映射/稳定 id/202 三态/相对 url/长超时/500/200-error/all 模式不触发） |

部署侧仍需：`szlib-mcp` 容器与 `open-webui` 同网络；`SZLIB_PUBLIC_BASE_URL` 指向用户可访问的
`https://mcp.example.com/szlib`（否则 202 里只有相对路径，用户点不开）。
