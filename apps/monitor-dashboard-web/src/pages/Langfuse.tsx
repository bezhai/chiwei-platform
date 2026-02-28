import { useEffect, useState } from 'react';
import { Button, Spin, Result } from 'antd';
import { LinkOutlined } from '@ant-design/icons';
import { api } from '../api/client';

export default function Langfuse() {
  const [url, setUrl] = useState('');

  useEffect(() => {
    const fetchConfig = async () => {
      const { data } = await api.get('/config');
      setUrl(data.langfuseUrl || '');
    };
    fetchConfig();
  }, []);

  if (!url) {
    return (
      <div className="page-container" style={{ display: 'flex', justifyContent: 'center', paddingTop: 100 }}>
        <Spin size="large" />
      </div>
    );
  }

  return (
    <div className="page-container">
      <div className="page-header">
        <h1 className="page-title">Langfuse 追踪</h1>
      </div>
      <div className="content-card" style={{ padding: '48px 24px', display: 'flex', justifyContent: 'center' }}>
        <Result
          status="info"
          title="访问 Langfuse 控制台"
          subTitle="Langfuse 不支持嵌入访问，请点击下方按钮在新窗口中打开。"
          extra={
            <Button
              type="primary"
              icon={<LinkOutlined />}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              size="large"
            >
              打开 Langfuse
            </Button>
          }
        />
      </div>
    </div>
  );
}
