import { useEffect, useState, useCallback } from 'react';
import { Card, Table, Tag, Typography, Tabs, Modal, Input, Button, Space, Tooltip, message } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { api } from '../api/client';

const { Text } = Typography;

interface MutationItem {
  id: number;
  db: string;
  sql: string;
  reason: string;
  status: string;
  submitted_by: string;
  reviewed_by: string;
  review_note: string;
  executed_at: string | null;
  error: string;
  created_at: string;
  updated_at: string;
}

const statusConfig: Record<string, { color: string; label: string }> = {
  pending: { color: 'processing', label: '待审批' },
  approved: { color: 'success', label: '已通过' },
  rejected: { color: 'warning', label: '已拒绝' },
  failed: { color: 'error', label: '执行失败' },
};

const submitterColors: Record<string, string> = {
  'claude-code': 'purple',
};

export default function DbMutations() {
  const [data, setData] = useState<MutationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('pending');
  const [reviewModal, setReviewModal] = useState<MutationItem | null>(null);
  const [note, setNote] = useState('');
  const [actionLoading, setActionLoading] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get('/ops/db-mutations', { params: { status: activeTab } });
      setData(res.data || []);
    } catch (e) {
      console.error('Failed to fetch mutations:', e);
    } finally {
      setLoading(false);
    }
  }, [activeTab]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleAction = async (action: 'approve' | 'reject') => {
    if (!reviewModal) return;
    if (action === 'reject' && !note.trim()) {
      message.warning('请填写拒绝原因');
      return;
    }
    setActionLoading(true);
    try {
      await api.post(`/ops/db-mutations/${reviewModal.id}/${action}`, { note });
      message.success(action === 'approve' ? '已审批通过并执行' : '已拒绝');
      setReviewModal(null);
      setNote('');
      fetchData();
    } catch (e: any) {
      message.error(e?.response?.data?.message || '操作失败');
    } finally {
      setActionLoading(false);
    }
  };

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      key: 'id',
      width: 60,
    },
    {
      title: '数据库',
      dataIndex: 'db',
      key: 'db',
      width: 100,
      render: (v: string) => <Tag bordered={false}>{v}</Tag>,
    },
    {
      title: 'SQL',
      dataIndex: 'sql',
      key: 'sql',
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={<pre style={{ maxHeight: 300, overflow: 'auto', margin: 0, fontSize: 11, fontFamily: 'var(--font-mono)' }}>{v}</pre>}>
          <Text style={{ fontSize: 12, fontFamily: 'var(--font-mono)' }}>
            {v.length > 80 ? v.slice(0, 80) + '...' : v}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '原因',
      dataIndex: 'reason',
      key: 'reason',
      ellipsis: true,
      width: 200,
    },
    {
      title: '提交人',
      dataIndex: 'submitted_by',
      key: 'submitted_by',
      width: 120,
      render: (v: string) => <Tag bordered={false} color={submitterColors[v] || 'default'} style={{ fontWeight: 500 }}>{v}</Tag>,
    },
    {
      title: '提交时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (v: string) => (
        <Tooltip title={dayjs(v).format('YYYY-MM-DD HH:mm:ss')}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            {dayjs(v).format('MM-DD HH:mm:ss')}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string) => {
        const cfg = statusConfig[v] || { color: 'default', label: v };
        return <Tag bordered={false} color={cfg.color} style={{ fontWeight: 500 }}>{cfg.label}</Tag>;
      },
    },
    ...(activeTab === 'pending'
      ? [
          {
            title: '操作',
            key: 'actions',
            width: 80,
            render: (_: unknown, record: MutationItem) => (
              <Button
                type="primary"
                size="small"
                onClick={() => { setReviewModal(record); setNote(''); }}
              >
                处理
              </Button>
            ),
          },
        ]
      : activeTab === 'failed'
        ? [
            {
              title: '错误信息',
              dataIndex: 'error',
              key: 'error',
              width: 200,
              ellipsis: true,
              render: (v: string) => v ? (
                <Tooltip title={<pre style={{ maxHeight: 400, overflow: 'auto', margin: 0, fontSize: 11, fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{v}</pre>}>
                  <Text type="danger" style={{ fontSize: 12 }}>{v}</Text>
                </Tooltip>
              ) : '-',
            },
          ]
        : []),
  ];

  return (
    <div className="page-container">
      <div className="page-header">
        <div>
          <h1 className="page-title">DB 变更审批</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>
            DDL/DML 变更申请的审核与执行
          </Text>
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
              transition: 'all 0.2s',
            }}
            className="hover-card"
          >
            <ReloadOutlined spin={loading} style={{ color: '#64748b' }} />
            <Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>刷新</Text>
          </div>
        </Tooltip>
      </div>

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key)}
        items={[
          { key: 'pending', label: '待审批' },
          { key: 'approved', label: '已通过' },
          { key: 'rejected', label: '已拒绝' },
          { key: 'failed', label: '执行失败' },
        ]}
        style={{ marginBottom: 16 }}
      />

      <Card bordered={false} className="content-card" bodyStyle={{ padding: 0, overflow: 'hidden' }}>
        <Table
          dataSource={data}
          columns={columns}
          rowKey="id"
          loading={loading}
          size="middle"
          pagination={{
            pageSize: 20,
            showTotal: (t) => `共 ${t} 条记录`,
            style: { padding: '16px 24px', margin: 0, borderTop: '1px solid #f1f5f9' },
          }}
        />
      </Card>

      {/* Review Modal */}
      <Modal
        title="处理变更申请"
        open={!!reviewModal}
        onCancel={() => { setReviewModal(null); setNote(''); }}
        width={720}
        footer={
          <Space>
            <Button onClick={() => { setReviewModal(null); setNote(''); }}>取消</Button>
            <Button danger loading={actionLoading} onClick={() => handleAction('reject')}>
              拒绝
            </Button>
            <Button type="primary" loading={actionLoading} onClick={() => handleAction('approve')}>
              通过并执行
            </Button>
          </Space>
        }
      >
        {reviewModal && (
          <>
            <div style={{ marginBottom: 12 }}>
              <Tag bordered={false}>{reviewModal.db}</Tag>
              <Tag bordered={false} color={submitterColors[reviewModal.submitted_by] || 'default'}>
                {reviewModal.submitted_by}
              </Tag>
            </div>
            <pre style={{
              background: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 8,
              padding: 16,
              fontSize: 13,
              fontFamily: 'var(--font-mono)',
              maxHeight: 400,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}>
              {reviewModal.sql}
            </pre>
            {reviewModal.reason && (
              <div style={{ marginTop: 12 }}>
                <Text type="secondary">原因：</Text>
                <Text>{reviewModal.reason}</Text>
              </div>
            )}
            <Input.TextArea
              placeholder="备注（拒绝时必填）"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              style={{ marginTop: 12 }}
            />
          </>
        )}
      </Modal>
    </div>
  );
}
