"""
文件文本提取器
v0.2.1 优化：使用 pandas.head(100) 进行表格行级截断，避免破损的 CSV 破坏大模型解析格式
"""
import io
import csv
from pathlib import Path

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
            results.append(f"\n--- Sheet: {sheet} (共{len(df)}行) ---\n{csv_str}")
        return "\n".join(results)

    @staticmethod
    async def _handle_csv(file_name: str, file_bytes: bytes) -> str:
        text = file_bytes.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if len(rows) > 200:
            text = "\n".join([",".join(r) for r in rows[:200]])
            text += f"\n...(共{len(rows)}行，已截断并保留前200行)"
        else:
            text = "\n".join([",".join(r) for r in rows])
        return f"文件: {file_name}\n{text}"

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
