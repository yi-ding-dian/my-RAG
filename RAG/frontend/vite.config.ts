import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3002,
    proxy: {
      '/api': {
        target: 'http://localhost:8091',
        changeOrigin: true,
      },
    },
  },
  build: {
    // AntD 组件库体积大（含全量组件），gzip 后约 400KB，放宽警告阈值避免构建噪音
    chunkSizeWarningLimit: 1400,
    rollupOptions: {
      output: {
        // 代码分割：框架/vendor、AntD 组件库、图标库各成独立 chunk，页面本身走路由 lazy
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom', 'dayjs', 'axios'],
          antd: ['antd'],
          icons: ['@ant-design/icons'],
        },
      },
    },
  },
});
