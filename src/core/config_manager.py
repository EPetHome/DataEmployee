"""
配置文件管理器
v0.2.4 优化：
  1. 对 soul.path、config_history_dir 和 database.path 加入默认降级处理，防止配置缺失时 Path(None) 抛出 TypeError
  2. handle_admin_command 接收外部已实例化的 BaseLLM 驱动，实现客户端复用与解耦
"""
import os
import yaml
import shutil
import json
from pathlib import Path
from datetime import datetime
from .llm import BaseLLM

class ConfigManager:
    def __init__(self, config_dir: str = None):
        default_dir = config_dir or "config"
        self.config_dir = Path(os.getenv("CONFIG_DIR", default_dir))
        self._cache = {}
        self._load_all()

    def _load_all(self):
        if self.config_dir.exists():
            for yaml_file in self.config_dir.glob("*.yaml"):
                with open(yaml_file, encoding="utf-8") as f:
                    self._cache[yaml_file.stem] = yaml.safe_load(f)

    def get(self, key: str, default=None):
        parts = key.split(".")
        value = self._cache
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
        
        if value is None:
            env_key = "_".join(parts).upper()
            value = os.getenv(env_key)
            if value is None and "API_KEY" in env_key:
                value = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
                
        return value if value is not None else default

    async def handle_admin_command(self, user_id: str, command: str, llm: BaseLLM) -> str | None:
        """
        处理管理员配置指令。
        v0.2.3 优化：接收主程序传入的 BaseLLM，复用连接。
        """
        intent = await self._parse_config_intent(command, llm)
        action = intent.get("action", "normal_chat")

        if action == "normal_chat":
            return None

        if action == "show_config":
            return self._format_config_display()

        if action == "update_soul":
            return self._update_soul(intent.get("content", ""))

        if action == "update_param":
            return self._update_param(intent.get("key", ""), intent.get("value"))

        if action == "clear_memory":
            return await self._clear_user_memory(intent.get("user_id", ""))

        if action == "rollback":
            return self._rollback_config()

        return f"❓ 无法理解指令: {command}"

    async def _parse_config_intent(self, command: str, llm: BaseLLM) -> dict:
        """v0.2.3 优化：复用 BaseLLM 驱动实例进行配置解析"""
        try:
            response_text = await llm.chat(
                messages=[{
                    "role": "system",
                    "content": """你是配置管理解析器。分析用户指令，返回JSON。
支持的 action:
- normal_chat:  用户说的是普通咨询/对账工作，与系统参数配置无关。极力推荐用这个！
- show_config:  用户想查看当前配置
- update_soul:  用户想修改 system prompt。content=完整新 prompt
- update_param: 用户想修改某个参数。key=参数路径(如llm.temperature)，value=新值
- clear_memory: 用户想清空某人历史对话。user_id=用户ID
- rollback:     用户想回滚上一次配置变更

返回格式: {"action": "...", "content": "...", "key": "...", "value": "...", "user_id": "..."}"""
                }, {
                    "role": "user",
                    "content": f"当前soul.md开头: {self._get_soul_preview()}\n\n指令: {command}"
                }],
                temperature=0,
                response_format={"type": "json_object"}
            )
            return json.loads(response_text)
        except Exception as e:
            # 解析失败兜底，默认为普通对话
            return {"action": "normal_chat"}

    def _update_soul(self, new_content: str) -> str:
        soul_path = Path(self.get("model.soul.path") or "config/soul.md")
        backup_dir = Path(self.get("model.config_history_dir") or "data/config_history")
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        old_content = soul_path.read_text(encoding="utf-8")
        (backup_dir / f"soul_{timestamp}.md").write_text(old_content, encoding="utf-8")
        soul_path.write_text(new_content, encoding="utf-8")
        return f"✅ SOUL.md 已更新。备份至: {backup_dir}/soul_{timestamp}.md"

    def _update_param(self, key: str, value) -> str:
        parts = key.split(".", 1)
        file_name = parts[0]
        param_path = parts[1] if len(parts) > 1 else ""
        yaml_file = self.config_dir / f"{file_name}.yaml"
        if not yaml_file.exists():
            return f"❌ 配置文件 {file_name}.yaml 不存在"
        
        backup_dir = Path(self.get("model.config_history_dir") or "data/config_history")
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(yaml_file, backup_dir / f"{file_name}_{timestamp}.yaml")

        with open(yaml_file, encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
        
        # LLM 解析出的 value 往往是字符串，尝试还原为 YAML 标量类型（数字/布尔等）
        if isinstance(value, str):
            try:
                value = yaml.safe_load(value)
            except yaml.YAMLError:
                pass

        if param_path:
            keys = param_path.split(".")
            target = config_data
            for k in keys[:-1]:
                target = target.setdefault(k, {})
            target[keys[-1]] = value
        else:
            config_data = value

        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False)
        self._load_all()
        return f"✅ {key} = {value} 已生效"

    def _format_config_display(self) -> str:
        lines = ["📋 当前配置:\n"]
        lines.append(f"**SOUL.md 摘要:**\n{self._get_soul_preview()}...\n")
        for name, data in self._cache.items():
            lines.append(f"**{name}.yaml:**")
            lines.append(f"```yaml\n{yaml.dump(data, allow_unicode=True)}\n```")
        return "\n".join(lines)

    def _get_soul_preview(self) -> str:
        soul_path = Path(self.get("model.soul.path") or "config/soul.md")
        if soul_path.exists():
            return soul_path.read_text(encoding="utf-8")[:150]
        return "(空)"

    async def _clear_user_memory(self, user_id: str) -> str:
        from .memory import Memory
        memory = Memory(self.get("model.database.path") or "data/conversations.db")
        await memory.clear(user_id)
        return f"✅ 用户 {user_id} 的对话历史已清空"

    def _rollback_config(self) -> str:
        backup_dir = Path(self.get("model.config_history_dir") or "data/config_history")
        backups = sorted(backup_dir.glob("soul_*.md"), reverse=True)
        if not backups:
            return "⚠️ 没有可回滚的历史版本"
        # 备份是在每次修改前生成的，backups[0] 即最近一次修改前的内容
        prev = backups[0]
        Path(self.get("model.soul.path") or "config/soul.md").write_text(
            prev.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return f"✅ 已回滚到: {prev.name}"

config_manager_instance = ConfigManager()
