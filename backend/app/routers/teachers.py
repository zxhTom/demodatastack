from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from app.database import get_db
from app.models.education import Teacher, Department, User
from app.schemas.education import (
    TeacherCreate, TeacherUpdate, TeacherResponse, PaginatedResponse,
)
from app.services.auth import get_current_user

router = APIRouter()


@router.get("", response_model=PaginatedResponse[TeacherResponse])
async def list_teachers(
    skip: int = 0,
    limit: int = 20,
    department_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Teacher)
    if department_id:
        q = q.where(Teacher.department_id == department_id)
    total_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = total_result.scalar_one()
    result = await db.execute(q.offset(skip).limit(limit).order_by(Teacher.id))
    teachers = result.scalars().all()
    items = []
    for t in teachers:
        d = TeacherResponse.model_validate(t)
        if t.department_id:
            dept_result = await db.execute(select(Department).where(Department.id == t.department_id))
            dept = dept_result.scalar_one_or_none()
            d.department_name = dept.name if dept else None
        items.append(d)
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@router.post("", response_model=TeacherResponse, status_code=status.HTTP_201_CREATED)
async def create_teacher(
    data: TeacherCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    teacher = Teacher(**data.model_dump())
    db.add(teacher)
    await db.flush()
    await db.refresh(teacher)
    return teacher


@router.get("/{teacher_id}", response_model=TeacherResponse)
async def get_teacher(
    teacher_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Teacher).where(Teacher.id == teacher_id))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="教师不存在")
    return t


@router.put("/{teacher_id}", response_model=TeacherResponse)
async def update_teacher(
    teacher_id: int,
    data: TeacherUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Teacher).where(Teacher.id == teacher_id))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="教师不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(t, k, v)
    await db.flush()
    await db.refresh(t)
    return t


@router.delete("/{teacher_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_teacher(
    teacher_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Teacher).where(Teacher.id == teacher_id))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="教师不存在")
    await db.delete(t)
