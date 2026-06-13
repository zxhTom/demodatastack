-- 02_schema.sql: 教务管理系统数据库 Schema

-- 院系
CREATE TABLE IF NOT EXISTS departments (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    code        VARCHAR(20)  UNIQUE NOT NULL,
    description TEXT,
    dean        VARCHAR(50),
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- 教师
CREATE TABLE IF NOT EXISTS teachers (
    id            SERIAL PRIMARY KEY,
    employee_id   VARCHAR(20)  UNIQUE NOT NULL,
    name          VARCHAR(50)  NOT NULL,
    gender        VARCHAR(10),
    email         VARCHAR(100) UNIQUE,
    phone         VARCHAR(20),
    department_id INTEGER      REFERENCES departments(id) ON DELETE SET NULL,
    title         VARCHAR(50),
    hire_date     DATE,
    status        VARCHAR(20)  DEFAULT 'active',
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- 学生
CREATE TABLE IF NOT EXISTS students (
    id              SERIAL PRIMARY KEY,
    student_id      VARCHAR(20)  UNIQUE NOT NULL,
    name            VARCHAR(50)  NOT NULL,
    gender          VARCHAR(10),
    email           VARCHAR(100) UNIQUE,
    phone           VARCHAR(20),
    department_id   INTEGER      REFERENCES departments(id) ON DELETE SET NULL,
    grade           INTEGER,
    class_name      VARCHAR(50),
    enrollment_date DATE,
    status          VARCHAR(20)  DEFAULT 'active',
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- 课程
CREATE TABLE IF NOT EXISTS courses (
    id            SERIAL PRIMARY KEY,
    course_code   VARCHAR(20)   UNIQUE NOT NULL,
    name          VARCHAR(100)  NOT NULL,
    description   TEXT,
    credits       DECIMAL(3,1),
    hours         INTEGER,
    department_id INTEGER       REFERENCES departments(id) ON DELETE SET NULL,
    course_type   VARCHAR(50)   DEFAULT '必修',
    status        VARCHAR(20)   DEFAULT 'active',
    created_at    TIMESTAMPTZ   DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   DEFAULT NOW()
);

-- 学期
CREATE TABLE IF NOT EXISTS semesters (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(50)  NOT NULL,
    academic_year VARCHAR(20),
    start_date    DATE         NOT NULL,
    end_date      DATE         NOT NULL,
    status        VARCHAR(20)  DEFAULT 'active',
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- 教室
CREATE TABLE IF NOT EXISTS classrooms (
    id          SERIAL PRIMARY KEY,
    room_number VARCHAR(20)  UNIQUE NOT NULL,
    building    VARCHAR(50),
    capacity    INTEGER      DEFAULT 50,
    room_type   VARCHAR(50)  DEFAULT '普通教室',
    status      VARCHAR(20)  DEFAULT 'available'
);

-- 课程安排
CREATE TABLE IF NOT EXISTS course_schedules (
    id               SERIAL PRIMARY KEY,
    course_id        INTEGER     REFERENCES courses(id)   ON DELETE CASCADE,
    teacher_id       INTEGER     REFERENCES teachers(id)  ON DELETE SET NULL,
    semester_id      INTEGER     REFERENCES semesters(id) ON DELETE CASCADE,
    classroom_id     INTEGER     REFERENCES classrooms(id) ON DELETE SET NULL,
    day_of_week      SMALLINT    CHECK (day_of_week BETWEEN 1 AND 7),
    start_time       TIME        NOT NULL,
    end_time         TIME        NOT NULL,
    max_students     INTEGER     DEFAULT 50,
    current_students INTEGER     DEFAULT 0,
    status           VARCHAR(20) DEFAULT 'active',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(classroom_id, semester_id, day_of_week, start_time)
);

-- 选课
CREATE TABLE IF NOT EXISTS enrollments (
    id              SERIAL PRIMARY KEY,
    student_id      INTEGER     REFERENCES students(id)         ON DELETE CASCADE,
    schedule_id     INTEGER     REFERENCES course_schedules(id) ON DELETE CASCADE,
    enrollment_date TIMESTAMPTZ DEFAULT NOW(),
    status          VARCHAR(20) DEFAULT 'enrolled',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(student_id, schedule_id)
);

-- 成绩
CREATE TABLE IF NOT EXISTS grades (
    id            SERIAL PRIMARY KEY,
    enrollment_id INTEGER        REFERENCES enrollments(id) ON DELETE CASCADE,
    score         DECIMAL(5,2)   CHECK (score >= 0 AND score <= 100),
    grade_letter  VARCHAR(5),
    comment       TEXT,
    graded_at     TIMESTAMPTZ,
    graded_by     INTEGER        REFERENCES teachers(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ    DEFAULT NOW(),
    updated_at    TIMESTAMPTZ    DEFAULT NOW()
);

-- 考勤
CREATE TABLE IF NOT EXISTS attendance (
    id            SERIAL PRIMARY KEY,
    enrollment_id INTEGER     REFERENCES enrollments(id) ON DELETE CASCADE,
    date          DATE        NOT NULL,
    status        VARCHAR(20) DEFAULT 'present',
    notes         TEXT,
    recorded_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(enrollment_id, date)
);

-- 系统用户
CREATE TABLE IF NOT EXISTS users (
    id           SERIAL PRIMARY KEY,
    username     VARCHAR(50)  UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email        VARCHAR(100),
    role         VARCHAR(20)  DEFAULT 'student',
    reference_id INTEGER,
    is_active    BOOLEAN      DEFAULT TRUE,
    last_login   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_teachers_dept      ON teachers(department_id);
CREATE INDEX IF NOT EXISTS idx_students_dept      ON students(department_id);
CREATE INDEX IF NOT EXISTS idx_courses_dept       ON courses(department_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments(student_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_schedule ON enrollments(schedule_id);
CREATE INDEX IF NOT EXISTS idx_grades_enrollment  ON grades(enrollment_id);
CREATE INDEX IF NOT EXISTS idx_attendance_enrollment ON attendance(enrollment_id);

-- 自动更新 updated_at 触发器
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_departments_updated_at') THEN
        CREATE TRIGGER trg_departments_updated_at BEFORE UPDATE ON departments FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_teachers_updated_at') THEN
        CREATE TRIGGER trg_teachers_updated_at BEFORE UPDATE ON teachers FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_students_updated_at') THEN
        CREATE TRIGGER trg_students_updated_at BEFORE UPDATE ON students FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_courses_updated_at') THEN
        CREATE TRIGGER trg_courses_updated_at BEFORE UPDATE ON courses FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_enrollments_updated_at') THEN
        CREATE TRIGGER trg_enrollments_updated_at BEFORE UPDATE ON enrollments FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_grades_updated_at') THEN
        CREATE TRIGGER trg_grades_updated_at BEFORE UPDATE ON grades FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;
