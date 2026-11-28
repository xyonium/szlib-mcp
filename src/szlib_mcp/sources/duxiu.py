"""读秀（超星）学术搜索数据源。

已验证行为（2026-11）
- 入口：{portal}/go?id=2   检索框 #sw   提交 #searchbutton   无验证码
- 结果页结构：每个 `[class*="mpt_content"]` 是一个类型块

    DIV.name                 "期刊 相关约322321篇"      ← 类型 + 该类型总数
    A.double-line            标题
    BR BR
    SPAN.double-line         "邓振溪 小学科学 2023"      ← 元信息（作者 刊名 年）
    BR BR
    A / SPAN / ...           下一条

  因此按 A 与 SPAN 成对切分，就能得到单条记录，不会串行污染。
"""
from __future__ import annotations

import re
import time

from ..session import AuthRequired

MAX_WAIT = 40
# 等结果页新标签出现的时间上限（实测 ~1.3s 就出来）
SWITCH_WAIT = 15

# "期刊 相关约322321篇" / "图书 相关12067篇"
HEADER_RE = re.compile(r"([\u4e00-\u9fa5]{2,6})\s*相关约?\s*([\d,]+)\s*[篇种册条]")
YEAR_RE = re.compile(r"(19|20)\d{2}")

# 元信息里的作者/责任者标记
ROLE_RE = re.compile(r"(编著|主编|著|编|译|绘)")
DEGREE_RE = re.compile(r"(博士|硕士|学士)")

# 只有这些类型才有规范的「作者 + 出处 + 年」；其余（插图等）不拆，
# 否则会把书名当成作者。
BIBLIO_TYPES = {"期刊", "学位论文", "会议论文", "图书", "标准", "专利",
                "科技成果", "法律法规", "科技报告"}

# 结果排序优先级：越靠前越有文献价值
TYPE_PRIORITY = {
    "期刊": 0, "学位论文": 1, "会议论文": 2, "科技报告": 3,
    "标准": 4, "专利": 5, "科技成果": 6, "法律法规": 7, "图书": 8,
}


def _split_meta(meta: str, dtype: str) -> tuple[str, str, str]:
    """从元信息里尽力拆出 (authors, source, year)。

    读秀只给一段纯文本，没有分隔符，所以按类型做启发式：
      - 期刊:   "邓振溪 小学科学 2023"            → 作者… | 刊名 | 年
      - 图书:   "吴明平藏玲编著 …系列丛书 成都：…出版社 2021"
      - 学位:   "陈冠宇 硕士 军事科学院 2026"
      - 会议:   "邱雨琪孙夕平… 第四届中国石油物探学术年会 2026"
    拆不出来的部分留在 source/raw，不臆造。
    """
    raw = meta.strip()
    if not raw:
        return "", "", ""

    # 非文献类型（插图等）：整段当出处，不猜作者
    if dtype not in BIBLIO_TYPES:
        years = list(YEAR_RE.finditer(raw))
        year = years[-1].group(0) if years else ""
        src = raw[: years[-1].start()].strip() if years else raw
        return "", src, year

    # 年份：取最后一个 4 位年份
    years = list(YEAR_RE.finditer(raw))
    year = ""
    if years:
        year = years[-1].group(0)
        raw = raw[: years[-1].start()].strip()

    authors, source = "", ""
    if dtype == "学位论文":
        m = DEGREE_RE.search(raw)
        if m:
            authors = raw[: m.start()].strip()
            rest = raw[m.end():].strip()
            # 学校一般紧跟在学位后
            source = rest
        else:
            source = raw
    elif dtype == "图书":
        m = ROLE_RE.search(raw)
        if m:
            authors = raw[: m.end()].strip()
            source = raw[m.end():].strip()
        else:
            source = raw
    else:
        # 期刊 / 会议论文 / 其他：第一个空格前视为作者，其余为出处
        parts = raw.split(None, 1)
        if len(parts) == 2:
            authors, source = parts[0].strip(), parts[1].strip()
        else:
            source = raw
    return authors, source, year


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
    # 不再固定睡 4s：下面 wait_for_selector("#sw") 已经会等到检索框就绪，
    # 固定睡眠只是白等。留 600ms 让 VPN 注入的脚本先跑起来。
    await page.wait_for_timeout(600)

    if await _is_login_page(page):
        raise AuthRequired("会话未登录")

    try:
        await page.wait_for_selector("#sw", timeout=25000)
    except Exception:
        out["error"] = "SEARCH_BOX_NOT_FOUND"
        return out

    before = set(page.context.pages)
    await page.fill("#sw", query)
    await page.click("#searchbutton", timeout=15000)

    # 读秀把结果页开在**新标签**里（实测点击后 ~1.3s 出现，~5.6s 渲染出结果块）。
    # 旧实现一直在原页面上轮询 mpt_content，必然查不到，只能干等到 MAX_WAIT
    # 结束再切标签——这是单次检索耗时 ~50s 的真正原因，不是上游慢。
    result_page = None
    switch_deadline = time.time() + SWITCH_WAIT
    while time.time() < switch_deadline:
        await page.wait_for_timeout(400)
        new_pages = [p for p in page.context.pages if p not in before]
        if new_pages:
            result_page = new_pages[-1]
            break
    if result_page is None:
        # 没开新标签：有的入口是原地跳转，仍在当前页等
        result_page = page

    deadline = time.time() + MAX_WAIT
    while time.time() < deadline:
        try:
            n = await result_page.eval_on_selector_all(
                "[class*='mpt_content']", "e => e.length")
        except Exception:
            n = 0
        if n > 0:
            break
        await result_page.wait_for_timeout(600)

    page = result_page
    body = ""
    try:
        body = await page.inner_text("body")
    except Exception:
        pass

    # 总结果数
    m = re.search(r"相关结果\s*([\d,]+)\s*条", body)
    if m:
        out["total"] = int(m.group(1).replace(",", ""))

    # 按类型块解析题录
    blocks = []
    try:
        blocks = await page.evaluate(
            """() => {
                const bs = [...document.querySelectorAll("[class*='mpt_content']")];
                return bs.map(b => {
                    const nameEl = b.querySelector('.name');
                    const header = nameEl ? (nameEl.innerText || '').trim() : '';
                    const items = [];
                    let cur = null;
                    for (const c of b.children) {
                        if (c.tagName === 'A') {
                            if (cur) items.push(cur);
                            cur = {title: (c.innerText || '').trim(), meta: ''};
                        } else if (c.tagName === 'SPAN' && cur) {
                            cur.meta = (c.innerText || '').replace(/\\s+/g, ' ').trim();
                            items.push(cur);
                            cur = null;
                        }
                    }
                    if (cur) items.push(cur);
                    return {header, items};
                });
            }"""
        )
    except Exception as e:
        out["error"] = f"PARSE_FAILED: {type(e).__name__}"
        return out

    # 先收集所有块，再按类型优先级排序后截断：
    # 读秀的类型块顺序是随机的（图书/插图/期刊…），直接按 DOM 顺序截断
    # 会让本来最该拿到的期刊条目进不来。
    parsed = []
    cap = max(limit * 8, 60)
    for b in blocks:
        header = (b.get("header") or "").strip()
        hm = HEADER_RE.search(header)
        dtype, dtype_total = "", None
        if hm:
            dtype = hm.group(1)
            dtype_total = int(hm.group(2).replace(",", ""))
            out["by_type"][dtype] = dtype_total
        for it in (b.get("items") or []):
            title = (it.get("title") or "").strip()
            if not title:
                continue
            meta = (it.get("meta") or "").strip()
            authors, src, year = _split_meta(meta, dtype)
            parsed.append({
                "type": dtype,
                "title": title,
                "authors": authors,
                "source": src,
                "year": year,
                "meta": meta,
                # 向后兼容：旧版把整段容器文本放在 context，
                # 现在只放本条自己的元信息，不再串行污染。
                "context": meta,
            })
            if len(parsed) >= cap:
                break
        if len(parsed) >= cap:
            break

    parsed.sort(key=lambda x: TYPE_PRIORITY.get(x["type"], 99))
    parsed = parsed[:limit]

    if out["total"] is None and out["by_type"]:
        out["total"] = sum(out["by_type"].values())

    out["items"] = parsed
    out["ok"] = bool(parsed) or out["total"] is not None
    return out
