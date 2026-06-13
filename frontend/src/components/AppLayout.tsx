import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Avatar, Dropdown, Typography, Space } from 'antd'
import {
  DashboardOutlined, TeamOutlined, BookOutlined, UserOutlined,
  ReadOutlined, OrderedListOutlined, StarOutlined, LogoutOutlined, BankOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '../store/auth'

const { Header, Sider, Content } = Layout
const { Text } = Typography

const menuItems = [
  { key: '/dashboard',   icon: <DashboardOutlined />,   label: 'KPI 仪表盘' },
  { key: '/departments', icon: <BankOutlined />,         label: '院系管理' },
  { key: '/teachers',    icon: <TeamOutlined />,         label: '教师管理' },
  { key: '/students',    icon: <UserOutlined />,         label: '学生管理' },
  { key: '/courses',     icon: <BookOutlined />,         label: '课程管理' },
  { key: '/enrollments', icon: <OrderedListOutlined />,  label: '选课管理' },
  { key: '/grades',      icon: <StarOutlined />,         label: '成绩管理' },
]

export default function AppLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)

  const userMenuItems = [
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      onClick: () => { logout(); navigate('/login') },
    },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark" width={220} style={{ position: 'fixed', height: '100vh', left: 0, top: 0 }}>
        <div style={{ height: 64, display: 'flex', alignItems: 'center', justifyContent: 'center', borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
          <ReadOutlined style={{ color: '#fff', fontSize: 24, marginRight: 8 }} />
          <Text style={{ color: '#fff', fontWeight: 600, fontSize: 16 }}>教务管理</Text>
        </div>
        <Menu
          theme="dark"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ marginTop: 8 }}
        />
      </Sider>
      <Layout style={{ marginLeft: 220 }}>
        <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', alignItems: 'center', justifyContent: 'flex-end', boxShadow: '0 1px 4px rgba(0,0,0,0.1)' }}>
          <Dropdown menu={{ items: userMenuItems }} placement="bottomRight">
            <Space style={{ cursor: 'pointer' }}>
              <Avatar style={{ backgroundColor: '#1677ff' }}>
                {user?.username?.[0]?.toUpperCase()}
              </Avatar>
              <Text>{user?.username}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>（{user?.role}）</Text>
            </Space>
          </Dropdown>
        </Header>
        <Content style={{ margin: 24, minHeight: 280 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
