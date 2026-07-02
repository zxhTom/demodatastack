#!/usr/bin/env python3
"""共享的数据库连接配置加载器。

加载优先级（从高到低）：
  1. 环境变量 DB_DSN / DB_HOST 等
  2. 同目录下的 db.env 文件（key=value 格式）
  3. 内置默认值（仅用于本地演示）

这样你可以把真实连接信息只放在 db.env 里单独维护，不会进版本库。
"""
import os

# 内置默认值（与 docker-compose.yml 中的 timescaledb 一致，仅供本地演示）
_DEFAULTS = {
    "DB_HOST": "172.17.182.123",
    "DB_PORT": "5432",
    "DB_NAME": "eco_ma",
    "DB_USER": "hes",
    "DB_PASSWORD": "7L2wYCDWLQdqPr4bNsYBMr5nwutckP8Q",
    "DB_DSN": "",
}


def _load_env_file(path):
    """解析简单的 key=value 文件，忽略空行和 # 注释。"""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def load_config(env_file=None):
    """返回最终的配置 dict。"""
    here = os.path.dirname(os.path.abspath(__file__))
    env_file = env_file or os.path.join(here, "db.env")

    cfg = dict(_DEFAULTS)
    cfg.update(_load_env_file(env_file))
    # 环境变量优先级最高
    for key in list(cfg.keys()):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def get_dsn(env_file=None):
    """返回 psycopg2 可直接使用的 DSN 字符串。"""
    cfg = load_config(env_file)
    if cfg.get("DB_DSN"):
        return cfg["DB_DSN"]
    return (
        f"host={cfg['DB_HOST']} port={cfg['DB_PORT']} "
        f"dbname={cfg['DB_NAME']} user={cfg['DB_USER']} "
        f"password={cfg['DB_PASSWORD']}"
    )


if __name__ == "__main__":
    # 方便排查：打印当前解析到的连接信息（隐藏密码）
    cfg = load_config()
    safe = dict(cfg)
    if safe.get("DB_PASSWORD"):
        safe["DB_PASSWORD"] = "***"
    if safe.get("DB_DSN"):
        safe["DB_DSN"] = "***"
    for k, v in safe.items():
        print(f"{k}={v}")
