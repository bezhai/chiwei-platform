import { useEffect, useState } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import { api } from '../api/client';

const { Text } = Typography;

interface ModelMapping {
  id: string;
  alias: string;
  provider_name: string;
  real_model_name: string;
  description?: string | null;
  model_config?: Record<string, unknown> | null;
  created_at: string;
}

export default function ModelMappings() {
  const [data, setData] = useState<ModelMapping[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ModelMapping | null>(null);
  const [form] = Form.useForm();

  const fetchMappings = async () => {
    setLoading(true);
    try {
      const { data } = await api.get('/model-mappings');
      setData(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMappings();
  }, []);

  const openModal = (mapping?: ModelMapping) => {
    if (mapping) {
      setEditing(mapping);
      form.setFieldsValue({
        alias: mapping.alias,
        provider_name: mapping.provider_name,
        real_model_name: mapping.real_model_name,
        description: mapping.description,
        model_config: mapping.model_config ? JSON.stringify(mapping.model_config, null, 2) : '',
      });
    } else {
      setEditing(null);
      form.resetFields();
    }
    setOpen(true);
  };

  const handleOk = async () => {
    try {
      const values = await form.validateFields();
      const payload = {
        ...values,
        model_config: values.model_config ? JSON.parse(values.model_config) : null,
      };
      if (editing) {
        await api.put(`/model-mappings/${editing.id}`, payload);
        message.success('已更新');
      } else {
        await api.post('/model-mappings', payload);
        message.success('已创建');
      }
      setOpen(false);
      fetchMappings();
    } catch (error) {
      if (error instanceof SyntaxError) {
        message.error('model_config JSON 格式错误');
        return;
      }
      if ((error as Error).name !== 'Error') {
        return;
      }
    }
  };

  const handleDelete = async (id: string) => {
    await api.delete(`/model-mappings/${id}`);
    message.success('已删除');
    fetchMappings();
  };

  const columns: ColumnsType<ModelMapping> = [
    { title: '别名', dataIndex: 'alias', width: 200, fixed: 'left', render: (text) => <Text strong style={{ color: '#0f172a' }}>{text}</Text> },
    { title: '服务商', dataIndex: 'provider_name', width: 180, render: (text) => <Text type="secondary">{text}</Text> },
    { title: '真实模型', dataIndex: 'real_model_name', width: 220, render: (text) => <Text code style={{ background: '#f8fafc', border: 'none' }}>{text}</Text> },
    { title: '描述', dataIndex: 'description', width: 200, ellipsis: true, render: (text) => <Text type="secondary">{text || '-'}</Text> },
    {
      title: '配置',
      dataIndex: 'model_config',
      width: 250,
      render: (value: Record<string, unknown>) =>
        value ? <div style={{ maxHeight: 100, overflow: 'auto', fontSize: 12, fontFamily: 'var(--font-mono)', background: '#f8fafc', padding: 8, borderRadius: 6, border: '1px solid #e2e8f0' }}>{JSON.stringify(value, null, 2)}</div> : <Text type="secondary">-</Text>,
    },
    {
      title: '操作',
      width: 140,
      fixed: 'right',
      render: (_, record) => (
        <Space style={{ marginRight: 16 }}>
          <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openModal(record)}>
            编辑
          </Button>
          <Popconfirm title="确认删除该配置?" onConfirm={() => handleDelete(record.id)}>
            <Button type="text" danger size="small" icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-container">
      <div className="page-header" style={{ marginBottom: 24 }}>
        <div>
          <h1 className="page-title">模型映射配置</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>将前端抽象模型名称映射到具体服务商与底层大模型</Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()} size="large">
          新建映射
        </Button>
      </div>

      <div className="content-card" style={{ padding: 0, overflow: 'hidden' }}>
        <Table 
          rowKey="id" 
          columns={columns} 
          dataSource={data} 
          loading={loading}
          pagination={false}
          size="middle"
          scroll={{ x: 1200 }}
        />
      </div>

      <Modal
        title={<div style={{ fontSize: 18, fontWeight: 600, color: '#0f172a' }}>{editing ? '编辑映射' : '新建映射'}</div>}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleOk}
        okText={editing ? '更新' : '创建'}
        cancelText="取消"
        width={680}
        styles={{ content: { borderRadius: 16, padding: 24 } }}
      >
        <Form layout="vertical" form={form} style={{ marginTop: 24 }}>
          <Form.Item
            name="alias"
            label={<Text strong>别名 (Alias)</Text>}
            rules={[{ required: true, message: '请输入别名' }]}
            tooltip="应用中调用的模型名称，如 'gpt-4o-mini'"
          >
            <Input placeholder="例如: gpt-4o-mini" size="large" />
          </Form.Item>
          <Form.Item
            name="provider_name"
            label={<Text strong>服务商名称</Text>}
            rules={[{ required: true, message: '请输入服务商名称' }]}
            tooltip="对应 Providers 中的 Name"
          >
            <Input placeholder="例如: OpenAI Main" size="large" />
          </Form.Item>
          <Form.Item
            name="real_model_name"
            label={<Text strong>真实模型名称</Text>}
            rules={[{ required: true, message: '请输入真实模型名称' }]}
            tooltip="发送给服务商的实际模型参数"
          >
            <Input placeholder="例如: gpt-4o-mini-2024-07-18" size="large" />
          </Form.Item>
          <Form.Item name="description" label={<Text strong>描述</Text>}>
            <Input.TextArea rows={3} placeholder="备注说明" style={{ background: '#f8fafc', borderColor: '#e2e8f0' }} />
          </Form.Item>
          <Form.Item name="model_config" label={<Text strong>模型配置 (JSON)</Text>} tooltip="覆盖模型默认参数">
            <Input.TextArea
              rows={6}
              style={{ fontFamily: 'var(--font-mono)', fontSize: 13, background: '#f8fafc', borderColor: '#e2e8f0' }}
              placeholder={`{
  "temperature": 0.7,
  "max_tokens": 1000
}`}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
