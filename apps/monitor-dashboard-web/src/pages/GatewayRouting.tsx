import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Dropdown,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  AimOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  MoreOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RetweetOutlined,
  RollbackOutlined,
  SafetyOutlined,
  SaveOutlined,
  SwapOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import { api, getLane } from '../api/client';

const { Text, Title } = Typography;

interface GatewayTarget {
  service: string;
  lane: string;
  port: number;
  weight: number;
  strip_prefix?: string;
  rewrite_prefix?: string;
}

interface GatewayMatch {
  path_prefix: string;
  request_lane?: string;
  method?: string;
  headers?: Record<string, string>;
  query?: Record<string, string>;
  cookies?: Record<string, string>;
}

interface GatewayRule {
  name: string;
  enabled: boolean;
  priority: number;
  path_prefix: string;
  request_lane?: string;
  match: GatewayMatch;
  targets: GatewayTarget[];
  split_key_headers?: string[];
  created_at: string;
  updated_at: string;
  version: number;
  snapshot_version?: number;
}

interface GatewaySnapshot {
  version: number;
  updated_at: string;
  rules: GatewayRule[];
}

interface GatewayRuleSnapshot {
  snapshot_version: number;
  rules: GatewayRule[];
  created_by: string;
  reason: string;
  created_at: string;
}

interface GatewayExplainTarget {
  service: string;
  lane: string;
  port: number;
  weight: number;
  effective_lane: string;
}

interface GatewayRuleExplain {
  name: string;
  priority: number;
  path_prefix: string;
  request_lane?: string;
  enabled: boolean;
  status: string;
  reason: string;
}

interface GatewayExplainResult {
  path: string;
  request_lane?: string;
  matched: boolean;
  winning_rule?: string;
  winning_reason?: string;
  would_forward: boolean;
  would_redirect: boolean;
  stable_split: boolean;
  split_key_headers?: string[];
  candidate_targets?: GatewayExplainTarget[];
  effective_lane_note?: string;
  rules: GatewayRuleExplain[];
}

interface RuleFormValues {
  name: string;
  enabled: boolean;
  priority: number;
  path_prefix: string;
  request_lane?: string;
  split_key_headers?: string[];
  targets: GatewayTarget[];
  reason: string;
}

interface WeightsFormValues {
  reason: string;
  weights: Array<{ weight: number }>;
}

interface PreviewFormValues {
  path: string;
  x_lane?: string;
}

const emptySnapshot: GatewaySnapshot = {
  version: 0,
  updated_at: '',
  rules: [],
};

const statusMeta: Record<string, { label: string; color: string }> = {
  winner: { label: '命中', color: 'green' },
  shadowed: { label: '被遮挡', color: 'gold' },
  disabled: { label: '已禁用', color: 'red' },
  request_lane_mismatch: { label: '泳道不匹配', color: 'blue' },
  path_prefix_mismatch: { label: '路径不匹配', color: 'default' },
};

function requestLaneLabel(lane?: string) {
  return lane ? `只匹配 ${lane}` : '不限制请求泳道';
}

function targetLaneLabel(lane?: string) {
  return lane ? `${lane} 泳道` : '跟随请求泳道（x-lane）';
}

function formatTime(value?: string) {
  if (!value || value.startsWith('0001-')) {
    return '-';
  }
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('MM-DD HH:mm:ss') : '-';
}

function fullTime(value?: string) {
  if (!value || value.startsWith('0001-')) {
    return '-';
  }
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm:ss') : '-';
}

function getErrorMessage(error: unknown) {
  const responseData = (error as { response?: { data?: { message?: string; error?: string } } })?.response?.data;
  return responseData?.message || responseData?.error || (error instanceof Error ? error.message : '操作失败');
}

function targetIdentity(target: Pick<GatewayTarget, 'service' | 'lane'>) {
  return `${target.service}（${targetLaneLabel(target.lane)}）`;
}

function targetSummary(targets: GatewayTarget[]) {
  if (targets.length === 0) {
    return '未配置转发目标';
  }
  return `转发：${targets.map((target) => `${target.service}（${targetLaneLabel(target.lane)}，${target.weight}%）`).join('；')}`;
}

function stableSplitLabel(rule: GatewayRule) {
  return rule.split_key_headers?.length ? `按 ${rule.split_key_headers.join(', ')} 固定分流` : '按流量比例随机分配';
}

function splitHelpLabel() {
  return (
    <Space size={4}>
      <span>稳定分流依据</span>
      <Tooltip title="可选。配置后，同一个请求来源会稳定落到同一个目标，常用于灰度；为空时按流量比例随机分配。">
        <QuestionCircleOutlined style={{ color: '#94a3b8' }} />
      </Tooltip>
    </Space>
  );
}

function pathRewriteHelpLabel() {
  return (
    <Space size={4}>
      <span>路径处理</span>
      <Tooltip title="转发前是否去掉某段路径前缀，或把路径改写成另一个前缀。多数规则不需要配置。">
        <QuestionCircleOutlined style={{ color: '#94a3b8' }} />
      </Tooltip>
    </Space>
  );
}

function formatPathRewrite(target: GatewayTarget) {
  const items = [];
  if (target.strip_prefix) {
    items.push(`去掉 ${target.strip_prefix}`);
  }
  if (target.rewrite_prefix) {
    items.push(`改写为 ${target.rewrite_prefix}`);
  }
  return items.length ? items.join('，') : '不改路径';
}

function explainRuleReason(record: GatewayRuleExplain) {
  switch (record.status) {
    case 'winner':
      return '按路径、泳道和优先级命中';
    case 'shadowed':
      return '也能匹配，但已有更靠前的规则命中';
    case 'disabled':
      return '规则已停用';
    case 'request_lane_mismatch':
      return '请求 x-lane 不符合该规则要求';
    case 'path_prefix_mismatch':
      return '路径前缀不匹配';
    default:
      return record.reason || '-';
  }
}

function explainTargetSummary(targets?: GatewayExplainTarget[]) {
  if (!targets?.length) {
    return '无候选目标';
  }
  return targets
    .map((target) => `${target.service}（${target.effective_lane || '默认泳道'}，${target.weight}%）`)
    .join('；');
}

function pathMatchesPrefix(path: string, prefix: string) {
  if (path.startsWith(prefix)) {
    return true;
  }
  return prefix.endsWith('/') && path === prefix.slice(0, -1);
}

function normalizeOptional(value?: string) {
  return value?.trim() || '';
}

export default function GatewayRouting() {
  const navigate = useNavigate();
  const [rules, setRules] = useState<GatewayRule[]>([]);
  const [snapshot, setSnapshot] = useState<GatewaySnapshot>(emptySnapshot);
  const [snapshots, setSnapshots] = useState<GatewayRuleSnapshot[]>([]);
  const [selectedRuleName, setSelectedRuleName] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string>();
  const [ruleModalOpen, setRuleModalOpen] = useState(false);
  const [weightsModalOpen, setWeightsModalOpen] = useState(false);
  const [editingRuleName, setEditingRuleName] = useState<string | null>(null);
  const [snapshotDrawer, setSnapshotDrawer] = useState<GatewayRuleSnapshot | null>(null);
  const [explainResult, setExplainResult] = useState<GatewayExplainResult | null>(null);
  const [previewProbe, setPreviewProbe] = useState<{ path: string; xLane: string } | null>(null);
  const [ruleForm] = Form.useForm<RuleFormValues>();
  const [weightsForm] = Form.useForm<WeightsFormValues>();
  const [previewForm] = Form.useForm<PreviewFormValues>();

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [rulesRes, snapshotRes, snapshotsRes] = await Promise.all([
        api.get<GatewayRule[]>('/ops/gateway-rules'),
        api.get<GatewaySnapshot>('/ops/gateway-rules/snapshot'),
        api.get<GatewayRuleSnapshot[]>('/ops/gateway-rules/snapshots', { params: { limit: 20 } }),
      ]);
      if (!Array.isArray(rulesRes.data)) {
        throw new Error('gateway-rules list response must be an array');
      }
      if (!Array.isArray(snapshotRes.data.rules)) {
        throw new Error('gateway-rules snapshot response must contain rules');
      }
      if (!Array.isArray(snapshotsRes.data)) {
        throw new Error('gateway-rules snapshots response must be an array');
      }
      setRules(rulesRes.data);
      setSnapshot(snapshotRes.data);
      setSnapshots(snapshotsRes.data);
      setSelectedRuleName((current) => {
        if (current && rulesRes.data.some((rule) => rule.name === current)) {
          return current;
        }
        return rulesRes.data[0]?.name;
      });
    } catch (error) {
      message.error(getErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const selectedRule = useMemo(() => {
    if (!selectedRuleName) {
      return rules[0] ?? null;
    }
    return rules.find((rule) => rule.name === selectedRuleName) ?? rules[0] ?? null;
  }, [rules, selectedRuleName]);

  const latestSnapshot = snapshots[0];
  const enabledCount = rules.filter((rule) => rule.enabled).length;
  const disabledCount = rules.length - enabledCount;

  const openAuditLogs = () => {
    const params = new URLSearchParams({ action: 'ops.gateway-rules' });
    const lane = getLane();
    if (lane) {
      params.set('x-lane', lane);
    }
    navigate(`/audit-logs?${params.toString()}`);
  };

  const withReason = (title: string, content: ReactNode, onConfirm: (reason: string) => Promise<void>) => {
    let reason = '';
    Modal.confirm({
      title,
      content: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {content}
          <Input.TextArea
            autoSize={{ minRows: 3, maxRows: 5 }}
            placeholder="填写本次操作原因"
            onChange={(event) => {
              reason = event.target.value;
            }}
          />
        </Space>
      ),
      okText: '确认执行',
      cancelText: '取消',
      width: 520,
      onOk: async () => {
        const trimmed = reason.trim();
        if (!trimmed) {
          message.error('操作原因必填');
          throw new Error('reason required');
        }
        await onConfirm(trimmed);
      },
    });
  };

  const runWrite = async (key: string, operation: () => Promise<void>, successText: string) => {
    setActionLoading(key);
    try {
      await operation();
      message.success(successText);
      await fetchData();
    } catch (error) {
      message.error(getErrorMessage(error));
      throw error;
    } finally {
      setActionLoading(undefined);
    }
  };

  const openCreateRule = () => {
    setEditingRuleName(null);
    ruleForm.resetFields();
    ruleForm.setFieldsValue({
      enabled: true,
      priority: 100,
      path_prefix: '/',
      request_lane: '',
      split_key_headers: [],
      targets: [{ service: '', lane: 'prod', port: 80, weight: 100 }],
      reason: '',
    });
    setRuleModalOpen(true);
  };

  const openEditRule = (rule: GatewayRule) => {
    setEditingRuleName(rule.name);
    ruleForm.setFieldsValue({
      name: rule.name,
      enabled: rule.enabled,
      priority: rule.priority,
      path_prefix: rule.path_prefix || rule.match?.path_prefix,
      request_lane: rule.request_lane || rule.match?.request_lane || '',
      split_key_headers: rule.split_key_headers || [],
      targets: rule.targets.map((target) => ({ ...target })),
      reason: '',
    });
    setRuleModalOpen(true);
  };

  const canCreateWithPreview = (values: RuleFormValues) => {
    if (editingRuleName) {
      return true;
    }
    if (!previewProbe) {
      return false;
    }
    const pathPrefix = values.path_prefix.trim();
    const requestLane = normalizeOptional(values.request_lane);
    return pathMatchesPrefix(previewProbe.path, pathPrefix) && (!requestLane || previewProbe.xLane === requestLane);
  };

  const saveRule = async () => {
    const values = await ruleForm.validateFields();
    const weightSum = values.targets.reduce((sum, target) => sum + Number(target.weight || 0), 0);
    if (weightSum !== 100) {
      message.error(`流量比例总和必须为 100%，当前为 ${weightSum}%`);
      return;
    }
    if (!canCreateWithPreview(values)) {
      message.error('新建规则前必须先在预览面板验证覆盖该匹配路径的请求');
      return;
    }

    const pathPrefix = values.path_prefix.trim();
    const requestLane = normalizeOptional(values.request_lane);
    const payload = {
      enabled: values.enabled,
      priority: Number(values.priority),
      path_prefix: pathPrefix,
      request_lane: requestLane,
      match: {
        path_prefix: pathPrefix,
        request_lane: requestLane,
      },
      split_key_headers: values.split_key_headers || [],
      targets: values.targets.map((target) => ({
        service: target.service.trim(),
        lane: normalizeOptional(target.lane),
        port: Number(target.port),
        weight: Number(target.weight),
        strip_prefix: normalizeOptional(target.strip_prefix),
        rewrite_prefix: normalizeOptional(target.rewrite_prefix),
      })),
      reason: values.reason.trim(),
    };

    await runWrite(
      `save-${values.name}`,
      async () => {
        await api.put(`/ops/gateway-rules/${encodeURIComponent(values.name)}`, payload);
      },
      editingRuleName ? '规则已更新' : '规则已创建',
    );
    setRuleModalOpen(false);
    setEditingRuleName(null);
  };

  const openWeights = (rule: GatewayRule) => {
    weightsForm.resetFields();
    weightsForm.setFieldsValue({
      reason: '',
      weights: rule.targets.map((target) => ({ weight: target.weight })),
    });
    setWeightsModalOpen(true);
  };

  const setRuleWeights = async (
    rule: GatewayRule,
    weights: Array<{ service: string; lane: string; weight: number }>,
    reason: string,
    successText: string,
  ) => {
    await runWrite(
      `weights-${rule.name}`,
      async () => {
        await api.post(`/ops/gateway-rules/${encodeURIComponent(rule.name)}:set-weights`, {
          reason,
          weights,
        });
      },
      successText,
    );
  };

  const saveWeights = async () => {
    if (!selectedRule) {
      return;
    }
    const values = await weightsForm.validateFields();
    const weights = selectedRule.targets.map((target, index) => ({
      service: target.service,
      lane: target.lane,
      weight: Number(values.weights[index]?.weight ?? 0),
    }));
    const sum = weights.reduce((acc, target) => acc + target.weight, 0);
    if (sum !== 100) {
      message.error(`流量比例总和必须为 100%，当前为 ${sum}%`);
      return;
    }
    await setRuleWeights(selectedRule, weights, values.reason.trim(), '流量比例已更新');
    setWeightsModalOpen(false);
  };

  const cutBackToProd = (rule: GatewayRule) => {
    const prodTargets = rule.targets.filter((target) => target.lane === 'prod');
    if (prodTargets.length !== 1) {
      message.error('当前规则没有唯一的 prod 目标，无法一键切回 prod');
      return;
    }
    withReason(
      '切回 prod',
      <Text type="secondary">会把 prod 目标设为 100%，其他目标设为 0%。</Text>,
      async (reason) => {
        const weights = rule.targets.map((target) => ({
          service: target.service,
          lane: target.lane,
          weight: target.lane === 'prod' ? 100 : 0,
        }));
        await setRuleWeights(rule, weights, reason, '已切回 prod');
      },
    );
  };

  const toggleRule = (rule: GatewayRule) => {
    const action = rule.enabled ? 'disable' : 'enable';
    withReason(
      rule.enabled ? '停用规则' : '启用规则',
      <Text type="secondary">{rule.enabled ? '停用后，请求不会再命中这条规则。' : '启用后，这条规则会重新参与匹配。'}</Text>,
      async (reason) => {
        await runWrite(
          `${action}-${rule.name}`,
          async () => {
            await api.post(`/ops/gateway-rules/${encodeURIComponent(rule.name)}:${action}`, { reason });
          },
          rule.enabled ? '规则已停用' : '规则已启用',
        );
      },
    );
  };

  const deleteRule = (rule: GatewayRule) => {
    withReason(
      '删除规则',
      <Text type="danger">删除后仍可从历史快照回滚。</Text>,
      async (reason) => {
        await runWrite(
          `delete-${rule.name}`,
          async () => {
            await api.delete(`/ops/gateway-rules/${encodeURIComponent(rule.name)}`, { data: { reason } });
          },
          '规则已删除',
        );
      },
    );
  };

  const rollbackSnapshot = (item: GatewayRuleSnapshot) => {
    withReason(
      `回滚到 v${item.snapshot_version}`,
      (
        <Alert
          showIcon
          type="warning"
          message="会把当前规则恢复为该快照内容，并记录一条新的回滚操作。"
        />
      ),
      async (reason) => {
        await runWrite(
          `rollback-${item.snapshot_version}`,
          async () => {
            await api.post('/ops/gateway-rules:rollback', {
              snapshot_version: item.snapshot_version,
              reason,
            });
          },
          '已创建回滚快照',
        );
      },
    );
  };

  const preview = async () => {
    const values = await previewForm.validateFields();
    const path = values.path.trim();
    const xLane = normalizeOptional(values.x_lane);
    setActionLoading('preview');
    try {
      const { data } = await api.post<GatewayExplainResult>('/ops/gateway-rules:explain', {
        path,
        x_lane: xLane,
      });
      setExplainResult(data);
      setPreviewProbe({ path, xLane });
      message.success('预览完成');
    } catch (error) {
      message.error(getErrorMessage(error));
    } finally {
      setActionLoading(undefined);
    }
  };

  const targetColumns: ColumnsType<GatewayTarget> = [
    {
      title: '服务',
      dataIndex: 'service',
      render: (value: string) => <Text strong>{value}</Text>,
    },
    {
      title: '目标泳道',
      dataIndex: 'lane',
      render: (value: string) => <Tag bordered={false}>{targetLaneLabel(value)}</Tag>,
    },
    {
      title: '端口',
      dataIndex: 'port',
      width: 80,
    },
    {
      title: '流量比例',
      dataIndex: 'weight',
      width: 90,
      render: (value: number) => <Text strong>{value}%</Text>,
    },
    {
      title: pathRewriteHelpLabel(),
      key: 'rewrite',
      render: (_: unknown, target) => <Text type="secondary">{formatPathRewrite(target)}</Text>,
    },
  ];

  const candidateColumns: ColumnsType<GatewayExplainTarget> = [
    {
      title: '服务',
      dataIndex: 'service',
      render: (value: string) => <Text strong>{value}</Text>,
    },
    {
      title: '配置泳道',
      dataIndex: 'lane',
      render: (value: string) => <Tag bordered={false}>{targetLaneLabel(value)}</Tag>,
    },
    {
      title: '实际泳道',
      dataIndex: 'effective_lane',
      render: (value: string) => <Tag color={value ? 'blue' : 'default'} bordered={false}>{value || '默认泳道'}</Tag>,
    },
    {
      title: '流量比例',
      dataIndex: 'weight',
      width: 90,
      render: (value: number) => <Text strong>{value}%</Text>,
    },
  ];

  const explainColumns: ColumnsType<GatewayRuleExplain> = [
    {
      title: '规则',
      dataIndex: 'name',
      render: (value: string, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{value}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{record.path_prefix}</Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 120,
      render: (value: string) => {
        const meta = statusMeta[value] || { label: value, color: 'default' };
        return <Tag bordered={false} color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: '原因',
      dataIndex: 'reason',
      render: (_: string, record) => <Text type="secondary">{explainRuleReason(record)}</Text>,
    },
  ];

  const snapshotColumns: ColumnsType<GatewayRuleSnapshot> = [
    {
      title: '版本',
      dataIndex: 'snapshot_version',
      width: 100,
      render: (value: number) => <Tag color="blue" bordered={false}>v{value}</Tag>,
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 160,
      render: (value: string) => (
        <Tooltip title={fullTime(value)}>
          <Text type="secondary">{formatTime(value)}</Text>
        </Tooltip>
      ),
    },
    {
      title: '原因',
      dataIndex: 'reason',
      ellipsis: true,
      render: (value: string) => value || <Text type="secondary">-</Text>,
    },
    {
      title: '创建人',
      dataIndex: 'created_by',
      width: 120,
      render: (value: string) => <Tag bordered={false}>{value}</Tag>,
    },
    {
      title: '规则数',
      key: 'rules_count',
      width: 90,
      render: (_: unknown, item) => item.rules.length,
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_: unknown, item) => (
        <Space>
          <Button size="small" icon={<EyeOutlined />} onClick={() => setSnapshotDrawer(item)}>
            查看
          </Button>
          <Button
            size="small"
            danger
            icon={<RollbackOutlined />}
            loading={actionLoading === `rollback-${item.snapshot_version}`}
            onClick={() => rollbackSnapshot(item)}
          >
            回滚到此版
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-container gateway-routing-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">网关调度</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>
            管理入口路径、流量比例和快照回滚
          </Text>
        </div>
        <Space wrap>
          <Button icon={<SafetyOutlined />} onClick={openAuditLogs}>
            查看审计
          </Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={fetchData}>
            刷新
          </Button>
          <Button type="primary" icon={<SaveOutlined />} onClick={openCreateRule}>
            新建规则
          </Button>
        </Space>
      </div>

      <div className="ops-summary-strip gateway-stats">
        <div className="ops-summary-item">
          <span className="ops-summary-label">快照</span>
          <strong className="ops-summary-value">v{snapshot.version || 0}</strong>
        </div>
        <div className="ops-summary-item">
          <span className="ops-summary-label">规则</span>
          <strong className="ops-summary-value">{rules.length}</strong>
          <span className="ops-summary-meta">
            {disabledCount} 停用 / {enabledCount} 启用
          </span>
        </div>
        <div className="ops-summary-item wide">
          <span className="ops-summary-label">最近变更</span>
          <strong className="ops-summary-value compact">{latestSnapshot ? formatTime(latestSnapshot.created_at) : '-'}</strong>
        </div>
      </div>

      <div className="gateway-workbench">
        <section className="gateway-panel gateway-rule-list-panel">
          <div className="gateway-panel-header">
            <Title level={5}>规则列表</Title>
            <Tag bordered={false}>{rules.length} 条</Tag>
          </div>
          <div className="gateway-rule-list">
            {rules.length === 0 && !loading ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无规则" />
            ) : (
              rules.map((rule) => (
                <button
                  key={rule.name}
                  type="button"
                  className={`gateway-rule-item ${selectedRule?.name === rule.name ? 'active' : ''}`}
                  onClick={() => setSelectedRuleName(rule.name)}
                >
                  <div className="gateway-rule-line">
                    <Space>
                      <Tag bordered={false} color={rule.enabled ? 'green' : 'red'}>
                        {rule.enabled ? '启用' : '停用'}
                      </Tag>
                      <Text strong>{rule.name}</Text>
                    </Space>
                  </div>
                  <Text code className="gateway-rule-path">{rule.path_prefix}</Text>
                  <div className="gateway-rule-meta">
                    <Text type="secondary">匹配泳道：{requestLaneLabel(rule.request_lane)} · 优先级 {rule.priority}</Text>
                    <Text type="secondary">{targetSummary(rule.targets)}</Text>
                  </div>
                </button>
              ))
            )}
          </div>
        </section>

        <section className="gateway-panel gateway-detail-panel">
          {selectedRule ? (
            <>
              <div className="gateway-panel-header">
                <div>
                  <Title level={4}>{selectedRule.name}</Title>
                  <Text type="secondary">规则版本 {selectedRule.version} · 更新 {formatTime(selectedRule.updated_at)}</Text>
                </div>
                <Space wrap>
                  <Button icon={<EditOutlined />} onClick={() => openEditRule(selectedRule)}>
                    编辑
                  </Button>
                  <Button icon={<SwapOutlined />} onClick={() => openWeights(selectedRule)}>
                    调整流量
                  </Button>
                  <Dropdown
                    menu={{
                      items: [
                        {
                          key: 'cut-prod',
                          icon: <RetweetOutlined />,
                          label: '切回 prod',
                        },
                        {
                          key: 'toggle',
                          icon: selectedRule.enabled ? <PauseCircleOutlined /> : <PlayCircleOutlined />,
                          label: selectedRule.enabled ? '停用规则' : '启用规则',
                        },
                        {
                          type: 'divider',
                        },
                        {
                          key: 'delete',
                          danger: true,
                          icon: <DeleteOutlined />,
                          label: '删除规则',
                        },
                      ],
                      onClick: ({ key }) => {
                        if (key === 'cut-prod') {
                          cutBackToProd(selectedRule);
                        } else if (key === 'toggle') {
                          toggleRule(selectedRule);
                        } else if (key === 'delete') {
                          deleteRule(selectedRule);
                        }
                      },
                    }}
                  >
                    <Button icon={<MoreOutlined />}>更多操作</Button>
                  </Dropdown>
                </Space>
              </div>

              <Descriptions bordered size="small" column={{ xs: 1, md: 2 }}>
                <Descriptions.Item label="状态">
                  <Tag bordered={false} color={selectedRule.enabled ? 'green' : 'red'}>
                    {selectedRule.enabled ? '启用中' : '已停用'}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="优先级">{selectedRule.priority}</Descriptions.Item>
                <Descriptions.Item label="匹配路径">
                  <Text code>{selectedRule.path_prefix}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="匹配泳道">{requestLaneLabel(selectedRule.request_lane)}</Descriptions.Item>
                <Descriptions.Item label={splitHelpLabel()}>{stableSplitLabel(selectedRule)}</Descriptions.Item>
                <Descriptions.Item label="创建时间">{fullTime(selectedRule.created_at)}</Descriptions.Item>
              </Descriptions>

              <Divider orientation="left">转发目标</Divider>
              <Table
                rowKey={(target) => `${target.service}:${target.lane}:${target.port}`}
                columns={targetColumns}
                dataSource={selectedRule.targets}
                pagination={false}
                size="small"
              />
            </>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一条规则查看详情" />
          )}
        </section>
      </div>

      <section className="gateway-panel gateway-preview-panel">
        <div className="gateway-panel-header">
          <div>
            <Title level={5}>预览</Title>
            <Text type="secondary">输入请求路径和可选泳道，查看当前规则会如何匹配。</Text>
          </div>
        </div>
        <Form
          form={previewForm}
          layout="inline"
          className="gateway-preview-form"
          initialValues={{ path: '/api/agent/health', x_lane: '' }}
        >
          <Form.Item
            name="path"
            rules={[
              { required: true, message: '请输入请求路径' },
              { pattern: /^\//, message: '请求路径必须以 / 开头' },
            ]}
            style={{ flex: 1, minWidth: 260 }}
          >
            <Input prefix={<AimOutlined />} placeholder="/api/agent/health" />
          </Form.Item>
          <Form.Item name="x_lane" style={{ width: 220 }}>
            <Input placeholder="请求泳道，不填=未指定" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" loading={actionLoading === 'preview'} onClick={preview}>
              预览命中
            </Button>
          </Form.Item>
        </Form>

        {explainResult && (
          <div className="gateway-preview-result">
            <Alert
              showIcon
              type={explainResult.matched ? 'success' : 'warning'}
              message={
                explainResult.matched
                  ? `命中 ${explainResult.winning_rule}`
                  : '没有命中任何规则'
              }
              description={
                explainResult.matched
                  ? `候选目标：${explainTargetSummary(explainResult.candidate_targets)}；${explainResult.stable_split ? `稳定分流依据：${explainResult.split_key_headers?.join(', ')}` : '按流量比例随机分配'}`
                  : '这类请求不会被当前规则处理。'
              }
            />
            {explainResult.candidate_targets?.length ? (
              <Table
                rowKey={(target) => `${target.service}:${target.lane}:${target.port}`}
                columns={candidateColumns}
                dataSource={explainResult.candidate_targets}
                pagination={false}
                size="small"
              />
            ) : null}
            <Table
              rowKey={(item) => item.name}
              columns={explainColumns}
              dataSource={explainResult.rules}
              pagination={false}
              size="small"
            />
          </div>
        )}
      </section>

      <section className="gateway-panel">
        <div className="gateway-panel-header">
          <div>
            <Title level={5}>快照历史</Title>
            <Text type="secondary">最近 20 条快照历史。</Text>
          </div>
        </div>
        <Table
          rowKey="snapshot_version"
          columns={snapshotColumns}
          dataSource={snapshots}
          loading={loading}
          pagination={false}
          size="middle"
        />
      </section>

      <Modal
        title={editingRuleName ? `编辑 ${editingRuleName}` : '新建网关规则'}
        open={ruleModalOpen}
        onOk={saveRule}
        onCancel={() => {
          setRuleModalOpen(false);
          setEditingRuleName(null);
        }}
        confirmLoading={actionLoading?.startsWith('save-')}
        okText={editingRuleName ? '保存' : '创建'}
        cancelText="取消"
        width={860}
        destroyOnClose
      >
        <Form form={ruleForm} layout="vertical" requiredMark={false}>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="name"
                label="规则名"
                rules={[
                  { required: true, message: '请输入规则名' },
                  { pattern: /^[a-z0-9][a-z0-9-]*$/, message: '仅支持小写字母、数字和 -' },
                ]}
              >
                <Input disabled={!!editingRuleName} placeholder="agent-canary" />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="enabled" label="状态" valuePropName="checked">
                <Switch checkedChildren="启用" unCheckedChildren="禁用" />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="priority" label="优先级" rules={[{ required: true, message: '请输入优先级' }]}>
                <InputNumber min={0} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item
                name="path_prefix"
                label="匹配路径"
                rules={[
                  { required: true, message: '请输入匹配路径' },
                  { pattern: /^\/.*\/$/, message: '匹配路径必须以 / 开头并以 / 结尾' },
                ]}
              >
                <Input placeholder="/api/agent/" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="request_lane" label="匹配泳道">
                <Input placeholder="不填=不限制请求 x-lane" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="split_key_headers" label={splitHelpLabel()}>
            <Select mode="tags" placeholder="例如 x-user-id" tokenSeparators={[',']} />
          </Form.Item>

          <Divider orientation="left">转发目标</Divider>
          <Form.List name="targets">
            {(fields, { add, remove }) => (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                {fields.map((field) => (
                  <div key={field.key} className="gateway-target-editor">
                    <Row gutter={12}>
                      <Col xs={24} md={7}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'service']}
                          label="服务"
                          rules={[{ required: true, message: '服务必填' }]}
                        >
                          <Input placeholder="agent-service" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={5}>
                        <Form.Item {...field} name={[field.name, 'lane']} label="目标泳道">
                          <Input placeholder="不填=使用请求里的 x-lane" />
                        </Form.Item>
                      </Col>
                      <Col xs={12} md={4}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'port']}
                          label="端口"
                          rules={[{ required: true, message: '端口必填' }]}
                        >
                          <InputNumber min={1} precision={0} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col xs={12} md={4}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'weight']}
                          label="流量比例"
                          rules={[{ required: true, message: '流量比例必填' }]}
                        >
                          <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={4}>
                        <Form.Item label="操作">
                          <Button danger block disabled={fields.length === 1} onClick={() => remove(field.name)}>
                            删除
                          </Button>
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col xs={24} md={12}>
                        <Form.Item {...field} name={[field.name, 'strip_prefix']} label="去掉路径前缀">
                          <Input placeholder="可选" />
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item {...field} name={[field.name, 'rewrite_prefix']} label="改写路径前缀">
                          <Input placeholder="可选" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </div>
                ))}
                <Button block onClick={() => add({ service: '', lane: '', port: 80, weight: 0 })}>
                  添加转发目标
                </Button>
              </Space>
            )}
          </Form.List>

          <Divider />
          <Form.Item name="reason" label="变更原因" rules={[{ required: true, whitespace: true, message: '变更原因必填' }]}>
            <Input.TextArea autoSize={{ minRows: 3, maxRows: 5 }} placeholder="填写本次规则变更原因" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={selectedRule ? `调整流量 ${selectedRule.name}` : '调整流量'}
        open={weightsModalOpen}
        onOk={saveWeights}
        onCancel={() => setWeightsModalOpen(false)}
        confirmLoading={actionLoading?.startsWith('weights-')}
        okText="保存比例"
        cancelText="取消"
        width={640}
        destroyOnClose
      >
        {selectedRule && (
          <Form form={weightsForm} layout="vertical" requiredMark={false}>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {selectedRule.targets.map((target, index) => (
                <div key={`${target.service}:${target.lane}`} className="gateway-weight-row">
                  <div>
                    <Text strong>{targetIdentity(target)}</Text>
                    <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>端口 {target.port}</Text>
                  </div>
                  <Form.Item
                    name={['weights', index, 'weight']}
                    rules={[{ required: true, message: '流量比例必填' }]}
                    style={{ margin: 0 }}
                  >
                    <InputNumber min={0} max={100} precision={0} addonAfter="%" />
                  </Form.Item>
                </div>
              ))}
            </Space>
            <Form.Item
              name="reason"
              label="变更原因"
              rules={[{ required: true, whitespace: true, message: '变更原因必填' }]}
              style={{ marginTop: 20 }}
            >
              <Input.TextArea autoSize={{ minRows: 3, maxRows: 5 }} placeholder="填写本次调整原因" />
            </Form.Item>
          </Form>
        )}
      </Modal>

      <Drawer
        title={snapshotDrawer ? `快照 v${snapshotDrawer.snapshot_version}` : '快照'}
        open={!!snapshotDrawer}
        onClose={() => setSnapshotDrawer(null)}
        width={720}
      >
        {snapshotDrawer && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="创建时间">{fullTime(snapshotDrawer.created_at)}</Descriptions.Item>
              <Descriptions.Item label="创建人">{snapshotDrawer.created_by}</Descriptions.Item>
              <Descriptions.Item label="原因">{snapshotDrawer.reason || '-'}</Descriptions.Item>
              <Descriptions.Item label="规则数">{snapshotDrawer.rules.length}</Descriptions.Item>
            </Descriptions>
            <pre className="json-preview">{JSON.stringify(snapshotDrawer.rules, null, 2)}</pre>
          </Space>
        )}
      </Drawer>
    </div>
  );
}
