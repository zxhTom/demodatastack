import { useState, useEffect, useCallback } from 'react'
import { Table, Card, Tag, Button, Modal, Form, Input, Space, Popconfirm, message, InputNumber } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { gradesApi, type Grade } from '../api/education'
import dayjs from 'dayjs'

export default function GradesPage() {
  const [data, setData]           = useState<Grade[]>([])
  const [total, setTotal]         = useState(0)
  const [loading, setLoading]     = useState(false)
  const [page, setPage]           = useState(1)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing]     = useState<Grade | null>(null)
  const [form]                    = Form.useForm()

  const fetchData = useCallback(async (p = page) => {
    setLoading(true)
    try {
      const res = await gradesApi.list({ skip: (p - 1) * 20, limit: 20 })
      setData(res.items); setTotal(res.total)
    } finally { setLoading(false) }
  }, [page])

  useEffect(() => { fetchData() }, [fetchData])

  const handleSubmit = async (values: any) => {
    try {
      const score = parseFloat(values.score)
      const grade_letter = score >= 90 ? 'A' : score >= 80 ? 'B' : score >= 70 ? 'C' : score >= 60 ? 'D' : 'F'
      const payload = { ...values, score, grade_letter }
      editing ? await gradesApi.update(editing.id, payload) : await gradesApi.create(payload)
      message.success(editing ? '更新成功' : '录入成功')
      setModalOpen(false); form.resetFields(); setEditing(null); fetchData()
    } catch (err: any) { message.error(err.response?.data?.detail || '操作失败') }
  }

  const handleDelete = async (id: number) => {
    try { await gradesApi.remove(id); message.success('删除成功'); fetchData() }
    catch { message.error('删除失败') }
  }

  const openEdit = (r: Grade) => { setEditing(r); form.setFieldsValue(r); setModalOpen(true) }

  const gradeColor: Record<string, string> = { A: 'success', B: 'blue', C: 'warning', D: 'orange', F: 'error' }

  const columns = [
    { title: '学生',  dataIndex: 'student_name',  key: 'student', width: 120 },
    { title: '课程',  dataIndex: 'course_name',   key: 'course',  width: 200, ellipsis: true },
    { title: '分数',  dataIndex: 'score',         key: 'score',   width: 90,
      render: (v: number) => v != null ? v.toFixed(1) : '-' },
    { title: '等级',  dataIndex: 'grade_letter',  key: 'letter',  width: 80,
      render: (v: string) => v ? <Tag color={gradeColor[v] || 'default'}>{v}</Tag> : '-' },
    { title: '评语',  dataIndex: 'comment',       key: 'comment', ellipsis: true },
    { title: '录入时间', dataIndex: 'graded_at',  key: 'graded',  width: 160,
      render: (v: string) => v ? dayjs(v).format('MM-DD HH:mm') : '-' },
    { title: '操作',  key: 'action', width: 120,
      render: (_: any, r: Grade) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Popconfirm title="确认删除？" onConfirm={() => handleDelete(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Card title="成绩管理" extra={
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setEditing(null); setModalOpen(true) }}>
        录入成绩
      </Button>
    }>
      <Table columns={columns} dataSource={data} rowKey="id" loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: (p) => { setPage(p); fetchData(p) } }}
        scroll={{ x: 700 }} size="middle" />
      <Modal title={editing ? '修改成绩' : '录入成绩'} open={modalOpen} onOk={() => form.submit()}
        onCancel={() => { setModalOpen(false); setEditing(null) }}>
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          {!editing && (
            <Form.Item name="enrollment_id" label="选课ID" rules={[{ required: true }]}>
              <InputNumber style={{ width: '100%' }} placeholder="输入选课记录 ID" />
            </Form.Item>
          )}
          <Form.Item name="score" label="分数（0-100）" rules={[{ required: true }]}>
            <InputNumber min={0} max={100} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="comment" label="评语"><Input.TextArea rows={2} /></Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
