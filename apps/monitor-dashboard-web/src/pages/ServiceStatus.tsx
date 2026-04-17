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
    <div style={{ padding: '8px 12px', background: '#f8fafc', borderRadius: 8, marginTop: 8, border: '1px solid #e2e8f0' }}>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8, fontWeight: 500 }}>
        预期: {data.desired} / 就绪: {data.ready} / 可用: {data.available}
      </Text>
      {data.pods?.map((pod) => (
        <div key={pod.name} style={{ fontSize: 12, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Tag color={pod.ready ? 'green' : 'red'} style={{ margin: 0, fontSize: 11, border: 'none' }}>{pod.status}</Tag>
          <Text code style={{ fontSize: 11, background: 'transparent', border: 'none', padding: 0 }}>{pod.name}</Text>
          {pod.restarts > 0 && <Text type="warning" style={{ fontSize: 11 }}>重启: {pod.restarts}</Text>}
          {pod.reason && <Text type="danger" style={{ fontSize: 11 }}>{pod.reason}</Text>}
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
      render: (name: string) => <Text strong style={{ color: '#0f172a' }}>{name}</Text>,
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (desc: string) => <Text type="secondary">{desc}</Text>,
    },
    {
      title: '端口',
      dataIndex: 'port',
      key: 'port',
      width: 100,
      render: (port: number | string) =>
        port === 0 ? <Tag bordered={false}>Worker</Tag> : <Text type="secondary">{port}</Text>,
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
                  <Tag bordered={false} color={cfg.color} icon={cfg.icon} style={{ marginRight: 0, fontWeight: 500 }}>
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
      title: '主线版本',
      key: 'version',
      width: 120,
      render: (_: unknown, row: ServiceRow) => {
        const prodRelease = row.releases.find((r) => r.lane === 'prod');
        const tag = prodRelease ? getImageTag(prodRelease.image) : '';
        return tag ? <Tag bordered={false}>{tag}</Tag> : <Text type="secondary">-</Text>;
      },
    },
    {
      title: '最后更新',
      key: 'updatedAt',
      width: 160,
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
      <div className="page-header">
        <div>
          <h1 className="page-title">服务状态</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block', fontSize: 14 }}>实时监控所有服务的部署状态与资源情况</Text>
        </div>
        <Tooltip title="每 30 秒自动刷新">
          <div 
            onClick={() => { setLoading(true); fetchData(); }}
            style={{ 
              display: 'flex', 
              alignItems: 'center', 
              gap: 8, 
              padding: '8px 16px', 
              background: '#fff', 
              borderRadius: 8, 
              cursor: 'pointer',
              border: '1px solid #e2e8f0',
              boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
              transition: 'all 0.2s'
            }}
            className="hover-card"
          >
            <ReloadOutlined spin={loading} style={{ color: '#64748b' }} />
            <Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>刷新</Text>
          </div>
        </Tooltip>
      </div>

      <div className="metrics-strip">
        <div className="metrics-item">
          <div className="metrics-label">
            <CloudServerOutlined style={{ color: '#64748b' }} />
            <span>总服务数</span>
          </div>
          <div className="metrics-value">
            {apps.length}
          </div>
        </div>
        <div className="metrics-item">
          <div className="metrics-label">
            <CheckCircleOutlined style={{ color: '#10b981' }} />
            <span>运行中</span>
          </div>
          <div className="metrics-value">
            {runningCount}
            {failedCount > 0 && <span className="metrics-sub">/ {failedCount} 异常</span>}
          </div>
        </div>
        <div className="metrics-item">
          <div className="metrics-label">
            <DeploymentUnitOutlined style={{ color: '#8b5cf6' }} />
            <span>活跃泳道</span>
          </div>
          <div className="metrics-value">
            {lanes.length}
          </div>
        </div>
      </div>

      {laneBindings.length > 0 && (
        <Card bordered={false} className="content-card" style={{ marginBottom: 24 }} bodyStyle={{ padding: '16px 24px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <Text strong style={{ fontSize: 14, color: '#334155' }}>路由绑定</Text>
            <Space wrap size={[8, 8]}>
              {laneBindings.map((b) => (
                <Tag key={`${b.route_type}-${b.route_key}`} bordered={false} className="custom-route-tag">
                  <span className="route-key">{b.route_type}:{b.route_key}</span>
                  <span className="route-arrow">→</span>
                  <span className="route-lane">{b.lane_name}</span>
                </Tag>
              ))}
            </Space>
          </div>
        </Card>
      )}

      <Card bordered={false} className="content-card" bodyStyle={{ padding: 0, overflow: 'hidden' }}>
        <Table
          dataSource={dataSource}
          columns={columns}
          loading={loading}
          pagination={false}
          size="middle"
          className="custom-table"
          expandable={{
            expandedRowKeys: expandedRows,
            onExpand: (expanded, record) => {
              setExpandedRows(expanded ? [record.key] : []);
            },
            expandIcon: ({ expanded, onExpand, record }) =>
              record.releases.length > 0 ? (
                expanded
                  ? <DownOutlined style={{ cursor: 'pointer', fontSize: 12, marginRight: 8, color: '#64748b' }} onClick={(e) => onExpand(record, e)} />
                  : <RightOutlined style={{ cursor: 'pointer', fontSize: 12, marginRight: 8, color: '#64748b' }} onClick={(e) => onExpand(record, e)} />
              ) : <span style={{ width: 20, display: 'inline-block' }} />,
            expandedRowRender: (record) => (
              <div style={{ padding: '16px 32px', background: '#fafafa', borderTop: '1px solid #f1f5f9' }}>
                {record.releases.map((rel) => (
                  <div key={rel.id} style={{ marginBottom: 16, background: '#fff', padding: 16, borderRadius: 12, border: '1px solid #e2e8f0', boxShadow: '0 1px 2px rgba(0,0,0,0.02)' }}>
                    <Space size={12} style={{ marginBottom: 8 }}>
                      <Tag bordered={false} color={statusConfig[rel.status]?.color || 'default'} style={{ fontWeight: 500 }}>{rel.lane}</Tag>
                      <Text code style={{ fontSize: 12, border: 'none', background: '#f1f5f9' }}>{getImageTag(rel.image)}</Text>
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
