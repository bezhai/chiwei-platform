import { useEffect, useState } from 'react';
import { Spin } from 'antd';
import { api, getToken } from '../api/client';

export default function Kibana() {
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const fetchConfig = async () => {
      try {
        const { data } = await api.get('/config');
        setReady(Boolean(data.grafanaUrl));
      } finally {
        setLoading(false);
      }
    };
    fetchConfig();
  }, []);

  if (loading) {
    return (
      <div className="page-container" style={{ display: 'flex', justifyContent: 'center', paddingTop: 100 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!ready) {
    return null;
  }

  return (
    <div className="page-container">
      <div className="page-header">
        <h1 className="page-title">Grafana</h1>
      </div>
      <div className="iframe-container" style={{ height: 'calc(100vh - 180px)', minHeight: 600 }}>
        <iframe
          src={`/dashboard/api/grafana/?dashboard_token=${encodeURIComponent(getToken())}`}
          title="Grafana"
          style={{ border: 0, width: '100%', height: '100%' }}
        />
      </div>
    </div>
  );
}
