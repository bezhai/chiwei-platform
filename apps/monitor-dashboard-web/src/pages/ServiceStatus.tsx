import { useEffect, useState, useCallback } from 'react';
import { Card, Col, Row, Statistic, Table, Tag, Typography, Tooltip, Space } from 'antd';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  CloudServerOutlined,
  DeploymentUnitOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { api } from '../api/client';

dayjs.extend(relativeTime);

const { Title, Text } = Typography;

interface App {
  name: string;
  description?: string;
  port?: number;
  image_repo?: string;
  command?: string;
}

interface Release {
  id: string;
  app_name: string;
  lane: string;
  image: string;
  status: string;
  created_at: string;
  updated_at: string;
}

const getImageTag = (image?: string) => {
  if (!image) return '';
  const idx = image.lastIndexOf(':');
  return idx >= 0 ? image.slice(idx + 1) : image;
};

interface ServiceRow {
  key: string;
  name: string;
  description: string;
  port: number | string;
  releases: Release[];
}

const statusConfig: Record<string, { color: string; icon: React.ReactNode }> = {
  deployed: { color: 'success', icon: <CheckCircleOutlined /> },
  failed: { color: 'error', icon: <CloseCircleOutlined /> },
  pending: { color: 'warning', icon: <ClockCircleOutlined /> },
};

export default function ServiceStatus() {
  const [apps, setApps] = useState<App[]>([]);
  const [releases, setReleases] = useState<Release[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    try {
      const { data } = await api.get('/service-status');
      setApps(data.apps || []);
      setReleases(data.releases || []);
    } catch (e) {
      console.error('Failed to fetch service status:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, 30000);
    return () => clearInterval(timer);
  }, [fetchData]);

  const lanes = [...new Set(releases.map((r) => r.lane))].sort();

  const dataSource: ServiceRow[] = apps.map((app) => ({
    key: app.name,
    name: app.name,
    description: app.description || '-',
    port: app.port || '-',
    releases: releases.filter((r) => r.app_name === app.name),
  }));

  const runningCount = new Set(
    releases.filter((r) => r.status === 'deployed').map((r) => r.app_name),
  ).size;
  const failedCount = new Set(
    releases.filter((r) => r.status === 'failed').map((r) => r.app_name),
  ).size;

  const columns = [
    {
      title: '服务名',
      dataIndex: 'name',
      key: 'name',
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
    },
    {
      title: '端口',
      dataIndex: 'port',
      key: 'port',
      width: 80,
      render: (port: number | string) =>
        port === 0 ? <Tag>Worker</Tag> : port,
    },
    {
      title: '部署泳道',
      key: 'lanes',
      render: (_: unknown, row: ServiceRow) => {
        if (row.releases.length === 0) {
          return <Text type="secondary">未部署</Text>;
        }
        return (
          <Space size={[0, 6]} wrap>
            {row.releases.map((rel) => {
              const tag = getImageTag(rel.image);
              const cfg = statusConfig[rel.status] || statusConfig.pending;
              return (
                <Tooltip key={rel.id} title={`${rel.status} · ${tag}`}>
                  <Tag color={cfg.color} icon={cfg.icon} style={{ marginRight: 0 }}>
                    {rel.lane}
                  </Tag>
                </Tooltip>
              );
            })}
          </Space>
        );
      },
    },
    {
      title: '镜像版本',
      key: 'imageTag',
      width: 120,
      render: (_: unknown, row: ServiceRow) => {
        const tags = [...new Set(row.releases.map((r) => getImageTag(r.image)))].filter(Boolean);
        if (tags.length === 0) return '-';
        if (tags.length === 1) return <Text copyable={{ text: tags[0] }}>{tags[0].slice(0, 8)}</Text>;
        return (
          <Space direction="vertical" size={0}>
            {tags.map((t) => (
              <Text key={t} copyable={{ text: t }} style={{ fontSize: 12 }}>{t.slice(0, 8)}</Text>
            ))}
          </Space>
        );
      },
    },
    {
      title: '最后更新',
      key: 'updatedAt',
      width: 140,
      render: (_: unknown, row: ServiceRow) => {
        const latest = row.releases
          .map((r) => r.updated_at)
          .filter(Boolean)
          .sort()
          .pop();
        if (!latest) return '-';
        return (
          <Tooltip title={dayjs(latest).format('YYYY-MM-DD HH:mm:ss')}>
            <Text type="secondary">{dayjs(latest).fromNow()}</Text>
          </Tooltip>
        );
      },
    },
  ];

  return (
    <div className="page-container">
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <Title level={4} style={{ marginBottom: 4 }}>服务状态</Title>
          <Text type="secondary">实时监控所有服务的部署状态</Text>
        </div>
        <Tooltip title="每 30 秒自动刷新">
          <ReloadOutlined
            spin={loading}
            style={{ fontSize: 16, cursor: 'pointer', color: '#8c8c8c' }}
            onClick={() => { setLoading(true); fetchData(); }}
          />
        </Tooltip>
      </div>

      <Row gutter={[24, 24]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card bordered={false}>
            <Statistic
              title="总服务数"
              value={apps.length}
              prefix={<CloudServerOutlined />}
              valueStyle={{ fontWeight: 600 }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card bordered={false}>
            <Statistic
              title="运行中"
              value={runningCount}
              prefix={<CheckCircleOutlined style={{ color: '#52c41a' }} />}
              valueStyle={{ fontWeight: 600, color: '#52c41a' }}
              suffix={failedCount > 0 ? <Text type="danger" style={{ fontSize: 14 }}> / {failedCount} 异常</Text> : undefined}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card bordered={false}>
            <Statistic
              title="活跃泳道"
              value={lanes.length}
              prefix={<DeploymentUnitOutlined />}
              valueStyle={{ fontWeight: 600 }}
            />
          </Card>
        </Col>
      </Row>

      <Card bordered={false}>
        <Table
          dataSource={dataSource}
          columns={columns}
          loading={loading}
          pagination={false}
          size="middle"
        />
      </Card>
    </div>
  );
}
