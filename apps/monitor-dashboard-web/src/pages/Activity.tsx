import { useEffect, useState, useCallback } from 'react';
import { Card, Col, Row, Statistic, Table, Typography, Tooltip, Space, Tag, Button } from 'antd';
import {
  MessageOutlined,
  RobotOutlined,
  TeamOutlined,
  ReloadOutlined,
  BookOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { getLane } from '../api/client';

const { Title, Text, Paragraph } = Typography;

interface GroupStat {
  chat_id: string;
  group_name: string;
  message_count: number;
  bot_replies: number;
  daily_counts: Array<{ date: string; count: number }>;
}

interface DiaryRow {
  chat_id: string;
  latest_diary_date: string;
  latest_diary_content: string;
  diary_count_7d: number;
}

interface WeeklyRow {
  chat_id: string;
  latest_week_start: string;
  latest_weekly_content: string;
  review_count_4w: number;
}

function normalizePreviewContent(content?: string) {
  return content?.replace(/\s+/g, ' ').trim() || '';
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
  const height = 20;

  return (
    <svg width={width} height={height} style={{ verticalAlign: 'middle' }}>
      {values.map((v, i) => {
        const h = Math.max((v / max) * height, 1);
        return (
          <rect
            key={i}
            x={i * 6}
            y={height - h}
            width={4}
            height={h}
            fill="#2563eb"
            opacity={0.7}
            rx={1}
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
  const [diaryMap, setDiaryMap] = useState<Map<string, DiaryRow>>(new Map());
  const [weeklyMap, setWeeklyMap] = useState<Map<string, WeeklyRow>>(new Map());

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [overviewRes, diaryRes] = await Promise.all([
        api.get('/activity/overview', { params: { days: 7 } }),
        api.get('/activity/diary-status').catch(() => ({ data: { diary: [], weekly: [] } })),
      ]);

      setSummary(overviewRes.data.summary || {});
      setGroups(overviewRes.data.groups || []);

      // diary-status returns arrays of {chat_id, latest_diary_date, diary_count_7d}
      const dMap = new Map<string, DiaryRow>();
      for (const row of (diaryRes.data.diary || [])) {
        dMap.set(row.chat_id, row);
      }
      setDiaryMap(dMap);

      const wMap = new Map<string, WeeklyRow>();
      for (const row of (diaryRes.data.weekly || [])) {
        wMap.set(row.chat_id, row);
      }
      setWeeklyMap(wMap);
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
          style={{ padding: 0 }}
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
      width: 80,
      render: (_: unknown, record: GroupStat) => <Sparkline data={record.daily_counts} />,
    },
    {
      title: '消息数',
      dataIndex: 'message_count',
      key: 'message_count',
      width: 90,
      sorter: (a: GroupStat, b: GroupStat) => a.message_count - b.message_count,
    },
    {
      title: '赤尾回复',
      dataIndex: 'bot_replies',
      key: 'bot_replies',
      width: 100,
      sorter: (a: GroupStat, b: GroupStat) => a.bot_replies - b.bot_replies,
      defaultSortOrder: 'descend' as const,
    },
    {
      title: '最近日记',
      key: 'diary',
      width: 360,
      render: (_: unknown, record: GroupStat) => {
        const d = diaryMap.get(record.chat_id);
        if (!d?.latest_diary_date) return <Text type="secondary">-</Text>;
        const isRecent = dayjs().diff(dayjs(d.latest_diary_date), 'day') <= 1;
        const preview = normalizePreviewContent(d.latest_diary_content);
        return (
          <Space size={8} style={{ width: '100%' }}>
            <BookOutlined style={{ color: isRecent ? '#52c41a' : '#d9d9d9', flex: 'none' }} />
            <div style={{ minWidth: 0, flex: 1 }}>
              <Space size={6} wrap>
                <Text type={isRecent ? undefined : 'secondary'}>
                  {dayjs(d.latest_diary_date).format('MM-DD')}
                </Text>
                <Tag style={{ fontSize: 11, marginInlineEnd: 0 }}>{d.diary_count_7d}/7d</Tag>
              </Space>
              <Tooltip title={preview || '无内容'}>
                <Paragraph
                  type={preview ? undefined : 'secondary'}
                  style={{ marginBottom: 0 }}
                  ellipsis={{ rows: 2, tooltip: false }}
                >
                  {preview || '无内容'}
                </Paragraph>
              </Tooltip>
            </div>
          </Space>
        );
      },
    },
    {
      title: '最近周记',
      key: 'weekly',
      width: 360,
      render: (_: unknown, record: GroupStat) => {
        const w = weeklyMap.get(record.chat_id);
        if (!w?.latest_week_start) return <Text type="secondary">-</Text>;
        const isRecent = dayjs().diff(dayjs(w.latest_week_start), 'day') <= 7;
        const preview = normalizePreviewContent(w.latest_weekly_content);
        return (
          <Space size={8} style={{ width: '100%' }}>
            <FileTextOutlined style={{ color: isRecent ? '#52c41a' : '#d9d9d9', flex: 'none' }} />
            <div style={{ minWidth: 0, flex: 1 }}>
              <Space size={6} wrap>
                <Text type={isRecent ? undefined : 'secondary'}>
                  {dayjs(w.latest_week_start).format('MM-DD')}
                </Text>
                <Tag style={{ fontSize: 11, marginInlineEnd: 0 }}>{w.review_count_4w}/4w</Tag>
              </Space>
              <Tooltip title={preview || '无内容'}>
                <Paragraph
                  type={preview ? undefined : 'secondary'}
                  style={{ marginBottom: 0 }}
                  ellipsis={{ rows: 2, tooltip: false }}
                >
                  {preview || '无内容'}
                </Paragraph>
              </Tooltip>
            </div>
          </Space>
        );
      },
    },
  ];

  return (
    <div className="page-container">
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <Title level={4} style={{ marginBottom: 4 }}>赤尾动态</Title>
          <Text type="secondary">群活跃度与日记/周记生成概览</Text>
        </div>
        <Tooltip title="每 60 秒自动刷新">
          <ReloadOutlined
            spin={loading}
            style={{ fontSize: 16, cursor: 'pointer', color: '#8c8c8c' }}
            onClick={() => { setLoading(true); fetchData(); }}
          />
        </Tooltip>
      </div>

      <Row gutter={[24, 24]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card bordered={false}>
            <Statistic
              title="今日消息总数"
              value={Number(summary.today_total) || 0}
              prefix={<MessageOutlined />}
              valueStyle={{ fontWeight: 600 }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card bordered={false}>
            <Statistic
              title="赤尾回复数"
              value={Number(summary.today_bot_replies) || 0}
              prefix={<RobotOutlined style={{ color: '#2563eb' }} />}
              valueStyle={{ fontWeight: 600, color: '#2563eb' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card bordered={false}>
            <Statistic
              title="今日活跃群数"
              value={Number(summary.today_active_groups) || 0}
              prefix={<TeamOutlined />}
              valueStyle={{ fontWeight: 600 }}
            />
          </Card>
        </Col>
      </Row>

      <Card bordered={false}>
        <Table
          dataSource={groups}
          columns={columns}
          rowKey="chat_id"
          loading={loading}
          pagination={false}
          size="middle"
          scroll={{ x: 1200 }}
        />
      </Card>
    </div>
  );
}
