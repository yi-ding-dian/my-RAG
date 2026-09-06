import React from 'react';
import { Alert, Button, Space } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * 全局错误边界（P1-7 前端产品化优先级第一项）：
 * - 捕获页面渲染异常（React render/lifecycle），fallback 中文友好页
 *   + 刷新按钮，不再白屏/整站不可用；
 * - 仅捕获渲染期，事件回调错误仍交由 window.onerror（antd 已兜底提示）。
 */
class ErrorBoundary extends React.Component<
  React.PropsWithChildren,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // 生产可替换为上报（如 /api/audit 或 Sentry）；当前仅控制台留痕
    console.error('[ErrorBoundary]', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            minHeight: '100vh',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 24,
          }}
        >
          <Alert
            type="error"
            showIcon
            style={{ maxWidth: 560 }}
            message="页面渲染出现异常"
            description={
              <Space direction="vertical" size={8}>
                <span>
                  我们已拦截该错误（{this.state.error.message}），不影响其他
                  页面；如频繁出现请联系管理员排查。
                </span>
                <Button
                  icon={<ReloadOutlined />}
                  type="primary"
                  size="small"
                  onClick={() => window.location.reload()}
                >
                  刷新页面
                </Button>
              </Space>
            }
          />
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
