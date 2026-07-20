"""
FastAPI 主入口
v0.3.0 优化：
  1. 飞书 webhook 加入签名/Token 校验、事件去重（防重试重复回复）、后台异步处理（秒回避免飞书超时重试）。
  2. 调试 REST API 加入 DEBUG_API_TOKEN 鉴权，未配置时默认关闭；移除 CORS 全放行。
  3. 飞书凭据从 YAML 移至环境变量；lifespan 退出时关闭 httpx 连接。
"""
import os
import time
import asyncio
import logging
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Form, File, UploadFile, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from pathlib import Path

from .core.config_manager import config_manager_instance as cfg
from .core.llm import OpenAILLM, OpenAIVisionLLM
from .core.memory import Memory
from .core.agent import GenericAgent
from .adapters.feishu import FeishuAdapter
from .adapters.file_handler import FileHandler

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

agent_instance = None
feishu_adapter = None
llm_instance = None
vision_instance = None
ADMIN_USER_IDS = []

# 调试 API 令牌：未配置时 /api/* 调试端点整体关闭
DEBUG_API_TOKEN = os.getenv("DEBUG_API_TOKEN", "")

# 飞书事件去重缓存: event_id -> 首次处理时间戳
_seen_events: dict[str, float] = {}
_EVENT_DEDUP_TTL = 600.0

# 持有后台任务引用，防止被垃圾回收提前取消
_background_tasks: set[asyncio.Task] = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_instance, feishu_adapter, llm_instance, vision_instance, ADMIN_USER_IDS

    # 1. 加载管理员配置，若空输出警告提示
    #    注意: YAML 文件被缓存为文件名(不带.yaml)，所以 key 需带文件名前缀
    admin_ids_raw = cfg.get("model.admin.user_ids")
    if isinstance(admin_ids_raw, list):
        ADMIN_USER_IDS = [str(x) for x in admin_ids_raw]
    elif isinstance(admin_ids_raw, str):
        ADMIN_USER_IDS = [x.strip() for x in admin_ids_raw.split(",") if x.strip()]
    else:
        ADMIN_USER_IDS = []

    if not ADMIN_USER_IDS:
        logger.warning("⚠️ [WARN] No admin users configured. Natural language config management is DISABLED.")
    if not DEBUG_API_TOKEN:
        logger.warning("⚠️ [WARN] DEBUG_API_TOKEN 未配置，/api/* 调试端点已禁用（网页调试台不可用）。")

    # 2. 装配唯一的底座 LLM 实例
    llm_instance = OpenAILLM(
        api_key=cfg.get("model.llm.api_key"),
        base_url=cfg.get("model.llm.base_url"),
        model=cfg.get("model.llm.model"),
        temperature=cfg.get("model.llm.temperature", 0.0),
        max_tokens=cfg.get("model.llm.max_tokens")
    )

    # 2.5 多模态驱动装配（fapiao-ocr 技能槽，可选）
    vision_key = os.getenv("VISION_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if cfg.get("model.vision.enabled"):
        if vision_key:
            vision_instance = OpenAIVisionLLM(
                api_key=vision_key,
                base_url=cfg.get("model.vision.base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
                model=cfg.get("model.vision.model") or "qwen-vl-plus",
            )
            logger.info(f"✅ 图片票据 OCR 已启用: {vision_instance.model}")
        else:
            logger.warning("⚠️ model.vision.enabled=true 但未配置 VISION_API_KEY/DASHSCOPE_API_KEY，图片 OCR 保持关闭。")

    # 3. 内核装配
    memory = Memory(db_path=cfg.get("model.database.path") or "data/conversations.db")
    agent_instance = GenericAgent(
        llm=llm_instance,
        memory=memory,
        soul_path=cfg.get("model.soul.path") or "config/soul.md",
        policy_path=cfg.get("model.policy.path") or "config/policy.yaml"
    )

    # 4. 平台适配器装配（凭据优先走环境变量，YAML 仅作兜底）
    feishu_adapter = FeishuAdapter(
        config_mgr=cfg,
        app_id=cfg.get("feishu.feishu.app_id") or os.getenv("FEISHU_APP_ID"),
        app_secret=cfg.get("feishu.feishu.app_secret") or os.getenv("FEISHU_APP_SECRET"),
        vision=vision_instance
    )
    yield

    # 5. 优雅退出：释放 httpx 连接池
    await llm_instance.close()
    await feishu_adapter.close()
    if vision_instance:
        await vision_instance.close()

app = FastAPI(title="通用 Agent 平台", version="0.4.0", lifespan=lifespan)


def _is_duplicate_event(event_id: str) -> bool:
    """飞书对超时/非 200 响应会重试推送，按 event_id 去重。"""
    now = time.time()
    if len(_seen_events) > 1000:
        for k in [k for k, ts in _seen_events.items() if now - ts > _EVENT_DEDUP_TTL]:
            _seen_events.pop(k, None)
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = now
    return False


async def _process_feishu_event(body: dict):
    """后台处理飞书事件：耗时的 LLM 调用不阻塞 webhook 响应。"""
    try:
        event = await feishu_adapter.handle_webhook(body)
        if not event:
            return

        # 图像过滤
        if event.text == "[IMAGE_FALLBACK]":
            reply = (
                "当前暂不支持图片格式的票据识别。\n"
                "请上传 PDF 格式的电子发票/账单，或 Excel 电子报表。"
            )
            await feishu_adapter.reply(event.chat_id, event.msg_id, reply)
            return

        # 决策路由
        if event.user_id in ADMIN_USER_IDS and event.text.strip().startswith("/config"):
            admin_cmd = event.text.strip().replace("/config", "", 1).strip()
            reply = await cfg.handle_admin_command(event.user_id, admin_cmd, llm_instance)

            if reply is None:
                reply = await agent_instance.execute(event.user_id, admin_cmd, event.files)
        else:
            reply = await agent_instance.execute(event.user_id, event.text, event.files)

        await feishu_adapter.reply(event.chat_id, event.msg_id, reply)
    except Exception:
        logger.exception("[Feishu] 事件后台处理异常")


@app.post("/feishu/callback")
async def feishu_callback(request: Request):
    raw_body = await request.body()
    body = feishu_adapter.parse_and_verify_request(
        raw_body,
        timestamp=request.headers.get("X-Lark-Request-Timestamp", ""),
        nonce=request.headers.get("X-Lark-Request-Nonce", ""),
        signature=request.headers.get("X-Lark-Signature", ""),
    )
    if body is None:
        raise HTTPException(status_code=403, detail="signature/token verification failed")

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    event_id = body.get("header", {}).get("event_id", "")
    if event_id and _is_duplicate_event(event_id):
        return {"code": 0}

    # 立即返回 200，重活丢给后台任务，避免飞书 3 秒超时触发重试
    task = asyncio.create_task(_process_feishu_event(body))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"code": 0}


# ── 调试与 REST API 路由（需 DEBUG_API_TOKEN 鉴权）──

def require_debug_token(x_debug_token: str | None = Header(None)):
    if not DEBUG_API_TOKEN:
        raise HTTPException(status_code=403, detail="Debug API disabled. Set DEBUG_API_TOKEN to enable.")
    if x_debug_token != DEBUG_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid debug token")

@app.post("/api/chat", dependencies=[Depends(require_debug_token)])
async def api_chat(
    user_id: str = Form(...),
    message: str = Form(...),
    file: list[UploadFile] = File(default=[])
):
    files_list = []
    for upload in file:
        if not upload.filename:
            continue
        file_bytes = await upload.read()
        ext = Path(upload.filename).suffix.lower()
        if ext in IMAGE_EXTS:
            # 图片走票据 OCR（fapiao-ocr 技能槽）
            if vision_instance:
                ocr_text = await vision_instance.ocr_invoice(file_bytes)
                files_list.append({
                    "name": upload.filename,
                    "content": f"[图片票据 OCR 识别结果]\n{ocr_text}"
                })
            else:
                files_list.append({
                    "name": upload.filename,
                    "content": "[图片票据识别未启用（未配置视觉模型），请改传 PDF 电子发票或 Excel 报表]"
                })
        else:
            file_text = await FileHandler.process(upload.filename, file_bytes)
            # raw 字节供 Agent 将表格入库，实现 Text-to-SQL 与多文件勾稽
            files_list.append({"name": upload.filename, "content": file_text, "raw": file_bytes})

    reply = await agent_instance.execute(user_id, message, files_list)
    return {"reply": reply}

class ChatRequest(BaseModel):
    user_id: str
    message: str

@app.post("/api/config", dependencies=[Depends(require_debug_token)])
async def api_config(req: ChatRequest):
    if req.user_id not in ADMIN_USER_IDS:
        return {"result": "⛔ 你没有管理员权限。"}

    result = await cfg.handle_admin_command(req.user_id, req.message, llm_instance)
    if result is None:
        result = await agent_instance.execute(req.user_id, req.message)
    return {"result": result}

@app.delete("/api/chat/history", dependencies=[Depends(require_debug_token)])
async def clear_history(user_id: str):
    # 同步清空对话记忆与该用户已入库的数据表
    await agent_instance.clear_user(user_id)
    return {"result": "ok"}

@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_path = Path(__file__).parent / "web" / "index.html"
    return html_path.read_text(encoding="utf-8")
