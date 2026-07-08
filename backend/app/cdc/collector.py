"""CDC 采集服务：PG 逻辑复制 → Redis Stream（替代 Debezium + Kafka）。

监听 cdc_pub 发布中的表变更，解析后推送到 Redis Stream，
由 backend 的 stream consumer 消费。独立进程运行：

    python -m app.cdc.collector
"""
import json
import logging
import time

import psycopg2
import psycopg2.errors
import redis
from psycopg2.extras import LogicalReplicationConnection

from app.config import settings
from app.cdc.pgoutput import PgOutputParser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cdc-collector")


class CdcCollector:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.parser = PgOutputParser()
        self.count = 0
        self.last_log = time.time()

    def __call__(self, msg):
        event = self.parser.parse(msg.payload)
        if event is not None:
            event["ts_ms"] = int(time.time() * 1000)
            self.redis.xadd(
                settings.cdc_stream_key,
                {"payload": json.dumps(event, ensure_ascii=False)},
                maxlen=settings.cdc_stream_maxlen,
                approximate=True,
            )
            self.count += 1
            if self.count % 100 == 0 or time.time() - self.last_log > 60:
                logger.info("已推送 %d 条变更事件到 %s", self.count, settings.cdc_stream_key)
                self.last_log = time.time()
        msg.cursor.send_feedback(flush_lsn=msg.data_start)


def ensure_slot(conn):
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT pg_create_logical_replication_slot(%s, 'pgoutput');",
                (settings.cdc_slot,),
            )
            logger.info("复制槽已创建: %s", settings.cdc_slot)
        except psycopg2.errors.DuplicateObject:
            logger.info("复制槽已存在: %s", settings.cdc_slot)


def run_once():
    redis_client = redis.from_url(settings.redis_url)
    redis_client.ping()

    conn = psycopg2.connect(settings.cdc_pg_dsn, connection_factory=LogicalReplicationConnection)
    try:
        ensure_slot(conn)
        cur = conn.cursor()
        cur.start_replication(
            slot_name=settings.cdc_slot,
            decode=False,
            options={
                "proto_version": "1",
                "publication_names": settings.cdc_publication,
            },
        )
        logger.info(
            "CDC 监听已启动: slot=%s publication=%s → stream=%s",
            settings.cdc_slot, settings.cdc_publication, settings.cdc_stream_key,
        )
        cur.consume_stream(CdcCollector(redis_client))
    finally:
        conn.close()


def main():
    logger.info("=" * 50)
    logger.info("CDC 采集服务启动（PG 逻辑复制 → Redis Stream）")
    logger.info("=" * 50)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("收到停止信号，退出")
            break
        except Exception as e:
            logger.warning("CDC 采集异常: %s，5 秒后重连...", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
