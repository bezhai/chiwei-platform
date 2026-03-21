import { ThemeConfig } from 'antd';

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: '#000000', // Modern monochrome primary
    colorSuccess: '#10b981',
    colorWarning: '#f59e0b',
    colorError: '#ef4444',
    colorInfo: '#3b82f6',
    borderRadius: 6, // Slightly sharper for modern feel, but components can be larger
    borderRadiusLG: 12,
    fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
    fontSize: 14,
    colorBgLayout: '#fafafa', // Lighter background for a cleaner canvas
    colorTextBase: '#0f172a',
    colorBorder: '#e2e8f0', // Lighter borders
    colorBorderSecondary: '#f1f5f9',
    wireframe: false,
    boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03)',
  },
  components: {
    Layout: {
      siderBg: '#ffffff',
      headerBg: 'rgba(255, 255, 255, 0.8)',
      bodyBg: '#fafafa',
    },
    Menu: {
      itemSelectedBg: '#f1f5f9', // Subtler selection
      itemSelectedColor: '#0f172a',
      itemColor: '#64748b',
      itemHoverBg: '#f8fafc',
      itemBorderRadius: 8,
      itemMarginInline: 12, // Give menu items some breathing room from the edges
    },
    Card: {
      boxShadowTertiary: '0 1px 2px 0 rgba(0, 0, 0, 0.05)',
      paddingLG: 24,
      borderRadiusLG: 16, // Softer cards
      colorBorderSecondary: '#e2e8f0', // Define card border specifically
    },
    Typography: {
      titleMarginBottom: 0,
      titleMarginTop: 0,
    },
    Table: {
      headerBg: '#f8fafc',
      headerColor: '#475569',
      headerBorderRadius: 8,
      borderColor: '#f1f5f9',
      rowHoverBg: '#f8fafc',
      cellPaddingBlock: 16, // More breathing room in rows
    },
    Button: {
      borderRadius: 6,
      controlHeight: 36, // Slightly taller default buttons
      paddingInline: 16,
      defaultShadow: 'none',
      primaryShadow: '0 2px 4px rgba(0,0,0,0.1)',
    },
    Input: {
      activeBorderColor: '#000000',
      hoverBorderColor: '#94a3b8',
      colorBorder: '#cbd5e1',
      paddingBlock: 6,
    },
    Select: {
      colorPrimaryHover: '#94a3b8',
      colorPrimary: '#000000',
    },
    Tag: {
      borderRadiusSM: 4,
      lineHeight: 2,
    }
  }
};
