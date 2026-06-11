import { useState } from 'react';
import { Button, Form, Input, message, Typography } from 'antd';
import { useNavigate } from 'react-router-dom';
import { LockOutlined, ArrowRightOutlined } from '@ant-design/icons';
import { api, setToken } from '../api/client';

const { Text } = Typography;

export default function Login() {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const onFinish = async (values: { password: string }) => {
    setLoading(true);
    try {
      const { data } = await api.post('/auth/login', { password: values.password });
      setToken(data.token);
      message.success('登录成功');
      navigate('/');
    } catch {
      message.error('验证失败，请检查密码');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-container">
      <div className="login-left">
        <div className="brand-section">
          <div className="brand-logo">
            <div className="brand-icon">CW</div>
            <span className="brand-text">赤尾观测</span>
          </div>

          <div className="hero-text-container">
            <div className="hero-title">
              Operator<br />
              Console
            </div>
            <div className="hero-subtitle">
              dashboard-web / protected control surface
            </div>
          </div>
        </div>

        <div className="testimonial">
          <div className="ops-status-grid">
            <div className="ops-status-row">
              <span className="ops-status-label">scope</span>
              <span className="ops-status-value">monitor-dashboard</span>
              <span className="ops-status-pill">guarded</span>
            </div>
            <div className="ops-status-row">
              <span className="ops-status-label">route</span>
              <span className="ops-status-value">/dashboard/api</span>
              <span className="ops-status-pill">jwt</span>
            </div>
            <div className="ops-status-row">
              <span className="ops-status-label">surface</span>
              <span className="ops-status-value">ops / audit / config / gateway</span>
              <span className="ops-status-pill">admin</span>
            </div>
          </div>
        </div>
      </div>

      <div className="login-right">
        <div className="login-form-wrapper">
          <div className="form-header">
            <div className="form-title">管理员认证</div>
            <div className="form-subtitle">Chiwei Observation Console</div>
          </div>

          <Form
            layout="vertical"
            onFinish={onFinish}
            requiredMark={false}
            size="large"
          >
            <Form.Item
              name="password"
              label={<span style={{ fontWeight: 600, fontSize: 13, color: 'var(--ink-soft)' }}>管理密码</span>}
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined style={{ color: 'var(--muted)', fontSize: 16 }} />}
                placeholder="请输入密码"
                className="custom-input"
              />
            </Form.Item>

            <Form.Item style={{ marginTop: 32 }}>
              <Button
                type="primary"
                htmlType="submit"
                loading={loading}
                block
                className="submit-btn"
              >
                登录 <ArrowRightOutlined />
              </Button>
            </Form.Item>
          </Form>

          <div style={{ marginTop: 40, textAlign: 'center' }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              dashboard-web / {new Date().getFullYear()}
            </Text>
          </div>
        </div>
      </div>
    </div>
  );
}
