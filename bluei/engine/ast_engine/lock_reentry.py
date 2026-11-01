"""Conservative, one-call-deep self-deadlock diagnostics; never an autofix."""
from __future__ import annotations

import ast
from collections import Counter


def _self_attribute(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def _entry(method: ast.FunctionDef) -> list[ast.stmt]:
    body = method.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def _ordinary_method(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.FunctionDef) and not node.decorator_list
        and bool(node.args.posonlyargs or node.args.args)
        and (node.args.posonlyargs or node.args.args)[0].arg == "self"
        and not any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node))
        and not any(isinstance(child, ast.Name) and child.id == "self"
                    and isinstance(child.ctx, (ast.Store, ast.Del)) for child in ast.walk(node))
    )


def find_lock_reentries(tree: ast.Module) -> list[ast.Call]:
    """Return locked call sites with a proven direct callee entry acquisition.

    ponytail: module-level classes and straight-line with bodies only; aliases,
    inheritance, dynamic dispatch and conditional entry acquisitions need a
    separate binding/control-flow analysis, not guessed lock identities.
    """
    if not any((isinstance(node, ast.Import) and any(alias.name == "threading" for alias in node.names))
               or (isinstance(node, ast.ImportFrom) and node.module == "threading") for node in tree.body):
        return []
    nodes = tuple(ast.walk(tree))
    bindings: Counter[str] = Counter()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] += 1
        elif isinstance(node, ast.arg):
            bindings[node.arg] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] += 1
    imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update("threading.Lock" for alias in node.names
                           if alias.name == "threading" and alias.asname is None and bindings["threading"] == 1)
        elif isinstance(node, ast.ImportFrom) and node.module == "threading" and not node.level:
            imports.update("Lock" for alias in node.names
                           if alias.name == "Lock" and alias.asname is None and bindings["Lock"] == 1)
    if not imports:
        return []
    # Explicit mutation invalidates the import/receiver identity proof.
    if any(isinstance(node, ast.Attribute) and node.attr == "Lock"
           and isinstance(node.value, ast.Name) and node.value.id == "threading"
           and isinstance(node.ctx, (ast.Store, ast.Del)) for node in nodes):
        return []
    if any((isinstance(node, ast.Name) and node.id in {"setattr", "delattr", "exec", "vars"})
           or (isinstance(node, ast.Attribute) and node.attr in {
               "setattr", "delattr", "exec", "__dict__", "__setattr__", "__delattr__"})
           for node in nodes):
        return []
    external_writes = {node.attr for node in nodes if isinstance(node, ast.Attribute)
                       and isinstance(node.ctx, (ast.Store, ast.Del)) and _self_attribute(node) is None}
    mutated_classes = set()
    for node in nodes:
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            mutated_classes.update(child.id for child in ast.walk(node) if isinstance(child, ast.Name))
    subclassed = {base.id for node in nodes if isinstance(node, ast.ClassDef)
                  for base in node.bases if isinstance(base, ast.Name)}

    matches = []
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        if cls.bases or cls.keywords or cls.decorator_list or bindings[cls.name] != 1:
            continue
        if cls.name in mutated_classes or cls.name in subclassed:
            continue
        definitions = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len({node.name for node in definitions}) != len(definitions):
            continue
        methods = {node.name: node for node in definitions if _ordinary_method(node)}
        init = methods.get("__init__")
        if init is None or {"__getattribute__", "__getattr__", "__setattr__"}.intersection(methods):
            continue
        writes = Counter(_self_attribute(node) for node in ast.walk(cls)
                         if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)))
        rebound_methods = {node.id for stmt in cls.body if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                           for node in ast.walk(stmt) if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
        methods = {name: method for name, method in methods.items()
                   if writes[name] == 0 and name not in rebound_methods and name not in external_writes}
        locks = set()
        for stmt in _entry(init):
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr)):
                break
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign):
                target = stmt.target
            else:
                continue
            name = _self_attribute(target)
            value = stmt.value
            constructor = None
            if isinstance(value, ast.Call):
                if isinstance(value.func, ast.Name) and value.func.id == "Lock":
                    constructor = "Lock"
                elif (isinstance(value.func, ast.Attribute) and value.func.attr == "Lock"
                      and isinstance(value.func.value, ast.Name) and value.func.value.id == "threading"):
                    constructor = "threading.Lock"
            if (name and writes[name] == 1 and name not in rebound_methods
                    and name not in external_writes and isinstance(value, ast.Call)
                    and not value.args and not value.keywords and constructor in imports):
                locks.add(name)
        if not locks:
            continue
        acquires = {}
        for name, method in methods.items():
            positional = method.args.posonlyargs + method.args.args
            required_keywords = [default for default in method.args.kw_defaults if default is None]
            if len(positional) - len(method.args.defaults) > 1 or required_keywords:
                continue
            body = _entry(method)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.With) and len(first.items) == 1 and first.items[0].optional_vars is None:
                lock = _self_attribute(first.items[0].context_expr)
            elif (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                  and isinstance(first.value.func, ast.Attribute) and first.value.func.attr == "acquire"
                  and not first.value.args and not first.value.keywords):
                lock = _self_attribute(first.value.func.value)
            else:
                continue
            if lock in locks:
                acquires[name] = lock

        def walk_body(body: list[ast.stmt], held: frozenset[str] = frozenset()) -> None:
            for stmt in body:
                if isinstance(stmt, ast.With):
                    if len(stmt.items) != 1 or stmt.items[0].optional_vars is not None:
                        break
                    lock = _self_attribute(stmt.items[0].context_expr)
                    if lock not in locks:
                        break
                    walk_body(stmt.body, held | {lock})
                elif isinstance(stmt, (ast.Expr, ast.Return, ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Call):
                    call = stmt.value
                    callee = _self_attribute(call.func)
                    # Only the directly evaluated no-argument call is proven;
                    # argument evaluation or an intervening call may release/rebind.
                    if not call.args and not call.keywords and acquires.get(callee) in held:
                        matches.append(call)
                    break
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    continue
                else:
                    break
        for name, method in methods.items():
            if name != "__init__":
                walk_body(_entry(method))
    return matches
