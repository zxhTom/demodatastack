import { useState, useEffect, useCallback } from 'react'
import { Table, Card, Tag, Button, Space, Popconfirm, message, Typography } from 'antd'
import { DeleteOutlined } from '@ant-design/icons'
import { enrollmentsApi, type Enrollment } from '../api/education'
import dayjs from 'dayjs'

const { Text } = Typography

export default function EnrollmentsPage() {
  const [data, setData]         = useState<Enrollment[]>([])
  const [total, setTotal]       = useState(0)
  const [loading, setLoading]   = useState(false)
  const [page, setPage]         = useState(1)

  const fetchData = useCallback(async (p = page) => {
    setLoading(true)
    try {
      const res = await enrollmentsApi.list({ skip: (p - 1) * 20, limit: 20 })
      setData(res.items); setTotal(res.total)
    } catch { message.error('加载失败') }
    finally { setLoading(false) }
  }, [page])

  useEffect(() => { fetchData() }, [fetchData])

  const handleDelete = async (id: number) => {
    try { await enrollmentsApi.remove(id); message.success('退课成功'); fetchData() }
    catch (err: any) { message.error(err.response?.data?.detail || '操作失败') }
  }

  const columns = [
    { title: 'ID',     dataIndex: 'id',              key: 'id',      width: 70 },
    { title: '学生',   dataIndex: 'student_name',    key: 'student', width: 120 },
    { title: '课程',   dataIndex: 'course_name',     key: 'course',  width: 180, ellipsis: true },
    { title: '学期',   dataIndex: 'semester_name',   key: 'semester',width: 140 },
    { title: '选课时间',dataIndex: 'enrollment_date', key: 'date',   width: 160,
      render: (v: string) => v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '-' },
    { title: '状态',   dataIndex: 'status',          key: 'status',  width: 100,
      render: (v: string) => {
        const map: Record<string, string> = { enrolled: 'success', dropped: 'error', completed: 'blue' }
        const label: Record<string, string> = { enrolled: '已选', dropped: '已退', completed: '已完成' }
        return <Tag color={map[v] || 'default'}>{label[v] || v}</Tag>
      },
    },
    { title: '操作', key: 'action', width: 100,
      render: (_: any, r: Enrollment) => (
        <Popconfirm title="确认退课？" onConfirm={() => handleDelete(r.id)}>
          <Button size="small" danger icon={<DeleteOutlined />}>退课</Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <Card title="选课管理">
      <Space style={{ marginBottom: 12 }}>
        <Text type="secondary">共 {total} 条选课记录</Text>
      </Space>
      <Table columns={columns} dataSource={data} rowKey="id" loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: (p) => { setPage(p); fetchData(p) } }}
        scroll={{ x: 800 }} size="middle" />
    </Card>
  )
}
