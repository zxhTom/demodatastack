from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from typing import Optional
from app.database import get_db
from app.models.education import Enrollment, Student, CourseSchedule, Course, Semester, User
from app.schemas.education import (
    EnrollmentCreate, EnrollmentUpdate, EnrollmentResponse, PaginatedResponse,
)
from app.services.auth import get_current_user

router = APIRouter()


@router.get("", response_model=PaginatedResponse[EnrollmentResponse])
async def list_enrollments(
    skip: int = 0,
    limit: int = 20,
    student_id: Optional[int] = None,
    schedule_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(Enrollment)
    if student_id:
        q = q.where(Enrollment.student_id == student_id)
    if schedule_id:
        q = q.where(Enrollment.schedule_id == schedule_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    result = await db.execute(q.offset(skip).limit(limit).order_by(Enrollment.id))
    enrollments = result.scalars().all()
    items = []
    for e in enrollments:
        d = EnrollmentResponse.model_validate(e)
        if e.student_id:
            s = (await db.execute(select(Student).where(Student.id == e.student_id))).scalar_one_or_none()
            d.student_name = s.name if s else None
        if e.schedule_id:
            cs = (await db.execute(select(CourseSchedule).where(CourseSchedule.id == e.schedule_id))).scalar_one_or_none()
            if cs:
                c = (await db.execute(select(Course).where(Course.id == cs.course_id))).scalar_one_or_none()
                d.course_name = c.name if c else None
                sem = (await db.execute(select(Semester).where(Semester.id == cs.semester_id))).scalar_one_or_none()
                d.semester_name = sem.name if sem else None
        items.append(d)
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=items)


@router.post("", response_model=EnrollmentResponse, status_code=201)
async def create_enrollment(
    data: EnrollmentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    cs = (await db.execute(select(CourseSchedule).where(CourseSchedule.id == data.schedule_id))).scalar_one_or_none()
    if not cs:
        raise HTTPException(status_code=404, detail="课程安排不存在")
    if cs.current_students >= cs.max_students:
        raise HTTPException(status_code=400, detail="该课程已满员")
    existing = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == data.student_id,
            Enrollment.schedule_id == data.schedule_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="已选过该课程")
    enrollment = Enrollment(**data.model_dump())
    db.add(enrollment)
    await db.execute(
        update(CourseSchedule)
        .where(CourseSchedule.id == data.schedule_id)
        .values(current_students=CourseSchedule.current_students + 1)
    )
    await db.flush()
    await db.refresh(enrollment)
    return enrollment


@router.put("/{enrollment_id}", response_model=EnrollmentResponse)
async def update_enrollment(
    enrollment_id: int,
    data: EnrollmentUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    e = (await db.execute(select(Enrollment).where(Enrollment.id == enrollment_id))).scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="选课记录不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(e, k, v)
    await db.flush()
    await db.refresh(e)
    return e


@router.delete("/{enrollment_id}", status_code=204)
async def delete_enrollment(
    enrollment_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    e = (await db.execute(select(Enrollment).where(Enrollment.id == enrollment_id))).scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="选课记录不存在")
    if e.schedule_id:
        await db.execute(
            update(CourseSchedule)
            .where(CourseSchedule.id == e.schedule_id)
            .values(current_students=CourseSchedule.current_students - 1)
        )
    await db.delete(e)
