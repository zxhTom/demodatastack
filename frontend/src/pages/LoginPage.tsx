import { Form, Input, Button, Card, Typography, message } from 'antd'
import { UserOutlined, LockOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { login } from '../api/auth'
import { useAuthStore } from '../store/auth'

const { Title, Text } = Typography

export default function LoginPage() {
  const navigate = useNavigate()
  const loginStore = useAuthStore((s) => s.login)
  const [form] = Form.useForm()

  const handleSubmit = async (values: { username: string; password: string }) => {
    try {
      const data = await login(values)
      loginStore(data.access_token, {
        id: data.user_id,
        username: data.username,
        role: data.role,
      })
      message.success('登录成功')
      navigate('/dashboard')
    } catch (err: any) {
      message.error(err.response?.data?.detail || '用户名或密码错误')
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        background: 'linear-gradient(135deg, #1677ff 0%, #0958d9 100%)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <Card style={{ width: 400, borderRadius: 12, boxShadow: '0 8px 32px rgba(0,0,0,0.2)' }}>
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <Title level={2} style={{ marginBottom: 4 }}>教务管理系统</Title>
          <Text type="secondary">Educational Management System</Text>
        </div>
        <Form form={form} onFinish={handleSubmit} size="large">
          <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名（admin）" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码（admin123）" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block>
              登录
            </Button>
          </Form.Item>
        </Form>
        <Text type="secondary" style={{ fontSize: 12 }}>
          管理员：admin / admin123 &nbsp;|&nbsp; 教师：teacher01 / teacher123
        </Text>
      </Card>
    </div>
  )
}
