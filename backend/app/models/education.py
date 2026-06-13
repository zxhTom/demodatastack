from __future__ import annotations
from datetime import datetime, date, time
from decimal import Decimal
from typing import Optional
from sqlalchemy import (
    Integer, String, Text, Boolean, Date, Time,
    DECIMAL, ForeignKey, SmallInteger, UniqueConstraint, CheckConstraint,
    BigInteger, DateTime,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import INET, JSONB
from app.database import Base

# Use DateTime(timezone=True) as the portable TIMESTAMPTZ equivalent
TIMESTAMPTZ = DateTime(timezone=True)


class Department(Base):
    __tablename__ = "departments"

    id:          Mapped[int]            = mapped_column(Integer,     primary_key=True)
    name:        Mapped[str]            = mapped_column(String(100),  nullable=False)
    code:        Mapped[str]            = mapped_column(String(20),   unique=True, nullable=False)
    description: Mapped[Optional[str]]  = mapped_column(Text)
    dean:        Mapped[Optional[str]]  = mapped_column(String(50))
    created_at:  Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    updated_at:  Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    teachers: Mapped[list["Teacher"]] = relationship("Teacher", back_populates="department")
    students: Mapped[list["Student"]] = relationship("Student", back_populates="department")
    courses:  Mapped[list["Course"]]  = relationship("Course",  back_populates="department")


class Teacher(Base):
    __tablename__ = "teachers"

    id:            Mapped[int]              = mapped_column(Integer,    primary_key=True)
    employee_id:   Mapped[str]              = mapped_column(String(20), unique=True, nullable=False)
    name:          Mapped[str]              = mapped_column(String(50), nullable=False)
    gender:        Mapped[Optional[str]]    = mapped_column(String(10))
    email:         Mapped[Optional[str]]    = mapped_column(String(100), unique=True)
    phone:         Mapped[Optional[str]]    = mapped_column(String(20))
    department_id: Mapped[Optional[int]]    = mapped_column(Integer, ForeignKey("departments.id", ondelete="SET NULL"))
    title:         Mapped[Optional[str]]    = mapped_column(String(50))
    hire_date:     Mapped[Optional[date]]   = mapped_column(Date)
    status:        Mapped[str]              = mapped_column(String(20), default="active")
    created_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    updated_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    department: Mapped[Optional["Department"]] = relationship("Department", back_populates="teachers")


class Student(Base):
    __tablename__ = "students"

    id:              Mapped[int]             = mapped_column(Integer,     primary_key=True)
    student_id:      Mapped[str]             = mapped_column(String(20),  unique=True, nullable=False)
    name:            Mapped[str]             = mapped_column(String(50),  nullable=False)
    gender:          Mapped[Optional[str]]   = mapped_column(String(10))
    email:           Mapped[Optional[str]]   = mapped_column(String(100), unique=True)
    phone:           Mapped[Optional[str]]   = mapped_column(String(20))
    department_id:   Mapped[Optional[int]]   = mapped_column(Integer, ForeignKey("departments.id", ondelete="SET NULL"))
    grade:           Mapped[Optional[int]]   = mapped_column(Integer)
    class_name:      Mapped[Optional[str]]   = mapped_column(String(50))
    enrollment_date: Mapped[Optional[date]]  = mapped_column(Date)
    status:          Mapped[str]             = mapped_column(String(20), default="active")
    created_at:      Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    updated_at:      Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    department: Mapped[Optional["Department"]] = relationship("Department", back_populates="students")


class Course(Base):
    __tablename__ = "courses"

    id:            Mapped[int]            = mapped_column(Integer,       primary_key=True)
    course_code:   Mapped[str]            = mapped_column(String(20),    unique=True, nullable=False)
    name:          Mapped[str]            = mapped_column(String(100),   nullable=False)
    description:   Mapped[Optional[str]] = mapped_column(Text)
    credits:       Mapped[Optional[Decimal]] = mapped_column(DECIMAL(3, 1))
    hours:         Mapped[Optional[int]] = mapped_column(Integer)
    department_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("departments.id", ondelete="SET NULL"))
    course_type:   Mapped[str]           = mapped_column(String(50), default="必修")
    status:        Mapped[str]           = mapped_column(String(20), default="active")
    created_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    updated_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    department: Mapped[Optional["Department"]] = relationship("Department", back_populates="courses")


class Semester(Base):
    __tablename__ = "semesters"

    id:            Mapped[int]           = mapped_column(Integer,    primary_key=True)
    name:          Mapped[str]           = mapped_column(String(50), nullable=False)
    academic_year: Mapped[Optional[str]] = mapped_column(String(20))
    start_date:    Mapped[date]          = mapped_column(Date,        nullable=False)
    end_date:      Mapped[date]          = mapped_column(Date,        nullable=False)
    status:        Mapped[str]           = mapped_column(String(20),  default="active")
    created_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)


class Classroom(Base):
    __tablename__ = "classrooms"

    id:          Mapped[int]            = mapped_column(Integer,    primary_key=True)
    room_number: Mapped[str]            = mapped_column(String(20), unique=True, nullable=False)
    building:    Mapped[Optional[str]]  = mapped_column(String(50))
    capacity:    Mapped[int]            = mapped_column(Integer,    default=50)
    room_type:   Mapped[str]            = mapped_column(String(50), default="普通教室")
    status:      Mapped[str]            = mapped_column(String(20), default="available")


class CourseSchedule(Base):
    __tablename__ = "course_schedules"

    id:               Mapped[int]           = mapped_column(Integer,   primary_key=True)
    course_id:        Mapped[Optional[int]] = mapped_column(Integer,   ForeignKey("courses.id",    ondelete="CASCADE"))
    teacher_id:       Mapped[Optional[int]] = mapped_column(Integer,   ForeignKey("teachers.id",   ondelete="SET NULL"))
    semester_id:      Mapped[Optional[int]] = mapped_column(Integer,   ForeignKey("semesters.id",  ondelete="CASCADE"))
    classroom_id:     Mapped[Optional[int]] = mapped_column(Integer,   ForeignKey("classrooms.id", ondelete="SET NULL"))
    day_of_week:      Mapped[Optional[int]] = mapped_column(SmallInteger)
    start_time:       Mapped[time]          = mapped_column(Time,       nullable=False)
    end_time:         Mapped[time]          = mapped_column(Time,       nullable=False)
    max_students:     Mapped[int]           = mapped_column(Integer,    default=50)
    current_students: Mapped[int]           = mapped_column(Integer,    default=0)
    status:           Mapped[str]           = mapped_column(String(20), default="active")
    created_at:       Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    course:    Mapped[Optional["Course"]]    = relationship("Course")
    teacher:   Mapped[Optional["Teacher"]]   = relationship("Teacher")
    semester:  Mapped[Optional["Semester"]]  = relationship("Semester")
    classroom: Mapped[Optional["Classroom"]] = relationship("Classroom")


class Enrollment(Base):
    __tablename__ = "enrollments"

    id:              Mapped[int]           = mapped_column(Integer,    primary_key=True)
    student_id:      Mapped[Optional[int]] = mapped_column(Integer,    ForeignKey("students.id",          ondelete="CASCADE"))
    schedule_id:     Mapped[Optional[int]] = mapped_column(Integer,    ForeignKey("course_schedules.id",  ondelete="CASCADE"))
    enrollment_date: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    status:          Mapped[str]           = mapped_column(String(20), default="enrolled")
    created_at:      Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    updated_at:      Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    student:  Mapped[Optional["Student"]]         = relationship("Student")
    schedule: Mapped[Optional["CourseSchedule"]]   = relationship("CourseSchedule")

    __table_args__ = (UniqueConstraint("student_id", "schedule_id"),)


class Grade(Base):
    __tablename__ = "grades"

    id:            Mapped[int]              = mapped_column(Integer,      primary_key=True)
    enrollment_id: Mapped[Optional[int]]    = mapped_column(Integer,      ForeignKey("enrollments.id", ondelete="CASCADE"))
    score:         Mapped[Optional[Decimal]] = mapped_column(DECIMAL(5, 2))
    grade_letter:  Mapped[Optional[str]]    = mapped_column(String(5))
    comment:       Mapped[Optional[str]]    = mapped_column(Text)
    graded_at:     Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    graded_by:     Mapped[Optional[int]]    = mapped_column(Integer, ForeignKey("teachers.id", ondelete="SET NULL"))
    created_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)
    updated_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    enrollment: Mapped[Optional["Enrollment"]] = relationship("Enrollment")
    teacher:    Mapped[Optional["Teacher"]]    = relationship("Teacher")


class Attendance(Base):
    __tablename__ = "attendance"

    id:            Mapped[int]           = mapped_column(Integer,    primary_key=True)
    enrollment_id: Mapped[Optional[int]] = mapped_column(Integer,    ForeignKey("enrollments.id", ondelete="CASCADE"))
    date:          Mapped[date]          = mapped_column(Date,        nullable=False)
    status:        Mapped[str]           = mapped_column(String(20), default="present")
    notes:         Mapped[Optional[str]] = mapped_column(Text)
    recorded_at:   Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("enrollment_id", "date"),)


class User(Base):
    __tablename__ = "users"

    id:            Mapped[int]            = mapped_column(Integer,     primary_key=True)
    username:      Mapped[str]            = mapped_column(String(50),  unique=True, nullable=False)
    password_hash: Mapped[str]            = mapped_column(String(255), nullable=False)
    email:         Mapped[Optional[str]]  = mapped_column(String(100))
    role:          Mapped[str]            = mapped_column(String(20),  default="student")
    reference_id:  Mapped[Optional[int]]  = mapped_column(Integer)
    is_active:     Mapped[bool]           = mapped_column(Boolean,     default=True)
    last_login:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    created_at:    Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, default=datetime.utcnow)


class SystemLog(Base):
    __tablename__ = "system_logs"

    id:            Mapped[int]           = mapped_column(BigInteger, primary_key=True)
    log_time:      Mapped[datetime]      = mapped_column(TIMESTAMPTZ, default=datetime.utcnow, nullable=False)
    level:         Mapped[str]           = mapped_column(String(10),  nullable=False, default="INFO")
    service:       Mapped[Optional[str]] = mapped_column(String(50))
    user_id:       Mapped[Optional[int]] = mapped_column(Integer)
    action:        Mapped[Optional[str]] = mapped_column(String(100))
    resource_type: Mapped[Optional[str]] = mapped_column(String(50))
    resource_id:   Mapped[Optional[int]] = mapped_column(Integer)
    duration_ms:   Mapped[Optional[int]] = mapped_column(Integer)
    message:       Mapped[Optional[str]] = mapped_column(Text)
    meta_data:     Mapped[Optional[dict]] = mapped_column("metadata", JSONB)


class SystemLogTs(Base):
    __tablename__ = "system_logs_ts"

    log_time:      Mapped[datetime]      = mapped_column(TIMESTAMPTZ, primary_key=True, default=datetime.utcnow)
    level:         Mapped[str]           = mapped_column(String(10),  nullable=False, default="INFO")
    service:       Mapped[Optional[str]] = mapped_column(String(50))
    user_id:       Mapped[Optional[int]] = mapped_column(Integer)
    action:        Mapped[Optional[str]] = mapped_column(String(100))
    resource_type: Mapped[Optional[str]] = mapped_column(String(50))
    resource_id:   Mapped[Optional[int]] = mapped_column(Integer)
    duration_ms:   Mapped[Optional[int]] = mapped_column(Integer)
    message:       Mapped[Optional[str]] = mapped_column(Text)
    meta_data:     Mapped[Optional[dict]] = mapped_column("metadata", JSONB)


class KpiEvent(Base):
    __tablename__ = "kpi_events"

    event_time:   Mapped[datetime]        = mapped_column(TIMESTAMPTZ, primary_key=True, default=datetime.utcnow)
    metric_name:  Mapped[str]             = mapped_column(String(100), nullable=False)
    metric_value: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(15, 4))
    dimension:    Mapped[Optional[dict]]  = mapped_column("dimension", JSONB)
    tags_data:    Mapped[Optional[dict]]  = mapped_column("tags", JSONB)
