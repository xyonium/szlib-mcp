"""会话生命周期配置的约束测试（离线）。

背景：曾经 context TTL 硬编码 60 分钟，而 idle_stop 是 240 分钟。
context 先被回收 → 登录态丢失 → 下次请求要重新登录 12-16s，
"维护几小时后自然休息"这个设计意图根本没生效。
"""
from __future__ import annotations

import os

import pytest

from szlib_mcp.config import Config


def _cfg(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return Config.from_env()


def test_context_ttl_defaults_to_idle_stop(monkeypatch):
    cfg = _cfg(monkeypatch, SZLIB_IDLE_STOP_MINUTES=180)
    monkeypatch.delenv("SZLIB_CONTEXT_TTL_MINUTES", raising=False)
    cfg = Config.from_env()
    assert cfg.context_ttl_minutes == 180


def test_context_ttl_cannot_be_shorter_than_idle_stop(monkeypatch):
    monkeypatch.setenv("SZLIB_IDLE_STOP_MINUTES", "240")
    monkeypatch.setenv("SZLIB_CONTEXT_TTL_MINUTES", "30")
    cfg = Config.from_env()
    # 显式配置更短时也要被抬到 idle_stop，否则登录态撑不到休眠
    assert cfg.context_ttl_minutes == 240


def test_warmup_and_serialize_flags(monkeypatch):
    monkeypatch.setenv("SZLIB_WARMUP_ON_START", "false")
    monkeypatch.setenv("SZLIB_SERIALIZE_SEARCHES", "false")
    cfg = Config.from_env()
    assert cfg.warmup_on_start is False
    assert cfg.serialize_searches is False


def test_defaults_are_conservative(monkeypatch):
    for k in ("SZLIB_WARMUP_ON_START", "SZLIB_SERIALIZE_SEARCHES"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env()
    # 默认开启预热与串行化：串行化是反封禁的关键
    assert cfg.warmup_on_start is True
    assert cfg.serialize_searches is True
    assert cfg.min_request_interval >= 3.0
