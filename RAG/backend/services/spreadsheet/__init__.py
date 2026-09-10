"""表格（Excel / CSV）读取与预览

模块结构（一类职责一个文件）：
- reader.py: 统一入口 + 公共基础（Sheet / read_spreadsheet / 合并单元格填充 / 数值净化）
- csv.py: CSV 读取器（标准库 csv，零依赖）
- xlsx.py: xlsx 读取器（openpyxl，值直读）
- xls.py: xls 读取器（老 BIFF 格式，xlrd）
- formula.py: Excel 公式计算引擎（formulas 封装 + 磁盘缓存）
- preview.py: 类 Excel 原始观感 HTML 预览

调用方按具体子模块导入（如 `from backend.services.spreadsheet.reader
import read_spreadsheet`）；包内同名子模块（csv/xls/xlsx）与标准库重名不冲突
（Python 3 绝对导入，包内 `import csv` 仍取标准库）。
"""
