"""读秀（超星）学术搜索数据源。

已验证行为（2026-11）
- 入口：{portal}/go?id=2
- 检索框：#sw   提交：#searchbutton
- 无验证码
- 题录字段：类型 / 标题 / 作者 / 出处 / 年份 / 各类型总量
"""
from __future__ import annotations

import re
import time

from ..session import AuthRequired

RESULT_LINK = "a[href*='gozhizhen']"
MAX_WAIT = 40

TYPE_RE = re.compile(r"(期刊|学位论文|会议论文|图书|专利|标准|科技成果)\s*相关约\s*([\d,]+)\s*[篇种条]")


async def _is_login_page(page) -> bool:
    try:
        if "auth" in page.url or "userLogin" in page.url:
            return True
        body = await page.content()
        return "欢迎您" not in body and 'id="username"' in body
    except Exception:
        return False


async def search(page, portal: str, query: str, limit: int = 20,
                 db_id: int = 2) -> dict:
    """在读秀执行一次检索。page 必须来自已登录的隔离 Context。"""
    out = {"source": "duxiu", "query": query, "ok": False,
           "total": None, "by_type": {}, "items": [], "error": None}

    await page.goto(f"{portal}/go?id={db_id}", timeout=90000,
                    wait_until="domcontentloaded")
    try:
        await page.wait_for_url("**/vpn/**", timeout=45000)
    except Exception:
        pass
    await page.wait_for_timeout(4000)

    if await _is_login_page(page):
        raise AuthRequired("会话未登录")

    try:
        await page.wait_for_selector("#sw", timeout=25000)
    except Exception:
        out["error"] = "SEARCH_BOX_NOT_FOUND"
        return out

    await page.fill("#sw", query)
    await page.click("#searchbutton", timeout=15000)

    # 等待结果
    deadline = time.time() + MAX_WAIT
    while time.time() < deadline:
        await page.wait_for_timeout(2500)
        try:
            n = await page.eval_on_selector_all(RESULT_LINK, "e => e.length")
        except Exception:
            n = 0
        if n > 0:
            break

    # 结果可能开在新窗口
    body = ""
    for pg in page.context.pages:
        try:
            t = await pg.inner_text("body")
            if len(t) > len(body):
                body, page = t, pg
        except Exception:
            continue

    # 各类型总量
    for m in TYPE_RE.finditer(body):
        name, num = m.group(1), int(m.group(2).replace(",", ""))
        out["by_type"][name] = num
    m = re.search(r"相关结果\s*([\d,]+)\s*条", body)
    if m:
        out["total"] = int(m.group(1).replace(",", ""))
    if out["total"] is None and out["by_type"]:
        out["total"] = sum(out["by_type"].values())

    # 题录
    try:
        items = await page.evaluate(
            """(max) => {
                const links=[...document.querySelectorAll("a[href*='gozhizhen']")];
                const out=[];
                const seen=new Set();
                for(const a of links){
                    const title=(a.innerText||'').trim();
                    if(!title || seen.has(title)) continue;
                    seen.add(title);
                    let box=a.parentElement;
                    for(let i=0;i<4 && box;i++){
                        const t=(box.innerText||'').replace(/\\s+/g,' ').trim();
                        if(t.length>40 && t.length<420){ break; }
                        box=box.parentElement;
                    }
                    const ctx=(box? box.innerText:'').replace(/\\s+/g,' ').trim();
                    out.push({title:title.slice(0,200), context:ctx.slice(0,400)});
                    if(out.length>=max) break;
                }
                return out;
            }""", limit)
    except Exception:
        items = []

    parsed = []
    for it in items:
        ctx = it["context"]
        year = None
        my = re.search(r"\b(19|20)\d{2}\b", ctx)
        if my:
            year = my.group(0)
        ptype = ""
        for t in ("学位论文", "会议论文", "期刊", "图书", "专利", "标准"):
            if t in ctx:
                ptype = t
                break
        parsed.append({
            "type": ptype,
            "title": it["title"],
            "context": ctx,
            "year": year,
        })

    out["items"] = parsed
    out["ok"] = bool(parsed) or out["total"] is not None
    return out
