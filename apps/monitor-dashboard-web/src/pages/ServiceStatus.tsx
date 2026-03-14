import { useEffect, useState, useCallback } from 'react';
import { Card, Col, Row, Statistic, Table, Tag, Typography, Tooltip, Space, Spin } from 'antd';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  CloudServerOutlined,
  DeploymentUnitOutlined,
  ReloadOutlined,
  DownOutlined,
  RightOutlined,
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

interface PodInfo {
  name: string;
  status: string;
  ready: boolean;
  restarts: number;
  reason?: string;
}

interface PodStatus {
  deploy_name: string;
  desired: number;
  ready: number;
  available: number;
  pods: PodInfo[];
}

interface LaneBinding {
  route_type: string;
  route_key: string;
  lane_name: string;
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

function PodDetail({ app, lane }: { app: string; lane: string }) {
  const [data, setData] = useState<PodStatus | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get(`/ops/services/${app}/pods`, { params: { lane } })
      .then((res) => setData(res.data))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [app, lane]);

  if (loading) return <Spin size="small" />;
  if (!data) return <Text type="secondary">无法获取 Pod 信息</Text>;

  return (
    <div style={{ padding: '4px 0' }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        Desired: {data.desired} / Ready: {data.ready} / Available: {data.available}
      </Text>
      {data.pods?.map((pod) => (
        <div key={pod.name} style={{ fontSize: 12, marginTop: 4 }}>
          <Tag color={pod.ready ? 'green' : 'red'} style={{ fontSize: 11 }}>{pod.status}</Tag>
          <Text code style={{ fontSize: 11 }}>{pod.name}</Text>
          {pod.restarts > 0 && <Text type="warning" style={{ marginLeft: 8, fontSize: 11 }}>restarts: {pod.restarts}</Text>}
          {pod.reason && <Text type="danger" style={{ marginLeft: 8, fontSize: 11 }}>{pod.reason}</Text>}
        </div>
      ))}
    </div>
  );
}

export default function ServiceStatus() {
  const [apps, setApps] = useState<App[]>([]);
  const [releases, setReleases] = useState<Release[]>([]);
  const [loading, setLoading] = useState(true);
  const [laneBindings, setLaneBindings] = useState<LaneBinding[]>([]);
  const [expandedRows, setExpandedRows] = useState<string[]>([]);

  const fetchData = useCallback(async () => {
    try {
      const [statusRes, bindingsRes] = await Promise.all([
        api.get('/service-status'),
        api.get('/ops/lane-bindings').catch(() => ({ data: [] })),
      ]);
      setApps(statusRes.data.apps || []);
      setReleases(statusRes.data.releases || []);
      setLaneBindings(Array.isArray(bindingsRes.data) ? bindingsRes.data : []);
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
          <Space size={[8, 6]} wrap>
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
      title: '版本',
      key: 'version',
      width: 100,
      render: (_: unknown, row: ServiceRow) => {
        const prodRelease = row.releases.find((r) => r.lane === 'prod');
        const tag = prodRelease ? getImageTag(prodRelease.image) : '';
        return tag ? <Tag>{tag}</Tag> : <Text type="secondary">-</Text>;
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

      {laneBindings.length > 0 && (
        <Card bordered={false} style={{ marginBottom: 24 }} size="small">
          <Text strong style={{ fontSize: 13 }}>泳道绑定</Text>
          <div style={{ marginTop: 8 }}>
            <Space wrap size={[8, 6]}>
              {laneBindings.map((b) => (
                <Tag key={`${b.route_type}-${b.route_key}`} color="blue">
                  {b.route_type}:{b.route_key} → {b.lane_name}
                </Tag>
              ))}
            </Space>
          </div>
        </Card>
      )}

      <Card bordered={false}>
        <Table
          dataSource={dataSource}
          columns={columns}
          loading={loading}
          pagination={false}
          size="middle"
          expandable={{
            expandedRowKeys: expandedRows,
            onExpand: (expanded, record) => {
              setExpandedRows(expanded ? [record.key] : []);
            },
            expandIcon: ({ expanded, onExpand, record }) =>
              record.releases.length > 0 ? (
                expanded
                  ? <DownOutlined style={{ cursor: 'pointer', fontSize: 12, marginRight: 8 }} onClick={(e) => onExpand(record, e)} />
                  : <RightOutlined style={{ cursor: 'pointer', fontSize: 12, marginRight: 8 }} onClick={(e) => onExpand(record, e)} />
              ) : <span style={{ width: 20, display: 'inline-block' }} />,
            expandedRowRender: (record) => (
              <div style={{ padding: '8px 0' }}>
                {record.releases.map((rel) => (
                  <div key={rel.id} style={{ marginBottom: 12 }}>
                    <Space size={8} style={{ marginBottom: 4 }}>
                      <Tag color={statusConfig[rel.status]?.color || 'default'}>{rel.lane}</Tag>
                      <Text code style={{ fontSize: 12 }}>{getImageTag(rel.image)}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {dayjs(rel.updated_at).format('YYYY-MM-DD HH:mm:ss')}
                      </Text>
                    </Space>
                    <PodDetail app={record.name} lane={rel.lane} />
                  </div>
                ))}
              </div>
            ),
          }}
        />
      </Card>
    </div>
  );
}
