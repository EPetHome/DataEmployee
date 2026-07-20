"""
核心 Agent 内核
v0.4.0 优化：
  1. 接入 function calling 工具循环：calc（精确计算）与 query_table（Text-to-SQL 只读查询）。
  2. 上传的表格文件按用户入库 TabularStore，支持跨消息追问与多文件勾稽对账。
  3. 清空记忆时同步清空该用户的数据表仓。
"""
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from .llm import BaseLLM, OpenAILLM
from .memory import Memory
from .tools import TOOL_DEFS, safe_calc
from .tabular_store import TabularStore, TABULAR_EXTS

logger = logging.getLogger("agent")

MAX_TOOL_ROUNDS = 8
MAX_TOOL_RESULT_CHARS = 4000

class GenericAgent:
    def __init__(self, llm: BaseLLM, memory: Memory, soul_path: str, policy_path: str | None = None):
        if not soul_path:
            raise ValueError("soul_path must be configured and injected into GenericAgent.")
        self.llm = llm
        self.memory = memory
        self.soul_path = Path(soul_path)
        self.policy_path = Path(policy_path) if policy_path else None
        # 每个用户一个内存数据表仓（进程内，重启即清空）
        self._stores: dict[str, TabularStore] = {}

    async def clear_user(self, user_id: str):
        """清空用户对话记忆与已入库的数据表。"""
        await self.memory.clear(user_id)
        store = self._stores.pop(user_id, None)
        if store:
            store.close()

    async def execute(self, user_id: str, text: str, files: list[dict] | None = None) -> str:
        try:
            # 1. 系统提示词读取与历史对话加载（均在 try 块内，防御文件缺失或数据库连接异常）
            system_prompt = self._build_system_prompt()
            history = await self.memory.get_recent(user_id)

            messages = [{"role": "system", "content": system_prompt}]
            for h in history:
                messages.append({"role": h["role"], "content": h["content"]})

            # 2. 组装输入：文件文本 + 表格入库 + 数据表清单
            user_content = text
            if files:
                file_texts = []
                for f in files:
                    file_texts.append(
                        f"\n===== 文件: {f.get('name', 'unknown')} =====\n{f.get('content', '')}"
                    )
                    await self._maybe_ingest(user_id, f)
                user_content = "\n".join(file_texts) + f"\n\n{text}"

            store = self._stores.get(user_id)
            if store and store.manifest:
                user_content = store.manifest_text() + "\n\n" + user_content

            messages.append({"role": "user", "content": user_content})

            # 3. 工具调用循环与对话历史持久化
            reply = await self._run_tool_loop(messages, store)
            await self.memory.save(user_id, "user", user_content[:2000])
            await self.memory.save(user_id, "assistant", reply)
            return reply

        except Exception:
            # 统一异常处理，防止击穿导致 API 服务 500。
            # 详细堆栈只进服务端日志，不回显给用户，避免泄露内部路径/接口信息。
            logger.exception(f"Agent execute 失败 user_id={user_id}")
            return "⚠️ 会计服务暂时不可用，请稍后重试或联系管理员。"

    async def _maybe_ingest(self, user_id: str, file_dict: dict):
        """表格文件（携带原始字节）入库到该用户的数据表仓。"""
        raw = file_dict.get("raw")
        name = file_dict.get("name", "")
        if not raw or Path(name).suffix.lower() not in TABULAR_EXTS:
            return
        store = self._stores.setdefault(user_id, TabularStore())
        try:
            added = await asyncio.to_thread(store.ingest_file, name, raw)
            logger.info(f"[TabularStore] user={user_id} 入库 {name}: {[m['table'] for m in added]}")
        except Exception:
            logger.exception(f"[TabularStore] 入库失败: {name}")

    async def _run_tool_loop(self, messages: list[dict], store: TabularStore | None) -> str:
        """OpenAI 兼容 function calling 循环：模型请求工具 → 执行 → 回填 → 直至产出文本。"""
        if not isinstance(self.llm, OpenAILLM):
            return await self.llm.chat(messages)

        for _ in range(MAX_TOOL_ROUNDS):
            msg = await self.llm.chat_raw(messages, tools=TOOL_DEFS)
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return msg.get("content") or ""

            messages.append(msg)
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._exec_tool(name, args, store)
                logger.info(f"[Tool] {name}({args}) -> {result[:120]!r}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result[:MAX_TOOL_RESULT_CHARS],
                })
        return "⚠️ 分析步骤过多已中止，请把问题拆小后重试。"

    async def _exec_tool(self, name: str, args: dict, store: TabularStore | None) -> str:
        if name == "calc":
            return safe_calc(str(args.get("expression", "")))
        if name == "query_table":
            if not store or not store.manifest:
                return "当前没有已入库的数据表，请先让用户上传 Excel/CSV 表格文件。"
            return await asyncio.to_thread(store.query, str(args.get("sql", "")))
        return f"❌ 未知工具: {name}"

    def _build_system_prompt(self) -> str:
        parts = [self.soul_path.read_text(encoding="utf-8")]
        # 每次调用即时读取制度文件：管理员修改后无需重启即生效
        if self.policy_path and self.policy_path.exists():
            parts.append(
                "\n\n---\n\n## 公司财务制度（审计判定依据）\n\n"
                "审计结论必须引用以下制度的具体条款：\n\n"
                "```yaml\n" + self.policy_path.read_text(encoding="utf-8") + "\n```"
            )
        today = datetime.now().strftime("%Y年%m月%d日")
        parts.append(f"\n\n当前日期：{today}")
        return "".join(parts)
