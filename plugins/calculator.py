"""Example AVRONE plugin: safe arithmetic (no eval; walks the AST)."""
from __future__ import annotations

import ast
import math
import operator

from avrone.tools import Tool

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max, "log": math.log,
          "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "floor": math.floor, "ceil": math.ceil}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
MAX_EXPONENT = 10_000


def safe_eval(expr: str) -> float | int:
    if len(expr) > 500:
        raise ValueError("expression too long")
    tree = ast.parse(expr.replace("^", "**"), mode="eval")

    def ev(node: ast.AST):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
                and not node.keywords):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")

    return ev(tree)


async def calculate(ctx, args: dict) -> str:
    expr = str(args.get("expression", "")).strip()
    if not expr:
        return "ERROR: give an expression, e.g. {\"expression\": \"(2+3)*4\"}"
    try:
        result = safe_eval(expr)
    except ZeroDivisionError:
        return "ERROR: division by zero"
    except (ValueError, SyntaxError, TypeError, OverflowError) as e:
        return f"ERROR: {e}"
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        result = int(result)
    return f"{expr} = {result}"


def register(registry, settings) -> None:
    registry.register(Tool(
        "calculator",
        "evaluate an arithmetic expression exactly (+ - * / // % ** ^, sqrt, log, sin, pi, e...).",
        '{"expression": str}', calculate, source="plugin:calculator"))
