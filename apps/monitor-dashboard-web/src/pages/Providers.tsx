import { useEffect, useState } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import { api } from '../api/client';

const { Text } = Typography;

interface Provider {
  provider_id: string;
  name: string;
  api_key: string;
  base_url: string;
  client_type: string;
  is_active: boolean;
  created_at: string;
}

const clientTypeOptions = [
  { value: 'openai', label: 'openai' },
  { value: 'openai-responses', label: 'openai-responses' },
  { value: 'deepseek', label: 'deepseek' },
  { value: 'ark', label: 'ark' },
  { value: 'azure-http', label: 'azure-http' },
  { value: 'google', label: 'google' },
];

export default function Providers() {
  const [data, setData] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Provider | null>(null);
  const [form] = Form.useForm();

  const fetchProviders = async () => {
    setLoading(true);
    try {
      const { data } = await api.get('/providers');
      setData(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProviders();
  }, []);

  const openModal = (provider?: Provider) => {
    if (provider) {
      setEditing(provider);
      form.setFieldsValue({
        name: provider.name,
        api_key: '',
        base_url: provider.base_url,
        client_type: provider.client_type,
        is_active: provider.is_active,
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
      if (editing) {
        await api.put(`/providers/${editing.provider_id}`, values);
        message.success('已更新');
      } else {
        await api.post('/providers', values);
        message.success('已创建');
      }
      setOpen(false);
      fetchProviders();
    } catch (error) {
      if ((error as Error).name !== 'Error') {
        return;
      }
    }
  };

  const handleDelete = async (providerId: string) => {
    await api.delete(`/providers/${providerId}`);
    message.success('已删除');
    fetchProviders();
  };

  const columns: ColumnsType<Provider> = [
    { title: '名称', dataIndex: 'name', width: 180, fixed: 'left', render: (text) => <Text strong style={{ color: '#0f172a' }}>{text}</Text> },
    { 
      title: '客户端类型', 
      dataIndex: 'client_type', 
      width: 140,
      render: (type) => <Tag bordered={false} color="blue" style={{ fontWeight: 500 }}>{type}</Tag>
    },
    { 
      title: 'API 密钥', 
      dataIndex: 'api_key', 
      width: 200,
      ellipsis: true,
      render: (text) => text ? <Text type="secondary" style={{ fontFamily: 'var(--font-mono)', fontSize: 13 }}>{text.substring(0, 6)}...</Text> : '-'
    },
    { title: '基础地址', dataIndex: 'base_url', width: 300, ellipsis: true, render: (text) => <Text type="secondary" style={{ fontSize: 13 }}>{text}</Text> },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 100,
      render: (value: boolean) => (
        <Tag bordered={false} color={value ? 'success' : 'default'} style={{ fontWeight: 500 }}>
          {value ? '启用' : '禁用'}
        </Tag>
      ),
    },
    {
      title: '操作',
      width: 160,
      fixed: 'right',
      render: (_, record) => (
        <Space style={{ marginRight: 16 }}>
          <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openModal(record)}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除该服务商?"
            onConfirm={() => handleDelete(record.provider_id)}
          >
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
          <h1 className="page-title">模型服务商</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>管理接入的大模型 API 供应商与凭证</Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()} size="large">
          新建服务商
        </Button>
      </div>
      
      <div className="content-card" style={{ padding: 0, overflow: 'hidden' }}>
        <Table
          rowKey="provider_id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={false}
          size="middle"
          scroll={{ x: 1000 }}
        />
      </div>
      
      <Modal
        title={<div style={{ fontSize: 18, fontWeight: 600, color: '#0f172a' }}>{editing ? '编辑服务商' : '新建服务商'}</div>}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleOk}
        okText={editing ? '更新' : '创建'}
        cancelText="取消"
        styles={{ content: { borderRadius: 16, padding: 24 } }}
      >
        <Form layout="vertical" form={form} style={{ marginTop: 24 }}>
          <Form.Item
            name="name"
            label={<Text strong>名称</Text>}
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input placeholder="例如: OpenAI Main" size="large" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label={<Text strong>API 密钥</Text>}
            rules={editing ? [] : [{ required: true, message: '请输入 API Key' }]}
          >
            <Input.Password placeholder={editing ? '留空表示不更新' : 'sk-...'} size="large" />
          </Form.Item>
          <Form.Item
            name="base_url"
            label={<Text strong>基础地址</Text>}
            rules={[{ required: true, message: '请输入 Base URL' }]}
          >
            <Input placeholder="https://api.openai.com/v1" size="large" />
          </Form.Item>
          <Form.Item
            name="client_type"
            label={<Text strong>客户端类型</Text>}
            initialValue="openai"
            rules={[{ required: true }]}
          >
            <Select options={clientTypeOptions} size="large" />
          </Form.Item>
          <Form.Item name="is_active" label={<Text strong>启用状态</Text>} valuePropName="checked" initialValue>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
