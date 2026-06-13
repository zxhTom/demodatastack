import { useState, useEffect, useCallback } from 'react'
import { Table, Button, Modal, Form, Input, Select, DatePicker, Space, Popconfirm, message, Card, Tag } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, SearchOutlined } from '@ant-design/icons'
import { studentsApi, type Student } from '../api/education'
import dayjs from 'dayjs'

const { Option } = Select

export default function StudentsPage() {
  const [data, setData] = useState<Student[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Student | null>(null)
  const [form] = Form.useForm()
  const [search, setSearch] = useState('')

  const fetchData = useCallback(async (p = page) => {
    setLoading(true)
    try {
      const res = await studentsApi.list({ skip: (p - 1) * 20, limit: 20 })
      setData(res.items)
      setTotal(res.total)
    } catch { message.error('加载失败') }
    finally { setLoading(false) }
  }, [page])

  useEffect(() => { fetchData() }, [fetchData])

  const handleSubmit = async (values: any) => {
    try {
      const payload = {
        ...values,
        enrollment_date: values.enrollment_date ? dayjs(values.enrollment_date).format('YYYY-MM-DD') : undefined,
      }
      if (editing) {
        await studentsApi.update(editing.id, payload)
        message.success('更新成功')
      } else {
        await studentsApi.create(payload)
        message.success('添加成功')
      }
      setModalOpen(false)
      form.resetFields()
      setEditing(null)
      fetchData()
    } catch (err: any) { message.error(err.response?.data?.detail || '操作失败') }
  }

  const handleDelete = async (id: number) => {
    try {
      await studentsApi.remove(id)
      message.success('删除成功')
      fetchData()
    } catch { message.error('删除失败') }
  }

  const openEdit = (record: Student) => {
    setEditing(record)
    form.setFieldsValue({
      ...record,
      enrollment_date: record.enrollment_date ? dayjs(record.enrollment_date) : undefined,
    })
    setModalOpen(true)
  }

  const columns = [
    { title: '学号',   dataIndex: 'student_id',      key: 'student_id',      width: 120 },
    { title: '姓名',   dataIndex: 'name',             key: 'name',            width: 100 },
    { title: '性别',   dataIndex: 'gender',           key: 'gender',          width: 70 },
    { title: '院系',   dataIndex: 'department_name',  key: 'dept',            width: 120 },
    { title: '年级',   dataIndex: 'grade',            key: 'grade',           width: 70 },
    { title: '班级',   dataIndex: 'class_name',       key: 'class',           width: 120 },
    { title: '邮箱',   dataIndex: 'email',            key: 'email',           ellipsis: true },
    { title: '状态',   dataIndex: 'status',           key: 'status',          width: 90,
      render: (v: string) => <Tag color={v === 'active' ? 'success' : 'default'}>{v === 'active' ? '在读' : '离校'}</Tag> },
    { title: '操作',   key: 'action',                 width: 120,
      render: (_: any, r: Student) => (
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
    <Card
      title="学生管理"
      extra={
        <Space>
          <Input placeholder="搜索学生" prefix={<SearchOutlined />} value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 200 }} />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setEditing(null); setModalOpen(true) }}>
            新增学生
          </Button>
        </Space>
      }
    >
      <Table
        columns={columns}
        dataSource={data.filter((s) => !search || s.name.includes(search) || s.student_id.includes(search))}
        rowKey="id"
        loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: (p) => { setPage(p); fetchData(p) } }}
        scroll={{ x: 900 }}
        size="middle"
      />

      <Modal
        title={editing ? '编辑学生' : '新增学生'}
        open={modalOpen}
        onOk={() => form.submit()}
        onCancel={() => { setModalOpen(false); setEditing(null) }}
        width={600}
      >
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          <Form.Item name="student_id" label="学号" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="name"       label="姓名" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="gender"     label="性别">
            <Select placeholder="请选择">
              <Option value="男">男</Option>
              <Option value="女">女</Option>
            </Select>
          </Form.Item>
          <Form.Item name="email"           label="邮箱"><Input /></Form.Item>
          <Form.Item name="phone"           label="电话"><Input /></Form.Item>
          <Form.Item name="grade"           label="年级"><Input type="number" /></Form.Item>
          <Form.Item name="class_name"      label="班级"><Input /></Form.Item>
          <Form.Item name="enrollment_date" label="入学日期"><DatePicker style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="status"          label="状态">
            <Select>
              <Option value="active">在读</Option>
              <Option value="inactive">离校</Option>
            </Select>
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
