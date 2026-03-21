import { useEffect, useState, useCallback } from 'react';
import { Card, Table, Tag, Typography, Space, Select, DatePicker, Input, Button, Tooltip } from 'antd';
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import dayjs, { type Dayjs } from 'dayjs';
import { api } from '../api/client';

const { Title, Text } = Typography;
const { RangePicker } = DatePicker;

interface AuditLogItem {
  id: number;
  caller: string;
  action: string;
  params: Record<string, unknown> | null;
  result: string;
  error_message: string | null;
  duration_ms: number | null;
  created_at: string;
}

const callerColors: Record<string, string> = {
  'web-admin': 'blue',
  'claude-code': 'purple',
};

const resultColors: Record<string, string> = {
  success: 'success',
  error: 'error',
  denied: 'warning',
};

export default function AuditLogs() {
  const [data, setData] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [caller, setCaller] = useState<string>();
  const [action, setAction] = useState<string>();
  const [result, setResult] = useState<string>();
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null]>();

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string> = {
        page: String(page),
        pageSize: String(pageSize),
      };
      if (caller) params.caller = caller;
      if (action) params.action = action;
      if (result) params.result = result;
      if (dateRange?.[0]) params.from = dateRange[0].startOf('day').toISOString();
      if (dateRange?.[1]) params.to = dateRange[1].endOf('day').toISOString();

      const res = await api.get('/audit-logs', { params });
      setData(res.data.items || []);
      setTotal(res.data.total || 0);
    } catch (e) {
      console.error('Failed to fetch audit logs:', e);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, caller, action, result, dateRange]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (v: string) => (
        <Tooltip title={dayjs(v).format('YYYY-MM-DD HH:mm:ss.SSS')}>
          <Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>
            {dayjs(v).format('MM-DD HH:mm:ss')}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '调用者',
      dataIndex: 'caller',
      key: 'caller',
      width: 120,
      render: (v: string) => <Tag bordered={false} color={callerColors[v] || 'default'} style={{ fontWeight: 500 }}>{v}</Tag>,
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 220,
      render: (v: string) => <Text code style={{ fontSize: 12, background: '#f8fafc', border: 'none' }}>{v}</Text>,
    },
    {
      title: '参数',
      dataIndex: 'params',
      key: 'params',
      ellipsis: true,
      render: (v: Record<string, unknown> | null) => {
        if (!v) return <Text type="secondary">-</Text>;
        const str = JSON.stringify(v);
        return (
          <Tooltip title={<pre style={{ maxHeight: 300, overflow: 'auto', margin: 0, fontSize: 11, fontFamily: 'var(--font-mono)' }}>{JSON.stringify(v, null, 2)}</pre>}>
            <Text type="secondary" style={{ fontSize: 12, fontFamily: 'var(--font-mono)' }}>{str.length > 80 ? str.slice(0, 80) + '...' : str}</Text>
          </Tooltip>
        );
      },
    },
    {
      title: '结果',
      dataIndex: 'result',
      key: 'result',
      width: 90,
      render: (v: string) => <Tag bordered={false} color={resultColors[v] || 'default'} style={{ fontWeight: 500 }}>{v}</Tag>,
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      width: 80,
      render: (v: number | null) => v != null ? <Text type="secondary" style={{ fontSize: 13 }}>{v}ms</Text> : '-',
    },
    {
      title: '错误信息',
      dataIndex: 'error_message',
      key: 'error_message',
      width: 200,
      ellipsis: true,
      render: (v: string | null) => v ? <Text type="danger" style={{ fontSize: 12 }}>{v}</Text> : <Text type="secondary">-</Text>,
    },
  ];

  return (
    <div className="page-container">
      <div className="page-header">
        <div>
          <h1 className="page-title">审计日志</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>所有 API 操作的完整安全审计记录</Text>
        </div>
        <Tooltip title="刷新">
          <div 
            onClick={fetchData}
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

      <div className="filter-card">
        <Space wrap size={[16, 16]}>
          <Select
            allowClear
            placeholder="调用者"
            style={{ width: 140 }}
            value={caller}
            onChange={(v) => { setCaller(v); setPage(1); }}
            options={[
              { value: 'web-admin', label: 'Web Admin' },
              { value: 'claude-code', label: 'Claude Code' },
            ]}
          />
          <Input
            allowClear
            placeholder="操作名称"
            prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
            style={{ width: 220 }}
            value={action}
            onChange={(e) => { setAction(e.target.value || undefined); setPage(1); }}
          />
          <Select
            allowClear
            placeholder="结果"
            style={{ width: 120 }}
            value={result}
            onChange={(v) => { setResult(v); setPage(1); }}
            options={[
              { value: 'success', label: 'success' },
              { value: 'error', label: 'error' },
              { value: 'denied', label: 'denied' },
            ]}
          />
          <RangePicker
            value={dateRange as [Dayjs, Dayjs] | undefined}
            onChange={(v) => { setDateRange(v as [Dayjs | null, Dayjs | null]); setPage(1); }}
          />
          <Button onClick={() => { setCaller(undefined); setAction(undefined); setResult(undefined); setDateRange(undefined); setPage(1); }}>
            重置筛选
          </Button>
        </Space>
      </div>

      <Card bordered={false} className="content-card" bodyStyle={{ padding: 0, overflow: 'hidden' }}>
        <Table
          dataSource={data}
          columns={columns}
          rowKey="id"
          loading={loading}
          size="middle"
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: ['20', '50', '100'],
            showTotal: (t) => `共 ${t} 条记录`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
            style: { padding: '16px 24px', margin: 0, borderTop: '1px solid #f1f5f9' }
          }}
          rowClassName={(record) => record.result === 'error' || record.result === 'denied' ? 'audit-row-error' : ''}
        />
      </Card>
    </div>
  );
}
