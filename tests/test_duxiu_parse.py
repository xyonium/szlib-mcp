"""读秀题录解析的单元测试（离线，不联网）。

锁住两件事：
1. 元信息拆分（作者/出处/年份）对各类文献都正确；
2. 非文献类型（插图）不把书名误当作者。
"""
from __future__ import annotations

import pytest

from szlib_mcp.sources.duxiu import (
    BIBLIO_TYPES,
    HEADER_RE,
    TYPE_PRIORITY,
    _split_meta,
)


@pytest.mark.parametrize(
    "meta,dtype,expect",
    [
        ("邓振溪 小学科学 2023", "期刊", ("邓振溪", "小学科学", "2023")),
        ("本刊编辑部 小学教学研究 2024", "期刊", ("本刊编辑部", "小学教学研究", "2024")),
        ("陈冠宇 硕士 军事科学院 2026", "学位论文", ("陈冠宇", "军事科学院", "2026")),
        ("林书凝 博士 华东师范大学 2025", "学位论文", ("林书凝", "华东师范大学", "2025")),
        (
            "邱雨琪孙夕平于永才 第四届中国石油物探学术年会 2026",
            "会议论文",
            ("邱雨琪孙夕平于永才", "第四届中国石油物探学术年会", "2026"),
        ),
        (
            "吴明平藏玲编著 基于大概念的深度教学系列丛书 成都：电子科技大学出版社 2021",
            "图书",
            ("吴明平藏玲编著", "基于大概念的深度教学系列丛书 成都：电子科技大学出版社", "2021"),
        ),
    ],
)
def test_split_meta_biblio(meta, dtype, expect):
    assert _split_meta(meta, dtype) == expect


def test_split_meta_non_biblio_keeps_authors_empty():
    """插图块给的是书名，不能当作者。"""
    authors, source, year = _split_meta("机器学习实践 基于Python进行数据分析 2022", "插图")
    assert authors == ""
    assert source == "机器学习实践 基于Python进行数据分析"
    assert year == "2022"


def test_split_meta_empty():
    assert _split_meta("", "期刊") == ("", "", "")


def test_split_meta_without_year():
    authors, source, year = _split_meta("某人 某刊", "期刊")
    assert year == ""
    assert authors == "某人"
    assert source == "某刊"


@pytest.mark.parametrize(
    "header,expect",
    [
        ("期刊 相关约322321篇", ("期刊", 322321)),
        ("图书 相关12067篇", ("图书", 12067)),
        ("学位论文 相关约171824篇", ("学位论文", 171824)),
    ],
)
def test_header_regex(header, expect):
    m = HEADER_RE.search(header)
    assert m is not None
    assert (m.group(1), int(m.group(2).replace(",", ""))) == expect


def test_type_priority_puts_journal_first():
    """期刊应排在学位论文、图书之前，未知类型排最后。"""
    order = sorted(
        ["插图", "图书", "学位论文", "期刊", "未知类型"],
        key=lambda t: TYPE_PRIORITY.get(t, 99),
    )
    assert order[0] == "期刊"
    assert order[-1] == "未知类型"


def test_biblio_types_do_not_include_illustration():
    assert "插图" not in BIBLIO_TYPES
