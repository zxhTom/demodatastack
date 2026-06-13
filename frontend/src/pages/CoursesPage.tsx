import { useState, useEffect, useCallback } from 'react'
import { Table, Button, Modal, Form, Input, Select, Space, Popconfirm, message, Card, Tag } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { coursesApi, type Course } from '../api/education'

const { Option } = Select

export default function CoursesPage() {
  const [data, setData]           = useState<Course[]>([])
  const [total, setTotal]         = useState(0)
  const [loading, setLoading]     = useState(false)
  const [page, setPage]           = useState(1)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing]     = useState<Course | null>(null)
  const [form]                    = Form.useForm()

  const fetchData = useCallback(async (p = page) => {
    setLoading(true)
    try {
      const res = await coursesApi.list({ skip: (p - 1) * 20, limit: 20 })
      setData(res.items); setTotal(res.total)
    } finally { setLoading(false) }
  }, [page])

  useEffect(() => { fetchData() }, [fetchData])

  const handleSubmit = async (values: any) => {
    try {
      editing ? await coursesApi.update(editing.id, values) : await coursesApi.create(values)
      message.success(editing ? '更新成功' : '添加成功')
      setModalOpen(false); form.resetFields(); setEditing(null); fetchData()
    } catch (err: any) { message.error(err.response?.data?.detail || '操作失败') }
  }

  const handleDelete = async (id: number) => {
    try { await coursesApi.remove(id); message.success('删除成功'); fetchData() }
    catch { message.error('删除失败') }
  }

  const openEdit = (r: Course) => { setEditing(r); form.setFieldsValue(r); setModalOpen(true) }

  const columns = [
    { title: '课程代码', dataIndex: 'course_code',    key: 'code',   width: 120 },
    { title: '课程名称', dataIndex: 'name',           key: 'name',   width: 180 },
    { title: '学分',    dataIndex: 'credits',        key: 'credits',width: 70 },
    { title: '课时',    dataIndex: 'hours',          key: 'hours',  width: 70 },
    { title: '院系',    dataIndex: 'department_name',key: 'dept',   width: 120 },
    { title: '类型',    dataIndex: 'course_type',    key: 'type',   width: 90,
      render: (v: string) => <Tag color={v === '必修' ? 'blue' : 'cyan'}>{v}</Tag> },
    { title: '状态',    dataIndex: 'status',         key: 'status', width: 90,
      render: (v: string) => <Tag color={v === 'active' ? 'success' : 'default'}>{v === 'active' ? '开放' : '关闭'}</Tag> },
    { title: '操作',    key: 'action',               width: 120,
      render: (_: any, r: Course) => (
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
    <Card title="课程管理" extra={
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setEditing(null); setModalOpen(true) }}>
        新增课程
      </Button>
    }>
      <Table columns={columns} dataSource={data} rowKey="id" loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: (p) => { setPage(p); fetchData(p) } }}
        scroll={{ x: 800 }} size="middle" />
      <Modal title={editing ? '编辑课程' : '新增课程'} open={modalOpen} onOk={() => form.submit()}
        onCancel={() => { setModalOpen(false); setEditing(null) }} width={500}>
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          <Form.Item name="course_code"   label="课程代码" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="name"          label="课程名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="credits"       label="学分"><Input type="number" step="0.5" /></Form.Item>
          <Form.Item name="hours"         label="课时"><Input type="number" /></Form.Item>
          <Form.Item name="course_type"   label="课程类型">
            <Select><Option value="必修">必修</Option><Option value="选修">选修</Option></Select>
          </Form.Item>
          <Form.Item name="description"   label="描述"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="status"        label="状态">
            <Select><Option value="active">开放</Option><Option value="inactive">关闭</Option></Select>
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
