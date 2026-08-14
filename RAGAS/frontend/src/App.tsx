import React, { useState } from 'react';
import { BrowserRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu, Typography, theme } from 'antd';
import {
  DatabaseOutlined,
  ThunderboltOutlined,
  BarChartOutlined,
  EditOutlined,
  SettingOutlined,
  FileTextOutlined,
  ApartmentOutlined,
} from '@ant-design/icons';

import DatasetsPage from './pages/Datasets';
import EvaluatePage from './pages/Evaluate';
import ResultsPage from './pages/Results';
import DataEditorPage from './pages/DataEditor';
import SettingsPage from './pages/Settings';
import PromptsPage from './pages/Prompts';
import FlowPage from './pages/Flow';

const { Header, Sider, Content } = Layout;

const menuItems = [
  { key: '/settings', icon: <SettingOutlined />, label: '模型配置' },
  { key: '/datasets', icon: <DatabaseOutlined />, label: '数据集管理' },
  { key: '/data-editor', icon: <EditOutlined />, label: '数据编辑' },
  { key: '/prompts', icon: <FileTextOutlined />, label: '提示词管理' },
  { key: '/flow', icon: <ApartmentOutlined />, label: '工作流程' },
  { key: '/evaluate', icon: <ThunderboltOutlined />, label: '执行评估' },
  { key: '/results', icon: <BarChartOutlined />, label: '评估结果' },
];

const AppLayout: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = theme.useToken();

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={220} style={{ borderRight: `1px solid ${token.colorBorderSecondary}` }}>
        <div style={{ padding: '20px 24px', borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            RAGAS 评估系统
          </Typography.Title>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
          style={{ borderRight: 0, marginTop: 8 }}
        />
      </Sider>
      <Layout>
        <Content style={{ padding: 24, background: token.colorBgContainer }}>
          <Routes>
            <Route path="/" element={<DatasetsPage />} />
            <Route path="/datasets" element={<DatasetsPage />} />
            <Route path="/data-editor" element={<DataEditorPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/prompts" element={<PromptsPage />} />
            <Route path="/flow" element={<FlowPage />} />
            <Route path="/evaluate" element={<EvaluatePage />} />
            <Route path="/results" element={<ResultsPage />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
};

const App: React.FC = () => (
  <BrowserRouter>
    <AppLayout />
  </BrowserRouter>
);

export default App;
