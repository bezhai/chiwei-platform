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
  success: 'green',
  error: 'red',
  denied: 'orange',
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
      width: 180,
      render: (v: string) => (
        <Tooltip title={dayjs(v).format('YYYY-MM-DD HH:mm:ss.SSS')}>
          <Text type="secondary" style={{ fontSize: 13 }}>
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
      render: (v: string) => <Tag color={callerColors[v] || 'default'}>{v}</Tag>,
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 220,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: '参数',
      dataIndex: 'params',
      key: 'params',
      ellipsis: true,
      render: (v: Record<string, unknown> | null) => {
        if (!v) return '-';
        const str = JSON.stringify(v);
        return (
          <Tooltip title={<pre style={{ maxHeight: 300, overflow: 'auto', margin: 0, fontSize: 11 }}>{JSON.stringify(v, null, 2)}</pre>}>
            <Text type="secondary" style={{ fontSize: 12 }}>{str.length > 80 ? str.slice(0, 80) + '...' : str}</Text>
          </Tooltip>
        );
      },
    },
    {
      title: '结果',
      dataIndex: 'result',
      key: 'result',
      width: 90,
      render: (v: string) => <Tag color={resultColors[v] || 'default'}>{v}</Tag>,
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      width: 80,
      render: (v: number | null) => v != null ? `${v}ms` : '-',
    },
    {
      title: '错误信息',
      dataIndex: 'error_message',
      key: 'error_message',
      width: 200,
      ellipsis: true,
      render: (v: string | null) => v ? <Text type="danger" style={{ fontSize: 12 }}>{v}</Text> : '-',
    },
  ];

  return (
    <div className="page-container">
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <Title level={4} style={{ marginBottom: 4 }}>审计日志</Title>
          <Text type="secondary">所有 API 操作的完整记录</Text>
        </div>
        <Tooltip title="刷新">
          <ReloadOutlined
            spin={loading}
            style={{ fontSize: 16, cursor: 'pointer', color: '#8c8c8c' }}
            onClick={fetchData}
          />
        </Tooltip>
      </div>

      <Card bordered={false} style={{ marginBottom: 16 }}>
        <Space wrap size={[12, 12]}>
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
            prefix={<SearchOutlined />}
            style={{ width: 200 }}
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
            重置
          </Button>
        </Space>
      </Card>

      <Card bordered={false}>
        <Table
          dataSource={data}
          columns={columns}
          rowKey="id"
          loading={loading}
          size="small"
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: ['20', '50', '100'],
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
          rowClassName={(record) => record.result === 'error' || record.result === 'denied' ? 'audit-row-error' : ''}
        />
      </Card>
    </div>
  );
}
