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
import { PlusOutlined, EditOutlined, DeleteOutlined, UndoOutlined } from '@ant-design/icons';
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
      const { data } = await api.get('/dynamic-config');
      const raw: RawConfig[] = Array.isArray(data) ? data : (data?.data || []);
      const laneSet = new Set<string>(['prod']);
      raw.forEach((c: RawConfig) => laneSet.add(c.lane));
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

  const columns: ColumnsType<{ key: string; value: string; lane: string }> = [
    {
      title: 'Key',
      dataIndex: 'key',
      width: 280,
      render: (text: string) => <Text code>{text}</Text>,
    },
    {
      title: 'Value',
      dataIndex: 'value',
      ellipsis: true,
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
        <Space>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(record.key, record.value)}
          >
            编辑
          </Button>
          {selectedLane !== 'prod' && record.lane === selectedLane ? (
            <Popconfirm title="恢复到 prod 值？" onConfirm={() => handleDelete(record.key)}>
              <Button type="link" size="small" icon={<UndoOutlined />} danger>
                恢复
              </Button>
            </Popconfirm>
          ) : selectedLane === 'prod' ? (
            <Popconfirm title="删除此配置？" onConfirm={() => handleDelete(record.key)}>
              <Button type="link" size="small" icon={<DeleteOutlined />} danger>
                删除
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Space>
          <Text strong>泳道：</Text>
          <Select
            value={selectedLane}
            onChange={setSelectedLane}
            style={{ width: 160 }}
            options={lanes.map((l) => ({ label: l, value: l }))}
          />
        </Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新增配置
        </Button>
      </div>

      <Table
        dataSource={dataSource}
        columns={columns}
        loading={loading}
        pagination={false}
        size="middle"
        rowKey="key"
      />

      <Modal
        title={editingKey ? `编辑 ${editingKey}` : '新增配置'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => { setModalOpen(false); form.resetFields(); setEditingKey(null); }}
        okText="保存"
        cancelText="取消"
      >
        <Form form={form} layout="vertical">
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
              <Text type="secondary" style={{ marginLeft: 8 }}>
                此值仅在 {selectedLane} 泳道生效
              </Text>
            )}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
