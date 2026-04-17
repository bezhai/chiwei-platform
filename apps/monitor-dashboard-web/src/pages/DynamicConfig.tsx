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
  BranchesOutlined,
  ControlOutlined,
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
        <Tag color={lane === selectedLane && lane !== 'prod' ? 'blue' : 'default'}>
          {lane === selectedLane && lane !== 'prod' ? '本泳道' : 'prod'}
        </Tag>
      ),
    },
    {
      title: '操作',
      width: 160,
      render: (_: unknown, record: { key: string; value: string; lane: string }) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(record.key, record.value)}
          >
            编辑
          </Button>
          {selectedLane !== 'prod' && record.lane === selectedLane ? (
            <Popconfirm title="恢复到 prod 值？" onConfirm={() => handleDelete(record.key)}>
              <Button type="text" size="small" icon={<UndoOutlined />} danger>
                恢复
              </Button>
            </Popconfirm>
          ) : selectedLane === 'prod' ? (
            <Popconfirm title="删除此配置？" onConfirm={() => handleDelete(record.key)}>
              <Button type="text" size="small" icon={<DeleteOutlined />} danger>
                删除
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <div className="page-container dynamic-config-page">
      <div className="dynamic-config-hero">
        <div className="dynamic-config-hero-copy">
          <div className="dynamic-config-eyebrow">DYNAMIC CONFIG</div>
          <h1 className="dynamic-config-title">按泳道管理运行时配置</h1>
          <Text className="dynamic-config-subtitle">
            当前视图展示的是 {selectedLane} 的最终生效配置。非 prod 泳道只覆盖必要项，其余键自动回落到 prod。
          </Text>
        </div>
        <div className="dynamic-config-hero-actions">
          <div className="dynamic-config-lane-picker">
            <span className="dynamic-config-lane-label">泳道</span>
            <Select
              value={selectedLane}
              onChange={setSelectedLane}
              className="dynamic-config-select"
              options={lanes.map((l) => ({ label: l, value: l }))}
            />
          </div>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} size="large">
            新增配置
          </Button>
        </div>
      </div>

      <div className="dynamic-config-summary">
        <div className="dynamic-config-stat">
          <div className="dynamic-config-stat-icon">
            <ControlOutlined />
          </div>
          <div>
            <div className="dynamic-config-stat-label">生效键数</div>
            <div className="dynamic-config-stat-value">{dataSource.length}</div>
          </div>
        </div>
        <div className="dynamic-config-stat">
          <div className="dynamic-config-stat-icon">
            <BranchesOutlined />
          </div>
          <div>
            <div className="dynamic-config-stat-label">本泳道覆盖</div>
            <div className="dynamic-config-stat-value">{overrideCount}</div>
          </div>
        </div>
        <div className="dynamic-config-stat">
          <div className="dynamic-config-stat-icon">
            <UndoOutlined />
          </div>
          <div>
            <div className="dynamic-config-stat-label">继承 prod</div>
            <div className="dynamic-config-stat-value">{inheritedCount}</div>
          </div>
        </div>
      </div>

      <div className="dynamic-config-table-shell">
        <div className="dynamic-config-table-toolbar">
          <div className="dynamic-config-table-heading">
            <div className="dynamic-config-table-title">Resolved Config</div>
            <div className="dynamic-config-table-meta">
              {selectedLane === 'prod' ? '生产基线配置' : `${selectedLane} 泳道解析结果`}
            </div>
          </div>
          <Input
            allowClear
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            prefix={<SearchOutlined />}
            placeholder="搜索 key 或 value"
            className="dynamic-config-search"
          />
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
