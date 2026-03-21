import { useEffect, useState, useCallback, type ReactNode } from 'react';
import { Card, Col, Row, Statistic, Table, Typography, Space, Tag, Button, Modal, Tooltip } from 'antd';
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

interface PreviewModalState {
  title: string;
  content: string;
}

function normalizePreviewContent(content?: string) {
  return content?.replace(/\s+/g, ' ').trim() || '';
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|~~[^~]+~~|\*[^*]+\*|_[^_]+_)/g);

  return parts.filter(Boolean).map((part, index) => {
    if ((part.startsWith('**') && part.endsWith('**')) || (part.startsWith('__') && part.endsWith('__'))) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    if ((part.startsWith('*') && part.endsWith('*')) || (part.startsWith('_') && part.endsWith('_'))) {
      return <em key={index}>{part.slice(1, -1)}</em>;
    }
    if (part.startsWith('~~') && part.endsWith('~~')) {
      return <del key={index}>{part.slice(2, -2)}</del>;
    }
    if (part.startsWith('`') && part.endsWith('`')) {
      return <Text key={index} code style={{ background: '#f1f5f9', border: 'none', color: '#0f172a' }}>{part.slice(1, -1)}</Text>;
    }
    return <span key={index}>{part}</span>;
  });
}

function renderMarkdownLikeContent(content: string) {
  const lines = content.split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index].trimEnd();
    const trimmed = line.trim();

    if (!trimmed) {
      index++;
      continue;
    }

    if (trimmed.startsWith('### ')) {
      blocks.push(<Title key={blocks.length} level={5} style={{ marginTop: 16, marginBottom: 8, color: '#0f172a' }}>{renderInlineMarkdown(trimmed.slice(4))}</Title>);
      index++;
      continue;
    }
    if (trimmed.startsWith('## ')) {
      blocks.push(<Title key={blocks.length} level={4} style={{ marginTop: 24, marginBottom: 12, color: '#0f172a' }}>{renderInlineMarkdown(trimmed.slice(3))}</Title>);
      index++;
      continue;
    }
    if (trimmed.startsWith('# ')) {
      blocks.push(<Title key={blocks.length} level={3} style={{ marginTop: 32, marginBottom: 16, color: '#0f172a' }}>{renderInlineMarkdown(trimmed.slice(2))}</Title>);
      index++;
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ''));
        index++;
      }
      blocks.push(
        <ul key={blocks.length} style={{ paddingInlineStart: 20, marginTop: 8, marginBottom: 16, color: '#334155' }}>
          {items.map((item, itemIndex) => (
            <li key={itemIndex} style={{ marginBottom: 6 }}>
              {renderInlineMarkdown(item)}
            </li>
          ))}
        </ul>,
      );
      continue;
    }

    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+\.\s+/, ''));
        index++;
      }
      blocks.push(
        <ol key={blocks.length} style={{ paddingInlineStart: 20, marginTop: 8, marginBottom: 16, color: '#334155' }}>
          {items.map((item, itemIndex) => (
            <li key={itemIndex} style={{ marginBottom: 6 }}>
              {renderInlineMarkdown(item)}
            </li>
          ))}
        </ol>,
      );
      continue;
    }

    if (trimmed.startsWith('>')) {
      const quoteLines: string[] = [];
      while (index < lines.length && lines[index].trim().startsWith('>')) {
        quoteLines.push(lines[index].trim().replace(/^>\s?/, ''));
        index++;
      }
      blocks.push(
        <div
          key={blocks.length}
          style={{
            borderInlineStart: '3px solid #cbd5e1',
            background: '#f8fafc',
            padding: '12px 16px',
            color: '#475569',
            marginBottom: 16,
            borderRadius: '0 8px 8px 0',
            whiteSpace: 'pre-wrap',
          }}
        >
          {quoteLines.map((quoteLine, quoteIndex) => (
            <div key={quoteIndex}>{renderInlineMarkdown(quoteLine)}</div>
          ))}
        </div>,
      );
      continue;
    }

    const paragraphLines: string[] = [];
    while (index < lines.length) {
      const current = lines[index].trimEnd();
      const currentTrimmed = current.trim();
      if (
        !currentTrimmed ||
        currentTrimmed.startsWith('# ') ||
        currentTrimmed.startsWith('## ') ||
        currentTrimmed.startsWith('### ') ||
        /^[-*]\s+/.test(currentTrimmed) ||
        /^\d+\.\s+/.test(currentTrimmed) ||
        currentTrimmed.startsWith('>')
      ) {
        break;
      }
      paragraphLines.push(current);
      index++;
    }
    blocks.push(
      <Paragraph key={blocks.length} style={{ whiteSpace: 'pre-wrap', color: '#334155', marginBottom: 16, lineHeight: 1.6 }}>
        {paragraphLines.map((paragraphLine, paragraphIndex) => (
          <span key={paragraphIndex}>
            {paragraphIndex > 0 ? <br /> : null}
            {renderInlineMarkdown(paragraphLine)}
          </span>
        ))}
      </Paragraph>,
    );
  }

  return blocks;
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
            fill="#000000"
            opacity={0.8}
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
  const [diaryMap, setDiaryMap] = useState<Map<string, DiaryRow>>(new Map());
  const [weeklyMap, setWeeklyMap] = useState<Map<string, WeeklyRow>>(new Map());
  const [previewModal, setPreviewModal] = useState<PreviewModalState | null>(null);

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
          style={{ padding: 0, fontWeight: 500, color: '#000000' }}
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
      width: 100,
      sorter: (a: GroupStat, b: GroupStat) => a.bot_replies - b.bot_replies,
      defaultSortOrder: 'descend' as const,
      render: (val: number) => <Tag bordered={false} color={val > 0 ? 'blue' : 'default'}>{val.toLocaleString()}</Tag>
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
          <Space size={12} style={{ width: '100%', alignItems: 'flex-start' }}>
            <div style={{ background: isRecent ? '#ecfdf5' : '#f8fafc', padding: 8, borderRadius: 8, display: 'flex' }}>
              <BookOutlined style={{ color: isRecent ? '#10b981' : '#94a3b8', fontSize: 16 }} />
            </div>
            <div style={{ minWidth: 0, flex: 1, marginTop: 2 }}>
              <Space size={8} wrap style={{ marginBottom: 4 }}>
                <Text strong={isRecent} type={isRecent ? undefined : 'secondary'} style={{ fontSize: 13, color: isRecent ? '#0f172a' : undefined }}>
                  {dayjs(d.latest_diary_date).format('MM-DD')}
                </Text>
                <Tag bordered={false} color={isRecent ? 'success' : 'default'} style={{ fontSize: 11, marginInlineEnd: 0 }}>{d.diary_count_7d}/7d</Tag>
              </Space>
              <Paragraph
                type={preview ? undefined : 'secondary'}
                style={{ marginBottom: 4, color: '#475569', fontSize: 13, lineHeight: 1.5 }}
                ellipsis={{ rows: 2, tooltip: false }}
              >
                {preview || '无内容'}
              </Paragraph>
              <Button
                type="link"
                size="small"
                style={{ padding: 0, height: 'auto', fontSize: 12, color: '#3b82f6' }}
                onClick={() => setPreviewModal({
                  title: `${record.group_name} · ${dayjs(d.latest_diary_date).format('YYYY-MM-DD')} 日记`,
                  content: d.latest_diary_content || '无内容',
                })}
              >
                阅读全文
              </Button>
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
          <Space size={12} style={{ width: '100%', alignItems: 'flex-start' }}>
            <div style={{ background: isRecent ? '#eff6ff' : '#f8fafc', padding: 8, borderRadius: 8, display: 'flex' }}>
              <FileTextOutlined style={{ color: isRecent ? '#3b82f6' : '#94a3b8', fontSize: 16 }} />
            </div>
            <div style={{ minWidth: 0, flex: 1, marginTop: 2 }}>
              <Space size={8} wrap style={{ marginBottom: 4 }}>
                <Text strong={isRecent} type={isRecent ? undefined : 'secondary'} style={{ fontSize: 13, color: isRecent ? '#0f172a' : undefined }}>
                  {dayjs(w.latest_week_start).format('MM-DD')}
                </Text>
                <Tag bordered={false} color={isRecent ? 'processing' : 'default'} style={{ fontSize: 11, marginInlineEnd: 0 }}>{w.review_count_4w}/4w</Tag>
              </Space>
              <Paragraph
                type={preview ? undefined : 'secondary'}
                style={{ marginBottom: 4, color: '#475569', fontSize: 13, lineHeight: 1.5 }}
                ellipsis={{ rows: 2, tooltip: false }}
              >
                {preview || '无内容'}
              </Paragraph>
              <Button
                type="link"
                size="small"
                style={{ padding: 0, height: 'auto', fontSize: 12, color: '#3b82f6' }}
                onClick={() => setPreviewModal({
                  title: `${record.group_name} · ${dayjs(w.latest_week_start).format('YYYY-MM-DD')} 周记`,
                  content: w.latest_weekly_content || '无内容',
                })}
              >
                阅读全文
              </Button>
            </div>
          </Space>
        );
      },
    },
  ];

  return (
    <div className="page-container">
      <div className="page-header">
        <div>
          <h1 className="page-title">赤尾动态</h1>
          <Text type="secondary" style={{ marginTop: 8, display: 'block' }}>群活跃度与日记/周记生成概览</Text>
        </div>
        <Tooltip title="每 60 秒自动刷新">
          <div 
            onClick={() => { setLoading(true); fetchData(); }}
            style={{ 
              display: 'flex', 
              alignItems: 'center', 
              gap: 8, 
              padding: '8px 16px', 
              background: '#fff', 
              borderRadius: 8, 
              cursor: 'pointer',
              border: '1px solid #e2e8f0',
              boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
              transition: 'all 0.2s'
            }}
            className="hover-card"
          >
            <ReloadOutlined spin={loading} style={{ color: '#64748b' }} />
            <Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>刷新</Text>
          </div>
        </Tooltip>
      </div>

      <Row gutter={[24, 24]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card bordered={false} className="content-card" bodyStyle={{ padding: '20px 24px' }}>
            <Statistic
              title={<Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>今日消息总数</Text>}
              value={Number(summary.today_total) || 0}
              prefix={<MessageOutlined style={{ color: '#64748b' }} />}
              valueStyle={{ fontWeight: 700, fontSize: 32, color: '#0f172a', marginTop: 8 }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card bordered={false} className="content-card" bodyStyle={{ padding: '20px 24px' }}>
            <Statistic
              title={<Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>赤尾回复数</Text>}
              value={Number(summary.today_bot_replies) || 0}
              prefix={<RobotOutlined style={{ color: '#000000' }} />}
              valueStyle={{ fontWeight: 700, fontSize: 32, color: '#000000', marginTop: 8 }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card bordered={false} className="content-card" bodyStyle={{ padding: '20px 24px' }}>
            <Statistic
              title={<Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>今日活跃群数</Text>}
              value={Number(summary.today_active_groups) || 0}
              prefix={<TeamOutlined style={{ color: '#64748b' }} />}
              valueStyle={{ fontWeight: 700, fontSize: 32, color: '#0f172a', marginTop: 8 }}
            />
          </Card>
        </Col>
      </Row>

      <Card bordered={false} className="content-card" bodyStyle={{ padding: 0, overflow: 'hidden' }}>
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

      <Modal
        open={!!previewModal}
        title={<div style={{ fontSize: 18, fontWeight: 600, color: '#0f172a' }}>{previewModal?.title}</div>}
        footer={null}
        onCancel={() => setPreviewModal(null)}
        width={860}
        styles={{ 
          body: { maxHeight: '70vh', overflowY: 'auto', padding: '24px 0' },
          content: { borderRadius: 16, overflow: 'hidden', padding: 24 }
        }}
      >
        <div style={{ fontSize: 14, lineHeight: 1.8, color: '#334155' }}>
          {renderMarkdownLikeContent(previewModal?.content || '无内容')}
        </div>
      </Modal>
    </div>
  );
}
