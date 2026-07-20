"""
表格数据仓（Text-to-SQL 底座）
v0.4.0 新增：上传的 Excel/CSV 按用户入库到内存 SQLite，供大模型通过
query_table 工具执行只读 SQL 精确查询——彻底取代"心算加总"，
并支持多文件跨表 JOIN 勾稽对账。
"""
import io
import re
import sqlite3
import threading
from pathlib import Path

TABULAR_EXTS = {".xlsx", ".xls", ".csv"}
MAX_TABLES_PER_USER = 12
MAX_RESULT_ROWS = 50
# 只读防线第二层：语句中出现任何写操作关键词直接拒绝（第一层是 PRAGMA query_only）
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)

class TabularStore:
    """单个用户的表格数据仓。所有同步操作需经 asyncio.to_thread 调用。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.manifest: list[dict] = []
        self._counter = 0
        self._lock = threading.Lock()

    def ingest_file(self, file_name: str, file_bytes: bytes) -> list[dict]:
        """把一个表格文件的所有 sheet 入库，返回新增的表清单。"""
        import pandas as pd

        ext = Path(file_name).suffix.lower()
        frames: list[tuple[str, "pd.DataFrame"]] = []
        if ext in (".xlsx", ".xls"):
            xl = pd.ExcelFile(io.BytesIO(file_bytes))
            for sheet in xl.sheet_names:
                df = pd.read_excel(xl, sheet_name=sheet)
                if not df.empty:
                    frames.append((sheet, df))
        elif ext == ".csv":
            df = pd.read_csv(io.BytesIO(file_bytes))
            if not df.empty:
                frames.append(("csv", df))

        added = []
        with self._lock:
            self.conn.execute("PRAGMA query_only=OFF")
            for sheet, df in frames:
                self._counter += 1
                table = f"t{self._counter}"
                df.columns = [str(c).strip() for c in df.columns]
                df.to_sql(table, self.conn, if_exists="replace", index=False)
                entry = {
                    "table": table, "file": file_name, "sheet": sheet,
                    "rows": len(df), "columns": list(df.columns),
                }
                self.manifest.append(entry)
                added.append(entry)
            # 超出上限时淘汰最早入库的表
            while len(self.manifest) > MAX_TABLES_PER_USER:
                old = self.manifest.pop(0)
                self.conn.execute(f'DROP TABLE IF EXISTS "{old["table"]}"')
        return added

    def manifest_text(self) -> str:
        if not self.manifest:
            return ""
        lines = ["[数据表清单｜已入库为 SQLite 数据表，可用 query_table 工具执行只读 SQL 精确查询/跨表勾稽]"]
        for m in self.manifest:
            cols = ", ".join(m["columns"])
            lines.append(
                f'- 表 {m["table"]}: 来源《{m["file"]}》Sheet「{m["sheet"]}」, '
                f'{m["rows"]} 行, 列: [{cols}]'
            )
        lines.append('提示: 中文列名在 SQL 中需用双引号包裹，如 SELECT SUM("金额") FROM t1')
        return "\n".join(lines)

    def query(self, sql: str) -> str:
        stmt = sql.strip().rstrip(";").strip()
        if not stmt:
            return "❌ SQL 为空"
        if ";" in stmt:
            return "❌ 仅允许单条 SQL 语句"
        head = re.split(r"\s", stmt, 1)[0].upper()
        if head not in ("SELECT", "WITH"):
            return "❌ 仅允许 SELECT / WITH 开头的只读查询"
        if _FORBIDDEN.search(stmt):
            return "❌ 查询中包含被禁止的写操作关键词"

        try:
            with self._lock:
                self.conn.execute("PRAGMA query_only=ON")
                cur = self.conn.execute(stmt)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
        except sqlite3.Error as e:
            return f"❌ SQL 执行失败: {e}"

        truncated = len(rows) > MAX_RESULT_ROWS
        rows = rows[:MAX_RESULT_ROWS]
        if not cols:
            return "(空结果)"
        out = [",".join(cols)]
        out += [",".join("" if v is None else str(v) for v in r) for r in rows]
        if truncated:
            out.append(f"...(超过 {MAX_RESULT_ROWS} 行已截断，请改用聚合查询)")
        return "\n".join(out)

    def close(self):
        with self._lock:
            self.conn.close()
