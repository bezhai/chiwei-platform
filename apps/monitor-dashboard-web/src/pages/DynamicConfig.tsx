import { useEffect, useState, useCallback } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  UndoOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { api } from '../api/client';

const { Text } = Typography;

interface ConfigEntry {
  value: string;
  lane: string;
}

interface RawConfig {
  key: string;
  lane: string;
  value: string;
  updated_at: string;
}

function StatMark({ variant }: { variant: 'total' | 'override' | 'inherit' }) {
  if (variant === 'override') {
    return (
      <svg viewBox="0 0 16 16" aria-hidden="true" className="dynamic-config-stat-mark">
        <path d="M3 11.5h10" />
        <path d="M5.5 8.5 8 6l2.5 2.5" />
        <path d="M8 6v6" />
      </svg>
    );
  }

  if (variant === 'inherit') {
    return (
      <svg viewBox="0 0 16 16" aria-hidden="true" className="dynamic-config-stat-mark">
        <path d="M3 4.5h10" />
        <path d="M5.5 7.5 8 10l2.5-2.5" />
        <path d="M8 10V4" />
      </svg>
    );
  }

  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="dynamic-config-stat-mark">
      <rect x="3" y="4" width="6" height="6" rx="1.5" />
      <rect x="7" y="7" width="6" height="6" rx="1.5" />
    </svg>
  );
}

export default function DynamicConfig() {
  const [lanes, setLanes] = useState<string[]>(['prod']);
  const [selectedLane, setSelectedLane] = useState('prod');
  const [resolved, setResolved] = useState<Record<string, ConfigEntry>>({});
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [form] = Form.useForm();

  const fetchResolved = useCallback(async (lane: string) => {
    setLoading(true);
    try {
      const { data } = await api.get('/dynamic-config/resolved', { params: { lane } });
      setResolved(data?.configs || data?.data?.configs || {});
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchLanes = useCallback(async () => {
    try {
      const [configRes, statusRes] = await Promise.all([
        api.get('/dynamic-config').catch(() => ({ data: [] })),
        api.get('/service-status').catch(() => ({ data: { releases: [] } })),
      ]);
      const laneSet = new Set<string>(['prod']);
      // 从已有配置中提取 lane
      const raw: RawConfig[] = Array.isArray(configRes.data) ? configRes.data : (configRes.data?.data || []);
      raw.forEach((c: RawConfig) => laneSet.add(c.lane));
      // 从已部署 release 中提取 lane
      const releases: { lane?: string }[] = statusRes.data?.releases || [];
      releases.forEach((r) => { if (r.lane) laneSet.add(r.lane); });
      setLanes(Array.from(laneSet).sort());
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    fetchLanes();
  }, [fetchLanes]);

  useEffect(() => {
    fetchResolved(selectedLane);
  }, [selectedLane, fetchResolved]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      await api.put(`/dynamic-config/${encodeURIComponent(values.key)}`, {
        lane: selectedLane,
        value: values.value,
      });
      message.success('已保存');
      setModalOpen(false);
      form.resetFields();
      setEditingKey(null);
      fetchResolved(selectedLane);
      fetchLanes();
    } catch {
      // form validation error
    }
  };

  const handleDelete = async (key: string) => {
    if (selectedLane === 'prod') {
      await api.delete(`/dynamic-config/${encodeURIComponent(key)}`);
      message.success('已删除');
    } else {
      await api.delete(`/dynamic-config/${encodeURIComponent(key)}`, {
        params: { lane: selectedLane },
      });
      message.success('已恢复到 prod');
    }
    fetchResolved(selectedLane);
    fetchLanes();
  };

  const openEdit = (key: string, value: string) => {
    setEditingKey(key);
    form.setFieldsValue({ key, value });
    setModalOpen(true);
  };

  const openCreate = () => {
    setEditingKey(null);
    form.resetFields();
    setModalOpen(true);
  };

  const dataSource = Object.entries(resolved)
    .map(([key, entry]) => ({ key, ...entry }))
    .sort((a, b) => a.key.localeCompare(b.key));

  const filteredData = dataSource.filter((item) => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return true;
    return item.key.toLowerCase().includes(keyword) || item.value.toLowerCase().includes(keyword);
  });

  const overrideCount = dataSource.filter((item) => item.lane === selectedLane && selectedLane !== 'prod').length;
  const inheritedCount = dataSource.length - overrideCount;
  const createActionLabel = selectedLane === 'prod' ? '新增配置项' : '添加局部覆盖';

  const columns: ColumnsType<{ key: string; value: string; lane: string }> = [
    {
      title: 'Key',
      dataIndex: 'key',
      width: 300,
      render: (text: string) => (
        <div className="dynamic-config-key-cell">
          <Text code>{text}</Text>
        </div>
      ),
    },
    {
      title: 'Value',
      dataIndex: 'value',
      ellipsis: true,
      render: (value: string) => (
        <div className="dynamic-config-value-cell">
          <span>{value}</span>
        </div>
      ),
    },
    {
      title: '来源',
      dataIndex: 'lane',
      width: 120,
      render: (lane: string) => (
        <Tag className={`dynamic-config-source-tag${lane === selectedLane && lane !== 'prod' ? ' is-lane' : ''}`} bordered={false}>
          {lane === selectedLane && lane !== 'prod' ? '当前泳道' : '基础 PROD'}
        </Tag>
      ),
    },
    {
      title: '操作',
      width: 110,
      render: (_: unknown, record: { key: string; value: string; lane: string }) => (
        <Space size={4}>
          <Tooltip title="编辑">
            <Button
              type="text"
              size="small"
              shape="circle"
              className="dynamic-config-icon-button"
              icon={<EditOutlined />}
              onClick={() => openEdit(record.key, record.value)}
            />
          </Tooltip>
          {selectedLane !== 'prod' && record.lane === selectedLane ? (
            <Popconfirm title="恢复到 prod 值？" onConfirm={() => handleDelete(record.key)}>
              <Tooltip title="恢复到 prod">
                <Button
                  type="text"
                  size="small"
                  shape="circle"
                  className="dynamic-config-icon-button"
                  icon={<UndoOutlined />}
                  danger
                />
              </Tooltip>
            </Popconfirm>
          ) : selectedLane === 'prod' ? (
            <Popconfirm title="删除此配置？" onConfirm={() => handleDelete(record.key)}>
              <Tooltip title="删除">
                <Button
                  type="text"
                  size="small"
                  shape="circle"
                  className="dynamic-config-icon-button"
                  icon={<DeleteOutlined />}
                  danger
                />
              </Tooltip>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <div className="page-container dynamic-config-page">
      <div className="dynamic-config-header">
        <div className="dynamic-config-header-top">
          <span className="dynamic-config-eyebrow">动态配置</span>
          <div className="dynamic-config-inline-field">
            <span className="dynamic-config-inline-label">泳道</span>
            <Select
              value={selectedLane}
              onChange={setSelectedLane}
              className="dynamic-config-select"
              options={lanes.map((l) => ({ label: l, value: l }))}
            />
          </div>
        </div>
        <h1 className="dynamic-config-title">泳道配置管理</h1>
        <Text className="dynamic-config-subtitle">
          当前视图展示 {selectedLane} 泳道的最终生效配置。非 prod 泳道仅需配置差异项，其余自动回落到 prod。
        </Text>
      </div>

      <div className="dynamic-config-summary-strip">
        <div className="dynamic-config-summary-item">
          <div className="dynamic-config-summary-label">
            <StatMark variant="total" />
            <span>统一生效总数</span>
          </div>
          <div className="dynamic-config-summary-value">{dataSource.length}</div>
        </div>
        <div className="dynamic-config-summary-divider" />
        <div className="dynamic-config-summary-item">
          <div className="dynamic-config-summary-label">
            <StatMark variant="override" />
            <span>当前泳道特异覆盖</span>
          </div>
          <div className="dynamic-config-summary-value">{overrideCount}</div>
        </div>
        <div className="dynamic-config-summary-divider" />
        <div className="dynamic-config-summary-item">
          <div className="dynamic-config-summary-label">
            <StatMark variant="inherit" />
            <span>继承自基础 PROD</span>
          </div>
          <div className="dynamic-config-summary-value">{inheritedCount}</div>
        </div>
      </div>

      <div className="dynamic-config-workbench">
        <div className="dynamic-config-toolbar">
          <div className="dynamic-config-toolbar-context">
            <div className="dynamic-config-toolbar-title">配置明细</div>
            <div className="dynamic-config-toolbar-meta">
              {selectedLane === 'prod' ? '当前展示生产基线配置' : `当前展示 ${selectedLane} 泳道覆盖后的最终结果`}
            </div>
          </div>
          <div className="dynamic-config-toolbar-actions">
            <Input
              allowClear
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              prefix={<SearchOutlined />}
              placeholder="搜索 Key 或 Value"
              className="dynamic-config-search"
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              {createActionLabel}
            </Button>
          </div>
        </div>

        <Table
          dataSource={filteredData}
          columns={columns}
          loading={loading}
          pagination={false}
          size="middle"
          rowKey="key"
          className="dynamic-config-table"
          scroll={{ x: 980 }}
        />
      </div>

      <Modal
        title={editingKey ? `编辑 ${editingKey}` : '新增配置'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => { setModalOpen(false); form.resetFields(); setEditingKey(null); }}
        okText="保存"
        cancelText="取消"
        width={640}
        styles={{ content: { borderRadius: 18, padding: 24 } }}
      >
        <Form form={form} layout="vertical" className="dynamic-config-form">
          <Form.Item
            name="key"
            label="Key"
            rules={[{ required: true, message: '请输入 key' }]}
          >
            <Input disabled={!!editingKey} placeholder="如 default_model" />
          </Form.Item>
          <Form.Item
            name="value"
            label="Value"
            rules={[{ required: true, message: '请输入 value' }]}
          >
            <Input.TextArea rows={3} placeholder="配置值" />
          </Form.Item>
          <Form.Item label="泳道">
            <Tag>{selectedLane}</Tag>
            {selectedLane !== 'prod' && (
              <Text type="secondary" className="dynamic-config-form-hint">
                此值仅在 {selectedLane} 泳道生效
              </Text>
            )}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
