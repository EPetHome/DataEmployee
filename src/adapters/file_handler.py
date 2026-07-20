"""
文件文本提取器
v0.2.1 优化：使用 pandas.head(100) 进行表格行级截断，避免破损的 CSV 破坏大模型解析格式
v0.4.0 优化：对表格文件附加 [系统预计算汇总]——数值列合计/分组统计由代码精确计算，
             大模型只引用不心算，根治 LLM 算术不可靠导致的账目金额错误。
"""
import io
import csv
from pathlib import Path

# 这类列求和无业务意义，从汇总中剔除
_SKIP_COL_KEYWORDS = ("序号", "编号", "行号", "id", "no.", "no ")
# 这类列作为分组维度无业务意义（日期粒度太散、备注为自由文本）
_SKIP_GROUP_KEYWORDS = ("日期", "时间", "备注", "说明", "摘要", "date", "time", "remark")

SUMMARY_HEADER = "[系统预计算汇总｜以下数字由代码精确计算，回答金额时必须直接引用，禁止自行加总]"

class FileHandler:
    @staticmethod
    async def process(file_name: str, file_bytes: bytes) -> str:
        ext = Path(file_name).suffix.lower()
        handlers = {
            ".xlsx": FileHandler._handle_excel,
            ".xls":  FileHandler._handle_excel,
            ".csv":  FileHandler._handle_csv,
            ".pdf":  FileHandler._handle_pdf,
            ".txt":  FileHandler._handle_text,
        }
        handler = handlers.get(ext)
        if not handler:
            return f"[不支持该文件类型: {ext}]"
        try:
            return await handler(file_name, file_bytes)
        except Exception as e:
            return f"[文件解析失败: {str(e)}]"

    @staticmethod
    def _summarize_dataframe(df) -> str:
        """对 DataFrame 数值列生成精确统计（合计/笔数/均值/极值 + 按维度分组合计）。"""
        import pandas as pd

        def _skip(col_name) -> bool:
            name = str(col_name).lower()
            return any(k in name for k in _SKIP_COL_KEYWORDS)

        numeric_cols = [
            c for c in df.columns
            if pd.api.types.is_numeric_dtype(df[c]) and not _skip(c)
        ]
        if not numeric_cols:
            return ""

        lines = []
        for col in numeric_cols:
            s = df[col].dropna()
            if s.empty:
                continue
            lines.append(
                f"- 「{col}」列: 合计={s.sum():,.2f}, 笔数={int(s.count())}, "
                f"均值={s.mean():,.2f}, 最大={s.max():,.2f}, 最小={s.min():,.2f}"
            )

        def _is_text(col_name) -> bool:
            # 兼容 pandas 2.x (object dtype) 与 3.x (专用 str dtype)
            s = df[col_name]
            return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)

        def _skip_group(col_name) -> bool:
            name = str(col_name).lower()
            return _skip(col_name) or any(k in name for k in _SKIP_GROUP_KEYWORDS)

        # 低基数文本列视为分组维度（报销人/部门/费用类别等）
        cat_cols = [
            c for c in df.columns
            if _is_text(c) and 1 < df[c].nunique(dropna=True) <= 15 and not _skip_group(c)
        ]
        for cat in cat_cols[:4]:
            for num in numeric_cols[:2]:
                grouped = df.groupby(cat, dropna=True)[num].agg(["sum", "count"])
                parts = [
                    f"{idx}: {row['sum']:,.2f}({int(row['count'])}笔)"
                    for idx, row in grouped.iterrows()
                ]
                lines.append(f"- 按「{cat}」分组的「{num}」: " + "; ".join(parts))

        if not lines:
            return ""
        return f"\n{SUMMARY_HEADER}\n" + "\n".join(lines)

    @staticmethod
    async def _handle_excel(file_name: str, file_bytes: bytes) -> str:
        import pandas as pd
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        results = [f"文件: {file_name}"]
        for sheet in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name=sheet, header=None)
            if len(df) > 100:
                truncated_df = df.head(100)
                csv_str = truncated_df.to_csv(index=False, encoding="utf-8")
                csv_str += f"\n...(共 {len(df)} 行，为了避免上下文超出已自动截断，当前仅展示前 100 行)..."
            else:
                csv_str = df.to_csv(index=False, encoding="utf-8")

            # 汇总基于首行为表头的完整数据计算（不受 100 行展示截断影响）
            try:
                summary = FileHandler._summarize_dataframe(
                    pd.read_excel(xl, sheet_name=sheet)
                )
            except Exception:
                summary = ""
            results.append(f"\n--- Sheet: {sheet} (共{len(df)}行) ---\n{csv_str}{summary}")
        return "\n".join(results)

    @staticmethod
    async def _handle_csv(file_name: str, file_bytes: bytes) -> str:
        text = file_bytes.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if len(rows) > 200:
            body = "\n".join([",".join(r) for r in rows[:200]])
            body += f"\n...(共{len(rows)}行，已截断并保留前200行)"
        else:
            body = "\n".join([",".join(r) for r in rows])

        summary = ""
        try:
            import pandas as pd
            df = pd.read_csv(io.StringIO(text))
            summary = FileHandler._summarize_dataframe(df)
        except Exception:
            pass
        return f"文件: {file_name}\n{body}{summary}"

    @staticmethod
    async def _handle_pdf(file_name: str, file_bytes: bytes) -> str:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        results = [f"文件: {file_name} (共{len(doc)}页)"]
        for i, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                if len(text) > 2000:
                    text = text[:2000] + "..."
                results.append(f"\n--- 第{i+1}页 ---\n{text}")
        doc.close()
        return "\n".join(results)

    @staticmethod
    async def _handle_text(file_name: str, file_bytes: bytes) -> str:
        text = file_bytes.decode("utf-8", errors="replace")
        if len(text) > 3000:
            text = text[:3000] + "...(已截断)"
        return f"文件: {file_name}\n{text}"
