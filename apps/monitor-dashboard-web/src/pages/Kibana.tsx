import { useEffect, useState } from 'react';
import { Button, Result, Spin } from 'antd';
import { LinkOutlined } from '@ant-design/icons';
import { api } from '../api/client';

export default function Kibana() {
  const [url, setUrl] = useState('');

  useEffect(() => {
    const fetchConfig = async () => {
      const { data } = await api.get('/config');
      setUrl(data.grafanaUrl || '');
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
        <h1 className="page-title">Grafana</h1>
      </div>
      <div className="content-card" style={{ padding: '48px 24px', display: 'flex', justifyContent: 'center' }}>
        <Result
          status="info"
          title="Grafana 需要在新窗口中打开"
          subTitle="当前 Grafana 返回 X-Frame-Options: deny，浏览器会阻止嵌入访问。"
          extra={
            <Button
              type="primary"
              icon={<LinkOutlined />}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              size="large"
            >
              打开 Grafana
            </Button>
          }
        />
      </div>
    </div>
  );
}
