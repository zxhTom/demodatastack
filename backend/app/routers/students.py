from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from app.database import get_db
from app.models.education import Student, Department, User
from app.schemas.education import (
    StudentCreate, StudentUpdate, StudentResponse, PaginatedResponse,
)
from app.services.auth import get_current_user

router = APIRouter()


@router.get("", response_model=PaginatedResponse[StudentResponse])
async def list_students(
    skip: int = 0,
    limit: int = 20,
    department_id: Optional[int] = None,
    grade: Optional[int] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Student)
    if department_id:
        q = q.where(Student.department_id == department_id)
    if grade:
        q = q.where(Student.grade == grade)
    if status:
        q = q.where(Student.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    result = await db.execute(q.offset(skip).limit(limit).order_by(Student.id))
    students = result.scalars().all()
    items = []
    for s in students:
        d = StudentResponse.model_validate(s)
        if s.department_id:
            dept = (await db.execute(select(Department).where(Department.id == s.department_id))).scalar_one_or_none()
            d.department_name = dept.name if dept else None
        items.append(d)
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@router.post("", response_model=StudentResponse, status_code=201)
async def create_student(
    data: StudentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    student = Student(**data.model_dump())
    db.add(student)
    await db.flush()
    await db.refresh(student)
    return student


@router.get("/{student_id}", response_model=StudentResponse)
async def get_student(
    student_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    s = (await db.execute(select(Student).where(Student.id == student_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="学生不存在")
    return s


@router.put("/{student_id}", response_model=StudentResponse)
async def update_student(
    student_id: int,
    data: StudentUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    s = (await db.execute(select(Student).where(Student.id == student_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="学生不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    await db.flush()
    await db.refresh(s)
    return s


@router.delete("/{student_id}", status_code=204)
async def delete_student(
    student_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    s = (await db.execute(select(Student).where(Student.id == student_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="学生不存在")
    await db.delete(s)
