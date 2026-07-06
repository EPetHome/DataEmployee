"""
核心 Agent 内核
v0.2.4 优化：
  1. 全局错误兜底范围扩大至整个 execute 流程（含 Prompt 读取、DB 查询等），防御 FileNotFoundError 和 DB 异常。
  2. 对传入文件采用 f.get() 进行防 KeyError 防御性提取。
"""
from pathlib import Path
from datetime import datetime
from .llm import BaseLLM
from .memory import Memory

class GenericAgent:
    def __init__(self, llm: BaseLLM, memory: Memory, soul_path: str):
        if not soul_path:
            raise ValueError("soul_path must be configured and injected into GenericAgent.")
        self.llm = llm
        self.memory = memory
        self.soul_path = Path(soul_path)

    async def execute(self, user_id: str, text: str, files: list[dict] | None = None) -> str:
        try:
            # 1. 系统提示词读取与历史对话加载（均在 try 块内，防御文件缺失或数据库连接异常）
            system_prompt = self._build_system_prompt()
            history = await self.memory.get_recent(user_id)

            messages = [{"role": "system", "content": system_prompt}]
            for h in history:
                messages.append({"role": h["role"], "content": h["content"]})

            # 2. 组装输入，防御性读取文件 Key 避免 KeyError
            user_content = text
            if files:
                file_texts = [
                    f"\n===== 文件: {f.get('name', 'unknown')} =====\n{f.get('content', '')}" 
                    for f in files
                ]
                user_content = "\n".join(file_texts) + f"\n\n{text}"

            messages.append({"role": "user", "content": user_content})

            # 3. 大模型调用与对话历史持久化
            reply = await self.llm.chat(messages)
            await self.memory.save(user_id, "user", user_content[:2000])
            await self.memory.save(user_id, "assistant", reply)
            return reply

        except Exception as e:
            # 统一异常处理，防止击穿导致 API 服务 500
            return f"⚠️ 会计服务暂时不可用，请稍后重试。(Error: {str(e)[:80]})"

    def _build_system_prompt(self) -> str:
        soul = self.soul_path.read_text(encoding="utf-8")
        today = datetime.now().strftime("%Y年%m月%d日")
        return soul + f"\n\n当前日期：{today}"
