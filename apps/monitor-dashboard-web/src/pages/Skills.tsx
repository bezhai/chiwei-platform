import { useEffect, useState, useCallback } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
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
  FolderOpenOutlined,
  CloseOutlined,
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

interface OpenTab {
  key: string;
  skillName: string;
  filePath: string;
}

function languageForFile(filename: string): string {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.md')) return 'markdown';
  if (lower.endsWith('.py')) return 'python';
  if (lower.endsWith('.sh')) return 'shell';
  if (lower.endsWith('.json')) return 'json';
  if (lower.endsWith('.ts')) return 'typescript';
  if (lower.endsWith('.tsx')) return 'typescript';
  return 'plaintext';
}

function fileIcon(filename: string) {
  if (filename.endsWith('.md')) return <FileMarkdownOutlined />;
  if (filename.endsWith('.py')) return <PythonOutlined />;
  return <FileOutlined />;
}

function fileBasename(filePath: string): string {
  const parts = filePath.split('/');
  return parts[parts.length - 1] || filePath;
}

function fileTabKey(skillName: string, filePath: string): string {
  return `file::${skillName}::${filePath}`;
}

function parseFileNodeKey(nodeKey: string): { skillName: string; filePath: string } | null {
  const match = nodeKey.match(/^file::([^:]+)::(.+)$/);
  if (!match) return null;
  return { skillName: match[1], filePath: match[2] };
}

function buildFileTree(skillName: string, files: string[] | undefined): TreeDataNode[] {
  if (!files?.length) return [];

  const root: TreeDataNode[] = [];
  const dirMap = new Map<string, TreeDataNode>();

  const sorted = [...files].sort((a, b) => {
    const aDepth = a.split('/').length;
    const bDepth = b.split('/').length;
    if (aDepth !== bDepth) return aDepth - bDepth;
    return a.localeCompare(b);
  });

  for (const filePath of sorted) {
    const parts = filePath.split('/');
    let parentChildren = root;

    for (let i = 0; i < parts.length - 1; i++) {
      const dirKey = parts.slice(0, i + 1).join('/');
      if (!dirMap.has(dirKey)) {
        const dirNode: TreeDataNode = {
          key: `dir::${skillName}::${dirKey}`,
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

    const fileName = parts[parts.length - 1];
    parentChildren.push({
      key: fileTabKey(skillName, filePath),
      title: fileName,
      icon: fileIcon(fileName),
      isLeaf: true,
    });
  }

  return root;
}

function buildExplorerTree(skills: Skill[], selectedSkillName?: string | null): TreeDataNode[] {
  return [...skills]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((skill) => ({
      key: `skill::${skill.name}`,
      title: skill.name,
      icon: selectedSkillName === skill.name ? <FolderOpenOutlined /> : <FolderOutlined />,
      children: buildFileTree(skill.name, skill.files),
    }));
}

export default function Skills() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loadingList, setLoadingList] = useState(false);
  const [selectedSkill, setSelectedSkill] = useState<Skill | null>(null);
  const [selectedFile, setSelectedFile] = useState('');
  const [fileContent, setFileContent] = useState('');
  const [originalContent, setOriginalContent] = useState('');
  const [loadingFile, setLoadingFile] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [openTabs, setOpenTabs] = useState<OpenTab[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [form] = Form.useForm();

  const fetchSkills = useCallback(async () => {
    setLoadingList(true);
    try {
      const { data } = await api.get('/skills');
      const nextSkills = data || [];
      setSkills(nextSkills);
      setExpandedKeys(
        nextSkills.flatMap((skill: Skill) => [
          `skill::${skill.name}`,
          ...skill.files
            .filter((file) => file.includes('/'))
            .map((file) => `dir::${skill.name}::${file.split('/').slice(0, -1).join('/')}`),
        ]),
      );
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    fetchSkills();
  }, [fetchSkills]);

  const openFile = useCallback(async (skillName: string, filePath: string) => {
    setLoadingFile(true);
    try {
      const { data } = await api.get(`/skills/${skillName}/files/${filePath}`);
      const content = data?.content ?? '';
      const skill = skills.find((item) => item.name === skillName) || null;
      setSelectedSkill(skill);
      setSelectedFile(filePath);
      setFileContent(content);
      setOriginalContent(content);
      setOpenTabs((current) => {
        const key = fileTabKey(skillName, filePath);
        if (current.some((tab) => tab.key === key)) return current;
        return [...current, { key, skillName, filePath }];
      });
    } catch {
      setFileContent('');
      setOriginalContent('');
    } finally {
      setLoadingFile(false);
    }
  }, [skills]);

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

    setOpenTabs((current) => current.filter((tab) => tab.skillName !== name));
    await fetchSkills();
  };

  const handleExplorerSelect = (keys: React.Key[]) => {
    const key = String(keys[0] || '');
    if (!key) return;

    if (key.startsWith('skill::')) {
      const skillName = key.slice('skill::'.length);
      const skill = skills.find((item) => item.name === skillName) || null;
      setSelectedSkill(skill);
      return;
    }

    if (key.startsWith('file::')) {
      const node = parseFileNodeKey(key);
      if (node) openFile(node.skillName, node.filePath);
    }
  };

  const handleTabSelect = async (tab: OpenTab) => {
    await openFile(tab.skillName, tab.filePath);
  };

  const closeTab = async (tabKey: string) => {
    const nextTabs = openTabs.filter((tab) => tab.key !== tabKey);
    setOpenTabs(nextTabs);

    if (tabKey !== fileTabKey(selectedSkill?.name || '', selectedFile)) {
      return;
    }

    const fallback = nextTabs[nextTabs.length - 1];
    if (fallback) {
      await openFile(fallback.skillName, fallback.filePath);
      return;
    }

    setSelectedFile('');
    setFileContent('');
    setOriginalContent('');
  };

  const activeTabKey = selectedSkill && selectedFile ? fileTabKey(selectedSkill.name, selectedFile) : '';
  const explorerTree = buildExplorerTree(skills, selectedSkill?.name);
  const isDirty = fileContent !== originalContent;

  return (
    <div className="page-container skills-page skills-editor-page">
      <div className="skills-ide-shell">
        <div className="skills-activity-bar">
          <div className="skills-activity-logo">S</div>
          <div className="skills-activity-item is-active">
            <FolderOutlined />
          </div>
        </div>

        <aside className="skills-sidebar">
          <div className="skills-sidebar-header">
            <div>
              <div className="skills-sidebar-title">EXPLORER</div>
              <div className="skills-sidebar-subtitle">skills</div>
            </div>
            <Button
              type="text"
              icon={<PlusOutlined />}
              onClick={() => setCreateOpen(true)}
              size="small"
            />
          </div>

          <div className="skills-sidebar-section">
            <div className="skills-sidebar-section-title">
              <span>OPEN EDITORS</span>
              <Text type="secondary">{openTabs.length}</Text>
            </div>

            {openTabs.length > 0 ? (
              <div className="skills-open-editors">
                {openTabs.map((tab) => {
                  const isActive = tab.key === activeTabKey;
                  const isActiveDirty = isActive && isDirty;
                  return (
                    <button
                      key={tab.key}
                      type="button"
                      className={`skills-open-editor${isActive ? ' is-active' : ''}`}
                      onClick={() => handleTabSelect(tab)}
                    >
                      <span className="skills-open-editor-icon">{fileIcon(tab.filePath)}</span>
                      <span className="skills-open-editor-label">{fileBasename(tab.filePath)}</span>
                      <span className="skills-open-editor-skill">{tab.skillName}</span>
                      <span
                        className={`skills-open-editor-close${isActiveDirty ? ' is-dirty' : ''}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          void closeTab(tab.key);
                        }}
                      >
                        {isActiveDirty ? '●' : <CloseOutlined />}
                      </span>
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="skills-open-editors-empty">No open editors</div>
            )}
          </div>

          <div className="skills-sidebar-section">
            <div className="skills-sidebar-section-title">
              <span>SKILLS</span>
              <Text type="secondary">{skills.length}</Text>
            </div>

            {loadingList ? (
              <div className="skills-empty-panel">
                <Text type="secondary">技能列表加载中...</Text>
              </div>
            ) : (
              <Tree
                className="skills-explorer-tree"
                showIcon
                blockNode
                expandedKeys={expandedKeys}
                onExpand={(keys) => setExpandedKeys(keys.map(String))}
                treeData={explorerTree}
                selectedKeys={
                  activeTabKey
                    ? [activeTabKey]
                    : selectedSkill
                      ? [`skill::${selectedSkill.name}`]
                      : []
                }
                onSelect={handleExplorerSelect}
                titleRender={(node) => {
                  const nodeKey = String(node.key);
                  if (!nodeKey.startsWith('skill::')) return node.title;

                  const skillName = nodeKey.slice('skill::'.length);
                  const skill = skills.find((item) => item.name === skillName);
                  if (!skill) return node.title;

                  return (
                    <div className="skills-explorer-item">
                      <span className="skills-explorer-item-name">{skill.name}</span>
                      <div className="skills-explorer-item-tail">
                        <span className="skills-explorer-item-meta">{skill.files.length}</span>
                        <Popconfirm
                          title={`确认删除 "${skill.name}"?`}
                          onConfirm={(e) => {
                            e?.stopPropagation();
                            handleDelete(skill.name);
                          }}
                          onCancel={(e) => e?.stopPropagation()}
                        >
                          <button
                            type="button"
                            className="skills-explorer-delete"
                            onClick={(e) => e.stopPropagation()}
                            aria-label={`删除 ${skill.name}`}
                          >
                            <DeleteOutlined />
                          </button>
                        </Popconfirm>
                      </div>
                    </div>
                  );
                }}
              />
            )}
          </div>
        </aside>

        <section className="skills-editor-panel">
          <div className="skills-editor-topbar">
            <div className="skills-editor-breadcrumb">
              <span>WORKSPACE</span>
              {selectedSkill ? (
                <>
                  <span>/</span>
                  <span>{selectedSkill.name}</span>
                </>
              ) : null}
              {selectedFile ? (
                <>
                  <span>/</span>
                  <span>{selectedFile}</span>
                </>
              ) : null}
            </div>
            <div className="skills-editor-toolbar">
              <Text type="secondary" className="skills-language-indicator">
                {selectedFile ? languageForFile(selectedFile) : 'workspace'}
              </Text>
              <Button
                type="text"
                size="small"
                icon={<SaveOutlined />}
                onClick={handleSave}
                loading={saving}
                disabled={!selectedFile || !isDirty}
              >
                Save
              </Button>
            </div>
          </div>

          <div className="skills-tabs-bar">
            {openTabs.length > 0 ? (
              openTabs.map((tab) => {
                const isActive = tab.key === activeTabKey;
                return (
                  <button
                    key={tab.key}
                    type="button"
                    className={`skills-tab${isActive ? ' is-active' : ''}`}
                    onClick={() => handleTabSelect(tab)}
                  >
                    <span className="skills-tab-icon">{fileIcon(tab.filePath)}</span>
                    <span className="skills-tab-label">{fileBasename(tab.filePath)}</span>
                    <span
                      className="skills-tab-close"
                      onClick={(e) => {
                        e.stopPropagation();
                        void closeTab(tab.key);
                      }}
                    >
                      <CloseOutlined />
                    </span>
                  </button>
                );
              })
            ) : (
              <div className="skills-tab-placeholder">打开一个 skill 文件开始编辑</div>
            )}
          </div>

          <div className="skills-editor-content">
            {selectedSkill && selectedFile ? (
              loadingFile ? (
                <div className="skills-empty-editor">
                  <Text type="secondary">加载中...</Text>
                </div>
              ) : (
                <Editor
                  height="100%"
                  theme="vs-dark"
                  language={languageForFile(selectedFile)}
                  value={fileContent}
                  onChange={(val) => setFileContent(val ?? '')}
                  options={{
                    minimap: { enabled: false },
                    wordWrap: 'on',
                    fontSize: 14,
                    fontFamily: 'JetBrains Mono, Menlo, Monaco, Courier New, monospace',
                    lineNumbers: 'on',
                    lineNumbersMinChars: 3,
                    scrollBeyondLastLine: false,
                    automaticLayout: true,
                    padding: { top: 16 },
                  }}
                />
              )
            ) : (
              <div className="skills-empty-editor">
                <Text type="secondary">Select a file from the explorer to start editing.</Text>
              </div>
            )}
          </div>

          <div className="skills-status-bar">
            <span>{selectedSkill ? `skill ${selectedSkill.name}` : 'workspace'}</span>
            <span>{selectedFile ? fileBasename(selectedFile) : 'no file'}</span>
            <span>{selectedFile ? languageForFile(selectedFile) : 'plaintext'}</span>
            <span>{isDirty ? 'unsaved' : 'saved'}</span>
          </div>
        </section>
      </div>

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
