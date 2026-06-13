from __future__ import annotations
from datetime import datetime, date, time
from decimal import Decimal
from typing import Optional, Generic, TypeVar, List
from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    total:    int
    skip:     int
    limit:    int
    items:    List[T]


# ─── Department ────────────────────────────────────────────────
class DepartmentBase(BaseModel):
    name:        str
    code:        str
    description: Optional[str] = None
    dean:        Optional[str] = None

class DepartmentCreate(DepartmentBase):
    pass

class DepartmentUpdate(BaseModel):
    name:        Optional[str] = None
    code:        Optional[str] = None
    description: Optional[str] = None
    dean:        Optional[str] = None

class DepartmentResponse(DepartmentBase):
    id:         int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Teacher ────────────────────────────────────────────────────
class TeacherBase(BaseModel):
    employee_id:   str
    name:          str
    gender:        Optional[str] = None
    email:         Optional[str] = None
    phone:         Optional[str] = None
    department_id: Optional[int] = None
    title:         Optional[str] = None
    hire_date:     Optional[date] = None
    status:        str = "active"

class TeacherCreate(TeacherBase):
    pass

class TeacherUpdate(BaseModel):
    name:          Optional[str]  = None
    gender:        Optional[str]  = None
    email:         Optional[str]  = None
    phone:         Optional[str]  = None
    department_id: Optional[int]  = None
    title:         Optional[str]  = None
    hire_date:     Optional[date] = None
    status:        Optional[str]  = None

class TeacherResponse(TeacherBase):
    id:              int
    department_name: Optional[str] = None
    created_at:      Optional[datetime] = None
    updated_at:      Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Student ────────────────────────────────────────────────────
class StudentBase(BaseModel):
    student_id:      str
    name:            str
    gender:          Optional[str]  = None
    email:           Optional[str]  = None
    phone:           Optional[str]  = None
    department_id:   Optional[int]  = None
    grade:           Optional[int]  = None
    class_name:      Optional[str]  = None
    enrollment_date: Optional[date] = None
    status:          str = "active"

class StudentCreate(StudentBase):
    pass

class StudentUpdate(BaseModel):
    name:            Optional[str]  = None
    gender:          Optional[str]  = None
    email:           Optional[str]  = None
    phone:           Optional[str]  = None
    department_id:   Optional[int]  = None
    grade:           Optional[int]  = None
    class_name:      Optional[str]  = None
    enrollment_date: Optional[date] = None
    status:          Optional[str]  = None

class StudentResponse(StudentBase):
    id:              int
    department_name: Optional[str] = None
    created_at:      Optional[datetime] = None
    updated_at:      Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Course ─────────────────────────────────────────────────────
class CourseBase(BaseModel):
    course_code:   str
    name:          str
    description:   Optional[str]     = None
    credits:       Optional[Decimal] = None
    hours:         Optional[int]     = None
    department_id: Optional[int]     = None
    course_type:   str = "必修"
    status:        str = "active"

class CourseCreate(CourseBase):
    pass

class CourseUpdate(BaseModel):
    name:          Optional[str]     = None
    description:   Optional[str]     = None
    credits:       Optional[Decimal] = None
    hours:         Optional[int]     = None
    department_id: Optional[int]     = None
    course_type:   Optional[str]     = None
    status:        Optional[str]     = None

class CourseResponse(CourseBase):
    id:              int
    department_name: Optional[str] = None
    created_at:      Optional[datetime] = None
    updated_at:      Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Semester ───────────────────────────────────────────────────
class SemesterBase(BaseModel):
    name:          str
    academic_year: Optional[str] = None
    start_date:    date
    end_date:      date
    status:        str = "active"

class SemesterCreate(SemesterBase):
    pass

class SemesterUpdate(BaseModel):
    name:          Optional[str]  = None
    academic_year: Optional[str]  = None
    start_date:    Optional[date] = None
    end_date:      Optional[date] = None
    status:        Optional[str]  = None

class SemesterResponse(SemesterBase):
    id:         int
    created_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Classroom ──────────────────────────────────────────────────
class ClassroomBase(BaseModel):
    room_number: str
    building:    Optional[str] = None
    capacity:    int = 50
    room_type:   str = "普通教室"
    status:      str = "available"

class ClassroomCreate(ClassroomBase):
    pass

class ClassroomUpdate(BaseModel):
    building:  Optional[str] = None
    capacity:  Optional[int] = None
    room_type: Optional[str] = None
    status:    Optional[str] = None

class ClassroomResponse(ClassroomBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


# ─── CourseSchedule ─────────────────────────────────────────────
class CourseScheduleBase(BaseModel):
    course_id:    Optional[int]  = None
    teacher_id:   Optional[int]  = None
    semester_id:  Optional[int]  = None
    classroom_id: Optional[int]  = None
    day_of_week:  Optional[int]  = None
    start_time:   time
    end_time:     time
    max_students: int = 50
    status:       str = "active"

class CourseScheduleCreate(CourseScheduleBase):
    pass

class CourseScheduleUpdate(BaseModel):
    teacher_id:   Optional[int]  = None
    classroom_id: Optional[int]  = None
    day_of_week:  Optional[int]  = None
    start_time:   Optional[time] = None
    end_time:     Optional[time] = None
    max_students: Optional[int]  = None
    status:       Optional[str]  = None

class CourseScheduleResponse(CourseScheduleBase):
    id:               int
    current_students: int
    course_name:      Optional[str] = None
    teacher_name:     Optional[str] = None
    semester_name:    Optional[str] = None
    classroom_number: Optional[str] = None
    created_at:       Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Enrollment ─────────────────────────────────────────────────
class EnrollmentBase(BaseModel):
    student_id:  Optional[int] = None
    schedule_id: Optional[int] = None
    status:      str = "enrolled"

class EnrollmentCreate(EnrollmentBase):
    pass

class EnrollmentUpdate(BaseModel):
    status: Optional[str] = None

class EnrollmentResponse(EnrollmentBase):
    id:              int
    enrollment_date: Optional[datetime] = None
    student_name:    Optional[str] = None
    course_name:     Optional[str] = None
    semester_name:   Optional[str] = None
    created_at:      Optional[datetime] = None
    updated_at:      Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── Grade ──────────────────────────────────────────────────────
class GradeBase(BaseModel):
    enrollment_id: Optional[int]     = None
    score:         Optional[Decimal] = None
    grade_letter:  Optional[str]     = None
    comment:       Optional[str]     = None
    graded_by:     Optional[int]     = None

class GradeCreate(GradeBase):
    pass

class GradeUpdate(BaseModel):
    score:        Optional[Decimal] = None
    grade_letter: Optional[str]    = None
    comment:      Optional[str]    = None

class GradeResponse(GradeBase):
    id:           int
    graded_at:    Optional[datetime] = None
    student_name: Optional[str] = None
    course_name:  Optional[str] = None
    created_at:   Optional[datetime] = None
    updated_at:   Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ─── KPI ────────────────────────────────────────────────────────
class KpiStatsResponse(BaseModel):
    total_students:    int
    total_teachers:    int
    total_courses:     int
    total_enrollments: int
    active_semesters:  int

class KpiEventResponse(BaseModel):
    event_time:   datetime
    metric_name:  str
    metric_value: Optional[Decimal] = None
    dimension:    Optional[dict]    = None
    tags:         Optional[dict]    = None
    model_config = ConfigDict(from_attributes=True)
