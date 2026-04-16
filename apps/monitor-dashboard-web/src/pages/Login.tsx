import { useState } from 'react';
import { Button, Form, Input, message, Typography, Space } from 'antd';
import { useNavigate } from 'react-router-dom';
import { LockOutlined, ArrowRightOutlined, CheckCircleFilled } from '@ant-design/icons';
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
            <div className="brand-icon">
              <span style={{ fontSize: 24 }}>🔭</span>
            </div>
            <span className="brand-text">赤尾观测中心</span>
          </div>

          <div className="hero-text-container">
            <div className="hero-title">
              全域观测<br />
              尽在掌握
            </div>
            <div className="hero-subtitle">
              针对 Inner Bot 基础设施的综合监控、实时分析与高级控制中心。
            </div>
          </div>
        </div>

        <div className="testimonial">
          <Space align="start" size={16}>
            <CheckCircleFilled style={{ color: '#10b981', fontSize: 24, marginTop: 4 }} />
            <div>
              <div style={{ fontWeight: 600, color: '#0f172a', marginBottom: 4, fontSize: 15 }}>
                系统运转正常
              </div>
              <div style={{ color: '#64748b', fontSize: 13, lineHeight: 1.5 }}>
                所有服务节点状态良好
                <br />
                最近检查: 刚刚
              </div>
            </div>
          </Space>
        </div>
      </div>

      <div className="login-right">
        <div className="login-form-wrapper">
          <div className="form-header">
            <div className="form-title">欢迎回来</div>
            <div className="form-subtitle">请输入管理员密码以继续访问控制台。</div>
          </div>

          <Form
            layout="vertical"
            onFinish={onFinish}
            requiredMark={false}
            size="large"
          >
            <Form.Item
              name="password"
              label={<span style={{ fontWeight: 600, fontSize: 13, color: '#334155' }}>管理密码</span>}
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined style={{ color: '#94a3b8', fontSize: 16 }} />}
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
              &copy; {new Date().getFullYear()} Chiwei Observation Center. All rights reserved.
            </Text>
          </div>
        </div>
      </div>
    </div>
  );
}
