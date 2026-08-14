import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider, App as AntApp } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import 'antd/dist/reset.css';
import './index.css';

import App from './App';
import { ThemeProvider, useTheme, buildTheme } from './theme';

dayjs.locale('zh-cn');

/** 主题入口：跟随 ThemeContext 当前预设构建 ConfigProvider 主题（业务组件零感知） */
const ThemedRoot: React.FC = () => {
  const { preset } = useTheme();
  return (
    <ConfigProvider locale={zhCN} theme={buildTheme(preset)}>
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  );
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <ThemedRoot />
    </ThemeProvider>
  </React.StrictMode>,
);
