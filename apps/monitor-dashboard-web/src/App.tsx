import { Layout, Menu, ConfigProvider, Button, Dropdown, Avatar, Space, Typography } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import {
  ApiOutlined,
  CloudServerOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  MessageOutlined,
  MonitorOutlined,
  UserOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from '@ant-design/icons';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { useState, lazy, Suspense } from 'react';

dayjs.locale('zh-cn');
import AuthGuard from './components/AuthGuard';

const Login = lazy(() => import('./pages/Login'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const Kibana = lazy(() => import('./pages/Kibana'));
const Langfuse = lazy(() => import('./pages/Langfuse'));
const Messages = lazy(() => import('./pages/Messages'));
const Providers = lazy(() => import('./pages/Providers'));
const ModelMappings = lazy(() => import('./pages/ModelMappings'));
const MongoExplorer = lazy(() => import('./pages/MongoExplorer'));
import { themeConfig } from './theme';
import { clearToken } from './api/client';

const { Sider, Content, Header } = Layout;
const { Text } = Typography;

interface MenuItem {
  key?: string;
  icon?: React.ReactNode;
  label?: string;
  type?: 'divider' | 'group' | null;
}

const menuItems: MenuItem[] = [
  { key: '/', icon: <DashboardOutlined />, label: '概览' },
  { key: '/messages', icon: <MessageOutlined />, label: '消息记录' },
  { key: '/providers', icon: <CloudServerOutlined />, label: '服务商' },
  { key: '/model-mappings', icon: <ApiOutlined />, label: '模型映射' },
  { type: 'divider' },
  { key: '/kibana', icon: <FileSearchOutlined />, label: 'Grafana' },
  { key: '/langfuse', icon: <MonitorOutlined />, label: 'Langfuse 链路' },
  { key: '/mongo', icon: <DatabaseOutlined />, label: 'Mongo 浏览器' },
];

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const isLogin = location.pathname === '/login';

  const handleLogout = () => {
    clearToken();
    navigate('/login');
  };

  const userMenu = {
    items: [
      {
        key: 'logout',
        icon: <LogoutOutlined />,
        label: '退出登录',
        onClick: handleLogout,
      },
    ],
  };

  if (isLogin) {
    return (
      <ConfigProvider theme={themeConfig} locale={zhCN}>
        <Suspense fallback={null}>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="*" element={<Navigate to="/login" replace />} />
          </Routes>
        </Suspense>
      </ConfigProvider>
    );
  }

  const currentPath = location.pathname;
  // @ts-ignore
  const pageTitle = menuItems.find(item => item.key === currentPath)?.label || 'Dashboard';
  const primaryColor = themeConfig.token?.colorPrimary as string;

  return (
    <ConfigProvider theme={themeConfig} locale={zhCN}>
      <Layout className="app-shell" style={{ minHeight: '100vh' }}>
        <Sider
          width={240}
          theme="light"
          className="app-sider"
          collapsible
          collapsed={collapsed}
          trigger={null}
          style={{
            borderRight: '1px solid #e2e8f0',
            position: 'fixed',
            left: 0,
            top: 0,
            bottom: 0,
            zIndex: 10,
          }}
        >
          <div className="app-logo" style={{
            height: 64,
            display: 'flex',
            alignItems: 'center',
            padding: collapsed ? '0 24px' : '0 24px',
            justifyContent: collapsed ? 'center' : 'flex-start',
            borderBottom: '1px solid #f1f5f9'
          }}>
             <div style={{
               width: 32,
               height: 32,
               background: `linear-gradient(135deg, ${primaryColor}, #3b82f6)`,
               borderRadius: 8,
               display: 'flex',
               alignItems: 'center',
               justifyContent: 'center',
               marginRight: collapsed ? 0 : 12,
               color: '#fff',
               fontWeight: 'bold',
               fontSize: 20,
               flexShrink: 0,
               boxShadow: '0 4px 6px -1px rgba(37, 99, 235, 0.3)'
             }}>🔭</div>
             {!collapsed && (
               <Text strong style={{ fontSize: 18, color: '#0f172a' }}>赤尾观测中心</Text>
             )}
          </div>
          <Menu
            mode="inline"
            selectedKeys={[currentPath]}
            // @ts-ignore
            items={menuItems}
            onClick={({ key }) => navigate(key)}
            style={{ borderRight: 0, padding: '16px 0' }}
          />
        </Sider>
        <Layout style={{ marginLeft: collapsed ? 80 : 240, transition: 'all 0.2s' }}>
          <Header style={{
            padding: '0 24px',
            background: 'rgba(255, 255, 255, 0.8)',
            backdropFilter: 'blur(8px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: '1px solid #f1f5f9',
            position: 'sticky',
            top: 0,
            zIndex: 9
          }}>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <Button
                type="text"
                icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => setCollapsed(!collapsed)}
                style={{ fontSize: '16px', width: 32, height: 32, marginRight: 16 }}
              />
              <Text strong style={{ fontSize: 18, color: '#0f172a' }}>{pageTitle}</Text>
            </div>

            <Space size={16}>
              <Dropdown menu={userMenu} placement="bottomRight">
                <Space style={{ cursor: 'pointer', padding: '4px 8px', borderRadius: 6 }} className="user-dropdown">
                  <Avatar icon={<UserOutlined />} style={{ backgroundColor: primaryColor }} size="small" />
                  <Text strong style={{ fontSize: 14 }}>Admin</Text>
                </Space>
              </Dropdown>
            </Space>
          </Header>
          <Content style={{ padding: '24px', minHeight: 280, maxWidth: 1600, margin: '0 auto', width: '100%' }}>
            <AuthGuard>
              <Suspense fallback={null}>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/kibana" element={<Kibana />} />
                  <Route path="/langfuse" element={<Langfuse />} />
                  <Route path="/messages" element={<Messages />} />
                  <Route path="/providers" element={<Providers />} />
                  <Route path="/model-mappings" element={<ModelMappings />} />
                  <Route path="/mongo" element={<MongoExplorer />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </Suspense>
            </AuthGuard>
          </Content>
        </Layout>
      </Layout>
    </ConfigProvider>
  );
}
