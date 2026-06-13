import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, departments, teachers, students, courses, enrollments, grades, kpi

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.kafka.consumer import start_kafka_consumer
    task = asyncio.create_task(start_kafka_consumer())
    logger.info("教务管理系统 API 启动完成")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("教务管理系统 API 已关闭")


app = FastAPI(
    title="教务管理系统 API",
    description="教务管理系统 REST API，包含院系、教师、学生、课程、成绩管理及实时 KPI 监控",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,        prefix="/api/auth",        tags=["认证"])
app.include_router(departments.router, prefix="/api/departments", tags=["院系管理"])
app.include_router(teachers.router,    prefix="/api/teachers",    tags=["教师管理"])
app.include_router(students.router,    prefix="/api/students",    tags=["学生管理"])
app.include_router(courses.router,     prefix="/api/courses",     tags=["课程管理"])
app.include_router(enrollments.router, prefix="/api/enrollments", tags=["选课管理"])
app.include_router(grades.router,      prefix="/api/grades",      tags=["成绩管理"])
app.include_router(kpi.router,         prefix="/api/kpi",         tags=["KPI监控"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "edumanage-api"}
