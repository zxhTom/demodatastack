import { useEffect, useState, useRef } from 'react'
import { Card, Row, Col, Statistic, Table, Tag, Typography, Space } from 'antd'
import { UserOutlined, TeamOutlined, BookOutlined, OrderedListOutlined, SyncOutlined } from '@ant-design/icons'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from 'recharts'
import { kpiApi, createKpiWebSocket, type KpiStats, type KpiEvent } from '../api/kpi'
import dayjs from 'dayjs'

const { Title, Text } = Typography

const metricColors: Record<string, string> = {
  student_count:    '#1677ff',
  teacher_count:    '#52c41a',
  course_count:     '#faad14',
  enrollment_count: '#f5222d',
  grade_count:      '#722ed1',
}

export default function DashboardPage() {
  const [stats, setStats] = useState<KpiStats | null>(null)
  const [events, setEvents] = useState<KpiEvent[]>([])
  const [chartData, setChartData] = useState<any[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    kpiApi.getStats().then(setStats).catch(console.error)
    kpiApi.getRecentEvents(30).then((data) => {
      setEvents(data)
      const grouped: Record<string, any> = {}
      data.slice().reverse().forEach((e) => {
        const t = dayjs(e.event_time).format('HH:mm:ss')
        if (!grouped[t]) grouped[t] = { time: t }
        grouped[t][e.metric_name] = (grouped[t][e.metric_name] || 0) + (e.metric_value || 0)
      })
      setChartData(Object.values(grouped).slice(-20))
    }).catch(console.error)
  }, [])

  useEffect(() => {
    const connect = () => {
      try {
        const ws = createKpiWebSocket()
        wsRef.current = ws
        ws.onopen = () => setConnected(true)
        ws.onmessage = (e) => {
          const data = JSON.parse(e.data)
          setStats({
            total_students:    data.total_students,
            total_teachers:    data.total_teachers,
            total_courses:     data.total_courses,
            total_enrollments: data.total_enrollments,
            active_semesters:  1,
          })
        }
        ws.onclose = () => {
          setConnected(false)
          setTimeout(connect, 5000)
        }
        ws.onerror = () => ws.close()
      } catch { /* ignore */ }
    }
    connect()
    return () => wsRef.current?.close()
  }, [])

  const columns = [
    { title: '时间', dataIndex: 'event_time', key: 'time', render: (v: string) => dayjs(v).format('MM-DD HH:mm:ss'), width: 160 },
    { title: '指标', dataIndex: 'metric_name', key: 'metric', render: (v: string) => <Tag color={metricColors[v] || 'blue'}>{v}</Tag> },
    { title: '变化值', dataIndex: 'metric_value', key: 'value', render: (v: number) => v > 0 ? `+${v}` : v },
    { title: '操作', dataIndex: 'dimension', key: 'op', render: (v: any) => v?.op ? <Tag>{v.op}</Tag> : '-' },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>KPI 实时监控仪表盘</Title>
        <Tag color={connected ? 'success' : 'default'} icon={<SyncOutlined spin={connected} />}>
          {connected ? 'WebSocket 已连接' : '连接中...'}
        </Tag>
      </Space>

      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card><Statistic title="学生总数" value={stats?.total_students ?? '-'} prefix={<UserOutlined />} valueStyle={{ color: '#1677ff' }} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="教师总数" value={stats?.total_teachers ?? '-'} prefix={<TeamOutlined />} valueStyle={{ color: '#52c41a' }} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="课程总数" value={stats?.total_courses ?? '-'} prefix={<BookOutlined />} valueStyle={{ color: '#faad14' }} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title="选课总数" value={stats?.total_enrollments ?? '-'} prefix={<OrderedListOutlined />} valueStyle={{ color: '#f5222d' }} /></Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={14}>
          <Card title="KPI 事件趋势（近期）" bodyStyle={{ padding: '8px 0' }}>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={chartData} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="time" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend />
                {Object.entries(metricColors).map(([key, color]) => (
                  <Line key={key} type="monotone" dataKey={key} stroke={color} dot={false} strokeWidth={2} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </Card>
        </Col>
        <Col span={10}>
          <Card title="最近 CDC 事件" bodyStyle={{ padding: 0 }}>
            <Table
              columns={columns}
              dataSource={events.slice(0, 15)}
              rowKey={(r, i) => `${r.event_time}-${i}`}
              size="small"
              pagination={false}
              scroll={{ y: 280 }}
            />
          </Card>
        </Col>
      </Row>
    </div>
  )
}
