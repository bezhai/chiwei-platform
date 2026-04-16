import { Layout, Menu, ConfigProvider, Button, Dropdown, Avatar, Space, Typography, Tag, Drawer, Grid, Spin } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import {
  ApiOutlined,
  CloudServerOutlined,
  CodeOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  MessageOutlined,
  MonitorOutlined,
  UserOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ThunderboltOutlined,
  AuditOutlined,
  EditOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { useEffect, useState, lazy, Suspense } from 'react';

dayjs.locale('zh-cn');
import AuthGuard from './components/AuthGuard';

const Login = lazy(() => import('./pages/Login'));
const ServiceStatus = lazy(() => import('./pages/ServiceStatus'));
const Activity = lazy(() => import('./pages/Activity'));
const Kibana = lazy(() => import('./pages/Kibana'));
const Langfuse = lazy(() => import('./pages/Langfuse'));
const Messages = lazy(() => import('./pages/Messages'));
const Providers = lazy(() => import('./pages/Providers'));
const ModelMappings = lazy(() => import('./pages/ModelMappings'));
const MongoExplorer = lazy(() => import('./pages/MongoExplorer'));
const AuditLogs = lazy(() => import('./pages/AuditLogs'));
const DbMutations = lazy(() => import('./pages/DbMutations'));
const DynamicConfig = lazy(() => import('./pages/DynamicConfig'));
const Skills = lazy(() => import('./pages/Skills'));
import { themeConfig } from './theme';
import { clearToken, getLane } from './api/client';

const { Sider, Content, Header } = Layout;
const { Text } = Typography;
const { useBreakpoint } = Grid;
const COLLAPSED_STORAGE_KEY = 'monitor_dashboard_sidebar_collapsed';

interface MenuItem {
  key?: string;
  icon?: React.ReactNode;
  label?: string;
  type?: 'divider' | 'group' | null;
}

const menuItems: MenuItem[] = [
  { key: '/', icon: <DashboardOutlined />, label: '总览' },
  { key: '/activity', icon: <ThunderboltOutlined />, label: '赤尾动态' },
  { key: '/messages', icon: <MessageOutlined />, label: '消息记录' },
  { key: '/audit-logs', icon: <AuditOutlined />, label: '审计日志' },
  { key: '/db-mutations', icon: <EditOutlined />, label: 'DB 变更' },
  { type: 'divider' },
  { key: '/providers', icon: <CloudServerOutlined />, label: '服务商' },
  { key: '/model-mappings', icon: <ApiOutlined />, label: '模型映射' },
  { key: '/dynamic-config', icon: <SettingOutlined />, label: '动态配置' },
  { key: '/skills', icon: <CodeOutlined />, label: '技能管理' },
  { type: 'divider' },
  { key: '/kibana', icon: <FileSearchOutlined />, label: 'Grafana' },
  { key: '/langfuse', icon: <MonitorOutlined />, label: 'Langfuse 链路' },
  { key: '/mongo', icon: <DatabaseOutlined />, label: 'Mongo 浏览器' },
];

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const screens = useBreakpoint();
  const isMobile = !screens.lg;
  const [collapsed, setCollapsed] = useState(() => {
    if (typeof window === 'undefined') {
      return false;
    }
    return localStorage.getItem(COLLAPSED_STORAGE_KEY) === '1';
  });
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const isLogin = location.pathname === '/login';

  useEffect(() => {
    localStorage.setItem(COLLAPSED_STORAGE_KEY, collapsed ? '1' : '0');
  }, [collapsed]);

  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname, location.search]);

  const handleLogout = () => {
    clearToken();
    navigate('/login');
  };

  const navigateWithLane = (path: string) => {
    const lane = getLane();
    navigate(lane ? `${path}?x-lane=${lane}` : path);
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
  const siderWidth = collapsed ? 80 : 240;
  const pageLoadingFallback = (
    <div className="route-loading-shell">
      <div className="route-loading-card">
        <Spin size="large" />
        <Text type="secondary">页面加载中</Text>
      </div>
    </div>
  );

  const navigationMenu = (
    <Menu
      mode="inline"
      selectedKeys={[currentPath]}
      // @ts-ignore
      items={menuItems}
      onClick={({ key }) => {
        navigateWithLane(String(key));
      }}
      style={{ borderRight: 0, padding: '16px 0', background: 'transparent' }}
    />
  );

  const brand = (
    <div className={`app-logo${collapsed && !isMobile ? ' app-logo-collapsed' : ''}`}>
      <div className="brand-mark">
        <span className="brand-mark-core" />
      </div>
      {(!collapsed || isMobile) && (
        <div className="app-logo-copy">
          <Text strong className="app-logo-title">赤尾观测中心</Text>
          <Text className="app-logo-subtitle">Monitor Dashboard</Text>
        </div>
      )}
    </div>
  );

  return (
    <ConfigProvider theme={themeConfig} locale={zhCN}>
      <Layout className="app-shell" style={{ minHeight: '100vh' }}>
        {!isMobile && (
          <Sider
            width={240}
            theme="light"
            className="app-sider"
            collapsible
            collapsed={collapsed}
            trigger={null}
            style={{
              position: 'fixed',
              left: 0,
              top: 0,
              bottom: 0,
              zIndex: 10,
            }}
          >
            {brand}
            {navigationMenu}
          </Sider>
        )}
        {isMobile && (
          <Drawer
            title={null}
            placement="left"
            closable={false}
            open={mobileNavOpen}
            onClose={() => setMobileNavOpen(false)}
            bodyStyle={{ padding: 0 }}
            width={280}
          >
            {brand}
            {navigationMenu}
          </Drawer>
        )}
        <Layout className="app-main-layout" style={{ marginLeft: isMobile ? 0 : siderWidth, transition: 'all 0.2s' }}>
          <Header className="app-header" style={{ padding: isMobile ? '0 16px' : '0 22px' }}>
            <div className="app-header-left">
              <Button
                className="shell-toggle"
                type="text"
                icon={isMobile || collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => {
                  if (isMobile) {
                    setMobileNavOpen(true);
                    return;
                  }
                  setCollapsed(!collapsed);
                }}
              />
              <div className="app-header-copy">
                <Text className="app-header-kicker">Control Room</Text>
                <Text strong className="app-header-title">
                  {pageTitle}
                </Text>
                {!isMobile && (
                  <Text className="app-header-subtitle">
                    统一监控、检索与运维操作台
                  </Text>
                )}
              </div>
            </div>

            <Space size={isMobile ? 8 : 12} className="app-header-actions">
              {getLane() && <Tag className="lane-pill" style={{ marginInlineEnd: 0 }}>{getLane()}</Tag>}
              <Dropdown menu={userMenu} placement="bottomRight">
                <Space className="user-dropdown user-chip">
                  <Avatar icon={<UserOutlined />} style={{ backgroundColor: primaryColor }} size="small" />
                  {!isMobile && <Text strong style={{ fontSize: 14 }}>Admin</Text>}
                </Space>
              </Dropdown>
            </Space>
          </Header>
          <Content className="app-content" style={{ padding: isMobile ? '16px' : '24px', minHeight: 280 }}>
            <AuthGuard>
              <Suspense fallback={pageLoadingFallback}>
                <Routes>
                  <Route path="/" element={<ServiceStatus />} />
                  <Route path="/activity" element={<Activity />} />
                  <Route path="/audit-logs" element={<AuditLogs />} />
                  <Route path="/db-mutations" element={<DbMutations />} />
                  <Route path="/kibana" element={<Kibana />} />
                  <Route path="/langfuse" element={<Langfuse />} />
                  <Route path="/messages" element={<Messages />} />
                  <Route path="/providers" element={<Providers />} />
                  <Route path="/model-mappings" element={<ModelMappings />} />
                  <Route path="/dynamic-config" element={<DynamicConfig />} />
                  <Route path="/skills" element={<Skills />} />
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
