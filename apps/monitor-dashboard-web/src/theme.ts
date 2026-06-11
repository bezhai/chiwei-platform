import { ThemeConfig } from 'antd';

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: '#286f5a',
    colorSuccess: '#227a52',
    colorWarning: '#a9661b',
    colorError: '#b64a3a',
    colorInfo: '#3f6876',
    borderRadius: 4,
    borderRadiusLG: 6,
    fontFamily: "'IBM Plex Sans', 'PingFang SC', 'Microsoft YaHei', sans-serif",
    fontFamilyCode: "'IBM Plex Mono', 'JetBrains Mono', 'Menlo', 'Monaco', monospace",
    fontSize: 14,
    controlHeight: 34,
    colorBgLayout: '#f3f5f1',
    colorBgContainer: '#fbfcf8',
    colorBgElevated: '#fbfcf8',
    colorTextBase: '#17201b',
    colorTextSecondary: '#68746b',
    colorBorder: '#d5dcd5',
    colorBorderSecondary: '#e5e9e4',
    wireframe: false,
    boxShadow: '0 1px 0 rgba(23, 32, 27, 0.08)',
    boxShadowSecondary: '0 1px 0 rgba(23, 32, 27, 0.08)',
  },
  components: {
    Layout: {
      siderBg: '#151b17',
      headerBg: '#f3f5f1',
      bodyBg: '#f3f5f1',
    },
    Menu: {
      itemSelectedBg: '#d9e4dc',
      itemSelectedColor: '#12241c',
      itemColor: '#5f6b63',
      itemHoverBg: '#edf1ec',
      itemBorderRadius: 4,
      itemMarginInline: 8,
      darkItemBg: '#151b17',
      darkItemColor: '#a8b3ab',
      darkItemHoverBg: '#223027',
      darkItemHoverColor: '#f7f8f4',
      darkItemSelectedBg: '#d9e4dc',
      darkItemSelectedColor: '#12241c',
    },
    Card: {
      boxShadowTertiary: 'none',
      paddingLG: 20,
      borderRadiusLG: 6,
      colorBorderSecondary: '#d5dcd5',
    },
    Typography: {
      titleMarginBottom: 0,
      titleMarginTop: 0,
    },
    Table: {
      headerBg: '#e9ede8',
      headerColor: '#4d5a51',
      headerBorderRadius: 4,
      borderColor: '#e1e6df',
      rowHoverBg: '#f0f3ef',
      cellPaddingBlock: 12,
    },
    Button: {
      borderRadius: 4,
      controlHeight: 34,
      paddingInline: 16,
      defaultShadow: 'none',
      primaryShadow: 'none',
    },
    Input: {
      activeBorderColor: '#286f5a',
      hoverBorderColor: '#9aa69d',
      colorBorder: '#cfd8d0',
      paddingBlock: 6,
    },
    Select: {
      colorPrimaryHover: '#286f5a',
      colorPrimary: '#286f5a',
    },
    Tag: {
      borderRadiusSM: 3,
      lineHeight: 2,
    }
  }
};
