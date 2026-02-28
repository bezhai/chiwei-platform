import { useEffect, useMemo, useState } from 'react';
import { Card, Col, Descriptions, Row, Statistic, Tag, Typography, Progress } from 'antd';
import dayjs from 'dayjs';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  DollarOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  SyncOutlined
} from '@ant-design/icons';
import { api } from '../api/client';

const { Title, Text } = Typography;

interface TokenStatsResponse {
  id?: string;
  name?: string;
  description?: string;
  isActive?: boolean;
  createdAt?: string;
  expiresAt?: string;
  expirationMode?: string;
  isActivated?: boolean;
  activationDays?: number;
  activatedAt?: string;
  usage?: {
    total?: {
      tokens?: number;
      inputTokens?: number;
      outputTokens?: number;
      cacheCreateTokens?: number;
      cacheReadTokens?: number;
      allTokens?: number;
      requests?: number;
      cost?: number;
      formattedCost?: string;
    };
  };
  limits?: {
    tokenLimit?: number;
    concurrencyLimit?: number;
    rateLimitWindow?: number;
    rateLimitRequests?: number;
    rateLimitCost?: number;
    dailyCostLimit?: number;
    totalCostLimit?: number;
    weeklyOpusCostLimit?: number;
    currentWindowRequests?: number;
    currentWindowTokens?: number;
    currentWindowCost?: number;
    currentDailyCost?: number;
    currentTotalCost?: number;
    weeklyOpusCost?: number;
    windowStartTime?: number;
    windowEndTime?: number;
    windowRemainingSeconds?: number;
  };
}

const formatNumber = (value?: number) => {
  if (value === undefined || value === null) return '-';
  return new Intl.NumberFormat('en-US').format(value);
};

const formatDate = (value?: string) => {
  if (!value) return '-';
  return dayjs(value).format('YYYY-MM-DD HH:mm');
};

const formatSeconds = (value?: number) => {
  if (value === undefined || value === null) return '-';
  const total = Math.max(0, Math.floor(value));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m ${total % 60}s`;
};

export default function Dashboard() {
  const [stats, setStats] = useState<TokenStatsResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchStats = async () => {
    try {
      const { data } = await api.get('/token-stats');
      const resolved = data?.data ?? data;
      setStats(resolved as TokenStatsResponse);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStats();
    const timer = setInterval(fetchStats, 60000);
    return () => clearInterval(timer);
  }, []);

  const usageTotal = stats?.usage?.total;
  const limits = stats?.limits;

  const statusTag = useMemo(() => {
    if (stats?.isActive) {
      return <Tag color="success" icon={<CheckCircleOutlined />}>活跃</Tag>;
    }
    return <Tag color="error" icon={<CloseCircleOutlined />}>非活跃</Tag>;
  }, [stats?.isActive]);

  const activatedTag = useMemo(() => {
    if (stats?.isActivated) {
      return <Tag color="processing" icon={<ClockCircleOutlined />}>已激活</Tag>;
    }
    return <Tag color="warning">未激活</Tag>;
  }, [stats?.isActivated]);

  const dailyCostPercent = limits?.dailyCostLimit
    ? Math.min(100, ((limits.currentDailyCost || 0) / limits.dailyCostLimit) * 100)
    : 0;

  const weeklyCostPercent = limits?.rateLimitCost
    ? Math.min(100, ((limits.currentWindowCost || 0) / limits.rateLimitCost) * 100)
    : 0;

  const recommendedToday = useMemo(() => {
    if (!limits) return 0;

    const nowUtc8 = new Date(Date.now() + 8 * 3600_000);
    const secondsPassedToday =
      nowUtc8.getUTCHours() * 3600 + nowUtc8.getUTCMinutes() * 60 + nowUtc8.getUTCSeconds();

    const weeklyUsedBeforeToday = (limits.currentWindowCost || 0) - (limits.currentDailyCost || 0);

    const daysFromMidnight = Math.max(
      1,
      ((limits.windowRemainingSeconds || 0) + secondsPassedToday) / 86400,
    );

    const dailyBudgetFromWeekly =
      ((limits.rateLimitCost || 0) - Math.max(0, weeklyUsedBeforeToday)) / daysFromMidnight;

    return Math.max(
      0,
      Math.min(dailyBudgetFromWeekly, limits.dailyCostLimit || 0) - (limits.currentDailyCost || 0),
    );
  }, [limits]);

  return (
    <div className="page-container">
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <Title level={4} style={{ marginBottom: 4 }}>数据概览</Title>
          <Text type="secondary">实时使用统计和限制</Text>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {statusTag}
          {activatedTag}
        </div>
      </div>

      <Row gutter={[24, 24]} align="stretch">
        <Col xs={24} lg={16} style={{ display: 'flex', flexDirection: 'column' }}>
          <Card bordered={false} title="使用总结" style={{ height: '100%', display: 'flex', flexDirection: 'column' }} bodyStyle={{ flex: 1 }}>
            <Row gutter={[24, 24]}>
              <Col span={8}>
                <Statistic
                  title="总花费"
                  value={usageTotal?.formattedCost || formatNumber(usageTotal?.cost)}
                  prefix={<DollarOutlined />}
                  valueStyle={{ color: '#0f172a', fontWeight: 600, fontSize: 24 }}
                />
              </Col>
              <Col span={8}>
                <Statistic
                  title="总请求数"
                  value={formatNumber(usageTotal?.requests)}
                  prefix={<SyncOutlined />}
                  valueStyle={{ color: '#0f172a', fontWeight: 600, fontSize: 24 }}
                />
              </Col>
              <Col span={8}>
                <Statistic
                  title="总 Token 数"
                  value={formatNumber(usageTotal?.tokens)}
                  prefix={<DatabaseOutlined />}
                  valueStyle={{ color: '#0f172a', fontWeight: 600, fontSize: 24 }}
                />
              </Col>
            </Row>
            <div style={{ marginTop: 32 }}>
              <Text strong style={{ display: 'block', marginBottom: 16 }}>Token 详情</Text>
              <Row gutter={[24, 24]}>
                 <Col span={8}>
                   <div style={{ padding: 16, background: '#f8fafc', borderRadius: 8 }}>
                     <Text type="secondary" style={{ fontSize: 12 }}>输入 Tokens</Text>
                     <div style={{ fontSize: 18, fontWeight: 600, marginTop: 4 }}>{formatNumber(usageTotal?.inputTokens)}</div>
                   </div>
                 </Col>
                 <Col span={8}>
                   <div style={{ padding: 16, background: '#f8fafc', borderRadius: 8 }}>
                     <Text type="secondary" style={{ fontSize: 12 }}>输出 Tokens</Text>
                     <div style={{ fontSize: 18, fontWeight: 600, marginTop: 4 }}>{formatNumber(usageTotal?.outputTokens)}</div>
                   </div>
                 </Col>
                 <Col span={8}>
                   <div style={{ padding: 16, background: '#f8fafc', borderRadius: 8 }}>
                     <Text type="secondary" style={{ fontSize: 12 }}>缓存读取</Text>
                     <div style={{ fontSize: 18, fontWeight: 600, marginTop: 4 }}>{formatNumber(usageTotal?.cacheReadTokens)}</div>
                   </div>
                 </Col>
              </Row>
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={8} style={{ display: 'flex', flexDirection: 'column' }}>
          <Card bordered={false} title="限制与配额" style={{ height: '100%', display: 'flex', flexDirection: 'column' }} bodyStyle={{ flex: 1 }}>
            <div style={{ padding: 16, background: '#f0fdf4', borderRadius: 8, marginBottom: 24, border: '1px solid #bbf7d0' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <CheckCircleOutlined style={{ color: '#16a34a' }} />
                <Text strong style={{ color: '#166534' }}>今日推荐用量</Text>
              </div>
              <div style={{ fontSize: 20, fontWeight: 600, color: '#15803d' }}>
                ${recommendedToday.toFixed(2)}
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                基于周剩余与日限额计算
              </Text>
            </div>

            <div style={{ marginBottom: 24 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                <Text>每日花费</Text>
                <Text>{formatNumber(limits?.currentDailyCost)} / {formatNumber(limits?.dailyCostLimit)}</Text>
              </div>
              <Progress
                percent={parseFloat(dailyCostPercent.toFixed(2))}
                status={dailyCostPercent > 90 ? 'exception' : 'active'}
                strokeColor={dailyCostPercent > 90 ? '#ef4444' : '#3b82f6'}
              />
            </div>

            <div style={{ marginBottom: 24 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                <Text>周花费</Text>
                <Text>{formatNumber(limits?.currentWindowCost)} / {formatNumber(limits?.rateLimitCost)}</Text>
              </div>
              <Progress
                percent={parseFloat(weeklyCostPercent.toFixed(2))}
                status={weeklyCostPercent > 90 ? 'exception' : 'active'}
                strokeColor={weeklyCostPercent > 90 ? '#ef4444' : '#3b82f6'}
              />
            </div>

            <div style={{ padding: 16, background: '#f1f5f9', borderRadius: 8, marginTop: 'auto' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <ThunderboltOutlined style={{ color: '#f59e0b' }} />
                <Text strong>速率限制重置</Text>
              </div>
              <Text type="secondary" style={{ fontSize: 13 }}>
                重置时间: {formatSeconds(limits?.windowRemainingSeconds)}
              </Text>
            </div>
          </Card>
        </Col>

        <Col span={24}>
          <Card bordered={false} title="账户详情">
            <Descriptions column={{ xs: 1, sm: 2, lg: 4 }}>
              <Descriptions.Item label="API ID">
                <Text copyable>{stats?.id || '-'}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">{formatDate(stats?.createdAt)}</Descriptions.Item>
              <Descriptions.Item label="过期时间">{formatDate(stats?.expiresAt)}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>
    </div>
  );
}
