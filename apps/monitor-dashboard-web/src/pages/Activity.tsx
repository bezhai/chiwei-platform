import { useEffect, useState, useCallback } from 'react';
import { Card, Table, Typography, Tag, Button, Tooltip } from 'antd';
import {
  ReloadOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { getLane } from '../api/client';

const { Text } = Typography;

interface GroupStat {
  chat_id: string;
  group_name: string;
  message_count: number;
  bot_replies: number;
  daily_counts: Array<{ date: string; count: number }>;
}

function Sparkline({ data }: { data: Array<{ date: string; count: number }> }) {
  if (!data || data.length === 0) return <Text type="secondary">-</Text>;
  // Deduplicate by date
  const byDate = new Map<string, number>();
  for (const d of data) {
    byDate.set(d.date, (byDate.get(d.date) || 0) + d.count);
  }
  const values = [...byDate.values()];
  const max = Math.max(...values, 1);
  const width = values.length * 6;
  const height = 24;

  return (
    <svg width={width} height={height} style={{ verticalAlign: 'middle', overflow: 'visible' }}>
      {values.map((v, i) => {
        const h = Math.max((v / max) * height, 2);
        return (
          <rect
            key={i}
            x={i * 6}
            y={height - h}
            width={4}
            height={h}
            fill="var(--primary)"
            opacity={0.7}
            rx={2}
          />
        );
      })}
    </svg>
  );
}

export default function Activity() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [groups, setGroups] = useState<GroupStat[]>([]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const overviewRes = await api.get('/activity/overview', { params: { days: 7 } });

      setSummary(overviewRes.data.summary || {});
      setGroups(overviewRes.data.groups || []);
    } catch (e) {
      console.error('Failed to fetch activity data:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, 60000);
    return () => clearInterval(timer);
  }, [fetchData]);

  const columns = [
    {
      title: '群名称',
      dataIndex: 'group_name',
      key: 'group_name',
      render: (name: string, record: GroupStat) => (
        <Button
          type="link"
          style={{ padding: 0, fontWeight: 600, color: 'var(--primary)' }}
          onClick={() => {
            const lane = getLane();
            navigate(lane ? `/messages?chatId=${record.chat_id}&x-lane=${lane}` : `/messages?chatId=${record.chat_id}`);
          }}
        >
          {name}
        </Button>
      ),
    },
    {
      title: '7天趋势',
      key: 'sparkline',
      width: 100,
      render: (_: unknown, record: GroupStat) => <Sparkline data={record.daily_counts} />,
    },
    {
      title: '消息数',
      dataIndex: 'message_count',
      key: 'message_count',
      width: 100,
      sorter: (a: GroupStat, b: GroupStat) => a.message_count - b.message_count,
      render: (val: number) => <Text strong>{val.toLocaleString()}</Text>
    },
    {
      title: '赤尾回复',
      dataIndex: 'bot_replies',
      key: 'bot_replies',
      width: 120,
      sorter: (a: GroupStat, b: GroupStat) => a.bot_replies - b.bot_replies,
      defaultSortOrder: 'descend' as const,
      render: (val: number) => <Tag bordered={false} color={val > 0 ? 'success' : 'default'}>{val.toLocaleString()}</Tag>
    },
  ];

  return (
    <div className="page-container">
      <div className="page-header">
        <div>
          <h1 className="page-title">赤尾动态</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>群活跃度与赤尾回复概览</Text>
        </div>
        <Tooltip title="每 60 秒自动刷新">
          <Button
            icon={<ReloadOutlined spin={loading} />}
            onClick={() => { setLoading(true); fetchData(); }}
          >
            刷新
          </Button>
        </Tooltip>
      </div>

      <div className="ops-summary-strip">
        <div className="ops-summary-item">
          <span className="ops-summary-label">今日消息</span>
          <strong className="ops-summary-value">{Number(summary.today_total) || 0}</strong>
        </div>
        <div className="ops-summary-item">
          <span className="ops-summary-label">赤尾回复</span>
          <strong className="ops-summary-value">{Number(summary.today_bot_replies) || 0}</strong>
        </div>
        <div className="ops-summary-item">
          <span className="ops-summary-label">活跃群</span>
          <strong className="ops-summary-value">{Number(summary.today_active_groups) || 0}</strong>
        </div>
      </div>

      <Card bordered={false} className="content-card ops-table-shell" bodyStyle={{ padding: 0, overflow: 'hidden' }}>
        <Table
          dataSource={groups}
          columns={columns}
          rowKey="chat_id"
          loading={loading}
          pagination={false}
          size="middle"
          scroll={{ x: 640 }}
        />
      </Card>
    </div>
  );
}
