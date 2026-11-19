"""命令行入口：check / keepalive / login。"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_config
from .session import AuthRequired, SessionManager


def cmd_check(_args) -> int:
    s = SessionManager()
    valid = s.check_http()
    idle = s.idle_seconds()
    dormant = s.is_dormant()
    idle_txt = "无活动记录" if idle == float("inf") else f"{idle/60:.1f} 分钟"
    print(f"会话: {'有效 ✅' if valid else '失效 ❌'}")
    print(f"空闲: {idle_txt}（超过 {s.cfg.idle_stop_minutes} 分钟进入休眠）")
    print(f"休眠: {'是' if dormant else '否'}")
    print(f"凭据: {'已配置' if s.cfg.has_credentials else '未配置'}")
    print(f"profile: {s.cfg.profile_dir}")
    return 0 if valid else 1


def cmd_keepalive(args) -> int:
    s = SessionManager()
    print(f"保活启动：空闲 {s.cfg.idle_stop_minutes} 分钟后自动休眠，"
          f"间隔 {s.cfg.keepalive_min_minutes}-{s.cfg.keepalive_max_minutes} 分钟随机")
    if args.once:
        valid = s.check_http()
        print(f"单次检查: {'有效 ✅' if valid else '失效 ❌'}")
        return 0 if valid else 1
    try:
        asyncio.run(s.keepalive_loop())
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def cmd_login(args) -> int:
    s = SessionManager()
    try:
        ok = asyncio.run(s.login(headful=args.headful))
        print(f"登录{'成功 ✅' if ok else '失败 ❌'}")
        return 0 if ok else 1
    except AuthRequired as e:
        print(f"需要人工介入: {e}")
        return 2


def main() -> int:
    ap = argparse.ArgumentParser(prog="szlib-mcp")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("check", help="检查会话状态")
    p1.set_defaults(func=cmd_check)

    p2 = sub.add_parser("keepalive", help="运行保活循环")
    p2.add_argument("--once", action="store_true")
    p2.set_defaults(func=cmd_keepalive)

    p3 = sub.add_parser("login", help="用 env 凭据登录")
    p3.add_argument("--headful", action="store_true", help="弹出窗口（需要过验证码时）")
    p3.set_defaults(func=cmd_login)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
