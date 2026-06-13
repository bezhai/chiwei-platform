import { useEffect, useState, useCallback, useMemo, type Key } from 'react';
import {
  Button,
  Empty,
  Popconfirm,
  Spin,
  Tag,
  Tooltip,
  Tree,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  SaveOutlined,
  FileMarkdownOutlined,
  PythonOutlined,
  FileOutlined,
  FolderOutlined,
  FolderOpenOutlined,
  CodeOutlined,
  ReloadOutlined,
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

const WORKSPACE_KEY = 'workspace:skills';

function languageForFile(filename: string): string {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.md')) return 'markdown';
  if (lower.endsWith('.py')) return 'python';
  if (lower.endsWith('.sh')) return 'shell';
  if (lower.endsWith('.json')) return 'json';
  if (lower.endsWith('.ts') || lower.endsWith('.tsx')) return 'typescript';
  if (lower.endsWith('.js') || lower.endsWith('.jsx')) return 'javascript';
  if (lower.endsWith('.yaml') || lower.endsWith('.yml')) return 'yaml';
  return 'plaintext';
}

function fileIcon(filename: string) {
  if (filename.endsWith('.md')) return <FileMarkdownOutlined />;
  if (filename.endsWith('.py')) return <PythonOutlined />;
  return <FileOutlined />;
}

function skillNodeKey(skillName: string) {
  return `skill:${skillName}`;
}

function dirNodeKey(skillName: string, dirPath: string) {
  return `dir:${skillName}:${dirPath}`;
}

function fileNodeKey(skillName: string, filePath: string) {
  return `file:${skillName}:${filePath}`;
}

function parseFileNodeKey(key: string): { skillName: string; filePath: string } | null {
  const parts = key.split(':');
  if (parts[0] !== 'file' || parts.length < 3) return null;
  return {
    skillName: parts[1],
    filePath: parts.slice(2).join(':'),
  };
}

function parentDirectoryKeys(skillName: string, filePath: string): Key[] {
  const parts = filePath.split('/');
  const keys: Key[] = [WORKSPACE_KEY, skillNodeKey(skillName)];
  for (let index = 0; index < parts.length - 1; index++) {
    keys.push(dirNodeKey(skillName, parts.slice(0, index + 1).join('/')));
  }
  return keys;
}

function sortTreeNodes(nodes: TreeDataNode[]) {
  nodes.sort((a, b) => {
    const aIsFile = a.isLeaf === true;
    const bIsFile = b.isLeaf === true;
    if (aIsFile !== bIsFile) return aIsFile ? 1 : -1;
    return String(a.title).localeCompare(String(b.title));
  });

  for (const node of nodes) {
    if (node.children) sortTreeNodes(node.children);
  }
}

function buildSkillFileTree(skill: Skill): TreeDataNode[] {
  const root: TreeDataNode[] = [];
  const dirMap = new Map<string, TreeDataNode>();

  for (const filePath of skill.files || []) {
    const parts = filePath.split('/');
    let parentChildren = root;

    for (let index = 0; index < parts.length - 1; index++) {
      const dirPath = parts.slice(0, index + 1).join('/');
      const key = dirNodeKey(skill.name, dirPath);
      if (!dirMap.has(key)) {
        const dirNode: TreeDataNode = {
          key,
          title: parts[index],
          icon: <FolderOutlined />,
          children: [],
          selectable: false,
        };
        parentChildren.push(dirNode);
        dirMap.set(key, dirNode);
      }
      parentChildren = dirMap.get(key)!.children as TreeDataNode[];
    }

    const fileName = parts[parts.length - 1];
    parentChildren.push({
      key: fileNodeKey(skill.name, filePath),
      title: fileName,
      icon: fileIcon(fileName),
      isLeaf: true,
    });
  }

  sortTreeNodes(root);
  return root;
}

function skillDirectoryTitle(skill: Skill) {
  return (
    <span className="skill-editor-skill-title">
      <span className="skill-editor-skill-name">{skill.name}</span>
      <span className="skill-editor-file-count">{skill.files?.length || 0}</span>
    </span>
  );
}

function buildWorkspaceTree(skills: Skill[]): TreeDataNode[] {
  return [
    {
      key: WORKSPACE_KEY,
      title: 'skills',
      icon: <FolderOpenOutlined />,
      selectable: false,
      children: skills.map((skill) => ({
        key: skillNodeKey(skill.name),
        title: skillDirectoryTitle(skill),
        icon: <FolderOutlined />,
        selectable: false,
        children: buildSkillFileTree(skill),
      })),
    },
  ];
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
  const [expandedKeys, setExpandedKeys] = useState<Key[]>([WORKSPACE_KEY]);

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
    const skill = skills.find((item) => item.name === skillName) || null;
    setSelectedSkill(skill);
    setSelectedFile(filename);
    setExpandedKeys((current) => Array.from(new Set([...current, ...parentDirectoryKeys(skillName, filename)])));
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
  }, [skills]);

  useEffect(() => {
    if (selectedSkill || !skills.length) return;
    const firstSkill = skills[0];
    const firstFile = firstSkill.files?.find((file) => file === 'SKILL.md') || firstSkill.files?.[0];
    if (firstFile) {
      loadFile(firstSkill.name, firstFile);
    }
  }, [skills, selectedSkill, loadFile]);

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

  const handleTreeSelect = (keys: Key[]) => {
    const key = keys[0];
    if (!key) return;
    const fileNode = parseFileNodeKey(String(key));
    if (fileNode) {
      loadFile(fileNode.skillName, fileNode.filePath);
    }
  };

  const workspaceTree = useMemo(() => buildWorkspaceTree(skills), [skills]);
  const isDirty = fileContent !== originalContent;
  const selectedTreeKey = selectedSkill && selectedFile ? fileNodeKey(selectedSkill.name, selectedFile) : undefined;
  const currentPath = selectedSkill && selectedFile ? `skills/${selectedSkill.name}/${selectedFile}` : 'skills/';
  const currentLanguage = selectedFile ? languageForFile(selectedFile) : 'plaintext';

  return (
    <div className="page-container skills-page">
      <div className="page-header skill-editor-page-header">
        <div>
          <h1 className="page-title">技能管理</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>
            workspace / skills
          </Text>
        </div>
      </div>

      <div className="skill-editor-shell">
        <aside className="skill-editor-activitybar">
          <Tooltip title="Explorer" placement="right">
            <div className="skill-editor-activity active">
              <FolderOpenOutlined />
            </div>
          </Tooltip>
        </aside>

        <aside className="skill-editor-explorer">
          <div className="skill-editor-panel-title">
            <span>EXPLORER</span>
            <div className="skill-editor-panel-actions">
              <Tooltip title="刷新">
                <Button type="text" size="small" icon={<ReloadOutlined />} onClick={fetchSkills} loading={loadingList} />
              </Tooltip>
            </div>
          </div>

          <div className="skill-editor-workspace-row">
            <span>workspace</span>
            <Tag bordered={false}>{skills.length}</Tag>
          </div>

          <Spin spinning={loadingList}>
            <Tree
              className="skill-editor-tree"
              showIcon
              blockNode
              treeData={workspaceTree}
              expandedKeys={expandedKeys}
              selectedKeys={selectedTreeKey ? [selectedTreeKey] : []}
              onExpand={(keys) => setExpandedKeys(keys)}
              onSelect={handleTreeSelect}
            />
          </Spin>
        </aside>

        <main className="skill-editor-main">
          <div className="skill-editor-tabbar">
            {selectedSkill && selectedFile ? (
              <div className={`skill-editor-tab ${isDirty ? 'dirty' : ''}`}>
                {fileIcon(selectedFile)}
                <span>{selectedFile.split('/').pop()}</span>
                {isDirty && <span className="skill-editor-dirty-dot" />}
              </div>
            ) : (
              <div className="skill-editor-tab muted">untitled</div>
            )}

            <div className="skill-editor-save-area">
              {selectedSkill && selectedFile && (
                <>
                  {isDirty && <Tag color="warning">未保存</Tag>}
                  <Button
                    type="primary"
                    icon={<SaveOutlined />}
                    onClick={handleSave}
                    loading={saving}
                    disabled={!isDirty}
                  >
                    保存
                  </Button>
                </>
              )}
            </div>
          </div>

          <div className="skill-editor-pathbar">
            <CodeOutlined />
            <span>{currentPath}</span>
          </div>

          <div className="skill-editor-canvas">
            {selectedSkill && selectedFile ? (
              loadingFile ? (
                <div className="skill-editor-empty">
                  <Spin />
                  <Text type="secondary">Loading file</Text>
                </div>
              ) : (
                <Editor
                  height="100%"
                  language={currentLanguage}
                  value={fileContent}
                  onChange={(value) => setFileContent(value ?? '')}
                  options={{
                    minimap: { enabled: false },
                    wordWrap: 'on',
                    fontSize: 13,
                    lineNumbers: 'on',
                    scrollBeyondLastLine: false,
                    automaticLayout: true,
                    fontFamily: 'IBM Plex Mono, JetBrains Mono, Menlo, Monaco, monospace',
                    lineHeight: 21,
                    padding: { top: 14, bottom: 14 },
                    renderLineHighlight: 'line',
                  }}
                />
              )
            ) : (
              <div className="skill-editor-empty">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有打开的文件" />
              </div>
            )}
          </div>

          <div className="skill-editor-statusbar">
            <span>{currentLanguage}</span>
            <span>{selectedFile || 'no file selected'}</span>
            <span>{isDirty ? 'modified' : 'saved'}</span>
          </div>
        </main>

        <aside className="skill-editor-inspector">
          <div className="skill-editor-panel-title">
            <span>DETAILS</span>
          </div>
          {selectedSkill ? (
            <div className="skill-editor-details">
              <div>
                <span className="skill-editor-detail-label">name</span>
                <strong>{selectedSkill.name}</strong>
              </div>
              <div>
                <span className="skill-editor-detail-label">description</span>
                <p>{selectedSkill.description || '暂无描述'}</p>
              </div>
              <div className="skill-editor-detail-grid">
                <div>
                  <span className="skill-editor-detail-label">files</span>
                  <strong>{selectedSkill.files?.length || 0}</strong>
                </div>
                <div>
                  <span className="skill-editor-detail-label">open</span>
                  <strong>{selectedFile ? selectedFile.split('/').pop() : '-'}</strong>
                </div>
              </div>
              <Popconfirm
                title={`确认删除 "${selectedSkill.name}"?`}
                onConfirm={() => handleDelete(selectedSkill.name)}
              >
                <Button danger icon={<DeleteOutlined />}>
                  删除技能
                </Button>
              </Popconfirm>
            </div>
          ) : (
            <div className="skill-editor-details muted">No skill selected</div>
          )}
        </aside>
      </div>
    </div>
  );
}
