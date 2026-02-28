import { useEffect, useState } from 'react';
import { Button, Input, Select, message, Spin, Pagination, Tooltip } from 'antd';
import { SearchOutlined, ReloadOutlined, SortAscendingOutlined, SortDescendingOutlined, FilterOutlined } from '@ant-design/icons';
import { api } from '../api/client';

interface RowData {
  _id: string;
  [key: string]: unknown;
}

const directionOptions = [
  { value: '1', label: 'Asc' },
  { value: '-1', label: 'Desc' },
];

// JSON Viewer Component for syntax highlighting
const JsonViewer = ({ data, level = 0 }: { data: unknown; level?: number }) => {
  if (data === null) return <span style={{ color: '#777' }}>null</span>;
  if (data === undefined) return <span style={{ color: '#777' }}>undefined</span>;
  
  if (typeof data === 'string') {
    // Check if it looks like an ObjectId (24 hex chars) but passed as string, though usually _id key handles it.
    return <span style={{ color: '#50a14f' }}>"{data}"</span>;
  }
  if (typeof data === 'number') return <span style={{ color: '#098658' }}>{data}</span>;
  if (typeof data === 'boolean') return <span style={{ color: '#0000ff' }}>{String(data)}</span>;

  if (Array.isArray(data)) {
    if (data.length === 0) return <span>[]</span>;
    return (
      <span>
        [
        <div style={{ paddingLeft: '1.5em' }}>
          {data.map((item, index) => (
            <div key={index}>
              <JsonViewer data={item} level={level + 1} />
              {index < data.length - 1 && ','}
            </div>
          ))}
        </div>
        {level > 0 ? ']' : ']'}
      </span>
    );
  }

  if (typeof data === 'object') {
    const keys = Object.keys(data as object);
    if (keys.length === 0) return <span>{'{'}{'}'}</span>;
    return (
      <span>
        {'{'}
        <div style={{ paddingLeft: '1.5em' }}>
          {keys.map((key, index) => {
            const value = (data as any)[key];
            const isId = key === '_id';
            return (
              <div key={key}>
                <span style={{ color: '#000', fontWeight: 500 }}>"{key}"</span>: 
                {' '}
                {isId ? (
                   <span style={{ color: '#e45649' }}>"{String(value)}"</span>
                ) : (
                   <JsonViewer data={value} level={level + 1} />
                )}
                {index < keys.length - 1 && ','}
              </div>
            );
          })}
        </div>
        {'}'}
      </span>
    );
  }

  return <span>{String(data)}</span>;
};

export default function MongoExplorer() {
  const [filter, setFilter] = useState('{}');
  const [projection, setProjection] = useState('');
  const [sortField, setSortField] = useState('created_at');
  const [sortDirection, setSortDirection] = useState('-1');
  const [data, setData] = useState<RowData[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [expandedFilter, setExpandedFilter] = useState(false);

  const buildProjection = () => {
    if (!projection.trim()) {
      return {};
    }
    return projection
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
      .reduce<Record<string, number>>((acc, key) => {
        acc[key] = 1;
        return acc;
      }, {});
  };

  const fetchData = async (pageOverride?: number, sizeOverride?: number) => {
    setLoading(true);
    try {
      const parsedFilter = filter.trim() ? JSON.parse(filter) : {};
      const payload = {
        filter: parsedFilter,
        projection: buildProjection(),
        sort: sortField ? { [sortField]: Number(sortDirection) } : { created_at: -1 },
        page: pageOverride ?? page,
        pageSize: sizeOverride ?? pageSize,
      };
      const { data } = await api.post('/mongo/query', payload);
      const rows = data.data as RowData[];
      setData(rows);
      setTotal(data.total);
      setPage(data.page);
      setPageSize(data.pageSize);
    } catch (error) {
      message.error('Query failed: Invalid JSON or network error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData(1, pageSize);
  }, []);

  const handleRefresh = () => fetchData(page, pageSize);

  return (
    <div className="mongo-explorer-container">
      {/* Header / Toolbar */}
      <div className="explorer-header">
        <div className="header-left">
           <h2 className="explorer-title">Data Explorer</h2>
           <span className="collection-badge">lark_event</span>
           <span className="total-count">{total.toLocaleString()} documents</span>
        </div>
        <div className="header-right">
           <Button 
             type="text" 
             icon={<ReloadOutlined />} 
             onClick={handleRefresh}
             className="icon-btn"
           />
           <Button 
             type={expandedFilter ? 'primary' : 'default'}
             shape="round"
             icon={<FilterOutlined />} 
             onClick={() => setExpandedFilter(!expandedFilter)}
           >
             Filter
           </Button>
        </div>
      </div>

      {/* Filter Bar - Collapsible or Inline */}
      {expandedFilter && (
        <div className="filter-panel">
           <div className="filter-group">
              <label>Query Filter (JSON)</label>
              <Input.TextArea 
                value={filter} 
                onChange={e => setFilter(e.target.value)} 
                autoSize={{ minRows: 2, maxRows: 6 }}
                className="code-input"
                placeholder='{"status": "success"}'
              />
           </div>
           <div className="filter-row">
             <div className="filter-group small">
                <label>Projection</label>
                <Input 
                  value={projection} 
                  onChange={e => setProjection(e.target.value)} 
                  placeholder="field1, field2" 
                />
             </div>
             <div className="filter-group small">
                <label>Sort By</label>
                <Input 
                  value={sortField} 
                  onChange={e => setSortField(e.target.value)} 
                  placeholder="created_at" 
                />
             </div>
             <div className="filter-group small">
                <label>Order</label>
                <Select 
                  value={sortDirection} 
                  onChange={setSortDirection} 
                  options={directionOptions} 
                  style={{ width: '100%' }}
                />
             </div>
             <div className="filter-actions">
               <Button type="primary" shape="round" icon={<SearchOutlined />} onClick={() => fetchData(1, pageSize)}>
                 Apply
               </Button>
             </div>
           </div>
        </div>
      )}

      {/* Main Content Area */}
      <div className="documents-list">
        {loading ? (
          <div className="loading-state">
            <Spin size="large" />
          </div>
        ) : (
          data.map((item) => (
            <div key={item._id} className="document-card">
              <div className="document-content">
                <JsonViewer data={item} />
              </div>
            </div>
          ))
        )}
      </div>

      {/* Footer / Pagination */}
      <div className="explorer-footer">
         <Pagination 
           current={page} 
           pageSize={pageSize} 
           total={total}
           showSizeChanger 
           onChange={(p, s) => fetchData(p, s)}
           showTotal={(total) => `Total ${total} items`}
         />
      </div>
    </div>
  );
}
