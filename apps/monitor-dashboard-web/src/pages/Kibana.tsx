import { useEffect, useState } from 'react';
import { Spin } from 'antd';
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
      <div className="iframe-container" style={{ height: 'calc(100vh - 180px)', minHeight: 600 }}>
        <iframe src={url} title="Grafana" style={{ border: 0, width: '100%', height: '100%' }} />
      </div>
    </div>
  );
}
