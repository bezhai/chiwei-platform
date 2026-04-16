import { ThemeConfig } from 'antd';

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: '#0f766e',
    colorSuccess: '#10b981',
    colorWarning: '#d97706',
    colorError: '#ef4444',
    colorInfo: '#2563eb',
    borderRadius: 12,
    borderRadiusLG: 24,
    fontFamily: "'Avenir Next', 'Segoe UI Variable Display', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
    fontSize: 14,
    colorBgLayout: '#f6f0e4',
    colorBgContainer: '#fffdf8',
    colorTextBase: '#172033',
    colorBorder: '#e7dcc7',
    colorBorderSecondary: '#f0e7d8',
    wireframe: false,
    boxShadow: '0 20px 50px rgba(23, 32, 51, 0.08)',
  },
  components: {
    Layout: {
      siderBg: 'rgba(255, 251, 243, 0.88)',
      headerBg: 'rgba(255, 250, 242, 0.88)',
      bodyBg: 'transparent',
    },
    Menu: {
      itemSelectedBg: 'rgba(15, 118, 110, 0.12)',
      itemSelectedColor: '#0f172a',
      itemColor: '#5f6573',
      itemHoverBg: 'rgba(23, 32, 51, 0.04)',
      itemBorderRadius: 14,
      itemMarginInline: 12,
      itemHeight: 44,
      activeBarWidth: 0,
    },
    Card: {
      boxShadowTertiary: '0 18px 35px rgba(23, 32, 51, 0.06)',
      paddingLG: 24,
      borderRadiusLG: 24,
      colorBorderSecondary: '#ede2d0',
    },
    Typography: {
      titleMarginBottom: 0,
      titleMarginTop: 0,
    },
    Table: {
      headerBg: '#f5efe2',
      headerColor: '#6b5f4f',
      headerBorderRadius: 18,
      borderColor: '#f0e7d8',
      rowHoverBg: '#fffcf4',
      cellPaddingBlock: 18,
    },
    Button: {
      borderRadius: 999,
      controlHeight: 40,
      paddingInline: 18,
      defaultShadow: 'none',
      primaryShadow: '0 12px 24px rgba(15, 118, 110, 0.18)',
    },
    Input: {
      activeBorderColor: '#0f766e',
      hoverBorderColor: '#9da4b0',
      colorBorder: '#d7cbb9',
      paddingBlock: 6,
    },
    Select: {
      colorPrimaryHover: '#0f766e',
      colorPrimary: '#0f766e',
    },
    Tag: {
      borderRadiusSM: 999,
      lineHeight: 2,
    },
    Drawer: {
      colorBgElevated: 'rgba(255, 251, 243, 0.96)',
    },
  },
};
