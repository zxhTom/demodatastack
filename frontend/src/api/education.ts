import client from './client'

export interface PaginatedResponse<T> {
  total: number
  skip: number
  limit: number
  items: T[]
}

// ─── Departments ────────────────────────────────────────────────
export interface Department {
  id: number
  name: string
  code: string
  description?: string
  dean?: string
  created_at?: string
  updated_at?: string
}

export const departmentsApi = {
  list: (params?: { skip?: number; limit?: number }) =>
    client.get<PaginatedResponse<Department>>('/departments', { params }).then((r) => r.data),
  get: (id: number) => client.get<Department>(`/departments/${id}`).then((r) => r.data),
  create: (data: Partial<Department>) => client.post<Department>('/departments', data).then((r) => r.data),
  update: (id: number, data: Partial<Department>) => client.put<Department>(`/departments/${id}`, data).then((r) => r.data),
  remove: (id: number) => client.delete(`/departments/${id}`),
}

// ─── Teachers ───────────────────────────────────────────────────
export interface Teacher {
  id: number
  employee_id: string
  name: string
  gender?: string
  email?: string
  phone?: string
  department_id?: number
  department_name?: string
  title?: string
  hire_date?: string
  status: string
}

export const teachersApi = {
  list: (params?: { skip?: number; limit?: number; department_id?: number }) =>
    client.get<PaginatedResponse<Teacher>>('/teachers', { params }).then((r) => r.data),
  get: (id: number) => client.get<Teacher>(`/teachers/${id}`).then((r) => r.data),
  create: (data: Partial<Teacher>) => client.post<Teacher>('/teachers', data).then((r) => r.data),
  update: (id: number, data: Partial<Teacher>) => client.put<Teacher>(`/teachers/${id}`, data).then((r) => r.data),
  remove: (id: number) => client.delete(`/teachers/${id}`),
}

// ─── Students ───────────────────────────────────────────────────
export interface Student {
  id: number
  student_id: string
  name: string
  gender?: string
  email?: string
  phone?: string
  department_id?: number
  department_name?: string
  grade?: number
  class_name?: string
  enrollment_date?: string
  status: string
}

export const studentsApi = {
  list: (params?: { skip?: number; limit?: number; department_id?: number; grade?: number; status?: string }) =>
    client.get<PaginatedResponse<Student>>('/students', { params }).then((r) => r.data),
  get: (id: number) => client.get<Student>(`/students/${id}`).then((r) => r.data),
  create: (data: Partial<Student>) => client.post<Student>('/students', data).then((r) => r.data),
  update: (id: number, data: Partial<Student>) => client.put<Student>(`/students/${id}`, data).then((r) => r.data),
  remove: (id: number) => client.delete(`/students/${id}`),
}

// ─── Courses ────────────────────────────────────────────────────
export interface Course {
  id: number
  course_code: string
  name: string
  description?: string
  credits?: number
  hours?: number
  department_id?: number
  department_name?: string
  course_type: string
  status: string
}

export const coursesApi = {
  list: (params?: { skip?: number; limit?: number; department_id?: number; course_type?: string }) =>
    client.get<PaginatedResponse<Course>>('/courses', { params }).then((r) => r.data),
  get: (id: number) => client.get<Course>(`/courses/${id}`).then((r) => r.data),
  create: (data: Partial<Course>) => client.post<Course>('/courses', data).then((r) => r.data),
  update: (id: number, data: Partial<Course>) => client.put<Course>(`/courses/${id}`, data).then((r) => r.data),
  remove: (id: number) => client.delete(`/courses/${id}`),
}

// ─── Enrollments ────────────────────────────────────────────────
export interface Enrollment {
  id: number
  student_id?: number
  schedule_id?: number
  student_name?: string
  course_name?: string
  semester_name?: string
  enrollment_date?: string
  status: string
}

export const enrollmentsApi = {
  list: (params?: { skip?: number; limit?: number; student_id?: number; schedule_id?: number }) =>
    client.get<PaginatedResponse<Enrollment>>('/enrollments', { params }).then((r) => r.data),
  create: (data: Partial<Enrollment>) => client.post<Enrollment>('/enrollments', data).then((r) => r.data),
  update: (id: number, data: Partial<Enrollment>) => client.put<Enrollment>(`/enrollments/${id}`, data).then((r) => r.data),
  remove: (id: number) => client.delete(`/enrollments/${id}`),
}

// ─── Grades ─────────────────────────────────────────────────────
export interface Grade {
  id: number
  enrollment_id?: number
  score?: number
  grade_letter?: string
  comment?: string
  graded_at?: string
  student_name?: string
  course_name?: string
}

export const gradesApi = {
  list: (params?: { skip?: number; limit?: number; enrollment_id?: number; student_id?: number }) =>
    client.get<PaginatedResponse<Grade>>('/grades', { params }).then((r) => r.data),
  create: (data: Partial<Grade>) => client.post<Grade>('/grades', data).then((r) => r.data),
  update: (id: number, data: Partial<Grade>) => client.put<Grade>(`/grades/${id}`, data).then((r) => r.data),
  remove: (id: number) => client.delete(`/grades/${id}`),
}
