"""Polyglot function-only test harness and driver synthesizer.

When students submit LeetCode-style function-only solutions (e.g. def twoSum(nums, target) or int solve(int n))
without a main() or stdin/stdout driver, this synthesizer wraps the submission in a lightweight,
deterministic I/O harness so that test cases can actually be verified in the OS sandbox.
"""
import ast
import re
from typing import Optional, Tuple


def _synthesize_python_driver(raw_code: str) -> Optional[str]:
    """Inspects Python code via AST; if a top-level function or Solution class exists, appends an I/O driver."""
    try:
        tree = ast.parse(raw_code)
    except SyntaxError:
        return None

    # Check if a main block or explicit stdin reader already exists
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            # if __name__ == "__main__":
            if isinstance(node.test, ast.Compare):
                left = getattr(node.test.left, "id", "")
                if left == "__name__":
                    return None
        elif isinstance(node, ast.Call):
            func_id = getattr(node.func, "id", "")
            if func_id in ("input", "raw_input"):
                return None
            if isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") in ("sys", "stdin"):
                return None

    target_func = None
    target_class = None

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name != "main" and not stmt.name.startswith("_"):
                target_func = stmt.name
                break
        elif isinstance(stmt, ast.ClassDef):
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name != "__init__" and not item.name.startswith("_"):
                        target_class = stmt.name
                        target_func = item.name
                        break

    if not target_func:
        return None

    caller = f"{target_class}().{target_func}" if target_class else target_func

    harness = f"""

# ---------------------------------------------------------------------------
# Auto-synthesized test driver for function-only submission
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, json, inspect

    _target = {caller}
    _sig = inspect.signature(_target)
    _num_params = len(_sig.parameters)

    _raw = sys.stdin.read().strip()
    _args = []

    if _num_params > 0 and _raw:
        # Strategy 1: try JSON lines / JSON array
        _parsed_json = None
        try:
            _parsed_json = json.loads(_raw)
            if isinstance(_parsed_json, list) and len(_parsed_json) == _num_params:
                _args = _parsed_json
            else:
                _lines = [l.strip() for l in _raw.splitlines() if l.strip()]
                if len(_lines) == _num_params:
                    _args = [json.loads(l) for l in _lines]
                else:
                    _parsed_json = None
        except Exception:
            _parsed_json = None

        # Strategy 2: whitespace tokens converted to numbers or strings
        if not _args:
            _tokens = _raw.split()
            if len(_tokens) == _num_params:
                for _tok in _tokens:
                    try:
                        _args.append(int(_tok))
                    except ValueError:
                        try:
                            _args.append(float(_tok))
                        except ValueError:
                            _args.append(_tok)
            elif len(_raw.splitlines()) == _num_params:
                for _line in _raw.splitlines():
                    _val = _line.strip()
                    try:
                        _args.append(int(_val))
                    except ValueError:
                        try:
                            _args.append(float(_val))
                        except ValueError:
                            _args.append(_val)
            else:
                _args = [_raw]

    import inspect
    import asyncio
    try:
        _res = _target(*_args[:_num_params]) if _num_params > 0 else _target()
        if inspect.iscoroutine(_res):
            _res = asyncio.run(_res)
    except TypeError:
        # Fallback: pass single input string if signature mismatched
        _res = _target(_raw) if _num_params == 1 else None
        if inspect.iscoroutine(_res):
            _res = asyncio.run(_res)

    if _res is not None:
        if isinstance(_res, (list, dict, tuple)):
            print(json.dumps(_res))
        elif isinstance(_res, bool):
            print(str(_res))
        else:
            print(_res)
"""
    return raw_code + "\n" + harness


def _synthesize_cpp_c_driver(raw_code: str, language: str = "cpp") -> Optional[str]:
    """Synthesizes a C/C++ main driver for simple scalar function submissions."""
    if re.search(r"\bmain\s*\(", raw_code):
        return None

    # Match scalar, void, or vector function: e.g. int solve(int a, int b), void rev(int* a), vector<int> twoSum(...)
    pattern = re.compile(
        r"^(?:\s*(?:inline|static)\s+)?(void|int|long\s+long|double|float|bool|char\*|string|std::string|(?:std::)?vector<[^>]+>)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*\{",
        re.M,
    )
    m = pattern.search(raw_code)
    if not m:
        return None

    ret_type, func_name, params_str = m.group(1), m.group(2), m.group(3).strip()
    if func_name in ("if", "for", "while", "switch", "catch"):
        return None

    if not params_str or params_str == "void":
        param_list = []
    else:
        param_list = [p.strip() for p in params_str.split(",") if p.strip()]

    # Parse param types and names
    param_vars = []
    cpp_inputs = []
    for i, p in enumerate(param_list):
        # Strip C++ default argument: e.g. "int x = 10" -> "int x"
        p_clean = re.sub(r"\s*=.*$", "", p).strip()
        p_clean = re.sub(r"const\s+|\s*&\s*", " ", p_clean).strip()
        parts = p_clean.split()
        if len(parts) >= 2:
            p_type, p_name = " ".join(parts[:-1]), parts[-1]
        elif len(parts) == 1:
            p_type, p_name = parts[0], f"arg_{i}"
        else:
            return None
        is_ptr = "*" in p_clean
        p_type = p_type.replace("*", "").strip()
        p_name = p_name.replace("*", "").strip()
        if is_ptr:
            p_type = f"{p_type}*"
        param_vars.append((p_type, p_name))
        cpp_inputs.append(p_name)

    args_call = ", ".join(name for _, name in param_vars)

    if language in ("cpp", "c++"):
        declarations_list = []
        cin_inputs = []
        for pt, pn in param_vars:
            if "*" in pt:
                base_type = pt.replace("*", "").strip()
                declarations_list.append(f"{base_type} _val_{pn}{{}}; {base_type}* {pn} = &_val_{pn};")
                cin_inputs.append(f"_val_{pn}")
            else:
                declarations_list.append(f"{pt} {pn}{{}};")
                cin_inputs.append(pn)

        declarations = "\n    ".join(declarations_list)
        reads = " >> ".join(["std::cin"] + cin_inputs) + ";" if cin_inputs else ""
        if ret_type == "void":
            print_block = f"""    {func_name}({args_call});"""
        elif "vector" in ret_type:
            print_block = f"""    auto _ans = {func_name}({args_call});
    for (size_t _i = 0; _i < _ans.size(); ++_i) {{
        std::cout << _ans[_i] << (_i + 1 == _ans.size() ? "" : " ");
    }}
    std::cout << "\\n";"""
        else:
            print_block = f"""    auto _ans = {func_name}({args_call});
    std::cout << std::boolalpha << _ans << "\\n";"""

        harness = f"""
#include <iostream>
#include <string>
#include <vector>

int main() {{
    std::ios_base::sync_with_stdio(false);
    std::cin.tie(NULL);
    {declarations}
    {reads}
{print_block}
    return 0;
}}
"""
        return raw_code + "\n" + harness
    else:
        # C driver (supports basic ints/floats/pointers/void)
        decl_lines = []
        fmt_parts = []
        scan_args = []
        for pt, pn in param_vars:
            is_ptr = "*" in pt
            base_type = pt.replace("*", "").strip()
            if "long long" in base_type:
                fmt = "%lld"
            elif "double" in base_type:
                fmt = "%lf"
            elif "float" in base_type:
                fmt = "%f"
            else:
                fmt = "%d"
            fmt_parts.append(fmt)

            if is_ptr:
                decl_lines.append(f"{base_type} _val_{pn} = 0; {base_type}* {pn} = &_val_{pn};")
                scan_args.append(pn)
            else:
                decl_lines.append(f"{base_type} {pn} = 0;")
                scan_args.append(f"&{pn}")

        scan_fmt = " ".join(fmt_parts)
        scan_args_str = ", ".join(scan_args)
        decls = "\n    ".join(decl_lines)
        scan_stmt = f'scanf("{scan_fmt}", {scan_args_str});' if scan_args else ""

        if ret_type == "void":
            call_stmt = f"    {func_name}({args_call});"
        else:
            out_fmt = "%lld\\n" if "long long" in ret_type else ("%f\\n" if "float" in ret_type or "double" in ret_type else "%d\\n")
            call_stmt = f'    printf("{out_fmt}", {func_name}({args_call}));'

        harness = f"""
#include <stdio.h>
#include <stdbool.h>

int main(void) {{
    {decls}
    {scan_stmt}
{call_stmt}
    return 0;
}}
"""
        return raw_code + "\n" + harness


def _synthesize_js_driver(raw_code: str) -> Optional[str]:
    """Synthesizes stdin/stdout harness for function-only JavaScript solutions."""
    if re.search(r"\b(?:process\.stdin|readline|readFileSync)\b", raw_code):
        return None
    # Find exported or top-level function: e.g. function solve(...) or const solve = (...) => ...
    m = re.search(r"^\s*(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)", raw_code, re.M)
    if not m:
        m = re.search(r"^\s*(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s+)?(?:\(([^)]*)\)|([a-zA-Z0-9_]+))\s*=>", raw_code, re.M)
    if not m:
        return None

    fn_name = m.group(1)
    params_str = m.group(2) or m.group(3) or ""
    param_count = len([p for p in params_str.split(",") if p.strip()])

    harness = f"""
// ---------------------------------------------------------------------------
// Auto-synthesized test driver for function-only JavaScript submission
// ---------------------------------------------------------------------------
(async () => {{
    const fs = require('fs');
    let raw = '';
    try {{ raw = fs.readFileSync(0, 'utf-8').trim(); }} catch(e) {{}}
    let args = [];
    const numParams = {param_count};
    if (numParams > 0 && raw) {{
        try {{
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed) && parsed.length === numParams) {{
                args = parsed;
            }}
        }} catch(e) {{}}
        if (args.length === 0) {{
            const tokens = raw.split(/\\s+/);
            if (tokens.length === numParams) {{
                args = tokens.map(t => isNaN(Number(t)) ? t : Number(t));
            }} else {{
                const lines = raw.split(/\\r?\\n/).map(l => l.trim()).filter(Boolean);
                if (lines.length === numParams) {{
                    args = lines.map(t => isNaN(Number(t)) ? t : Number(t));
                }} else {{
                    args = [raw];
                }}
            }}
        }}
    }}
    try {{
        const res = typeof {fn_name} === 'function' ? await {fn_name}(...args.slice(0, numParams)) : undefined;
        if (res !== undefined) {{
            if (typeof res === 'object' && res !== null) console.log(JSON.stringify(res));
            else console.log(res);
        }}
    }} catch(err) {{
        console.error(err);
    }}
}})().catch(err => console.error(err));
"""
    return raw_code + "\n" + harness


def synthesize_driver(raw_code: str, language: str) -> Tuple[str, bool]:
    """Returns (runnable_code, was_synthesized). If synthesis is not applicable, returns (raw_code, False)."""
    lang = (language or "c").lower()
    if lang == "python":
        res = _synthesize_python_driver(raw_code)
        return (res, True) if res else (raw_code, False)
    elif lang in ("cpp", "c++", "c"):
        res = _synthesize_cpp_c_driver(raw_code, lang)
        return (res, True) if res else (raw_code, False)
    elif lang in ("javascript", "js"):
        res = _synthesize_js_driver(raw_code)
        return (res, True) if res else (raw_code, False)
    return raw_code, False
