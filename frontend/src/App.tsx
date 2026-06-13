import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './store/auth'
import LoginPage from './pages/LoginPage'
import AppLayout from './components/AppLayout'
import DashboardPage from './pages/DashboardPage'
import DepartmentsPage from './pages/DepartmentsPage'
import TeachersPage from './pages/TeachersPage'
import StudentsPage from './pages/StudentsPage'
import CoursesPage from './pages/CoursesPage'
import EnrollmentsPage from './pages/EnrollmentsPage'
import GradesPage from './pages/GradesPage'

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token)
  if (!token) return <Navigate to="/login" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard"   element={<DashboardPage />} />
        <Route path="departments" element={<DepartmentsPage />} />
        <Route path="teachers"    element={<TeachersPage />} />
        <Route path="students"    element={<StudentsPage />} />
        <Route path="courses"     element={<CoursesPage />} />
        <Route path="enrollments" element={<EnrollmentsPage />} />
        <Route path="grades"      element={<GradesPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
