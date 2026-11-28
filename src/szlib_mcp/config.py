"""环境变量配置。所有可调项集中在这里，便于测试与复用。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load_dotenv(path: str | Path | None = None) -> None:
    """极简 .env 加载：KEY=VALUE，支持 # 注释与引号剥离。

    已存在的真实环境变量优先，不会被 .env 覆盖。
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parents[2] / ".env")

    for p in candidates:
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
            return
        except Exception:
            continue


@dataclass
class Config:
    username: str = ""
    password: str = ""
    profile_dir: Path = Path("./data/profile")
    headless: bool = True

    idle_stop_minutes: int = 240
    keepalive_min_minutes: int = 30
    keepalive_max_minutes: int = 50

    # 会话上下文保留时长。必须 >= idle_stop_minutes，否则"维护几小时后
    # 自然休息"不成立——context 一关，登录态就丢了，下次要重新登录 ~16s。
    context_ttl_minutes: int = 240

    # 开机预热：后台启动浏览器 + 登录 + 打开门户页，让第一个真实检索
    # 不用等这 ~22s。只做一次登录（人类打开网站也是这个频率），不预先检索。
    warmup_on_start: bool = True

    # 串行化检索：同一账号并发检索是典型爬虫特征，这里强制排队。
    serialize_searches: bool = True

    min_request_interval: float = 3.0

    portal: str = "https://er.szlib.org.cn:8899"
    db_ids: dict[str, int] = field(default_factory=lambda: {
        "cnki": 4, "wanfang": 65, "duxiu": 2, "vip": 189,
    })

    # 对外服务（验证页 / MCP）
    public_base_url: str = ""
    verify_port: int = 8765
    verify_ttl: int = 300

    state_dir: Path = Path("./data")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.username = _env_str("SZLIB_USERNAME")
        cfg.password = _env_str("SZLIB_PASSWORD")
        cfg.profile_dir = Path(_env_str("SZLIB_PROFILE_DIR", "./data/profile")).expanduser()
        cfg.headless = _env_bool("SZLIB_HEADLESS", True)

        cfg.idle_stop_minutes = _env_int("SZLIB_IDLE_STOP_MINUTES", 240)
        cfg.keepalive_min_minutes = _env_int("SZLIB_KEEPALIVE_MIN_MINUTES", 30)
        cfg.keepalive_max_minutes = _env_int("SZLIB_KEEPALIVE_MAX_MINUTES", 50)
        if cfg.keepalive_min_minutes > cfg.keepalive_max_minutes:
            cfg.keepalive_min_minutes = cfg.keepalive_max_minutes
        if cfg.keepalive_min_minutes < 5:
            cfg.keepalive_min_minutes = 5

        cfg.min_request_interval = float(_env_int("SZLIB_MIN_REQUEST_INTERVAL", 3))

        cfg.context_ttl_minutes = _env_int("SZLIB_CONTEXT_TTL_MINUTES",
                                           cfg.idle_stop_minutes)
        if cfg.context_ttl_minutes < cfg.idle_stop_minutes:
            # context 比休眠还短 = 登录态撑不到"自然休息"，强制对齐
            cfg.context_ttl_minutes = cfg.idle_stop_minutes
        cfg.warmup_on_start = _env_bool("SZLIB_WARMUP_ON_START", True)
        cfg.serialize_searches = _env_bool("SZLIB_SERIALIZE_SEARCHES", True)

        cfg.portal = _env_str("SZLIB_PORTAL", cfg.portal).rstrip("/")
        for key, env_name, default in (
            ("cnki", "SZLIB_DB_CNKI", 4),
            ("wanfang", "SZLIB_DB_WANFANG", 65),
            ("duxiu", "SZLIB_DB_DUXIU", 2),
            ("vip", "SZLIB_DB_VIP", 189),
        ):
            cfg.db_ids[key] = _env_int(env_name, default)

        cfg.state_dir = cfg.profile_dir.parent

        cfg.public_base_url = _env_str("SZLIB_PUBLIC_BASE_URL").rstrip("/")
        cfg.verify_port = _env_int("SZLIB_VERIFY_PORT", 8765)
        cfg.verify_ttl = _env_int("SZLIB_VERIFY_TTL", 300)
        return cfg

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


_cfg: Config | None = None


def get_config(reload: bool = False) -> Config:
    global _cfg
    if _cfg is None or reload:
        load_dotenv()
        _cfg = Config.from_env()
    return _cfg
