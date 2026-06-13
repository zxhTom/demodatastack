# Kafka CDC 消息数据结构设计文档

## 1. 概述

本系统使用 **Debezium PostgreSQL Connector** 捕获数据库变更事件（CDC），并将变更数据发布到 Apache Kafka。
下游消费者通过订阅对应的 Kafka Topic 即可实时获取教务系统的所有数据变更。

---

## 2. Topic 命名规范

每张数据库表对应一个独立的 Kafka Topic，命名格式为：

```
{server_name}.{schema}.{table_name}
```

| Topic 名称                              | 对应表           | 说明         |
|----------------------------------------|-----------------|-------------|
| `edumanage.public.departments`         | departments     | 院系变更     |
| `edumanage.public.teachers`            | teachers        | 教师变更     |
| `edumanage.public.students`            | students        | 学生变更     |
| `edumanage.public.courses`             | courses         | 课程变更     |
| `edumanage.public.enrollments`         | enrollments     | 选课变更     |
| `edumanage.public.grades`              | grades          | 成绩变更     |
| `edumanage.public.attendance`          | attendance      | 考勤变更     |
| `edumanage.public.course_schedules`    | course_schedules| 课程安排变更 |

---

## 3. 消息结构

每条 Kafka 消息包含 **Key** 和 **Value** 两部分，均使用 JSON 格式。

### 3.1 消息 Key（主键标识）

```json
{
  "schema": {
    "type": "struct",
    "fields": [
      { "type": "int32", "optional": false, "field": "id" }
    ],
    "optional": false,
    "name": "edumanage.public.students.Key"
  },
  "payload": {
    "id": 42
  }
}
```

**字段说明：**
- `schema`: 消息结构描述（Debezium Schema Registry 格式）
- `payload.id`: 数据库主键值，唯一标识被操作的记录

### 3.2 消息 Value（完整变更数据）

```json
{
  "schema": { "..." },
  "payload": {
    "before": { "...旧数据..." },
    "after":  { "...新数据..." },
    "source": {
      "version":   "2.4.0.Final",
      "connector": "postgresql",
      "name":      "edumanage",
      "ts_ms":     1718000000000,
      "db":        "edumanage",
      "schema":    "public",
      "table":     "students",
      "lsn":       12345678,
      "snapshot":  "false"
    },
    "op":        "u",
    "ts_ms":     1718000000100,
    "transaction": null
  }
}
```

---

## 4. 操作类型（op 字段）

| op 值 | 操作类型   | before 字段 | after 字段 | 说明                     |
|-------|-----------|------------|-----------|--------------------------|
| `c`   | INSERT    | `null`     | 新记录     | 新增一条记录             |
| `u`   | UPDATE    | 旧记录     | 新记录     | 更新（含变更前后对比）    |
| `d`   | DELETE    | 旧记录     | `null`     | 删除记录                 |
| `r`   | READ      | `null`     | 当前记录   | 初始快照（启动时）       |

> **重要**: 对于 UPDATE 操作，`before` 包含所有字段的原始值，`after` 包含更新后的所有字段值。
> 下游消费者可以通过比对 `before` 和 `after` 来精确知道哪些字段发生了变化。

---

## 5. 完整消息示例

### 5.1 学生新增（INSERT）

**Topic**: `edumanage.public.students`

```json
{
  "payload": {
    "before": null,
    "after": {
      "id":              101,
      "student_id":      "S20240101",
      "name":            "张三",
      "gender":          "男",
      "email":           "zhangsan@stu.edu.com",
      "phone":           "13900010001",
      "department_id":   1,
      "grade":           1,
      "class_name":      "计算机2024-1",
      "enrollment_date": 19966,
      "status":          "active",
      "created_at":      1718000000000000,
      "updated_at":      1718000000000000
    },
    "source": {
      "name":    "edumanage",
      "table":   "students",
      "ts_ms":   1718000000000,
      "lsn":     987654321
    },
    "op":    "c",
    "ts_ms": 1718000000100
  }
}
```

### 5.2 成绩更新（UPDATE）

**Topic**: `edumanage.public.grades`

```json
{
  "payload": {
    "before": {
      "id":            55,
      "enrollment_id": 200,
      "score":         75.00,
      "grade_letter":  "C",
      "comment":       null,
      "updated_at":    1718000000000000
    },
    "after": {
      "id":            55,
      "enrollment_id": 200,
      "score":         88.50,
      "grade_letter":  "B",
      "comment":       "期末补测成绩",
      "updated_at":    1718003600000000
    },
    "source": {
      "name":  "edumanage",
      "table": "grades",
      "ts_ms": 1718003600000
    },
    "op":    "u",
    "ts_ms": 1718003600100
  }
}
```

### 5.3 选课删除（DELETE / 退课）

**Topic**: `edumanage.public.enrollments`

```json
{
  "payload": {
    "before": {
      "id":              88,
      "student_id":      42,
      "schedule_id":     15,
      "enrollment_date": 1718000000000000,
      "status":          "enrolled"
    },
    "after": null,
    "source": {
      "name":  "edumanage",
      "table": "enrollments",
      "ts_ms": 1718100000000
    },
    "op":    "d",
    "ts_ms": 1718100000100
  }
}
```

---

## 6. 时间字段说明

Debezium 对时间字段有特殊处理：

| 原始类型       | Kafka 消息格式        | 单位              | 说明                         |
|--------------|----------------------|------------------|------------------------------|
| TIMESTAMPTZ  | 整数 microseconds    | 微秒（epoch）     | 除以 1000 得到毫秒            |
| DATE         | 整数 days            | 天数（epoch）     | 加上 1970-01-01 得到日期      |
| TIME         | 整数 microseconds    | 微秒（午夜起）    | 除以 1000000 得到秒           |

**时间转换示例（Python）：**

```python
from datetime import datetime, timedelta, date

# TIMESTAMPTZ: microseconds -> datetime
ts_us = 1718000000000000
dt = datetime.utcfromtimestamp(ts_us / 1_000_000)

# DATE: days since epoch -> date  
days = 19966
d = date(1970, 1, 1) + timedelta(days=days)

# source.ts_ms: milliseconds -> datetime
ts_ms = 1718000000000
dt = datetime.utcfromtimestamp(ts_ms / 1000)
```

---

## 7. 关键字段索引

| 字段路径           | 类型    | 说明                                    |
|--------------------|--------|-----------------------------------------|
| `payload.op`       | string | 操作类型: c/u/d/r                       |
| `payload.before`   | object | 操作前数据（UPDATE/DELETE 有值）        |
| `payload.after`    | object | 操作后数据（INSERT/UPDATE 有值）        |
| `payload.ts_ms`    | long   | 变更处理时间（毫秒，Debezium 侧）       |
| `payload.source.ts_ms` | long | 数据库 WAL 时间戳（毫秒）           |
| `payload.source.lsn`   | long | PostgreSQL WAL 日志序列号（用于幂等性）|
| `payload.source.table` | string | 表名                                |

---

## 8. 下游消费建议

### 8.1 幂等性保证
- 使用 `source.lsn` 作为幂等键，避免重复处理
- 消费者应记录已处理的最大 LSN

### 8.2 变更提取（UPDATE 差异）

```python
def extract_changes(before: dict, after: dict) -> dict:
    """提取 UPDATE 变更的字段差异"""
    if before is None or after is None:
        return {}
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    }
```

### 8.3 KPI 指标采集逻辑

| 事件             | 指标              | 变化值 |
|-----------------|-------------------|--------|
| students INSERT | student_count     | +1     |
| students DELETE | student_count     | -1     |
| enrollments INSERT | enrollment_count | +1  |
| enrollments DELETE | enrollment_count | -1  |
| grades INSERT   | grade_count       | +1     |
| grades UPDATE (score change) | grade_avg | 重新计算 |

---

## 9. 错误处理

- **消息积压**: 设置合理的 `max.poll.records` 和消费者并发数
- **解析失败**: 使用 Dead Letter Queue（DLQ）收集无法解析的消息
- **时区问题**: 所有时间戳为 UTC，消费者负责转换为本地时区
- **Schema 变更**: `include.schema.changes=true` 会在 schema 变更时发送通知 Topic
