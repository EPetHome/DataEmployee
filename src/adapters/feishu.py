"""
飞书专属适配器
v0.3.0 优化：
  1. 支持事件订阅安全校验：Verification Token 校验 + Encrypt Key 签名校验与 AES 解密。
  2. 修复文件消息解析：file_key/file_name 位于 message.content 的 JSON 内部，而非 message 顶层。
  3. 修复附件下载接口：改用 /im/v1/messages/{message_id}/resources/{file_key}?type=file。
  4. 全适配器复用单个 httpx.AsyncClient；token 换取加锁防并发重复请求，并校验响应 code。
"""
import os
import time
import json
import base64
import hashlib
import asyncio
import logging
import httpx
from .base import BaseIMAdapter, IMEvent
from .file_handler import FileHandler

logger = logging.getLogger("feishu")

class FeishuAdapter(BaseIMAdapter):
    def __init__(self, config_mgr, app_id: str, app_secret: str, vision=None):
        self.cfg = config_mgr
        self.app_id = app_id
        self.app_secret = app_secret
        # 多模态驱动（OpenAIVisionLLM），配置后图片消息走票据 OCR 而非降级提示
        self.vision = vision
        # 注意: YAML 缓存以文件名为顶层 key，故需带 feishu.feishu. 前缀
        self.api_base = (
            self.cfg.get("feishu.feishu.api_base")
            or os.getenv("FEISHU_API_BASE")
            or "https://open.feishu.cn"
        )
        self.verification_token = (
            self.cfg.get("feishu.feishu.verification_token")
            or os.getenv("FEISHU_VERIFICATION_TOKEN")
            or ""
        )
        self.encrypt_key = (
            self.cfg.get("feishu.feishu.encrypt_key")
            or os.getenv("FEISHU_ENCRYPT_KEY")
            or ""
        )
        if not self.verification_token and not self.encrypt_key:
            logger.warning(
                "⚠️ 未配置 FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY，"
                "webhook 将不做来源校验，任何人都可伪造事件！生产环境必须配置。"
            )

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._token_cache = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self):
        await self._client.aclose()

    # ── 事件安全校验 ──

    def parse_and_verify_request(
        self, raw_body: bytes, timestamp: str, nonce: str, signature: str
    ) -> dict | None:
        """
        校验并解析飞书事件请求体。校验失败返回 None。
        - 配置了 encrypt_key: 校验 X-Lark-Signature 签名并解密 encrypt 字段
        - 配置了 verification_token: 校验事件体中的 token 字段
        """
        try:
            if self.encrypt_key:
                expected = hashlib.sha256(
                    (timestamp + nonce + self.encrypt_key).encode("utf-8") + raw_body
                ).hexdigest()
                if signature != expected:
                    logger.warning("[Feishu] 签名校验失败，已拒绝该请求")
                    return None

            body = json.loads(raw_body)

            if self.encrypt_key and "encrypt" in body:
                body = self._decrypt_event(body["encrypt"])

            if self.verification_token:
                token = body.get("token") or body.get("header", {}).get("token")
                if token != self.verification_token:
                    logger.warning("[Feishu] verification_token 不匹配，已拒绝该请求")
                    return None

            return body
        except Exception:
            logger.exception("[Feishu] 事件解析/校验异常")
            return None

    def _decrypt_event(self, encrypt_str: str) -> dict:
        """飞书标准 AES-256-CBC 解密: key=SHA256(encrypt_key), iv=密文前16字节"""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.sha256(self.encrypt_key.encode("utf-8")).digest()
        data = base64.b64decode(encrypt_str)
        iv, cipher_text = data[:16], data[16:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(cipher_text) + decryptor.finalize()
        plain = padded[: -padded[-1]]
        return json.loads(plain.decode("utf-8"))

    # ── 事件转换 ──

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
            content = json.loads(message.get("content", "{}"))

            if msg_type == "text":
                return IMEvent(user_id=user_open_id, chat_id=chat_id, msg_id=msg_id,
                               text=content.get("text", ""))

            elif msg_type == "file":
                # file_key/file_name 在 content JSON 内部，不在 message 顶层
                file_key = content.get("file_key")
                file_name = content.get("file_name", "unknown")
                if not file_key:
                    logger.warning(f"[Feishu] 文件消息缺少 file_key: {message}")
                    return None
                file_bytes = await self._download_resource(msg_id, file_key, "file")
                file_text = await FileHandler.process(file_name, file_bytes)
                return IMEvent(
                    user_id=user_open_id,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    text="请分析我上传的文件",
                    # raw 字节供 Agent 将表格入库，实现 Text-to-SQL 精确查询与多文件勾稽
                    files=[{"name": file_name, "content": file_text, "raw": file_bytes}]
                )

            elif msg_type == "image":
                image_key = content.get("image_key")
                if self.vision and image_key:
                    image_bytes = await self._download_resource(msg_id, image_key, "image")
                    ocr_text = await self.vision.ocr_invoice(image_bytes)
                    return IMEvent(
                        user_id=user_open_id,
                        chat_id=chat_id,
                        msg_id=msg_id,
                        text="请分析我上传的票据图片",
                        files=[{"name": "票据图片", "content": f"[图片票据 OCR 识别结果]\n{ocr_text}"}]
                    )
                return IMEvent(user_id=user_open_id, chat_id=chat_id, msg_id=msg_id, text="[IMAGE_FALLBACK]")
        except Exception:
            logger.exception("[Feishu] webhook 事件转换异常")
            return None

        return None

    async def reply(self, chat_id: str, msg_id: str, text: str) -> bool:
        try:
            token = await self._get_tenant_token()
            chunks = self._split_long_message(text, max_len=4000)
            for chunk in chunks:
                resp = await self._client.post(
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
                    logger.error(f"[Feishu] 回复失败 Status: {resp.status_code}, Body: {resp.text}")
            return True
        except Exception:
            logger.exception("[Feishu] 回复消息异常")
            return False

    async def _get_tenant_token(self) -> str:
        """获取飞书 tenant_access_token，带缓存、并发锁与错误码校验。"""
        now = time.time()
        if self._token_cache and now < self._token_expires_at - 60:
            return self._token_cache

        async with self._token_lock:
            # 双重检查：等锁期间可能已有协程完成刷新
            now = time.time()
            if self._token_cache and now < self._token_expires_at - 60:
                return self._token_cache

            token_resp = await self._client.post(
                f"{self.api_base}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret}
            )
            data = token_resp.json()
            if data.get("code") != 0 or not data.get("tenant_access_token"):
                raise RuntimeError(f"飞书换取 tenant_access_token 失败: {data}")

            self._token_cache = data["tenant_access_token"]
            self._token_expires_at = now + data.get("expire", 7200)
            return self._token_cache

    async def _download_resource(self, msg_id: str, resource_key: str, resource_type: str) -> bytes:
        """下载消息附件（file/image）。接收消息中的资源必须走 messages/{msg_id}/resources 接口。"""
        token = await self._get_tenant_token()
        file_resp = await self._client.get(
            f"{self.api_base}/open-apis/im/v1/messages/{msg_id}/resources/{resource_key}",
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"}
        )
        file_resp.raise_for_status()
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
