import { useEffect, useState, useMemo } from 'react';
import {
  Button,
  Checkbox,
  DatePicker,
  Input,
  Popover,
  Select,
  Space,
  Table,
  Tag,
  Row,
  Col,
  Typography,
  Tooltip,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import { useSearchParams } from 'react-router-dom';
import {
  SearchOutlined,
  ReloadOutlined,
  SettingOutlined,
  CaretDownOutlined,
  CopyOutlined,
} from '@ant-design/icons';
import { api } from '../api/client';

const { Text } = Typography;

interface MessageRow {
  message_id: string;
  user_id: string;
  user_name: string;
  role: string;
  chat_id: string;
  chat_name: string;
  chat_type: string;
  bot_name?: string | null;
  content: string;
  message_type?: string | null;
  root_message_id: string;
  reply_message_id?: string | null;
  create_time: string;
  vector_status: string;
}

// --- Enum maps ---

const chatTypeMap: Record<string, string> = {
  p2p: '私聊',
  group: '群聊',
};

const vectorStatusMap: Record<string, { label: string; color: string }> = {
  pending: { label: '待处理', color: 'default' },
  completed: { label: '已完成', color: 'green' },
  failed: { label: '失败', color: 'red' },
  skipped: { label: '已跳过', color: 'orange' },
};

const messageTypeMap: Record<string, string> = {
  text: '文本',
  post: '富文本',
  image: '图片',
  file: '文件',
  folder: '文件夹',
  audio: '音频',
  media: '视频',
  sticker: '表情包',
  interactive: '卡片',
  hongbao: '红包',
  share_calendar_event: '日程分享卡片',
  calendar: '日程邀请卡片',
  general_calendar: '日程转让卡片',
  share_chat: '群名片',
  share_user: '个人名片',
  system: '系统消息',
  location: '位置',
  video_chat: '视频通话',
  todo: '任务',
  vote: '投票',
  merge_forward: '合并转发',
};

const messageTypeFilterOptions = [
  { value: 'text', label: '文本' },
  { value: 'post', label: '富文本' },
  { value: 'image', label: '图片' },
  { value: 'file', label: '文件' },
  { value: 'interactive', label: '卡片' },
  { value: 'system', label: '系统消息' },
  { value: 'sticker', label: '表情包' },
  { value: 'media', label: '视频' },
  { value: 'audio', label: '音频' },
];

// --- Column definitions ---

interface ColumnDef {
  key: string;
  title: string;
  defaultVisible: boolean;
}

const ALL_COLUMNS: ColumnDef[] = [
  { key: 'message_id', title: '消息 ID', defaultVisible: true },
  { key: 'user_name', title: '用户', defaultVisible: true },
  { key: 'user_id', title: '用户 ID', defaultVisible: false },
  { key: 'role', title: '角色', defaultVisible: true },
  { key: 'chat_name', title: '会话', defaultVisible: true },
  { key: 'chat_id', title: '会话 ID', defaultVisible: false },
  { key: 'chat_type', title: '会话类型', defaultVisible: true },
  { key: 'bot_name', title: '机器人', defaultVisible: true },
  { key: 'content', title: '内容', defaultVisible: true },
  { key: 'message_type', title: '消息类型', defaultVisible: true },
  { key: 'root_message_id', title: '根消息 ID', defaultVisible: false },
  { key: 'reply_message_id', title: '父消息 ID', defaultVisible: false },
  { key: 'create_time', title: '创建时间', defaultVisible: true },
  { key: 'vector_status', title: '向量状态', defaultVisible: true },
];

const DEFAULT_VISIBLE_KEYS = ALL_COLUMNS.filter((c) => c.defaultVisible).map((c) => c.key);
const STORAGE_KEY = 'messages_visible_columns';

function loadVisibleColumns(): string[] {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) return DEFAULT_VISIBLE_KEYS;
    const parsed = JSON.parse(stored) as string[];
    const validKeys = new Set(ALL_COLUMNS.map((c) => c.key));
    const filtered = parsed.filter((k) => validKeys.has(k));
    return filtered.length > 0 ? filtered : DEFAULT_VISIBLE_KEYS;
  } catch {
    return DEFAULT_VISIBLE_KEYS;
  }
}

function saveVisibleColumns(keys: string[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(keys));
}

// --- Helper ---

const copyToClipboard = (text: string, successMessage = '已复制') => {
  // Try navigator.clipboard first (modern browsers, HTTPS)
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text)
      .then(() => message.success(successMessage))
      .catch((err) => {
        console.error('Clipboard API failed', err);
        fallbackCopy(text, successMessage);
      });
  } else {
    fallbackCopy(text, successMessage);
  }
};

const fallbackCopy = (text: string, successMessage: string) => {
  try {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    
    // Ensure it's not visible but part of the DOM
    textArea.style.position = "fixed";
    textArea.style.left = "-9999px";
    textArea.style.top = "0";
    
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    
    const successful = document.execCommand('copy');
    document.body.removeChild(textArea);
    
    if (successful) {
      message.success(successMessage);
    } else {
      message.error('复制失败，请手动复制');
    }
  } catch (err) {
    console.error('Fallback copy failed', err);
    message.error('复制失败');
  }
};

// --- Component ---

const emptyFilters = {
  chatId: '',
  userId: '',
  role: '',
  botName: '',
  rootMessageId: '',
  replyMessageId: '',
  messageType: '',
  range: [] as string[],
};

export default function Messages() {
  const [searchParams, setSearchParams] = useSearchParams();
  
  // Initialize filters from URL params
  const [filters, setFilters] = useState(() => {
    const initialFilters = { ...emptyFilters };
    if (searchParams.get('chatId')) initialFilters.chatId = searchParams.get('chatId')!;
    if (searchParams.get('userId')) initialFilters.userId = searchParams.get('userId')!;
    if (searchParams.get('role')) initialFilters.role = searchParams.get('role')!;
    if (searchParams.get('botName')) initialFilters.botName = searchParams.get('botName')!;
    if (searchParams.get('rootMessageId')) initialFilters.rootMessageId = searchParams.get('rootMessageId')!;
    if (searchParams.get('replyMessageId')) initialFilters.replyMessageId = searchParams.get('replyMessageId')!;
    if (searchParams.get('messageType')) initialFilters.messageType = searchParams.get('messageType')!;
    if (searchParams.get('startTime') && searchParams.get('endTime')) {
      initialFilters.range = [searchParams.get('startTime')!, searchParams.get('endTime')!];
    }
    return initialFilters;
  });

  const [data, setData] = useState<MessageRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(() => {
    const p = searchParams.get('page');
    return p ? parseInt(p, 10) : 1;
  });
  const [pageSize, setPageSize] = useState(() => {
    const ps = searchParams.get('pageSize');
    return ps ? parseInt(ps, 10) : 20;
  });
  const [total, setTotal] = useState(0);
  const [visibleKeys, setVisibleKeys] = useState<string[]>(loadVisibleColumns);

  // Define buildColumns inside component or use memo with handlers
  const columns = useMemo(() => {
    const visibleSet = new Set(visibleKeys);
    
    // Handler for clicking on Chat Name
    const handleChatClick = (chatId: string) => {
      const newFilters = { ...filters, chatId };
      setFilters(newFilters);
      // Reset page to 1 when filtering
      setPage(1);
      // Directly trigger fetch with new filters
      fetchData(1, pageSize, newFilters);
    };

    // Generic handler for ID filtering
    const handleIdFilter = (key: 'rootMessageId' | 'replyMessageId', id: string) => {
      const newFilters = { ...filters, [key]: id };
      setFilters(newFilters);
      setPage(1);
      fetchData(1, pageSize, newFilters);
    };

    const columnMap: Record<string, ColumnsType<MessageRow>[number]> = {
      message_id: {
        title: '消息 ID',
        dataIndex: 'message_id',
        width: 220,
        ellipsis: true,
        render: (text) => {
          if (!text) return '-';
          
          const content = (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
               <div>
                <strong>消息 ID:</strong> {text}
                <Button 
                  type="text" 
                  size="small" 
                  icon={<CopyOutlined />} 
                  onClick={() => copyToClipboard(text, '已复制消息 ID')} 
                />
              </div>
            </div>
          );

          return (
             <Popover content={content} title="消息信息" trigger="hover">
               <span>{text}</span>
            </Popover>
          );
        },
      },
      user_name: {
        title: '用户',
        dataIndex: 'user_name',
        width: 120,
        ellipsis: true,
        render: (text, record) => (
          <Tooltip title={`User ID: ${record.user_id}`}>
            <span>{text}</span>
          </Tooltip>
        ),
      },
      user_id: {
        title: '用户 ID',
        dataIndex: 'user_id',
        width: 160,
        ellipsis: true,
        render: (text) => <Text copyable>{text}</Text>,
      },
      role: {
        title: '角色',
        dataIndex: 'role',
        width: 80,
        render: (role: string) => {
          const map: Record<string, { color: string; text: string }> = {
            user: { color: 'blue', text: '用户' },
            assistant: { color: 'green', text: '助手' },
            system: { color: 'orange', text: '系统' },
          };
          const info = map[role] || { color: 'default', text: role };
          return <Tag color={info.color}>{info.text}</Tag>;
        },
      },
      chat_name: {
        title: '会话',
        dataIndex: 'chat_name',
        width: 180,
        ellipsis: true,
        render: (text, record) => {
          const content = (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div><strong>会话名称:</strong> {text}</div>
              <div>
                <strong>会话 ID:</strong> {record.chat_id}
                <Button 
                  type="text" 
                  size="small" 
                  icon={<CopyOutlined />} 
                  onClick={() => copyToClipboard(record.chat_id, '已复制会话 ID')} 
                />
              </div>
            </div>
          );
          
          return (
            <Popover content={content} title="会话信息" trigger="hover">
              <Button 
                type="text" 
                size="small"
                style={{ padding: '0 4px', fontSize: 'inherit', height: 'auto', lineHeight: 'inherit' }}
                onClick={() => handleChatClick(record.chat_id)}
              >
                {text}
              </Button>
            </Popover>
          );
        },
      },
      chat_id: {
        title: '会话 ID',
        dataIndex: 'chat_id',
        width: 180,
        ellipsis: true,
        render: (text) => <Text copyable>{text}</Text>,
      },
      chat_type: {
        title: '会话类型',
        dataIndex: 'chat_type',
        width: 100,
        render: (type: string) => {
          const label = chatTypeMap[type] || type;
          return <Tag color={type === 'group' ? 'cyan' : 'purple'}>{label}</Tag>;
        },
      },
      bot_name: {
        title: '机器人',
        dataIndex: 'bot_name',
        width: 120,
        render: (text) => text || '-',
      },
      content: {
        title: '内容',
        dataIndex: 'content',
        width: 300,
        render: (text: string) => {
          if (!text) return '-';
          let displayText = text;
          let itemsData = null;
          try {
            const parsed = typeof text === 'string' ? JSON.parse(text) : text;
            if (parsed && typeof parsed === 'object') {
              if (parsed.text) displayText = parsed.text;
              if (parsed.items) itemsData = parsed.items;
            }
          } catch (e) {
            // ignore error, use original text
          }

          const handleCopyItems = () => {
            if (itemsData) {
              copyToClipboard(JSON.stringify(itemsData, null, 2), '已复制 items 结构');
            }
          };

          const popoverContent = (
            <div style={{ maxWidth: 400, maxHeight: 300, overflow: 'auto' }}>
              <div style={{ marginBottom: 8, whiteSpace: 'pre-wrap' }}>{displayText}</div>
              {itemsData && (
                <Button size="small" icon={<CopyOutlined />} onClick={handleCopyItems}>
                  复制 Items 结构
                </Button>
              )}
            </div>
          );

          return (
            <Popover content={popoverContent} title="完整内容" trigger="hover">
              <div
                style={{
                  maxWidth: 400,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  cursor: 'pointer',
                }}
              >
                {displayText}
              </div>
            </Popover>
          );
        },
      },
      message_type: {
        title: '消息类型',
        dataIndex: 'message_type',
        width: 100,
        render: (type: string) => messageTypeMap[type] || type || '-',
      },
      root_message_id: {
        title: '根消息 ID',
        dataIndex: 'root_message_id',
        width: 220,
        ellipsis: true,
        render: (text) => {
          if (!text) return '-';
          
          const content = (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
               <div>
                <strong>根消息 ID:</strong> {text}
                <Button 
                  type="text" 
                  size="small" 
                  icon={<CopyOutlined />} 
                  onClick={() => copyToClipboard(text, '已复制根消息 ID')} 
                />
              </div>
            </div>
          );

          return (
             <Popover content={content} title="根消息信息" trigger="hover">
              <Button 
                type="text" 
                size="small"
                style={{ padding: '0 4px', fontSize: 'inherit', height: 'auto', lineHeight: 'inherit', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis' }}
                onClick={() => handleIdFilter('rootMessageId', text)}
              >
                {text}
              </Button>
            </Popover>
          );
        },
      },
      reply_message_id: {
        title: '父消息 ID',
        dataIndex: 'reply_message_id',
        width: 220,
        ellipsis: true,
        render: (text) => {
          if (!text) return '-';
          
          const content = (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
               <div>
                <strong>父消息 ID:</strong> {text}
                <Button 
                  type="text" 
                  size="small" 
                  icon={<CopyOutlined />} 
                  onClick={() => copyToClipboard(text, '已复制父消息 ID')} 
                />
              </div>
            </div>
          );

          return (
             <Popover content={content} title="父消息信息" trigger="hover">
              <Button 
                type="text" 
                size="small"
                style={{ padding: '0 4px', fontSize: 'inherit', height: 'auto', lineHeight: 'inherit', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis' }}
                onClick={() => handleIdFilter('replyMessageId', text)}
              >
                {text}
              </Button>
            </Popover>
          );
        },
      },
      create_time: {
        title: '创建时间',
        dataIndex: 'create_time',
        width: 180,
        render: (text) => (text ? dayjs(Number(text)).format('YYYY-MM-DD HH:mm:ss') : '-'),
      },
      vector_status: {
        title: '向量状态',
        dataIndex: 'vector_status',
        width: 100,
        render: (status: string) => {
          const info = vectorStatusMap[status] || { label: status, color: 'default' };
          return <Tag color={info.color}>{info.label}</Tag>;
        },
      },
    };

    return ALL_COLUMNS.filter((c) => visibleSet.has(c.key))
      .map((c) => columnMap[c.key])
      .filter(Boolean);
  }, [visibleKeys, filters]); // Depend on visibleKeys and filters for handleChatClick closure

  const updateURL = (currentFilters: typeof filters, currentPage: number, currentPageSize: number) => {
    const params: Record<string, string> = {};
    if (currentFilters.chatId) params.chatId = currentFilters.chatId;
    if (currentFilters.userId) params.userId = currentFilters.userId;
    if (currentFilters.role) params.role = currentFilters.role;
    if (currentFilters.botName) params.botName = currentFilters.botName;
    if (currentFilters.rootMessageId) params.rootMessageId = currentFilters.rootMessageId;
    if (currentFilters.replyMessageId) params.replyMessageId = currentFilters.replyMessageId;
    if (currentFilters.messageType) params.messageType = currentFilters.messageType;
    if (currentFilters.range.length === 2) {
      params.startTime = currentFilters.range[0];
      params.endTime = currentFilters.range[1];
    }
    
    // Also sync pagination to URL
    params.page = currentPage.toString();
    params.pageSize = currentPageSize.toString();
    
    setSearchParams(params);
  };

  const fetchData = async (pageOverride?: number, sizeOverride?: number, filtersOverride?: typeof filters) => {
    setLoading(true);
    try {
      const currentPage = pageOverride ?? page;
      const currentSize = sizeOverride ?? pageSize;
      const currentFilters = filtersOverride ?? filters;
      
      const params: Record<string, string | number> = {
        page: currentPage,
        pageSize: currentSize,
      };
      if (currentFilters.chatId) params.chatId = currentFilters.chatId;
      if (currentFilters.userId) params.userId = currentFilters.userId;
      if (currentFilters.role) params.role = currentFilters.role;
      if (currentFilters.botName) params.botName = currentFilters.botName;
      if (currentFilters.rootMessageId) params.rootMessageId = currentFilters.rootMessageId;
      if (currentFilters.replyMessageId) params.replyMessageId = currentFilters.replyMessageId;
      if (currentFilters.messageType) params.messageType = currentFilters.messageType;
      if (currentFilters.range.length === 2) {
        params.startTime = currentFilters.range[0];
        params.endTime = currentFilters.range[1];
      }

      // Sync to URL whenever we fetch data (Search click or Pagination change)
      updateURL(currentFilters, currentPage, currentSize);

      const { data: result } = await api.get('/messages', { params });
      setData(result.data);
      setTotal(result.total);
      setPage(result.page);
      setPageSize(result.pageSize);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData(page, pageSize);
    // We only run this on mount/initial load. 
    // Subsequent updates are triggered by Search button or Pagination.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleReset = () => {
    const newFilters = { ...emptyFilters };
    setFilters(newFilters);
    setSearchParams({}); // Clear URL
    setPage(1);
    
    // Call API with empty filters
    setLoading(true);
    api.get('/messages', { params: { page: 1, pageSize } }).then(({ data: result }) => {
      setData(result.data);
      setTotal(result.total);
      setPage(result.page);
      setPageSize(result.pageSize);
      setLoading(false);
    });
  };

  const handleColumnChange = (keys: string[]) => {
    setVisibleKeys(keys);
    saveVisibleColumns(keys);
  };

  const handleResetColumns = () => {
    setVisibleKeys(DEFAULT_VISIBLE_KEYS);
    saveVisibleColumns(DEFAULT_VISIBLE_KEYS);
  };

  const columnSettingsContent = (
    <div style={{ maxHeight: 360, overflow: 'auto' }}>
      <Checkbox.Group
        value={visibleKeys}
        onChange={(vals) => handleColumnChange(vals as string[])}
        style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
      >
        {ALL_COLUMNS.map((c) => (
          <Checkbox key={c.key} value={c.key}>
            {c.title}
          </Checkbox>
        ))}
      </Checkbox.Group>
      <div style={{ marginTop: 12, borderTop: '1px solid #f0f0f0', paddingTop: 8 }}>
        <Button type="link" size="small" onClick={handleResetColumns} style={{ padding: 0 }}>
          恢复默认
        </Button>
      </div>
    </div>
  );

  return (
    <div className="page-container">
      {/* Reduced margin-bottom from default 24px to 16px to decrease gap */}
      <div className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h1 className="page-title" style={{ margin: 0 }}>消息记录</h1>
        <Popover content={columnSettingsContent} title="列设置" trigger="click" placement="bottomRight">
          <Button icon={<SettingOutlined />}>列设置</Button>
        </Popover>
      </div>

      <div className="filter-card">
        <Row gutter={[12, 12]}>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Input
              placeholder="会话 ID"
              value={filters.chatId}
              onChange={(e) => setFilters((prev) => ({ ...prev, chatId: e.target.value }))}
              allowClear
            />
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Input
              placeholder="用户 ID"
              value={filters.userId}
              onChange={(e) => setFilters((prev) => ({ ...prev, userId: e.target.value }))}
              allowClear
            />
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Select
              placeholder="角色"
              value={filters.role || undefined}
              onChange={(value) => setFilters((prev) => ({ ...prev, role: value }))}
              allowClear
              style={{ width: '100%' }}
              suffixIcon={<CaretDownOutlined style={{ pointerEvents: 'none', fontSize: 12, color: '#aaa' }} />}
              options={[
                { value: 'user', label: '用户' },
                { value: 'assistant', label: '助手' },
                { value: 'system', label: '系统' },
              ]}
            />
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Input
              placeholder="机器人名称"
              value={filters.botName}
              onChange={(e) => setFilters((prev) => ({ ...prev, botName: e.target.value }))}
              allowClear
            />
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Input
              placeholder="根消息 ID"
              value={filters.rootMessageId}
              onChange={(e) => setFilters((prev) => ({ ...prev, rootMessageId: e.target.value }))}
              allowClear
            />
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Input
              placeholder="父消息 ID"
              value={filters.replyMessageId}
              onChange={(e) => setFilters((prev) => ({ ...prev, replyMessageId: e.target.value }))}
              allowClear
            />
          </Col>
          
          {/* Second Row */}
          <Col xs={12} sm={8} md={6} lg={4}>
            <Select
              placeholder="消息类型"
              value={filters.messageType || undefined}
              onChange={(value) => setFilters((prev) => ({ ...prev, messageType: value }))}
              allowClear
              style={{ width: '100%' }}
              suffixIcon={<CaretDownOutlined style={{ pointerEvents: 'none', fontSize: 12, color: '#aaa' }} />}
              options={messageTypeFilterOptions}
            />
          </Col>
          <Col xs={24} sm={16} md={12} lg={6}>
             <DatePicker.RangePicker
              style={{ width: '100%' }}
              onChange={(dates) => {
                if (!dates) {
                  setFilters((prev) => ({ ...prev, range: [] }));
                  return;
                }
                setFilters((prev) => ({
                  ...prev,
                  range: [dates[0]!.valueOf().toString(), dates[1]!.valueOf().toString()],
                }));
              }}
              showTime
              value={
                filters.range.length === 2
                  ? [dayjs(Number(filters.range[0])), dayjs(Number(filters.range[1]))]
                  : null
              }
            />
          </Col>
          
          {/* Buttons aligned to the right, filling the remaining space */}
          <Col xs={24} sm={24} md={6} lg={14} style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Space>
              <Button type="primary" icon={<SearchOutlined />} onClick={() => fetchData(1, pageSize)}>
                查询
              </Button>
              <Button icon={<ReloadOutlined />} onClick={handleReset}>
                重置
              </Button>
            </Space>
          </Col>
        </Row>
      </div>

      <div className="content-card">
        <Table
          rowKey="message_id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (nextPage, nextPageSize) => {
              fetchData(nextPage, nextPageSize);
            },
          }}
          scroll={{ x: 1200 }}
          size="middle"
        />
      </div>
    </div>
  );
}
