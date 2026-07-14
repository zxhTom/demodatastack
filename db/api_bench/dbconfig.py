#!/usr/bin/env python3
"""数据库连接配置加载器，加载顺序：环境变量 > db.env > 内置默认值。

DB_PASSWORD / DB_DSN 支持 ENC(...) 密文（与 pg-cdc-agent 同一套机制）：
用 pg-cdc-agent 的 encrypt-password 子命令生成，密钥找 CDC_CONFIG_KEY
环境变量 / CDC_CONFIG_KEY_FILE / ~/.cdc_agent.key。
"""
import os
import sys

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


def _decrypt(value):
    if not (isinstance(value, str) and value.startswith("ENC(") and value.endswith(")")):
        return value
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        sys.exit("[错误] 配置里使用了 ENC(...) 密文，但未安装 cryptography 库：pip install cryptography")
    key = os.environ.get("CDC_CONFIG_KEY", "").strip().encode()
    if not key:
        key_file = os.environ.get("CDC_CONFIG_KEY_FILE", "").strip() or os.path.expanduser("~/.cdc_agent.key")
        if not os.path.isfile(key_file):
            sys.exit(f"[错误] 配置里使用了 ENC(...) 密文，但找不到解密密钥（{key_file}）。"
                     f"用 pg-cdc-agent 的 encrypt-password --gen-key 生成。")
        with open(key_file, "rb") as fh:
            key = fh.read().strip()
    try:
        return Fernet(key).decrypt(value[4:-1].encode()).decode()
    except InvalidToken:
        sys.exit("[错误] ENC(...) 密文解密失败：密钥与密文不匹配，或密文被截断/篡改。")


def load_config(env_file=None):
    here = os.path.dirname(os.path.abspath(__file__))
    env_file = env_file or os.path.join(here, "db.env")

    cfg = dict(_DEFAULTS)
    cfg.update(_load_env_file(env_file))
    for key in list(cfg.keys()):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    cfg["DB_PASSWORD"] = _decrypt(cfg.get("DB_PASSWORD", ""))
    cfg["DB_DSN"] = _decrypt(cfg.get("DB_DSN", ""))
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
