"""
FastAPI 主入口
v0.2.4 优化：
  1. 引入 logging 模块，当管理员 ID 列表配置缺失/为空时输出警告提示。
  2. 对数据库路径与人设文件路径使用安全降级加载，彻底杜绝 Path(None) 产生的崩溃。
"""
import logging
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from pathlib import Path

from .core.config_manager import config_manager_instance as cfg
from .core.llm import OpenAILLM
from .core.memory import Memory
from .core.agent import GenericAgent
from .adapters.feishu import FeishuAdapter

# 初始化标准日志记录器
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

agent_instance = None
feishu_adapter = None
llm_instance = None
ADMIN_USER_IDS = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_instance, feishu_adapter, llm_instance, ADMIN_USER_IDS
    
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

    # 2. 装配唯一的底座 LLM 实例
    llm_instance = OpenAILLM(
        api_key=cfg.get("model.llm.api_key"),
        base_url=cfg.get("model.llm.base_url"),
        model=cfg.get("model.llm.model")
    )

    # 3. 内核装配
    memory = Memory(db_path=cfg.get("model.database.path") or "data/conversations.db")
    agent_instance = GenericAgent(
        llm=llm_instance,
        memory=memory,
        soul_path=cfg.get("model.soul.path") or "config/soul.md"
    )

    # 4. 平台适配器装配
    feishu_adapter = FeishuAdapter(
        config_mgr=cfg,
        app_id=cfg.get("feishu.feishu.app_id"),
        app_secret=cfg.get("feishu.feishu.app_secret")
    )
    yield

app = FastAPI(title="通用 Agent 平台", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.post("/feishu/callback")
async def feishu_callback(request: Request):
    body = await request.json()
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    event = await feishu_adapter.handle_webhook(body)
    if not event:
        return {"code": 0}

    # 图像过滤
    if event.text == "[IMAGE_FALLBACK]":
        reply = (
            "当前暂不支持图片格式的票据识别。\n"
            "请上传 PDF 格式 of 电子发票/账单，或 Excel 电子报表。"
        )
        await feishu_adapter.reply(event.chat_id, event.msg_id, reply)
        return {"code": 0}

    # 决策路由
    if event.user_id in ADMIN_USER_IDS and event.text.strip().startswith("/config"):
        admin_cmd = event.text.strip().replace("/config", "", 1).strip()
        reply = await cfg.handle_admin_command(event.user_id, admin_cmd, llm_instance)
        
        if reply is None:
            reply = await agent_instance.execute(event.user_id, admin_cmd, event.files)
    else:
        reply = await agent_instance.execute(event.user_id, event.text, event.files)

    await feishu_adapter.reply(event.chat_id, event.msg_id, reply)
    return {"code": 0}


# ── 调试与 REST API 路由 ──

@app.post("/api/chat")
async def api_chat(
    user_id: str = Form(...),
    message: str = Form(...),
    file: UploadFile | None = File(None)
):
    files_list = []
    if file and file.filename:
        file_bytes = await file.read()
        from .adapters.file_handler import FileHandler
        file_text = await FileHandler.process(file.filename, file_bytes)
        files_list.append({"name": file.filename, "content": file_text})
        
    reply = await agent_instance.execute(user_id, message, files_list)
    return {"reply": reply}

class ChatRequest(BaseModel):
    user_id: str
    message: str

@app.post("/api/config")
async def api_config(req: ChatRequest):
    if req.user_id not in ADMIN_USER_IDS:
        return {"result": "⛔ 你没有管理员权限。"}
    
    result = await cfg.handle_admin_command(req.user_id, req.message, llm_instance)
    if result is None:
        result = await agent_instance.execute(req.user_id, req.message)
    return {"result": result}

@app.delete("/api/chat/history")
async def clear_history(user_id: str):
    await agent_instance.memory.clear(user_id)
    return {"result": "ok"}

@app.get("/", response_class=HTMLResponse)
async def get_index():
    html_path = Path(__file__).parent / "web" / "index.html"
    return html_path.read_text(encoding="utf-8")
