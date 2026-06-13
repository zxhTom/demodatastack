import asyncio
import json
import logging
from datetime import datetime
from aiokafka import AIOKafkaConsumer
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.education import KpiEvent

logger = logging.getLogger(__name__)

CDC_TOPICS = [
    "edumanage.public.students",
    "edumanage.public.teachers",
    "edumanage.public.courses",
    "edumanage.public.enrollments",
    "edumanage.public.grades",
    "edumanage.public.departments",
    "edumanage.public.attendance",
]

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


async def process_cdc_event(topic: str, value: dict):
    table = topic.split(".")[-1]
    metric = METRIC_MAP.get(table)
    if not metric:
        return

    op = value.get("op", "r")
    delta = OP_DELTA.get(op, 0)
    if delta == 0 and op != "u":
        return

    before = value.get("before")
    after  = value.get("after")
    changes = extract_changes(before, after) if op == "u" else {}

    dimension = {"table": table, "op": op}
    if changes:
        dimension["changes"] = changes
        dimension["changed_fields"] = list(changes.keys())

    async with AsyncSessionLocal() as db:
        try:
            event = KpiEvent(
                event_time=datetime.utcnow(),
                metric_name=metric,
                metric_value=delta,
                dimension=dimension,
                tags={"source": "debezium_cdc"},
            )
            db.add(event)
            await db.commit()
            if changes:
                logger.info(f"[{table}] UPDATE 变更字段: {list(changes.keys())}")
        except Exception as e:
            logger.error(f"写入 KPI 事件失败: {e}")
            await db.rollback()


async def start_kafka_consumer():
    while True:
        consumer = None
        try:
            consumer = AIOKafkaConsumer(
                *CDC_TOPICS,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id="kpi-consumer-group",
                auto_offset_reset="latest",
                enable_auto_commit=True,
                value_deserializer=lambda m: json.loads(m.decode("utf-8")) if m else None,
            )
            await consumer.start()
            logger.info("Kafka consumer 已启动，监听 CDC 事件...")
            async for msg in consumer:
                if msg.value:
                    await process_cdc_event(msg.topic, msg.value)
        except asyncio.CancelledError:
            logger.info("Kafka consumer 已停止")
            break
        except Exception as e:
            logger.warning(f"Kafka consumer 异常: {e}，5秒后重试...")
            await asyncio.sleep(5)
        finally:
            if consumer:
                try:
                    await consumer.stop()
                except Exception:
                    pass
