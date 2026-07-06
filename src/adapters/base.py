"""
统一适配层规约
"""
from abc import ABC, abstractmethod
from pydantic import BaseModel

class IMEvent(BaseModel):
    """标准的输入消息包装结构"""
    user_id: str
    chat_id: str
    msg_id: str
    text: str
    files: list[dict] = []  # 标准格式 [{"name": "xxx.xlsx", "content": "..."}]

class BaseIMAdapter(ABC):
    """IM 接口转换标准"""
    @abstractmethod
    async def handle_webhook(self, raw_request: dict) -> IMEvent | None:
        """把平台特定的 webhook 参数转换为标准的 IMEvent"""
        pass

    @abstractmethod
    async def reply(self, chat_id: str, msg_id: str, text: str) -> bool:
        """将通用 Agent 文本结果，以特定平台 API 发送回用户"""
        pass
