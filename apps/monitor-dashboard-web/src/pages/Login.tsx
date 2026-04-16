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
            <div className="brand-mark login-brand-mark">
              <span className="brand-mark-core" />
            </div>
            <div>
              <span className="brand-text">赤尾观测中心</span>
              <div className="brand-caption">Monitor Dashboard</div>
            </div>
          </div>

          <div className="hero-text-container">
            <div className="hero-kicker">Operations Control Room</div>
            <div className="hero-title">
              看见系统脉搏
              <br />
              也看见异常前奏
            </div>
            <div className="hero-subtitle">
              为 `dashboard-monitor` 设计的运维控制台，集中展示服务状态、活动轨迹、审计记录与配置变更。
            </div>
          </div>
        </div>

        <div className="hero-signal-grid">
          <div className="hero-signal-card">
            <div className="hero-signal-label">今日状态</div>
            <div className="hero-signal-value">All Green</div>
          </div>
          <div className="hero-signal-card">
            <div className="hero-signal-label">值班节奏</div>
            <div className="hero-signal-value">实时同步</div>
          </div>
        </div>

        <div className="testimonial">
          <Space align="start" size={16}>
            <CheckCircleFilled style={{ color: '#0f766e', fontSize: 24, marginTop: 4 }} />
            <div>
              <div style={{ fontWeight: 700, color: '#172033', marginBottom: 4, fontSize: 15 }}>
                控制室已就绪
              </div>
              <div style={{ color: '#5f6573', fontSize: 13, lineHeight: 1.6 }}>
                服务健康、链路追踪、消息检索与配置管理
                <br />
                在同一入口完成闭环操作
              </div>
            </div>
          </Space>
        </div>
      </div>

      <div className="login-right">
        <div className="login-panel">
          <div className="login-form-wrapper">
            <div className="form-header">
              <div className="form-title">欢迎回来</div>
              <div className="form-subtitle">请输入管理员密码，进入监控控制室。</div>
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
                &copy; {new Date().getFullYear()} Chiwei Observation Center
              </Text>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
