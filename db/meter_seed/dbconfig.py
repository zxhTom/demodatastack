#!/usr/bin/env python3
"""数据库连接配置加载器，加载顺序：环境变量 > db.env > 内置默认值。"""
import os

_DEFAULTS = {
    "DB_HOST": "172.17.182.123",
    "DB_PORT": "5432",
    "DB_NAME": "eco_ma",
    "DB_USER": "hes",
    "DB_PASSWORD": "7L2wYCDWLQdqPr4bNsYBMr5nwutckP8Q",
    "DB_DSN": "",
}


def _load_env_file(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def load_config(env_file=None):
    here = os.path.dirname(os.path.abspath(__file__))
    env_file = env_file or os.path.join(here, "db.env")

    cfg = dict(_DEFAULTS)
    cfg.update(_load_env_file(env_file))
    for key in list(cfg.keys()):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def get_dsn(env_file=None):
    cfg = load_config(env_file)
    if cfg.get("DB_DSN"):
        return cfg["DB_DSN"]
    return (
        f"host={cfg['DB_HOST']} port={cfg['DB_PORT']} "
        f"dbname={cfg['DB_NAME']} user={cfg['DB_USER']} "
        f"password={cfg['DB_PASSWORD']}"
    )


if __name__ == "__main__":
    cfg = load_config()
    safe = dict(cfg)
    if safe.get("DB_PASSWORD"):
        safe["DB_PASSWORD"] = "***"
    if safe.get("DB_DSN"):
        safe["DB_DSN"] = "***"
    for k, v in safe.items():
        print(f"{k}={v}")
