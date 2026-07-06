"""
飞书专属适配器
v0.2.4 优化：
  1. 引入内存级 Token 缓存机制，避免每次调用重复换取飞书 tenant_access_token。
  2. 保持对文件下载、飞书 API 发送请求的网络级 Try-Except 保护。
"""
import time
import json
import httpx
from .base import BaseIMAdapter, IMEvent
from .file_handler import FileHandler

class FeishuAdapter(BaseIMAdapter):
    def __init__(self, config_mgr, app_id: str, app_secret: str):
        self.cfg = config_mgr
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = self.cfg.get("feishu.api_base", "https://open.feishu.cn")
        
        # v0.2.4 优化：Token 缓存属性初始化
        self._token_cache = None
        self._token_expires_at = 0.0

    async def handle_webhook(self, raw_request: dict) -> IMEvent | None:
        try:
            if raw_request.get("header", {}).get("event_type") != "im.message.receive_v1":
                return None

            event = raw_request.get("event", {})
            message = event.get("message", {})
            msg_type = message.get("message_type", "text")
            chat_id = message.get("chat_id")
            msg_id = message.get("message_id")
            user_open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "unknown")

            if msg_type == "text":
                content = json.loads(message.get("content", "{}")).get("text", "")
                return IMEvent(user_id=user_open_id, chat_id=chat_id, msg_id=msg_id, text=content)

            elif msg_type == "file":
                file_key = message.get("file_key")
                file_name = message.get("file_name", "unknown")
                file_bytes = await self._download_file(file_key)
                file_text = await FileHandler.process(file_name, file_bytes)
                return IMEvent(
                    user_id=user_open_id,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    text="请分析我上传的文件",
                    files=[{"name": file_name, "content": file_text}]
                )

            elif msg_type == "image":
                return IMEvent(user_id=user_open_id, chat_id=chat_id, msg_id=msg_id, text="[IMAGE_FALLBACK]")
        except Exception as e:
            print(f"[FeishuAdapter Webhook Error] {str(e)}")
            return None

        return None

    async def reply(self, chat_id: str, msg_id: str, text: str) -> bool:
        try:
            token = await self._get_tenant_token()
            chunks = self._split_long_message(text, max_len=4000)
            async with httpx.AsyncClient() as client:
                for chunk in chunks:
                    resp = await client.post(
                        f"{self.api_base}/open-apis/im/v1/messages/{msg_id}/reply",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "content": json.dumps({"text": chunk}),
                            "msg_type": "text"
                        }
                    )
                    if resp.status_code != 200 or resp.json().get("code") != 0:
                        print(f"[FeishuAdapter Reply Error] Status: {resp.status_code}, Body: {resp.text}")
            return True
        except Exception as e:
            print(f"[FeishuAdapter Reply Error] {str(e)}")
            return False

    async def _get_tenant_token(self) -> str:
        """
        获取飞书 tenant_access_token。
        v0.2.4 优化：加入 Token 缓存与过期判定，杜绝重复的网络换取往返。
        """
        now = time.time()
        # 有效期内直接返回（扣除 60 秒的安全网关同步富余时间）
        if self._token_cache and now < self._token_expires_at - 60:
            return self._token_cache

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                f"{self.api_base}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret}
            )
            data = token_resp.json()
            token = data.get("tenant_access_token")
            # 飞书 Token 标准有效期为 7200s，若返回中有自定义 expire 则采用
            expires_in = data.get("expire", 7200)
            
            # 更新缓存与过期边界
            self._token_cache = token
            self._token_expires_at = now + expires_in
            return token

    async def _download_file(self, file_key: str) -> bytes:
        token = await self._get_tenant_token()
        async with httpx.AsyncClient() as client:
            file_resp = await client.get(
                f"{self.api_base}/open-apis/im/v1/files/{file_key}",
                headers={"Authorization": f"Bearer {token}"}
            )
            return file_resp.content

    def _split_long_message(self, text: str, max_len: int = 4000) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks = []
        paragraphs = text.split("\n\n")
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 <= max_len:
                current += para + "\n\n"
            else:
                if current:
                    chunks.append(current.strip())
                current = para + "\n\n"
        if current:
            chunks.append(current.strip())

        result = []
        for chunk in chunks:
            while len(chunk) > max_len:
                result.append(chunk[:max_len] + "\n...(续)")
                chunk = chunk[max_len:]
            if chunk:
                result.append(chunk)
        return result
