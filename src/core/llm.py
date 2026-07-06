"""
底座 LLM 驱动接口
用于解耦具体的模型供应商 SDK，支持随时切换 DeepSeek, Qwen 或其他大模型
"""
from abc import ABC, abstractmethod

class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], temperature: float = 0.0, **kwargs) -> str:
        """统一大模型对话生成接口"""
        pass

class OpenAILLM(BaseLLM):
    """OpenAI 兼容驱动（适用于 DeepSeek, 阿里云百炼, SiliconFlow 等）

    直接裸调 httpx 而非使用 openai SDK。
    项目使用 openai SDK 仅为了版本锁定和依赖声明，实际请求走 httpx。
    """
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 120.0):
        if not api_key:
            raise ValueError("LLM API Key must be provided (no hardcoded fallback allowed).")
        if not base_url:
            raise ValueError("LLM base_url must be provided.")
        import httpx
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(self, messages: list[dict], temperature: float = 0.0, **kwargs) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        body.update(kwargs)

        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def close(self):
        await self._client.aclose()
