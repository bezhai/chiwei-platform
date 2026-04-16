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
  Tree,
  Typography,
  message,
} from 'antd';
import {
  PlusOutlined,
  DeleteOutlined,
  SaveOutlined,
  FileMarkdownOutlined,
  PythonOutlined,
  FileOutlined,
  FolderOutlined,
} from '@ant-design/icons';
import type { TreeDataNode } from 'antd';
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

function fileIcon(filename: string) {
  if (filename.endsWith('.md')) return <FileMarkdownOutlined />;
  if (filename.endsWith('.py')) return <PythonOutlined />;
  return <FileOutlined />;
}

/** 将扁平文件路径列表构建成 Ant Design Tree 数据 */
function buildTreeData(files: string[]): TreeDataNode[] {
  const root: TreeDataNode[] = [];
  const dirMap = new Map<string, TreeDataNode>();

  const sorted = [...files].sort((a, b) => {
    // 文件夹排前面，同级按字母序
    const aDepth = a.split('/').length;
    const bDepth = b.split('/').length;
    if (aDepth !== bDepth) return aDepth - bDepth;
    return a.localeCompare(b);
  });

  for (const filePath of sorted) {
    const parts = filePath.split('/');
    let parentChildren = root;

    // 逐层创建目录节点
    for (let i = 0; i < parts.length - 1; i++) {
      const dirKey = parts.slice(0, i + 1).join('/');
      if (!dirMap.has(dirKey)) {
        const dirNode: TreeDataNode = {
          key: dirKey,
          title: parts[i],
          icon: <FolderOutlined />,
          children: [],
          selectable: false,
        };
        parentChildren.push(dirNode);
        dirMap.set(dirKey, dirNode);
      }
      parentChildren = dirMap.get(dirKey)!.children as TreeDataNode[];
    }

    // 叶子节点（文件）
    const fileName = parts[parts.length - 1];
    parentChildren.push({
      key: filePath,
      title: fileName,
      icon: fileIcon(fileName),
      isLeaf: true,
    });
  }

  return root;
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

  const loadFile = useCallback(async (skillName: string, filename: string) => {
    setLoadingFile(true);
    try {
      const { data } = await api.get(`/skills/${skillName}/files/${filename}`);
      const content = data?.content ?? '';
      setFileContent(content);
      setOriginalContent(content);
      setSelectedFile(filename);
    } catch {
      setFileContent('');
      setOriginalContent('');
    } finally {
      setLoadingFile(false);
    }
  }, []);

  const selectSkill = useCallback(async (skill: Skill) => {
    setSelectedSkill(skill);
    const firstFile = skill.files?.find(f => f === 'SKILL.md') || skill.files?.[0] || '';
    if (firstFile) {
      await loadFile(skill.name, firstFile);
    }
  }, [loadFile]);

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
  const treeData = selectedSkill ? buildTreeData(selectedSkill.files) : [];

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

      <div style={{ display: 'flex', gap: 12, height: 'calc(100vh - 180px)', overflow: 'hidden' }}>
        {/* Left: skill list */}
        <Card
          style={{ width: 240, flexShrink: 0, overflow: 'auto', height: '100%' }}
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
                    title={`确认删除 "${skill.name}"?`}
                    onConfirm={(e) => { e?.stopPropagation(); handleDelete(skill.name); }}
                    onCancel={(e) => e?.stopPropagation()}
                  >
                    <Button
                      type="text" danger size="small"
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

        {/* Middle: file tree */}
        {selectedSkill && (
          <Card
            style={{ width: 200, flexShrink: 0, overflow: 'auto', height: '100%' }}
            bodyStyle={{ padding: '8px 4px' }}
            title={<Text strong style={{ fontSize: 13 }}>{selectedSkill.name}</Text>}
          >
            <Tree
              showIcon
              defaultExpandAll
              treeData={treeData}
              selectedKeys={selectedFile ? [selectedFile] : []}
              onSelect={(keys) => {
                const key = keys[0] as string;
                if (key && selectedSkill) {
                  loadFile(selectedSkill.name, key);
                }
              }}
              style={{ fontSize: 13 }}
            />
          </Card>
        )}

        {/* Right: editor */}
        <Card
          style={{ flex: 1, overflow: 'hidden', height: '100%', display: 'flex', flexDirection: 'column' }}
          bodyStyle={{ flex: 1, display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}
          title={
            selectedFile ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Text code style={{ fontSize: 13 }}>{selectedFile}</Text>
                {isDirty && <Tag color="orange">未保存</Tag>}
              </div>
            ) : (
              <Text type="secondary">选择文件开始编辑</Text>
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
              <Text type="secondary">请在左侧选择技能，然后点击文件编辑</Text>
            </div>
          )}
        </Card>
      </div>

      {/* Create modal */}
      <Modal
        title="新建技能"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreate}
        okText="创建"
        cancelText="取消"
      >
        <Form layout="vertical" form={form} style={{ marginTop: 16 }}>
          <Form.Item
            name="name"
            label="技能名称"
            rules={[
              { required: true, message: '请输入技能名称' },
              { pattern: /^[a-z0-9_-]+$/, message: '只允许小写字母、数字、下划线和连字符' },
            ]}
          >
            <Input placeholder="例如: web_search" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} placeholder="技能的功能说明" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
