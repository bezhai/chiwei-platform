import { useEffect, useState } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import { api } from '../api/client';

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
    { title: '别名', dataIndex: 'alias', width: 220, fixed: 'left' },
    { title: '服务商', dataIndex: 'provider_name', width: 200 },
    { title: '真实模型', dataIndex: 'real_model_name', width: 240 },
    { title: '描述', dataIndex: 'description', width: 200, ellipsis: true },
    {
      title: '配置',
      dataIndex: 'model_config',
      width: 150,
      render: (value: Record<string, unknown>) =>
        value ? <div className="json-preview" style={{ maxHeight: 100, overflow: 'auto', fontSize: 12 }}>{JSON.stringify(value, null, 2)}</div> : null,
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
          <Popconfirm title="确认删除该配置?" onConfirm={() => handleDelete(record.id)}>
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
        <h1 className="page-title">模型映射配置</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal()}>
          新建映射
        </Button>
      </div>

      <div className="content-card">
        <Table 
          rowKey="id" 
          columns={columns} 
          dataSource={data} 
          loading={loading}
          pagination={false}
          size="middle"
          scroll={{ x: 1300 }}
        />
      </div>

      <Modal
        title={editing ? '编辑映射' : '新建映射'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleOk}
        okText={editing ? '更新' : '创建'}
        cancelText="取消"
        width={720}
      >
        <Form layout="vertical" form={form}>
          <Form.Item
            name="alias"
            label="别名 (Alias)"
            rules={[{ required: true, message: '请输入别名' }]}
            tooltip="应用中调用的模型名称"
          >
            <Input placeholder="例如: gpt-4o-mini" />
          </Form.Item>
          <Form.Item
            name="provider_name"
            label="服务商名称"
            rules={[{ required: true, message: '请输入服务商名称' }]}
            tooltip="对应 Providers 中的 Name"
          >
            <Input placeholder="例如: OpenAI Main" />
          </Form.Item>
          <Form.Item
            name="real_model_name"
            label="真实模型名称"
            rules={[{ required: true, message: '请输入真实模型名称' }]}
            tooltip="发送给服务商的实际模型参数"
          >
            <Input placeholder="例如: gpt-4o-mini-2024-07-18" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="备注说明" />
          </Form.Item>
          <Form.Item name="model_config" label="模型配置 (JSON)">
            <Input.TextArea
              rows={6}
              className="json-preview"
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
