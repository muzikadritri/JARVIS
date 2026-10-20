"""
actions/calculator.py — exact arithmetic, unit conversion and date maths.

A language model asked "what is 17.5 % of 2,340" answers from pattern, and is
wrong often enough to matter for money. This evaluates instead, with a parser
that accepts arithmetic and nothing else: the expression becomes a Python AST,
every node is checked against an allow-list, and only then is it computed.
There is no eval, no attribute access, no names beyond the listed functions
and constants, and exponents and factorials are bounded so "9**9**9" cannot
hang the assistant.
"""
from __future__ import annotations

import ast
import math
import operator
import re
from datetime import date, datetime, timedelta
from fractions import Fraction

from core import outcome

MAX_EXPONENT = 10_000
MAX_FACTORIAL = 1_000
MAX_EXPRESSION = 500

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _factorial(n):
    if n != int(n) or n < 0:
        raise ValueError("factorial needs a whole number ≥ 0")
    if n > MAX_FACTORIAL:
        raise ValueError(f"factorial is limited to {MAX_FACTORIAL}")
    return math.factorial(int(n))


def _percent(value, of):
    return value / 100 * of


_FUNCS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
    "asin": lambda x: math.degrees(math.asin(x)), "acos": lambda x: math.degrees(math.acos(x)),
    "atan": lambda x: math.degrees(math.atan(x)),
    "rsin": math.sin, "rcos": math.cos, "rtan": math.tan,
    "log": lambda x, base=10: math.log(x, base), "ln": math.log, "log2": math.log2,
    "exp": math.exp, "factorial": _factorial, "gcd": math.gcd, "lcm": math.lcm,
    "min": min, "max": max, "hypot": math.hypot, "percent": _percent,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "phi": (1 + 5 ** 0.5) / 2}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > MAX_EXPONENT:
                raise ValueError(f"exponents are limited to {MAX_EXPONENT}")
            return operator.pow(left, right)
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ValueError("that operator is not allowed")
        return op(left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"unknown name '{node.id}'")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("only numbers, + - * / // % **, parentheses and maths functions are allowed")


def _normalise(expr: str) -> str:
    text = str(expr or "").strip()[:MAX_EXPRESSION]
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("^", "**")
    text = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", text)          # 2,340 → 2340
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"percent(\1, ", text, flags=re.I)
    if "percent(" in text and text.count("(") > text.count(")"):
        text += ")" * (text.count("(") - text.count(")"))
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", text)
    text = re.sub(r"(\d+(?:\.\d+)?)!", r"factorial(\1)", text)
    return text


def evaluate(expr: str):
    tree = ast.parse(_normalise(expr), mode="eval")
    return _eval(tree)


def _format(value) -> str:
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return str(value)
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    if isinstance(value, int) and abs(value) >= 10 ** 30:
        return f"{value:.6e}"
    return str(value)


# ─────────────────────────────────────────────────────────────────────────────
#  Units: every unit maps to a factor of its dimension's base unit
# ─────────────────────────────────────────────────────────────────────────────
_UNITS: dict[str, tuple[str, float]] = {}


def _add(dim: str, factor: float, *names: str) -> None:
    for n in names:
        _UNITS[n] = (dim, factor)


_add("length", 1, "m", "meter", "meters", "metre", "metres", "meter")
_add("length", 1e-3, "mm", "millimeter", "millimeters")
_add("length", 1e-2, "cm", "centimeter", "centimeters", "zentimeter")
_add("length", 1e3, "km", "kilometer", "kilometers", "kilometre", "kilometres")
_add("length", 0.0254, "in", "inch", "inches", "zoll")
_add("length", 0.3048, "ft", "foot", "feet", "fuss")
_add("length", 0.9144, "yd", "yard", "yards")
_add("length", 1609.344, "mi", "mile", "miles", "meile", "meilen")
_add("length", 1852, "nmi", "nautical_mile", "seemeile")
_add("mass", 1, "kg", "kilogram", "kilograms", "kilo")
_add("mass", 1e-3, "g", "gram", "grams", "gramm")
_add("mass", 1e-6, "mg", "milligram", "milligrams")
_add("mass", 1000, "t", "tonne", "tonnes", "ton")
_add("mass", 0.45359237, "lb", "lbs", "pound", "pounds", "pfund")
_add("mass", 0.028349523125, "oz", "ounce", "ounces", "unze")
_add("mass", 6.35029318, "st", "stone")
_add("volume", 1, "l", "liter", "liters", "litre", "litres")
_add("volume", 1e-3, "ml", "milliliter", "milliliters")
_add("volume", 1e-2, "cl", "centiliter")
_add("volume", 3.785411784, "gal", "gallon", "gallons")
_add("volume", 0.946352946, "qt", "quart", "quarts")
_add("volume", 0.473176473, "pt", "pint", "pints")
_add("volume", 0.2365882365, "cup", "cups", "tasse")
_add("volume", 0.0295735295625, "floz", "fl_oz", "fluid_ounce")
_add("volume", 0.01478676478125, "tbsp", "tablespoon", "esslöffel")
_add("volume", 0.00492892159375, "tsp", "teaspoon", "teelöffel")
_add("volume", 1000, "m3", "cubic_meter")
_add("speed", 1, "m/s", "mps")
_add("speed", 1 / 3.6, "km/h", "kmh", "kph")
_add("speed", 0.44704, "mph")
_add("speed", 0.514444, "kn", "knot", "knots", "knoten")
_add("time", 1, "s", "sec", "second", "seconds", "sekunde", "sekunden")
_add("time", 1e-3, "ms", "millisecond", "milliseconds")
_add("time", 60, "min", "minute", "minutes", "minuten")
_add("time", 3600, "h", "hr", "hour", "hours", "stunde", "stunden")
_add("time", 86400, "d", "day", "days", "tag", "tage")
_add("time", 604800, "wk", "week", "weeks", "woche", "wochen")
_add("time", 31557600, "yr", "year", "years", "jahr", "jahre")
_add("data", 1, "b", "byte", "bytes")
_add("data", 1 / 8, "bit", "bits")
_add("data", 1e3, "kb", "kilobyte", "kilobytes")
_add("data", 1e6, "mb", "megabyte", "megabytes")
_add("data", 1e9, "gb", "gigabyte", "gigabytes")
_add("data", 1e12, "tb", "terabyte", "terabytes")
_add("data", 1024, "kib", "kibibyte")
_add("data", 1024 ** 2, "mib", "mebibyte")
_add("data", 1024 ** 3, "gib", "gibibyte")
_add("data", 1024 ** 4, "tib", "tebibyte")
_add("area", 1, "m2", "sqm", "square_meter", "square_meters", "quadratmeter")
_add("area", 1e6, "km2", "square_kilometer")
_add("area", 1e4, "ha", "hectare", "hectares", "hektar")
_add("area", 4046.8564224, "acre", "acres")
_add("area", 0.09290304, "ft2", "sqft", "square_foot", "square_feet")
_add("energy", 1, "j", "joule", "joules")
_add("energy", 1e3, "kj", "kilojoule")
_add("energy", 4.184, "cal", "calorie")
_add("energy", 4184, "kcal", "kilocalorie", "kilocalories")
_add("energy", 3.6e6, "kwh", "kilowatt_hour")
_add("pressure", 1, "pa", "pascal")
_add("pressure", 1e5, "bar")
_add("pressure", 6894.757, "psi")
_add("pressure", 101325, "atm")

_TEMPS = {"c": "c", "celsius": "c", "°c": "c", "f": "f", "fahrenheit": "f", "°f": "f",
          "k": "k", "kelvin": "k"}


def _unit_key(text: str) -> str:
    return str(text or "").strip().lower().replace(" ", "_").replace("²", "2").replace("³", "3")


def convert(value: float, src: str, dst: str) -> float:
    a, b = _unit_key(src), _unit_key(dst)
    if a in _TEMPS and b in _TEMPS:
        celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[_TEMPS[a]]
        return {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[_TEMPS[b]]
    if a not in _UNITS:
        raise ValueError(f"unknown unit '{src}'")
    if b not in _UNITS:
        raise ValueError(f"unknown unit '{dst}'")
    (dim_a, fa), (dim_b, fb) = _UNITS[a], _UNITS[b]
    if dim_a != dim_b:
        raise ValueError(f"cannot convert {dim_a} ({src}) into {dim_b} ({dst})")
    return value * fa / fb


def _parse_date(text: str) -> date:
    text = str(text or "").strip().lower()
    if text in ("", "today", "heute", "now"):
        return date.today()
    if text in ("tomorrow", "morgen"):
        return date.today() + timedelta(days=1)
    if text in ("yesterday", "gestern"):
        return date.today() - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text.replace(",", ""), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"could not read the date '{text}' — use YYYY-MM-DD")


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def calculator(parameters: dict, player=None, **_ignored) -> str:
    params = parameters or {}
    action = str(params.get("action") or "calculate").strip().lower()
    try:
        if action in ("calculate", "calc", "math", "evaluate"):
            expr = str(params.get("expression") or "").strip()
            if not expr:
                return outcome.ask("calculator", "What should be calculated?")
            value = evaluate(expr)
            facts = {"expression": expr, "result": _format(value)}
            if isinstance(value, float) and not value.is_integer() and abs(value) < 1e6:
                frac = Fraction(value).limit_denominator(1000)
                if abs(float(frac) - value) < 1e-12 and frac.denominator != 1:
                    facts["fraction"] = f"{frac.numerator}/{frac.denominator}"
            return outcome.done("calculator", **facts)

        if action == "convert":
            value = float(evaluate(str(params.get("value") if params.get("value") is not None else "1")))
            src, dst = params.get("from_unit"), params.get("to_unit")
            if not src or not dst:
                return outcome.ask("calculator", "From which unit to which unit?")
            out = convert(value, str(src), str(dst))
            return outcome.done("calculator", value=_format(value), from_unit=src,
                                to_unit=dst, result=_format(round(out, 10)))

        if action in ("days_between", "date_diff"):
            start, end = _parse_date(params.get("date") or ""), _parse_date(params.get("date2") or "")
            delta = (end - start).days
            weekdays = sum(1 for i in range(abs(delta))
                           if (min(start, end) + timedelta(days=i)).weekday() < 5)
            return outcome.done("calculator", start=start.isoformat(), end=end.isoformat(),
                                days=delta, weeks=round(delta / 7, 2), workdays=weekdays)

        if action in ("add_days", "date_add"):
            start = _parse_date(params.get("date") or "")
            days = int(float(evaluate(str(params.get("value") or "0"))))
            result = start + timedelta(days=days)
            return outcome.done("calculator", start=start.isoformat(), added_days=days,
                                result=result.isoformat(), weekday=result.strftime("%A"))

        if action == "weekday":
            day = _parse_date(params.get("date") or "")
            return outcome.done("calculator", date=day.isoformat(), weekday=day.strftime("%A"),
                                iso_week=day.isocalendar()[1])

        return outcome.failed("calculator", f"unknown action '{action}'",
                              valid="calculate,convert,days_between,add_days,weekday")
    except ZeroDivisionError:
        return outcome.failed("calculator", "division by zero")
    except (ValueError, SyntaxError, TypeError, OverflowError) as e:
        return outcome.failed("calculator", str(e) or type(e).__name__)


TOOL = {
    "name": "calculator",
    "description": (
        "Exact maths — use it for ANY calculation instead of computing in your "
        "head. Actions: 'calculate' (arithmetic, percentages like '17.5% of "
        "2340', powers, roots, trig in degrees, log/ln, factorial), 'convert' "
        "(length, mass, volume, temperature, speed, time, data sizes, area, "
        "energy, pressure), 'days_between', 'add_days', 'weekday'."
    ),
    "brief": "Exact maths, unit conversion and date arithmetic.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "calculate | convert | days_between | add_days | weekday"},
            "expression": {"type": "STRING", "description": "calculate: e.g. 'sqrt(2)*15^2'"},
            "value": {"type": "STRING", "description": "convert/add_days: the number"},
            "from_unit": {"type": "STRING", "description": "convert: e.g. 'mi', 'lb', 'f'"},
            "to_unit": {"type": "STRING", "description": "convert: e.g. 'km', 'kg', 'c'"},
            "date": {"type": "STRING", "description": "A date (YYYY-MM-DD, today, tomorrow)"},
            "date2": {"type": "STRING", "description": "days_between: the second date"},
        },
        "required": ["action"],
    },
    "handler": calculator,
}
