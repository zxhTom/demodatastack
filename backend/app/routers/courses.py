from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from app.database import get_db
from app.models.education import Course, Department, User
from app.schemas.education import (
    CourseCreate, CourseUpdate, CourseResponse, PaginatedResponse,
)
from app.services.auth import get_current_user

router = APIRouter()


@router.get("", response_model=PaginatedResponse[CourseResponse])
async def list_courses(
    skip: int = 0,
    limit: int = 20,
    department_id: Optional[int] = None,
    course_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Course)
    if department_id:
        q = q.where(Course.department_id == department_id)
    if course_type:
        q = q.where(Course.course_type == course_type)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    result = await db.execute(q.offset(skip).limit(limit).order_by(Course.id))
    courses = result.scalars().all()
    items = []
    for c in courses:
        d = CourseResponse.model_validate(c)
        if c.department_id:
            dept = (await db.execute(select(Department).where(Department.id == c.department_id))).scalar_one_or_none()
            d.department_name = dept.name if dept else None
        items.append(d)
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@router.post("", response_model=CourseResponse, status_code=201)
async def create_course(
    data: CourseCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    c = Course(**data.model_dump())
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return c


@router.get("/{course_id}", response_model=CourseResponse)
async def get_course(
    course_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    c = (await db.execute(select(Course).where(Course.id == course_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="课程不存在")
    return c


@router.put("/{course_id}", response_model=CourseResponse)
async def update_course(
    course_id: int,
    data: CourseUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    c = (await db.execute(select(Course).where(Course.id == course_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="课程不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    await db.flush()
    await db.refresh(c)
    return c


@router.delete("/{course_id}", status_code=204)
async def delete_course(
    course_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    c = (await db.execute(select(Course).where(Course.id == course_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="课程不存在")
    await db.delete(c)
