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
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import { api } from '../api/client';

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
    { title: '名称', dataIndex: 'name', width: 180, fixed: 'left' },
    { 
      title: '客户端类型', 
      dataIndex: 'client_type', 
      width: 120,
      render: (type) => <Tag>{type}</Tag>
    },
    { 
      title: 'API 密钥', 
      dataIndex: 'api_key', 
      width: 200,
      ellipsis: true,
      render: (text) => text ? `${text.substring(0, 6)}...` : '-'
    },
    { title: '基础地址', dataIndex: 'base_url', width: 300, ellipsis: true },
    {
      title: '启用状态',
      dataIndex: 'is_active',
      width: 120,
      render: (value: boolean) => (
        <Tag color={value ? 'success' : 'error'}>
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
          <Button size="small" icon={<EditOutlined />} onClick={() => openModal(record)}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除该服务商?"
            onConfirm={() => handleDelete(record.provider_id)}
          >
            <Button size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-container">
      <div className="page-header" style={{ marginBottom: 16 }}>
        <h1 className="page-title">模型服务商</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()}>
          新建服务商
        </Button>
      </div>
      
      <div className="content-card">
        <Table
          rowKey="provider_id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={false}
          size="middle"
          scroll={{ x: 1300 }}
        />
      </div>
      
      <Modal
        title={editing ? '编辑服务商' : '新建服务商'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleOk}
        okText={editing ? '更新' : '创建'}
        cancelText="取消"
      >
        <Form layout="vertical" form={form}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input placeholder="例如: OpenAI Main" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API 密钥"
            rules={editing ? [] : [{ required: true, message: '请输入 API Key' }]}
          >
            <Input.Password placeholder={editing ? '留空表示不更新' : 'sk-...'} />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="基础地址"
            rules={[{ required: true, message: '请输入 Base URL' }]}
          >
            <Input placeholder="https://api.openai.com/v1" />
          </Form.Item>
          <Form.Item
            name="client_type"
            label="客户端类型"
            initialValue="openai"
            rules={[{ required: true }]}
          >
            <Select options={clientTypeOptions} />
          </Form.Item>
          <Form.Item name="is_active" label="启用状态" valuePropName="checked" initialValue>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
