"""
Agent 工具集
v0.4.0 新增：
  - safe_calc: 基于 AST 白名单的安全四则运算器，杜绝大模型心算错账
  - TOOL_DEFS: OpenAI 兼容的 function calling 工具定义（calc / query_table）
"""
import ast
import operator as op

_MAX_EXPR_LEN = 500
_MAX_POW = 100

_BINOPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.FloorDiv: op.floordiv, ast.Mod: op.mod,
    ast.Pow: op.pow,
}
_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sum": sum}


def safe_calc(expression: str) -> str:
    """安全计算算术表达式。仅允许数字、四则运算与 round/abs/min/max/sum。"""
    expression = expression.strip()
    if not expression:
        return "❌ 表达式为空"
    if len(expression) > _MAX_EXPR_LEN:
        return "❌ 表达式过长"
    # 常见中文/千分位写法容错：全角符号转半角；数字间的千分位逗号无法安全区分，直接拒绝
    expression = expression.replace("（", "(").replace("）", ")").replace("×", "*").replace("÷", "/")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(right) > _MAX_POW:
                raise ValueError("指数过大")
            return _BINOPS[type(n.op)](left, right)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return v if isinstance(n.op, ast.UAdd) else -v
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in _FUNCS and not n.keywords):
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        if isinstance(n, (ast.Tuple, ast.List)):
            return [ev(e) for e in n.elts]
        raise ValueError(f"不支持的表达式元素: {type(n).__name__}")

    try:
        node = ast.parse(expression, mode="eval")
        result = ev(node)
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError, OverflowError) as e:
        return f"❌ 计算失败: {e}（表达式中请勿使用千分位逗号或变量名）"

    # 顶层结果为序列 → 几乎必然是把 43,990 这类千分位数字误写成了元组
    if isinstance(result, list):
        return "❌ 计算失败: 表达式被解析为多个值，请检查数字中是否误用了千分位逗号（写 43990 而非 43,990）"

    if isinstance(result, float):
        if result == int(result) and abs(result) < 1e15:
            return str(int(result))
        return f"{round(result, 6)}"
    return str(result)


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": (
                "精确计算算术表达式，用于金额加总、比例、税额等一切数值计算。"
                "禁止心算——任何不能直接引用现成数字的计算都必须调用本工具。"
                "支持 + - * / // % ** 括号及 round/abs/min/max/sum。"
                "数字不得带千分位逗号（写 43990 而非 43,990）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "算术表达式，如 16820/43990*100",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_table",
            "description": (
                "对用户已上传的表格数据执行只读 SQL 查询（SQLite 方言，仅允许 SELECT/WITH）。"
                "适用于精确统计、筛选、排序、分组汇总，以及多文件/多表 JOIN 交叉勾稽对账。"
                "可用的表名与列名见消息中的 [数据表清单]；中文列名需用双引号包裹，"
                "如 SELECT \"报销人\", SUM(\"金额\") FROM t1 GROUP BY \"报销人\"。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "单条只读 SQL 语句",
                    }
                },
                "required": ["sql"],
            },
        },
    },
]
