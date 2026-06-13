import { useState, useEffect, useCallback } from 'react'
import { Table, Button, Modal, Form, Input, Space, Popconfirm, message, Card } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { departmentsApi, type Department } from '../api/education'

export default function DepartmentsPage() {
  const [data, setData]         = useState<Department[]>([])
  const [total, setTotal]       = useState(0)
  const [loading, setLoading]   = useState(false)
  const [page, setPage]         = useState(1)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing]   = useState<Department | null>(null)
  const [form]                  = Form.useForm()

  const fetchData = useCallback(async (p = page) => {
    setLoading(true)
    try {
      const res = await departmentsApi.list({ skip: (p - 1) * 20, limit: 20 })
      setData(res.items); setTotal(res.total)
    } catch { message.error('加载失败') }
    finally { setLoading(false) }
  }, [page])

  useEffect(() => { fetchData() }, [fetchData])

  const handleSubmit = async (values: any) => {
    try {
      editing ? await departmentsApi.update(editing.id, values) : await departmentsApi.create(values)
      message.success(editing ? '更新成功' : '添加成功')
      setModalOpen(false); form.resetFields(); setEditing(null); fetchData()
    } catch (err: any) { message.error(err.response?.data?.detail || '操作失败') }
  }

  const handleDelete = async (id: number) => {
    try { await departmentsApi.remove(id); message.success('删除成功'); fetchData() }
    catch { message.error('删除失败') }
  }

  const openEdit = (r: Department) => {
    setEditing(r); form.setFieldsValue(r); setModalOpen(true)
  }

  const columns = [
    { title: '代码',   dataIndex: 'code',        key: 'code',   width: 100 },
    { title: '名称',   dataIndex: 'name',        key: 'name',   width: 150 },
    { title: '院长',   dataIndex: 'dean',        key: 'dean',   width: 100 },
    { title: '描述',   dataIndex: 'description', key: 'desc',   ellipsis: true },
    { title: '操作',   key: 'action',            width: 120,
      render: (_: any, r: Department) => (
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
    <Card title="院系管理" extra={
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setEditing(null); setModalOpen(true) }}>
        新增院系
      </Button>
    }>
      <Table columns={columns} dataSource={data} rowKey="id" loading={loading}
        pagination={{ total, pageSize: 20, current: page, onChange: (p) => { setPage(p); fetchData(p) } }} />
      <Modal title={editing ? '编辑院系' : '新增院系'} open={modalOpen} onOk={() => form.submit()} onCancel={() => { setModalOpen(false); setEditing(null) }}>
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          <Form.Item name="code" label="院系代码" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="name" label="院系名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="dean" label="院长"><Input /></Form.Item>
          <Form.Item name="description" label="描述"><Input.TextArea rows={3} /></Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
