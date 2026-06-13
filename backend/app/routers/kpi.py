import asyncio
import json
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from app.database import get_db, AsyncSessionLocal
from app.models.education import Student, Teacher, Course, Enrollment, KpiEvent, User
from app.schemas.education import KpiStatsResponse, KpiEventResponse
from app.services.auth import get_current_user

router = APIRouter()

# 存储所有活跃的 WebSocket 连接
active_connections: List[WebSocket] = []


async def get_kpi_stats_data(db: AsyncSession) -> dict:
    total_students    = (await db.execute(select(func.count(Student.id)))).scalar_one()
    total_teachers    = (await db.execute(select(func.count(Teacher.id)))).scalar_one()
    total_courses     = (await db.execute(select(func.count(Course.id)))).scalar_one()
    total_enrollments = (await db.execute(select(func.count(Enrollment.id)))).scalar_one()
    return {
        "total_students":    total_students,
        "total_teachers":    total_teachers,
        "total_courses":     total_courses,
        "total_enrollments": total_enrollments,
        "timestamp":         datetime.utcnow().isoformat(),
    }


@router.get("/stats", response_model=KpiStatsResponse)
async def get_kpi_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    data = await get_kpi_stats_data(db)
    return KpiStatsResponse(
        total_students=data["total_students"],
        total_teachers=data["total_teachers"],
        total_courses=data["total_courses"],
        total_enrollments=data["total_enrollments"],
        active_semesters=1,
    )


@router.get("/recent-events", response_model=List[KpiEventResponse])
async def get_recent_events(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(KpiEvent).order_by(KpiEvent.event_time.desc()).limit(limit)
    )
    return result.scalars().all()


@router.post("/events", status_code=201)
async def create_kpi_event(
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    event = KpiEvent(
        metric_name=data.get("metric_name", "manual"),
        metric_value=data.get("metric_value"),
        dimension=data.get("dimension"),
        tags=data.get("tags"),
    )
    db.add(event)
    await db.flush()
    return {"status": "created"}


@router.websocket("/ws")
async def kpi_websocket(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            async with AsyncSessionLocal() as db:
                stats = await get_kpi_stats_data(db)
            await websocket.send_text(json.dumps(stats))
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)


async def broadcast_kpi_update(data: dict):
    dead = []
    for ws in active_connections:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_connections.remove(ws)
