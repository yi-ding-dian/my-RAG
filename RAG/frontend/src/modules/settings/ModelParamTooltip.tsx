import React from 'react';
import { Tooltip, theme } from 'antd';

/** 参数表的一行：[标签, 值] */
export type ParamRow = [string, React.ReactNode];

/** 两列对齐的参数表：信息行放不下时，悬浮用它把参数摆全 */
const TABLE_STYLE: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'auto auto',
  justifyContent: 'start',
  columnGap: 16,
  rowGap: 2,
  fontSize: 12,
  lineHeight: '18px',
};

/**
 * 模型卡片的信息行（API 地址 + Key + 生成参数）。
 *
 * 单行显示、放不下就截断；悬浮给出两列对齐的完整参数表——被省略号吃掉的部分
 * 在这里看全。只包住这行文字本身，不含 radio / 操作按钮：鼠标移到它们上面时
 * 这个提示要跟着收起来，免得盖住按钮自己的 Tooltip。
 */
const ModelParamTooltip: React.FC<{ rows: ParamRow[]; text: string }> = ({ rows, text }) => {
  const { token } = theme.useToken();
  return (
    <Tooltip
      mouseEnterDelay={0.4}
      overlayStyle={{ maxWidth: 460 }}
      title={
        <div style={TABLE_STYLE}>
          {rows.map(([label, value]) => (
            <React.Fragment key={label}>
              <span style={{ opacity: 0.75 }}>{label}</span>
              <span>{value}</span>
            </React.Fragment>
          ))}
        </div>
      }
    >
      <div style={{
        fontSize: 12, color: token.colorTextTertiary,
        overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }}>
        {text}
      </div>
    </Tooltip>
  );
};

export default ModelParamTooltip;
