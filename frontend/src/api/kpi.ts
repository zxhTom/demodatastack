import client from './client'

export interface KpiStats {
  total_students: number
  total_teachers: number
  total_courses: number
  total_enrollments: number
  active_semesters: number
}

export interface KpiEvent {
  event_time: string
  metric_name: string
  metric_value?: number
  dimension?: Record<string, any>
  tags?: Record<string, any>
}

export const kpiApi = {
  getStats: () => client.get<KpiStats>('/kpi/stats').then((r) => r.data),
  getRecentEvents: (limit = 50) =>
    client.get<KpiEvent[]>('/kpi/recent-events', { params: { limit } }).then((r) => r.data),
}

export const createKpiWebSocket = (): WebSocket => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.hostname
  return new WebSocket(`${protocol}//${host}:8000/api/kpi/ws`)
}
