"""Redis Stream CDC 消费者（替代原 Kafka consumer）。

以消费者组方式读取 cdc-collector 推送的变更事件，转成 KPI 事件写入
kpi_events 超表。消息处理成功后 XACK 确认。
"""
import asyncio
import json
import logging
import socket
from datetime import datetime

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.education import KpiEvent

logger = logging.getLogger(__name__)

METRIC_MAP = {
    "students":    "student_count",
    "teachers":    "teacher_count",
    "courses":     "course_count",
    "enrollments": "enrollment_count",
    "grades":      "grade_count",
    "departments": "department_count",
    "attendance":  "attendance_count",
}

OP_DELTA = {"c": 1, "r": 0, "u": 0, "d": -1}

IGNORE_DIFF_FIELDS = {"updated_at", "created_at"}


def extract_changes(before: dict, after: dict) -> dict:
    """返回 UPDATE 中实际发生变化的字段：{field: {from: old, to: new}}"""
    if not before or not after:
        return {}
    return {
        k: {"from": before.get(k), "to": after.get(k)}
        for k in after
        if k not in IGNORE_DIFF_FIELDS and before.get(k) != after.get(k)
    }


async def process_cdc_event(event: dict):
    table = event.get("table", "")
    metric = METRIC_MAP.get(table)
    if not metric:
        return

    op = event.get("op", "r")
    delta = OP_DELTA.get(op, 0)
    if delta == 0 and op != "u":
        return

    before = event.get("before")
    after = event.get("after")
    changes = extract_changes(before, after) if op == "u" else {}

    dimension = {"table": table, "op": op}
    if changes:
        dimension["changes"] = changes
        dimension["changed_fields"] = list(changes.keys())

    async with AsyncSessionLocal() as db:
        try:
            db.add(KpiEvent(
                event_time=datetime.utcnow(),
                metric_name=metric,
                metric_value=delta,
                dimension=dimension,
                tags_data={"source": "redis_cdc"},
            ))
            await db.commit()
            if changes:
                logger.info(f"[{table}] UPDATE 变更字段: {list(changes.keys())}")
        except Exception as e:
            logger.error(f"写入 KPI 事件失败: {e}")
            await db.rollback()


async def _ensure_group(r):
    try:
        await r.xgroup_create(
            settings.cdc_stream_key, settings.cdc_consumer_group,
            id="$", mkstream=True,
        )
        logger.info(f"消费者组已创建: {settings.cdc_consumer_group}")
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def start_stream_consumer():
    consumer_name = f"kpi-{socket.gethostname()}"
    while True:
        r = None
        try:
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            await _ensure_group(r)
            logger.info(f"Redis Stream consumer 已启动，监听 {settings.cdc_stream_key} ...")
            while True:
                resp = await r.xreadgroup(
                    settings.cdc_consumer_group, consumer_name,
                    {settings.cdc_stream_key: ">"},
                    count=100, block=5000,
                )
                for _stream, messages in resp or []:
                    for msg_id, fields in messages:
                        try:
                            await process_cdc_event(json.loads(fields["payload"]))
                        except Exception as e:
                            logger.error(f"处理消息 {msg_id} 失败: {e}")
                        await r.xack(settings.cdc_stream_key, settings.cdc_consumer_group, msg_id)
        except asyncio.CancelledError:
            logger.info("Redis Stream consumer 已停止")
            break
        except Exception as e:
            logger.warning(f"Redis Stream consumer 异常: {e}，5秒后重试...")
            await asyncio.sleep(5)
        finally:
            if r:
                try:
                    await r.aclose()
                except Exception:
                    pass
