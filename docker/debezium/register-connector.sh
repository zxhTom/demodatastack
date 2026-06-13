#!/bin/bash
# register-connector.sh: 向 Kafka Connect 注册 Debezium PostgreSQL Connector
set -e

CONNECT_HOST="${KAFKA_CONNECT_HOST:-kafka-connect}"
CONNECT_PORT="${KAFKA_CONNECT_PORT:-8083}"
MAX_WAIT=180

echo "========================================"
echo "  Debezium Connector 注册脚本"
echo "  Kafka Connect: ${CONNECT_HOST}:${CONNECT_PORT}"
echo "========================================"

# 等待 Kafka Connect 就绪
echo "[1/3] 等待 Kafka Connect 就绪..."
for i in $(seq 1 $MAX_WAIT); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://${CONNECT_HOST}:${CONNECT_PORT}/connectors" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  Kafka Connect 已就绪 (尝试 ${i} 次)"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        echo "  错误: 超时等待 Kafka Connect"
        exit 1
    fi
    echo "  等待中... HTTP=${HTTP_CODE} (${i}/${MAX_WAIT})"
    sleep 2
done

# 检查 Connector 是否已存在
echo "[2/3] 检查 Connector 是否已注册..."
EXISTING=$(curl -s "http://${CONNECT_HOST}:${CONNECT_PORT}/connectors/edumanage-postgres-connector" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name',''))" 2>/dev/null || echo "")

if [ -n "$EXISTING" ]; then
    echo "  Connector 已存在，删除后重新注册..."
    curl -s -X DELETE "http://${CONNECT_HOST}:${CONNECT_PORT}/connectors/edumanage-postgres-connector"
    sleep 3
fi

# 注册 Debezium PostgreSQL Connector
echo "[3/3] 注册 Debezium PostgreSQL Connector..."
RESPONSE=$(curl -s -w "\nHTTP_STATUS:%{http_code}" \
  -X POST "http://${CONNECT_HOST}:${CONNECT_PORT}/connectors" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "edumanage-postgres-connector",
    "config": {
      "connector.class":                    "io.debezium.connector.postgresql.PostgresConnector",
      "database.hostname":                  "postgres-primary",
      "database.port":                      "5432",
      "database.user":                      "postgres",
      "database.password":                  "postgres123",
      "database.dbname":                    "edumanage",
      "topic.prefix":                       "edumanage",
      "plugin.name":                        "pgoutput",
      "publication.name":                   "dbz_publication",
      "slot.name":                          "debezium_slot",
      "table.include.list":                 "public.departments,public.teachers,public.students,public.courses,public.semesters,public.enrollments,public.grades,public.attendance,public.course_schedules",
      "key.converter":                      "org.apache.kafka.connect.json.JsonConverter",
      "key.converter.schemas.enable":       "true",
      "value.converter":                    "org.apache.kafka.connect.json.JsonConverter",
      "value.converter.schemas.enable":     "true",
      "heartbeat.interval.ms":             "10000",
      "slot.drop.on.stop":                  "false",
      "decimal.handling.mode":              "double",
      "time.precision.mode":                "connect",
      "include.schema.changes":             "true",
      "snapshot.mode":                      "initial",
      "tombstones.on.delete":               "false"
    }
  }')

HTTP_STATUS=$(echo "$RESPONSE" | grep "HTTP_STATUS:" | cut -d: -f2)
BODY=$(echo "$RESPONSE" | grep -v "HTTP_STATUS:")

echo "  响应状态: HTTP $HTTP_STATUS"
echo "  响应内容: $BODY"

if [ "$HTTP_STATUS" = "201" ] || [ "$HTTP_STATUS" = "200" ]; then
    echo ""
    echo "✅ Debezium Connector 注册成功！"
    echo ""
    echo "Kafka Topics 将自动创建："
    echo "  - edumanage.public.students"
    echo "  - edumanage.public.teachers"
    echo "  - edumanage.public.courses"
    echo "  - edumanage.public.enrollments"
    echo "  - edumanage.public.grades"
    echo "  - edumanage.public.departments"
    echo "  - edumanage.public.attendance"
else
    echo ""
    echo "❌ 注册失败 (HTTP $HTTP_STATUS)，请检查日志"
    exit 1
fi
