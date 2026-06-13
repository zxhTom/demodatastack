from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from datetime import datetime
from app.database import get_db
from app.models.education import Grade, Enrollment, Student, CourseSchedule, Course, User
from app.schemas.education import (
    GradeCreate, GradeUpdate, GradeResponse, PaginatedResponse,
)
from app.services.auth import get_current_user

router = APIRouter()


@router.get("", response_model=PaginatedResponse[GradeResponse])
async def list_grades(
    skip: int = 0,
    limit: int = 20,
    enrollment_id: Optional[int] = None,
    student_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Grade)
    if enrollment_id:
        q = q.where(Grade.enrollment_id == enrollment_id)
    if student_id:
        enrollment_ids = (await db.execute(
            select(Enrollment.id).where(Enrollment.student_id == student_id)
        )).scalars().all()
        q = q.where(Grade.enrollment_id.in_(enrollment_ids))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    result = await db.execute(q.offset(skip).limit(limit).order_by(Grade.id))
    grades = result.scalars().all()
    items = []
    for g in grades:
        d = GradeResponse.model_validate(g)
        if g.enrollment_id:
            e = (await db.execute(select(Enrollment).where(Enrollment.id == g.enrollment_id))).scalar_one_or_none()
            if e:
                s = (await db.execute(select(Student).where(Student.id == e.student_id))).scalar_one_or_none()
                d.student_name = s.name if s else None
                cs = (await db.execute(select(CourseSchedule).where(CourseSchedule.id == e.schedule_id))).scalar_one_or_none()
                if cs:
                    c = (await db.execute(select(Course).where(Course.id == cs.course_id))).scalar_one_or_none()
                    d.course_name = c.name if c else None
        items.append(d)
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@router.post("", response_model=GradeResponse, status_code=201)
async def create_grade(
    data: GradeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    grade = Grade(
        **data.model_dump(),
        graded_at=datetime.utcnow(),
    )
    db.add(grade)
    await db.flush()
    await db.refresh(grade)
    return grade


@router.put("/{grade_id}", response_model=GradeResponse)
async def update_grade(
    grade_id: int,
    data: GradeUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    g = (await db.execute(select(Grade).where(Grade.id == grade_id))).scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="成绩记录不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(g, k, v)
    g.graded_at = datetime.utcnow()
    await db.flush()
    await db.refresh(g)
    return g


@router.delete("/{grade_id}", status_code=204)
async def delete_grade(
    grade_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    g = (await db.execute(select(Grade).where(Grade.id == grade_id))).scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="成绩记录不存在")
    await db.delete(g)
