import { useState, useEffect, useCallback } from 'react'
import { Table, Button, Modal, Form, Input, Select, DatePicker, Space, Popconfirm, message, Card, Tag } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { teachersApi, type Teacher } from '../api/education'
import dayjs from 'dayjs'

const { Option } = Select

export default function TeachersPage() {
  const [data, setData]           = useState<Teacher[]>([])
  const [total, setTotal]         = useState(0)
  const [loading, setLoading]     = useState(false)
  const [page, setPage]           = useState(1)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing]     = useState<Teacher | null>(null)
  const [form]                    = Form.useForm()

  const fetchData = useCallback(async (p = page) => {
    setLoading(true)
    try {
      const res = await teachersApi.list({ skip: (p - 1) * 20, limit: 20 })
      setData(res.items); setTotal(res.total)
    } finally { setLoading(false) }
  }, [page])

  useEffect(() => { fetchData() }, [fetchData])

  const handleSubmit = async (values: any) => {
    try {
      const payload = { ...values, hire_date: values.hire_date ? dayjs(values.hire_date).format('YYYY-MM-DD') : undefined }
      editing ? await teachersApi.update(editing.id, payload) : await teachersApi.create(payload)
      message.success(editing ? '更新成功' : '添加成功')
      setModalOpen(false); form.resetFields(); setEditing(null); fetchData()
    } catch (err: any) { message.error(err.response?.data?.detail || '操作失败') }
  }

  const handleDelete = async (id: number) => {
    try { await teachersApi.remove(id); message.success('删除成功'); fetchData() }
    catch { message.error('删除失败') }
  }

  const openEdit = (r: Teacher) => {
    setEditing(r)
    form.setFieldsValue({ ...r, hire_date: r.hire_date ? dayjs(r.hire_date) : undefined })
    setModalOpen(true)
  }

  const columns = [
    { title: '工号',   dataIndex: 'employee_id',     key: 'eid',   width: 100 },
    { title: '姓名',   dataIndex: 'name',            key: 'name',  width: 100 },
    { title: '性别',   dataIndex: 'gender',          key: 'gender',width: 70 },
    { title: '院系',   dataIndex: 'department_name', key: 'dept',  width: 120 },
    { title: '职称',   dataIndex: 'title',           key: 'title', width: 100 },
    { title: '邮箱',   dataIndex: 'email',           key: 'email', ellipsis: true },
    { title: '状态',   dataIndex: 'status',          key: 'status',width: 90,
      render: (v: string) => <Tag color={v === 'active' ? 'success' : 'default'}>{v === 'active' ? '在职' : '离职'}</Tag> },
    { title: '操作',   key: 'action',                width: 120,
      render: (_: any, r: Teacher) => (
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
    <Card title="教师管理" extra={
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setEditing(null); setModalOpen(true) }}>
        新增教师
      </Button>
    }>
      <Table columns={columns} dataSource={data} rowKey="id" loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: (p) => { setPage(p); fetchData(p) } }}
        scroll={{ x: 800 }} size="middle" />
      <Modal title={editing ? '编辑教师' : '新增教师'} open={modalOpen} onOk={() => form.submit()}
        onCancel={() => { setModalOpen(false); setEditing(null) }} width={550}>
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          <Form.Item name="employee_id" label="工号"  rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="name"        label="姓名"  rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="gender"      label="性别">
            <Select><Option value="男">男</Option><Option value="女">女</Option></Select>
          </Form.Item>
          <Form.Item name="title"     label="职称"><Input /></Form.Item>
          <Form.Item name="email"     label="邮箱"><Input /></Form.Item>
          <Form.Item name="phone"     label="电话"><Input /></Form.Item>
          <Form.Item name="hire_date" label="入职日期"><DatePicker style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="status"    label="状态">
            <Select><Option value="active">在职</Option><Option value="inactive">离职</Option></Select>
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
