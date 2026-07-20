# DE 会计数字员工 — 启动与部署手册 (START.md)

本手册旨在指导您快速完成 **v0.2.4 通用 Agent 框架版（飞书会计数字员工）** 的配置、部署与日常运维管理。

---

## 一、 环境要求

*   **操作系统**：Linux / macOS / Windows
*   **运行环境**：Python 3.11+ (裸机部署) 或 Docker 20.10+ & Docker Compose 2.0+ (容器部署)
*   **外部依赖**：
    *   DeepSeek 开放平台账号及有效的 API Key
    *   飞书开放平台企业自建应用权限

---

## 二、 前期准备：配置飞书机器人

1.  **创建自建应用**：
    *   登录 [飞书开放平台](https://open.feishu.cn/)，点击“创建企业自建应用”，获取 `App ID` 和 `App Secret`。
2.  **启用机器人能力**：
    *   进入应用详情页，在左侧导航栏点击 **“应用功能” -> “机器人”**，开启机器人功能。
3.  **开通权限**：
    *   进入 **“开发配置” -> “权限管理”**，开通以下敏感权限：
        *   `im:message`（接收消息）
        *   `im:message.group_at_msg`（接收群组中 @ 机器人的消息）
        *   `im:message:send_as_bot`（以机器人身份发送消息）
        *   `im:resource`（获取文件/图片等媒体资源）
4.  **订阅事件**：
    *   进入 **“开发配置” -> “事件订阅”**，添加事件：
        *   `im.message.receive_v1`（接收消息 v1.0 版本）
5.  **配置回调地址**：
    *   在“事件订阅”页面配置“请求网址”（Request URL），格式为：
        `https://您的公网域名/feishu/callback`
    *   *注：本地调试时，可使用 ngrok、cpolar 或 LocalTunnel 将本地 8080 端口映射至公网。*

---

## 三、 配置应用程序

在启动服务前，请先完成参数配置。本项目实行**零硬编码设计**，所有配置相互分离。

### 1. 敏感凭证隔离 (`.env`)
在项目根目录下创建 `.env` 文件（或直接复制并重命名 `.env.example`）：
```bash
# 1. 底座大模型 Key (必须配置)
DEEPSEEK_API_KEY=sk-your-real-deepseek-api-key

# 2. 飞书接口调用凭证 (必须配置)
FEISHU_APP_ID=cli_your_feishu_app_id
FEISHU_APP_SECRET=your_feishu_app_secret

# 3. 飞书事件订阅安全校验 (生产环境强烈建议配置，来源: 开放平台 → 事件与回调 → 加密策略)
#    配置 Verification Token 后校验事件来源；配置 Encrypt Key 后自动启用签名校验与事件解密
FEISHU_VERIFICATION_TOKEN=your_verification_token
FEISHU_ENCRYPT_KEY=your_encrypt_key

# 4. 网页调试台 /api/* 端点的访问令牌。不配置则调试 API 整体禁用（生产环境建议留空）
DEBUG_API_TOKEN=any-random-secret

# 5. 可选覆盖项 (测试时可用，生产环境建议留空走 model.yaml)
# DATABASE_PATH=data/conversations.db
# ADMIN_USER_IDS=ou_admin_1_openid,ou_admin_2_openid
```

> ⚠️ **安全提醒**：真实凭据只放 `.env`（已被 `.gitignore`/`.dockerignore` 排除），
> `config/feishu.yaml` 中一律保持占位符，严禁提交真实密钥。

### 2. 系统通用配置 (`config/model.yaml`)
检查并修改基础运行配置：
```yaml
llm:
  model: "deepseek-chat"               # 大模型底座名称
  base_url: "https://api.deepseek.com/v1" # 大模型 API 终结点
  temperature: 0.0                     # 财务场景建议设为 0，最大程度杜绝幻觉
  max_tokens: 4096

database:
  path: "data/conversations.db"        # 历史会话数据库存放路径

soul:
  path: "config/soul.md"               # 会计提示词 Prompt 文件路径

config_history_dir: "data/config_history" # 配置修改历史备份目录

admin:
  user_ids:                            # 允许执行 /config 配置指令的管理员 OpenID 列表
    - "ou_admin_open_id_here"          # 请替换为您在飞书中的真实 OpenID
```

---

## 四、 运行与部署

### 方案 A：Docker Compose 部署 (推荐，生产环境首选)

1.  **一键构建并后台运行**：
    ```bash
    docker-compose up -d --build
    ```
2.  **查看运行日志**：
    ```bash
    docker-compose logs -f accountant-agent
    ```
3.  **停止服务**：
    ```bash
    docker-compose down
    ```

### 方案 B：本地裸机部署 (适用于开发调试)

1.  **安装项目依赖**：
    ```bash
    pip install -r requirements.txt
    ```
2.  **运行服务**：
    ```bash
    uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
    ```
3.  **本地调试映射（使用 ngrok 示例）**：
    ```bash
    ngrok http 8080
    ```
    将 ngrok 产生的 `https://xxx.ngrok-free.app` 填入飞书的事件回调 Request URL 中即可。

---

## 五、 管理员自然语言指令指南

当消息发送人属于 `ADMIN_USER_IDS` 清单时，在飞书/钉钉中发送以 `/config` 开头的指令，将自动触发**配置管理解析器**：

*   **查看当前运行配置**：
    *   *指令*：`/config 显示当前配置` 或 `/config show_config`
    *   *回复*：以表格和 YAML 块的形式展示当前 Prompt 及参数配置。
*   **调整底座模型参数**：
    *   *指令*：`/config 把模型温度调高到 0.3`
    *   *结果*：自动修改 `config/model.yaml` 中的 `llm.temperature` 并实现热重载生效。
*   **动态修改/扩展会计 Prompt（灵魂）**：
    *   *指令*：`/config 修改会计提示词，增加个人所得税计算能力`
    *   *结果*：自动修改 `config/soul.md`，追加对应能力，并在 `data/config_history/` 下自动创建上一个版本的备份。
*   **一键回滚 Prompt**：
    *   *指令*：`/config 回滚上一次灵魂配置`
    *   *结果*：恢复至上一个版本的 `soul.md` 备份。
*   **清空用户对话历史（强制重置上下文）**：
    *   *指令*：`/config 清空用户 ou_xxxx 的对话记录`
    *   *结果*：清空该用户在 SQLite 中的记忆，下一次对话时将重建上下文。

---

## 六、 常见问题与运维排查

1.  **启动时控制台发出 WARNING: No admin users configured...**：
    *   *原因*：在 `config/model.yaml` 或 `.env` 中没有配置 `admin.user_ids`，系统自动禁用自然语言配置管理功能以保障安全。
    *   *解决*：在配置文件中配入正确的飞书 Admin OpenID 并重启。
2.  **上传图片没反应**：
    *   *原因*：v0.2.4 删除了通义 VL 多模态依赖，当前版本不解析图片。
    *   *表现*：上传图片会统一自动回复友好提示：“*当前暂不支持图片识别，请上传 PDF 电子发票或 Excel 表格。*”
3.  **飞书发送消息时，Bot 没有回复**：
    *   *排查 A*：检查服务终端日志，确认是否输出了 `[FeishuAdapter Webhook Error]`。
    *   *排查 B*：检查飞书开放平台的 App ID 和 App Secret 是否正确填入 `.env`。
    *   *排查 C*：检查本地映射域名的公网连通性，以及飞书回调的 Challenge 验证是否通过。
