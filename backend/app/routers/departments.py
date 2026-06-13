from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.education import Department, User
from app.schemas.education import (
    DepartmentCreate, DepartmentUpdate, DepartmentResponse, PaginatedResponse,
)
from app.services.auth import get_current_user

router = APIRouter()


@router.get("", response_model=PaginatedResponse[DepartmentResponse])
async def list_departments(
    skip: int = 0,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    total_result = await db.execute(select(func.count(Department.id)))
    total = total_result.scalar_one()
    result = await db.execute(select(Department).offset(skip).limit(limit).order_by(Department.id))
    items = result.scalars().all()
    return PaginatedResponse(total=total, skip=skip, limit=limit, items=list(items))


@router.post("", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
async def create_department(
    data: DepartmentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    dept = Department(**data.model_dump())
    db.add(dept)
    await db.flush()
    await db.refresh(dept)
    return dept


@router.get("/{dept_id}", response_model=DepartmentResponse)
async def get_department(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Department).where(Department.id == dept_id))
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="院系不存在")
    return dept


@router.put("/{dept_id}", response_model=DepartmentResponse)
async def update_department(
    dept_id: int,
    data: DepartmentUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Department).where(Department.id == dept_id))
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="院系不存在")
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(dept, k, v)
    await db.flush()
    await db.refresh(dept)
    return dept


@router.delete("/{dept_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_department(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Department).where(Department.id == dept_id))
    dept = result.scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="院系不存在")
    await db.delete(dept)
