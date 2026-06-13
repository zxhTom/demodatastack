-- 03_data.sql: 教务管理系统初始化数据

-- 院系数据
INSERT INTO departments (name, code, description, dean) VALUES
('计算机学院',   'CS',   '培养计算机科学与技术专业人才',   '李明'),
('数学学院',     'MATH', '基础数学与应用数学研究',         '王芳'),
('外语学院',     'FL',   '英语、日语等外语专业教学',       '张华'),
('物理学院',     'PHY',  '理论物理与实验物理教学',         '刘强'),
('化学学院',     'CHEM', '有机化学与无机化学研究',         '陈静'),
('经济学院',     'ECON', '经济学与管理学教学与研究',       '赵磊')
ON CONFLICT (code) DO NOTHING;

-- 教室数据
INSERT INTO classrooms (room_number, building, capacity, room_type) VALUES
('A101', 'A楼', 60, '普通教室'),
('A102', 'A楼', 60, '普通教室'),
('A103', 'A楼', 80, '大教室'),
('B201', 'B楼', 50, '普通教室'),
('B202', 'B楼', 50, '普通教室'),
('C301', 'C楼', 40, '实验室'),
('C302', 'C楼', 40, '实验室'),
('D401', 'D楼', 100, '报告厅'),
('E101', 'E楼', 30, '讨论室'),
('E102', 'E楼', 30, '讨论室')
ON CONFLICT (room_number) DO NOTHING;

-- 学期数据
INSERT INTO semesters (name, academic_year, start_date, end_date, status) VALUES
('2024春季学期', '2023-2024', '2024-02-26', '2024-06-28', 'completed'),
('2024秋季学期', '2024-2025', '2024-09-02', '2025-01-17', 'active'),
('2025春季学期', '2024-2025', '2025-02-24', '2025-06-27', 'active')
ON CONFLICT DO NOTHING;

-- 教师数据
INSERT INTO teachers (employee_id, name, gender, email, phone, department_id, title, hire_date) VALUES
('T001', '张伟',   '男', 'zhangwei@edu.com',   '13800001001', 1, '教授',     '2010-09-01'),
('T002', '李娜',   '女', 'lina@edu.com',       '13800001002', 1, '副教授',   '2015-03-01'),
('T003', '王磊',   '男', 'wanglei@edu.com',    '13800001003', 1, '讲师',     '2020-09-01'),
('T004', '陈丽',   '女', 'chenli@edu.com',     '13800001004', 2, '教授',     '2008-09-01'),
('T005', '刘建国', '男', 'liujianguo@edu.com', '13800001005', 2, '副教授',   '2012-03-01'),
('T006', '赵敏',   '女', 'zhaomin@edu.com',    '13800001006', 2, '讲师',     '2019-09-01'),
('T007', '孙莉',   '女', 'sunli@edu.com',      '13800001007', 3, '教授',     '2009-09-01'),
('T008', '周平',   '男', 'zhouping@edu.com',   '13800001008', 3, '副教授',   '2014-03-01'),
('T009', '吴红',   '女', 'wuhong@edu.com',     '13800001009', 3, '讲师',     '2021-09-01'),
('T010', '郑超',   '男', 'zhengchao@edu.com',  '13800001010', 4, '教授',     '2007-09-01'),
('T011', '冯燕',   '女', 'fengyan@edu.com',    '13800001011', 4, '讲师',     '2018-03-01'),
('T012', '蒋志远', '男', 'jiangzhiyuan@edu.com','13800001012', 5, '副教授',  '2013-09-01'),
('T013', '韩雪',   '女', 'hanxue@edu.com',     '13800001013', 5, '讲师',     '2022-03-01'),
('T014', '曹阳',   '男', 'caoyang@edu.com',    '13800001014', 6, '教授',     '2006-09-01'),
('T015', '邓玲',   '女', 'dengling@edu.com',   '13800001015', 6, '副教授',   '2016-09-01'),
('T016', '杨帆',   '男', 'yangfan@edu.com',    '13800001016', 6, '讲师',     '2021-03-01'),
('T017', '石晓',   '女', 'shixiao@edu.com',    '13800001017', 1, '讲师',     '2023-09-01'),
('T018', '林东',   '男', 'lindong@edu.com',    '13800001018', 4, '讲师',     '2022-09-01')
ON CONFLICT (employee_id) DO NOTHING;

-- 学生数据 (每个院系约10名)
INSERT INTO students (student_id, name, gender, email, phone, department_id, grade, class_name, enrollment_date) VALUES
-- 计算机学院 (department_id=1)
('S20210101','赵云','男','zhaoyun@stu.edu.com','13900010101',1,3,'计算机2021-1','2021-09-01'),
('S20210102','钱多多','女','qianduoduo@stu.edu.com','13900010102',1,3,'计算机2021-1','2021-09-01'),
('S20210103','孙策','男','suncee@stu.edu.com','13900010103',1,3,'计算机2021-2','2021-09-01'),
('S20220101','周瑜','男','zhouyu@stu.edu.com','13900020101',1,2,'计算机2022-1','2022-09-01'),
('S20220102','吕布','男','lvbu@stu.edu.com','13900020102',1,2,'计算机2022-1','2022-09-01'),
('S20220103','貂蝉','女','diaochan@stu.edu.com','13900020103',1,2,'计算机2022-2','2022-09-01'),
('S20230101','关羽','男','guanyu@stu.edu.com','13900030101',1,1,'计算机2023-1','2023-09-01'),
('S20230102','张飞','男','zhangfei@stu.edu.com','13900030102',1,1,'计算机2023-1','2023-09-01'),
('S20230103','刘备','男','liubei@stu.edu.com','13900030103',1,1,'计算机2023-2','2023-09-01'),
('S20230104','诸葛亮','男','zhugeliang@stu.edu.com','13900030104',1,1,'计算机2023-2','2023-09-01'),
-- 数学学院 (department_id=2)
('S20210201','华容','女','huarong@stu.edu.com','13900010201',2,3,'数学2021-1','2021-09-01'),
('S20210202','黄盖','男','huanggai@stu.edu.com','13900010202',2,3,'数学2021-1','2021-09-01'),
('S20220201','周仓','男','zhoucang@stu.edu.com','13900020201',2,2,'数学2022-1','2022-09-01'),
('S20220202','马超','男','machao@stu.edu.com','13900020202',2,2,'数学2022-1','2022-09-01'),
('S20230201','黄忠','男','huangzhong@stu.edu.com','13900030201',2,1,'数学2023-1','2023-09-01'),
('S20230202','赵颖','女','zhaoying@stu.edu.com','13900030202',2,1,'数学2023-1','2023-09-01'),
-- 外语学院 (department_id=3)
('S20210301','魏延','男','weiyan@stu.edu.com','13900010301',3,3,'英语2021-1','2021-09-01'),
('S20210302','姜维','男','jiangwei@stu.edu.com','13900010302',3,3,'英语2021-1','2021-09-01'),
('S20220301','邓艾','男','dengai@stu.edu.com','13900020301',3,2,'英语2022-1','2022-09-01'),
('S20230301','钟会','男','zhonghui@stu.edu.com','13900030301',3,1,'英语2023-1','2023-09-01'),
-- 物理学院 (department_id=4)
('S20210401','司马懿','男','simayie@stu.edu.com','13900010401',4,3,'物理2021-1','2021-09-01'),
('S20220401','曹操','男','caocao@stu.edu.com','13900020401',4,2,'物理2022-1','2022-09-01'),
('S20230401','曹仁','男','caoren@stu.edu.com','13900030401',4,1,'物理2023-1','2023-09-01'),
-- 化学学院 (department_id=5)
('S20210501','夏侯惇','男','xiahoudun@stu.edu.com','13900010501',5,3,'化学2021-1','2021-09-01'),
('S20220501','徐晃','男','xuhuang@stu.edu.com','13900020501',5,2,'化学2022-1','2022-09-01'),
('S20230501','张辽','男','zhangliao@stu.edu.com','13900030501',5,1,'化学2023-1','2023-09-01'),
-- 经济学院 (department_id=6)
('S20210601','李典','男','lidian@stu.edu.com','13900010601',6,3,'经济2021-1','2021-09-01'),
('S20210602','乐进','男','lejin@stu.edu.com','13900010602',6,3,'经济2021-2','2021-09-01'),
('S20220601','于禁','男','yujin@stu.edu.com','13900020601',6,2,'经济2022-1','2022-09-01'),
('S20230601','庞德','男','pangde@stu.edu.com','13900030601',6,1,'经济2023-1','2023-09-01')
ON CONFLICT (student_id) DO NOTHING;

-- 课程数据
INSERT INTO courses (course_code, name, description, credits, hours, department_id, course_type) VALUES
-- 计算机学院
('CS101','程序设计基础','C/C++程序设计入门',3.0,48,1,'必修'),
('CS201','数据结构与算法','线性表、树、图、排序与查找',4.0,64,1,'必修'),
('CS301','操作系统原理','进程、内存、文件系统',3.0,48,1,'必修'),
('CS302','计算机网络','TCP/IP协议族与网络编程',3.0,48,1,'必修'),
('CS401','数据库系统','关系数据库设计与优化',3.0,48,1,'必修'),
-- 数学学院
('MATH101','高等数学（上）','极限、导数、积分',5.0,80,2,'必修'),
('MATH102','高等数学（下）','多元函数、级数、微分方程',5.0,80,2,'必修'),
('MATH201','线性代数','矩阵运算与线性变换',3.0,48,2,'必修'),
('MATH301','概率论与数理统计','概率分布与统计推断',3.0,48,2,'必修'),
-- 外语学院
('FL101','大学英语（一）','基础英语听说读写',4.0,64,3,'必修'),
('FL102','大学英语（二）','中级英语综合训练',4.0,64,3,'必修'),
('FL201','英语口语','日常交流与演讲训练',2.0,32,3,'选修'),
-- 物理学院
('PHY101','大学物理（上）','力学与热学',4.0,64,4,'必修'),
('PHY102','大学物理（下）','电磁学与光学',4.0,64,4,'必修'),
('PHY201','物理实验','实验操作与数据处理',1.5,24,4,'必修'),
-- 化学学院
('CHEM101','普通化学','原子结构与化学反应',3.0,48,5,'必修'),
('CHEM201','有机化学','有机物结构与反应机理',4.0,64,5,'必修'),
-- 经济学院
('ECON101','微观经济学','供需理论与市场结构',3.0,48,6,'必修'),
('ECON201','宏观经济学','国民收入与货币政策',3.0,48,6,'必修'),
('ECON301','会计学原理','财务会计基础',3.0,48,6,'必修')
ON CONFLICT (course_code) DO NOTHING;

-- 课程安排 (2024秋季学期, semester_id=2)
INSERT INTO course_schedules (course_id, teacher_id, semester_id, classroom_id, day_of_week, start_time, end_time, max_students)
SELECT c.id, t.id, 2, r.id, sched.dow, sched.st::TIME, sched.et::TIME, 50
FROM (VALUES
    ('CS101',  'T001', 'A101', 1, '08:00', '09:40'),
    ('CS201',  'T002', 'A102', 2, '10:00', '11:40'),
    ('CS301',  'T003', 'A103', 3, '14:00', '15:40'),
    ('CS302',  'T001', 'B201', 4, '08:00', '09:40'),
    ('CS401',  'T002', 'B202', 5, '10:00', '11:40'),
    ('MATH101','T004', 'A101', 1, '10:00', '11:40'),
    ('MATH102','T005', 'A102', 3, '08:00', '09:40'),
    ('MATH201','T006', 'A103', 2, '14:00', '15:40'),
    ('MATH301','T004', 'B201', 4, '10:00', '11:40'),
    ('FL101',  'T007', 'A101', 2, '08:00', '09:40'),
    ('FL102',  'T008', 'A102', 4, '14:00', '15:40'),
    ('FL201',  'T009', 'B201', 5, '14:00', '15:40'),
    ('PHY101', 'T010', 'A103', 1, '14:00', '15:40'),
    ('PHY102', 'T018', 'B201', 2, '10:00', '11:40'),
    ('PHY201', 'T010', 'C301', 3, '10:00', '11:40'),
    ('CHEM101','T012', 'C302', 1, '08:00', '09:40'),
    ('CHEM201','T013', 'A101', 3, '10:00', '11:40'),
    ('ECON101','T014', 'A102', 5, '08:00', '09:40'),
    ('ECON201','T015', 'A103', 1, '10:00', '11:40'),
    ('ECON301','T016', 'B202', 2, '14:00', '15:40')
) AS sched(cc, eid, rn, dow, st, et)
JOIN courses    c ON c.course_code  = sched.cc
JOIN teachers   t ON t.employee_id  = sched.eid
JOIN classrooms r ON r.room_number  = sched.rn
ON CONFLICT DO NOTHING;

-- 选课数据 (每个学生选几门课)
INSERT INTO enrollments (student_id, schedule_id, status)
SELECT s.id, cs.id, 'enrolled'
FROM students s
JOIN course_schedules cs ON TRUE
JOIN courses c ON c.id = cs.course_id
WHERE cs.semester_id = 2
  AND (
    (s.department_id = 1 AND c.course_code IN ('CS101','CS201','MATH101','FL101','PHY101'))
 OR (s.department_id = 2 AND c.course_code IN ('MATH101','MATH102','MATH201','PHY101','FL101'))
 OR (s.department_id = 3 AND c.course_code IN ('FL101','FL102','FL201','MATH101','ECON101'))
 OR (s.department_id = 4 AND c.course_code IN ('PHY101','PHY102','PHY201','MATH101','CS101'))
 OR (s.department_id = 5 AND c.course_code IN ('CHEM101','CHEM201','PHY101','MATH101','FL101'))
 OR (s.department_id = 6 AND c.course_code IN ('ECON101','ECON201','ECON301','MATH301','FL101'))
  )
ON CONFLICT (student_id, schedule_id) DO NOTHING;

-- 更新课程安排的实际人数
UPDATE course_schedules cs
SET current_students = (
    SELECT COUNT(*) FROM enrollments e WHERE e.schedule_id = cs.id AND e.status = 'enrolled'
);

-- 成绩数据 (为3年级学生打部分成绩)
INSERT INTO grades (enrollment_id, score, grade_letter, graded_at, graded_by)
SELECT e.id,
       (70 + floor(random() * 30))::DECIMAL(5,2) AS score,
       CASE WHEN (70 + floor(random() * 30)) >= 90 THEN 'A'
            WHEN (70 + floor(random() * 30)) >= 80 THEN 'B'
            WHEN (70 + floor(random() * 30)) >= 70 THEN 'C'
            ELSE 'D' END,
       NOW() - (random() * INTERVAL '30 days'),
       cs.teacher_id
FROM enrollments e
JOIN course_schedules cs ON cs.id = e.schedule_id
JOIN students s ON s.id = e.student_id
WHERE s.grade = 3
  AND e.status = 'enrolled'
ON CONFLICT DO NOTHING;

-- 系统用户
INSERT INTO users (username, password_hash, email, role, is_active) VALUES
('admin',    crypt('admin123',    gen_salt('bf', 12)), 'admin@edu.com',    'admin',   TRUE),
('teacher01',crypt('teacher123',  gen_salt('bf', 12)), 'teacher01@edu.com','teacher', TRUE),
('student01',crypt('student123',  gen_salt('bf', 12)), 'student01@edu.com','student', TRUE)
ON CONFLICT (username) DO NOTHING;
