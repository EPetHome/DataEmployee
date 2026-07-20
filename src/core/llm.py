"""
底座 LLM 驱动接口
用于解耦具体的模型供应商 SDK，支持随时切换 DeepSeek, Qwen 或其他大模型
v0.4.0 优化：
  1. chat_raw 支持 OpenAI 兼容的 function calling（tools 参数与 tool_calls 返回）。
  2. 新增 OpenAIVisionLLM 多模态驱动，用于图片票据 OCR（如 qwen-vl 系列）。
"""
import base64
from abc import ABC, abstractmethod

class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], temperature: float | None = None, **kwargs) -> str:
        """统一大模型对话生成接口"""
        pass

class OpenAILLM(BaseLLM):
    """OpenAI 兼容驱动（适用于 DeepSeek, 阿里云百炼, SiliconFlow 等）

    直接裸调 httpx 而非使用 openai SDK。
    项目使用 openai SDK 仅为了版本锁定和依赖声明，实际请求走 httpx。
    """
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 120.0,
                 temperature: float = 0.0, max_tokens: int | None = None):
        if not api_key:
            raise ValueError("LLM API Key must be provided (no hardcoded fallback allowed).")
        if not base_url:
            raise ValueError("LLM base_url must be provided.")
        import httpx
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = float(temperature or 0.0)
        self.max_tokens = int(max_tokens) if max_tokens else None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat_raw(self, messages: list[dict], temperature: float | None = None,
                       tools: list[dict] | None = None, **kwargs) -> dict:
        """返回完整的 assistant message（含 tool_calls），供工具调用循环使用。"""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = tools
        body.update(kwargs)

        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]

    async def chat(self, messages: list[dict], temperature: float | None = None, **kwargs) -> str:
        msg = await self.chat_raw(messages, temperature=temperature, **kwargs)
        return msg.get("content") or ""

    async def close(self):
        await self._client.aclose()


# 票据 OCR 提示词：要求结构化输出，无法识别的字段如实标注
INVOICE_OCR_PROMPT = """你是票据识别专家。请识别这张图片中的票据信息，按以下结构输出：
- 票据类型（增值税专用发票/普通发票/火车票/机票行程单/收据/其他）
- 发票代码 / 发票号码
- 开票日期
- 销售方名称 / 纳税人识别号
- 购买方名称 / 纳税人识别号
- 品目明细（名称、数量、单价、金额）
- 金额合计 / 税率 / 税额 / 价税合计（大小写）

要求：只输出图片中真实可见的内容，无法识别或不存在的字段标注"无法识别"，严禁猜测编造。
若图片不是票据，请简要描述图片内容并说明"非票据图片"。"""


def sniff_image_mime(image_bytes: bytes) -> str:
    """通过魔数判断图片 MIME 类型，默认按 JPEG 处理。"""
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class OpenAIVisionLLM(OpenAILLM):
    """OpenAI 兼容的多模态驱动（适用于 qwen-vl、GLM-4V、moonshot-v1-vision 等）。

    用于激活 fapiao-ocr 技能槽：将图片票据识别为结构化文本。
    """

    async def ocr_invoice(self, image_bytes: bytes) -> str:
        mime = sniff_image_mime(image_bytes)
        b64 = base64.b64encode(image_bytes).decode()
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": INVOICE_OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }]
        return await self.chat(messages)
