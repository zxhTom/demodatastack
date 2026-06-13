# 教务管理系统 API 接口文档

**Base URL**: `http://localhost:8000`  
**认证方式**: JWT Bearer Token  
**Content-Type**: `application/json`

---

## 认证接口

### 登录

```
POST /api/auth/login
```

**请求体：**
```json
{ "username": "admin", "password": "admin123" }
```

**响应：**
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "user_id": 1,
  "username": "admin",
  "role": "admin"
}
```

**内置账号：**
| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin123 | admin |
| teacher01 | teacher123 | teacher |
| student01 | student123 | student |

---

## 通用分页响应结构

```json
{
  "total": 100,
  "skip":  0,
  "limit": 20,
  "items": [ ... ]
}
```

---

## 院系管理 `/api/departments`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/departments?skip=0&limit=20 | 列表 |
| POST | /api/departments | 创建 |
| GET | /api/departments/{id} | 详情 |
| PUT | /api/departments/{id} | 更新 |
| DELETE | /api/departments/{id} | 删除 |

**院系对象：**
```json
{
  "id": 1,
  "name": "计算机学院",
  "code": "CS",
  "description": "培养计算机科学与技术专业人才",
  "dean": "李明",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

---

## 学生管理 `/api/students`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/students?department_id=1&grade=1&status=active | 列表（支持过滤） |
| POST | /api/students | 创建 |
| GET | /api/students/{id} | 详情 |
| PUT | /api/students/{id} | 更新 |
| DELETE | /api/students/{id} | 删除 |

---

## 教师管理 `/api/teachers`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/teachers?department_id=1 | 列表 |
| POST | /api/teachers | 创建 |
| GET | /api/teachers/{id} | 详情 |
| PUT | /api/teachers/{id} | 更新 |
| DELETE | /api/teachers/{id} | 删除 |

---

## 课程管理 `/api/courses`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/courses?course_type=必修 | 列表 |
| POST | /api/courses | 创建 |
| GET | /api/courses/{id} | 详情 |
| PUT | /api/courses/{id} | 更新 |
| DELETE | /api/courses/{id} | 删除 |

---

## 选课管理 `/api/enrollments`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/enrollments?student_id=1 | 列表 |
| POST | /api/enrollments | 创建（选课，检查人数限制） |
| PUT | /api/enrollments/{id} | 更新状态 |
| DELETE | /api/enrollments/{id} | 删除（退课） |

**创建选课请求：**
```json
{ "student_id": 1, "schedule_id": 5 }
```

**错误响应（满员）：**
```json
{ "detail": "该课程已满员" }
```

---

## 成绩管理 `/api/grades`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/grades?student_id=1 | 列表 |
| POST | /api/grades | 录入成绩 |
| PUT | /api/grades/{id} | 修改成绩 |
| DELETE | /api/grades/{id} | 删除 |

**录入成绩：**
```json
{
  "enrollment_id": 10,
  "score": 88.5,
  "comment": "表现优秀"
}
```

---

## KPI 监控 `/api/kpi`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/kpi/stats | 当前统计数据快照 |
| GET | /api/kpi/recent-events?limit=50 | 最近 KPI 事件 |
| POST | /api/kpi/events | 手动写入 KPI 事件 |
| WS | /api/kpi/ws | WebSocket 实时推送（每5秒） |

**统计响应：**
```json
{
  "total_students": 30,
  "total_teachers": 18,
  "total_courses":  20,
  "total_enrollments": 150,
  "active_semesters": 1
}
```

**WebSocket 消息格式：**
```json
{
  "total_students": 31,
  "total_teachers": 18,
  "total_courses":  20,
  "total_enrollments": 155,
  "timestamp": "2024-06-13T08:00:00.000000"
}
```

---

## 健康检查

```
GET /health
```
```json
{ "status": "ok", "service": "edumanage-api" }
```

---

## 交互式 API 文档

启动后访问：`http://localhost:8000/docs`（Swagger UI）
