import { useEffect, useState, useCallback } from 'react';
import {
  Button,
  Card,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Tag,
  Typography,
  message,
} from 'antd';
import { PlusOutlined, DeleteOutlined, SaveOutlined } from '@ant-design/icons';
import Editor from '@monaco-editor/react';
import { api } from '../api/client';

const { Text } = Typography;

interface Skill {
  name: string;
  description: string;
  files: string[];
}

function languageForFile(filename: string): string {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.md')) return 'markdown';
  if (lower.endsWith('.py')) return 'python';
  if (lower.endsWith('.sh')) return 'shell';
  if (lower.endsWith('.json')) return 'json';
  return 'plaintext';
}

export default function Skills() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loadingList, setLoadingList] = useState(false);
  const [selectedSkill, setSelectedSkill] = useState<Skill | null>(null);
  const [selectedFile, setSelectedFile] = useState<string>('');
  const [fileContent, setFileContent] = useState<string>('');
  const [originalContent, setOriginalContent] = useState<string>('');
  const [loadingFile, setLoadingFile] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm();

  const fetchSkills = useCallback(async () => {
    setLoadingList(true);
    try {
      const { data } = await api.get('/skills');
      setSkills(data || []);
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    fetchSkills();
  }, [fetchSkills]);

  const selectSkill = useCallback(async (skill: Skill) => {
    setSelectedSkill(skill);
    // Pick the first file by default
    const firstFile = skill.files?.[0] || 'SKILL.md';
    setSelectedFile(firstFile);
    await loadFile(skill.name, firstFile);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const loadFile = async (skillName: string, filename: string) => {
    setLoadingFile(true);
    try {
      const { data } = await api.get(`/skills/${skillName}/files/${filename}`);
      const content = data?.content ?? '';
      setFileContent(content);
      setOriginalContent(content);
    } catch {
      setFileContent('');
      setOriginalContent('');
    } finally {
      setLoadingFile(false);
    }
  };

  const handleTabChange = async (filename: string) => {
    if (!selectedSkill) return;
    setSelectedFile(filename);
    await loadFile(selectedSkill.name, filename);
  };

  const handleSave = async () => {
    if (!selectedSkill || !selectedFile) return;
    setSaving(true);
    try {
      await api.put(`/skills/${selectedSkill.name}/files/${selectedFile}`, { content: fileContent });
      setOriginalContent(fileContent);
      message.success('已保存');
    } catch {
      message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async () => {
    try {
      const values = await form.validateFields();
      await api.post('/skills', values);
      message.success('已创建');
      setCreateOpen(false);
      form.resetFields();
      await fetchSkills();
    } catch (err) {
      if ((err as { name?: string }).name !== 'Error') return;
      message.error('创建失败');
    }
  };

  const handleDelete = async (name: string) => {
    await api.delete(`/skills/${name}`);
    message.success('已删除');
    if (selectedSkill?.name === name) {
      setSelectedSkill(null);
      setSelectedFile('');
      setFileContent('');
      setOriginalContent('');
    }
    await fetchSkills();
  };

  const isDirty = fileContent !== originalContent;

  const allFiles = selectedSkill?.files ?? [];

  return (
    <div className="page-container">
      <div className="page-header" style={{ marginBottom: 24 }}>
        <div>
          <h1 className="page-title">技能管理</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>
            管理 Agent 技能定义文件（SKILL.md + 脚本）
          </Text>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)} size="large">
          新建技能
        </Button>
      </div>

      <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 180px)', overflow: 'hidden' }}>
        {/* Left panel: skill list */}
        <Card
          style={{ width: 280, flexShrink: 0, overflow: 'auto', height: '100%' }}
          bodyStyle={{ padding: 0 }}
          title={<Text strong>技能列表</Text>}
        >
          <List
            loading={loadingList}
            dataSource={skills}
            renderItem={(skill) => (
              <List.Item
                style={{
                  padding: '10px 16px',
                  cursor: 'pointer',
                  background: selectedSkill?.name === skill.name ? '#f0f7ff' : undefined,
                  borderLeft: selectedSkill?.name === skill.name ? '3px solid #2563eb' : '3px solid transparent',
                }}
                onClick={() => selectSkill(skill)}
                actions={[
                  <Popconfirm
                    key="delete"
                    title={`确认删除技能 "${skill.name}"?`}
                    onConfirm={(e) => {
                      e?.stopPropagation();
                      handleDelete(skill.name);
                    }}
                    onCancel={(e) => e?.stopPropagation()}
                  >
                    <Button
                      type="text"
                      danger
                      size="small"
                      icon={<DeleteOutlined />}
                      onClick={(e) => e.stopPropagation()}
                    />
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={<Text strong style={{ fontSize: 13 }}>{skill.name}</Text>}
                  description={
                    <Text type="secondary" style={{ fontSize: 12 }} ellipsis>
                      {skill.description || '暂无描述'}
                    </Text>
                  }
                />
              </List.Item>
            )}
          />
        </Card>

        {/* Right panel: file editor */}
        <Card
          style={{ flex: 1, overflow: 'hidden', height: '100%', display: 'flex', flexDirection: 'column' }}
          bodyStyle={{ flex: 1, display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}
          title={
            selectedSkill ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {allFiles.map((f) => (
                  <Tag
                    key={f}
                    color={f === selectedFile ? 'blue' : 'default'}
                    style={{ cursor: 'pointer', fontFamily: 'var(--font-mono)', fontSize: 12 }}
                    onClick={() => handleTabChange(f)}
                  >
                    {f}
                  </Tag>
                ))}
                {isDirty && <Tag color="orange">未保存</Tag>}
              </div>
            ) : (
              <Text type="secondary">选择左侧技能查看文件</Text>
            )
          }
          extra={
            selectedSkill && selectedFile ? (
              <Button
                type="primary"
                icon={<SaveOutlined />}
                onClick={handleSave}
                loading={saving}
                disabled={!isDirty}
              >
                保存
              </Button>
            ) : null
          }
        >
          {selectedSkill && selectedFile ? (
            <div style={{ flex: 1, overflow: 'hidden', height: '100%' }}>
              {loadingFile ? (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
                  <Text type="secondary">加载中...</Text>
                </div>
              ) : (
                <Editor
                  height="100%"
                  language={languageForFile(selectedFile)}
                  value={fileContent}
                  onChange={(val) => setFileContent(val ?? '')}
                  options={{
                    minimap: { enabled: false },
                    wordWrap: 'on',
                    fontSize: 14,
                    lineNumbers: 'on',
                    scrollBeyondLastLine: false,
                    automaticLayout: true,
                  }}
                />
              )}
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
              <Text type="secondary">请在左侧选择一个技能</Text>
            </div>
          )}
        </Card>
      </div>

      {/* Create skill modal */}
      <Modal
        title={<div style={{ fontSize: 18, fontWeight: 600, color: '#0f172a' }}>新建技能</div>}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreate}
        okText="创建"
        cancelText="取消"
        width={520}
        styles={{ content: { borderRadius: 16, padding: 24 } }}
      >
        <Form layout="vertical" form={form} style={{ marginTop: 24 }}>
          <Form.Item
            name="name"
            label={<Text strong>技能名称</Text>}
            rules={[{ required: true, message: '请输入技能名称' }, { pattern: /^[a-z0-9_-]+$/, message: '只允许小写字母、数字、下划线和连字符' }]}
            tooltip="唯一标识符，如 web-search"
          >
            <Input placeholder="例如: web-search" size="large" />
          </Form.Item>
          <Form.Item
            name="description"
            label={<Text strong>描述</Text>}
          >
            <Input.TextArea rows={3} placeholder="技能的功能说明" style={{ background: '#f8fafc', borderColor: '#e2e8f0' }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
