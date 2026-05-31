"""Tests for bluei.engine.ast_engine.context — AST parent maps, scope lookups, and queries."""

import ast

import pytest

from bluei.engine.ast_engine.context import (
    build_parent_map,
    extract_function_calls,
    extract_method_chain,
    extract_variable_names,
    find_enclosing_class,
    find_enclosing_function,
    find_enclosing_loop,
    find_parent,
    function_has_isinstance_guard,
    get_caller_object,
    get_function_param_names,
    get_source_segment,
    is_in_except_handler,
    is_in_return_statement,
)


class TestBuildParentMap:
    def test_simple_function(self):
        tree = ast.parse("def foo(): pass")
        pmap = build_parent_map(tree)
        assert len(pmap) > 0

    def test_module_is_not_child(self):
        tree = ast.parse("x = 1")
        pmap = build_parent_map(tree)
        assert tree not in pmap

    def test_assignment_parent_is_module(self):
        tree = ast.parse("x = 1")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                assert pmap[node] is tree
                break

    def test_nested_structure(self):
        source = """
class Foo:
    def bar(self):
        return 1
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        class_def = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)][0]
        func_def = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        assert pmap[func_def] is class_def
        assert pmap[class_def] is tree

    def test_empty_module(self):
        tree = ast.parse("")
        pmap = build_parent_map(tree)
        assert len(pmap) == 0


class TestFindParent:
    def test_returns_parent(self):
        tree = ast.parse("x = 1")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                assert find_parent(node, pmap) is tree
                break

    def test_returns_none_for_root(self):
        tree = ast.parse("x = 1")
        pmap = build_parent_map(tree)
        assert find_parent(tree, pmap) is None

    def test_returns_none_for_unknown(self):
        tree = ast.parse("x = 1")
        pmap = build_parent_map(tree)
        fake = ast.Name(id="fake")
        assert find_parent(fake, pmap) is None


class TestFindEnclosingFunction:
    def test_finds_function(self):
        source = """
def foo():
    x = 1
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        func = find_enclosing_function(assign, pmap)
        assert func is not None
        assert func.name == "foo"

    def test_finds_async_function(self):
        source = """
async def bar():
    y = 2
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        func = find_enclosing_function(assign, pmap)
        assert func is not None
        assert func.name == "bar"

    def test_no_enclosing_function(self):
        tree = ast.parse("x = 1")
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        assert find_enclosing_function(assign, pmap) is None

    def test_finds_function_in_class(self):
        source = """
class C:
    def method(self):
        z = 3
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        func = find_enclosing_function(assign, pmap)
        assert func.name == "method"


class TestFindEnclosingClass:
    def test_finds_class(self):
        source = """
class Foo:
    x = 1
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        cls = find_enclosing_class(assign, pmap)
        assert cls is not None
        assert cls.name == "Foo"

    def test_no_class(self):
        source = """
def foo():
    x = 1
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        assert find_enclosing_class(assign, pmap) is None

    def test_nested_class(self):
        source = """
class Outer:
    class Inner:
        val = 42
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        const = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        cls = find_enclosing_class(const, pmap)
        assert cls.name == "Inner"


class TestFindEnclosingLoop:
    def test_finds_for_loop(self):
        source = """
for i in range(10):
    x = i
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        loop = find_enclosing_loop(assign, pmap)
        assert isinstance(loop, ast.For)

    def test_finds_while_loop(self):
        source = """
while True:
    x = 1
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        loop = find_enclosing_loop(assign, pmap)
        assert isinstance(loop, ast.While)

    def test_no_loop(self):
        source = "x = 1"
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        assert find_enclosing_loop(assign, pmap) is None


class TestExtractVariableNames:
    def test_simple_assign(self):
        tree = ast.parse("x = 1; y = 2")
        names = extract_variable_names(tree)
        assert "x" in names
        assert "y" in names

    def test_nested_names(self):
        source = """
def foo(a, b):
    c = a + b
    return c
"""
        tree = ast.parse(source)
        names = extract_variable_names(tree)
        assert "a" in names
        assert "b" in names
        assert "c" in names

    def test_no_names(self):
        tree = ast.parse("pass")
        assert extract_variable_names(tree) == []


class TestExtractMethodChain:
    def test_simple_chain(self):
        source = "a.b().c()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        chain = extract_method_chain(call)
        assert "c" in chain
        assert "b" in chain

    def test_single_call(self):
        source = "obj.method()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        chain = extract_method_chain(call)
        assert "method" in chain

    def test_name_base(self):
        source = "x.y()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        chain = extract_method_chain(call)
        assert chain == ["y"]

    def test_attribute_base(self):
        source = "a.b.c()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        chain = extract_method_chain(call)
        assert "c" in chain

    def test_empty_chain(self):
        source = "foo()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        chain = extract_method_chain(call)
        assert chain == []


class TestExtractFunctionCalls:
    def test_attribute_calls(self):
        source = """
def foo():
    obj.method()
    bar()
"""
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        calls = extract_function_calls(func)
        assert "method" in calls
        assert "bar" in calls

    def test_no_calls(self):
        source = "def foo(): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        assert extract_function_calls(func) == []

    def test_nested_calls(self):
        source = """
def foo():
    return a.b().c()
"""
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        calls = extract_function_calls(func)
        assert "b" in calls
        assert "c" in calls


class TestGetSourceSegment:
    def test_with_source_segment(self):
        source = "x = 1 + 2"
        tree = ast.parse(source)
        assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
        seg = get_source_segment(source, assign)
        assert "1 + 2" in seg or "=" in seg

    def test_line_range_fallback(self):
        source = "x = 1\ny = 2\nz = 3"
        tree = ast.parse(source)
        names = [n for n in ast.walk(tree) if isinstance(n, ast.Name)]
        for name in names:
            if (
                hasattr(name, "lineno")
                and hasattr(name, "end_lineno")
                and name.lineno
                and name.end_lineno
            ):
                seg = get_source_segment(source, name)
                assert isinstance(seg, str)
                break

    def test_no_lineno(self):
        source = "x = 1"
        node = ast.Constant(value=42)
        result = get_source_segment(source, node)
        assert result == ""

    def test_empty_source(self):
        source = ""
        node = ast.Constant(value=42)
        result = get_source_segment(source, node)
        assert result == ""


class TestIsInReturnStatement:
    def test_in_return(self):
        source = """
def foo():
    return bar()
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        assert is_in_return_statement(call, pmap) is True

    def test_not_in_return(self):
        source = """
def foo():
    bar()
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        assert is_in_return_statement(call, pmap) is False

    def test_nested_in_return(self):
        source = """
def foo():
    return baz(qux())
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        inner = [
            c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "qux"
        ][0]
        assert is_in_return_statement(inner, pmap) is True


class TestIsInExceptHandler:
    def test_in_except(self):
        source = """
try:
    pass
except Exception:
    handle()
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        assert is_in_except_handler(call, pmap) is True

    def test_not_in_except(self):
        source = """
def foo():
    handle()
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        assert is_in_except_handler(call, pmap) is False

    def test_in_try_not_except(self):
        source = """
try:
    risky()
except:
    safe()
"""
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        risky_call = [
            c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "risky"
        ][0]
        assert is_in_except_handler(risky_call, pmap) is False


class TestGetFunctionParamNames:
    def test_regular_args(self):
        source = "def foo(a, b, c): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        names = get_function_param_names(func)
        assert names == ["a", "b", "c"]

    def test_vararg(self):
        source = "def foo(*args): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        names = get_function_param_names(func)
        assert "args" in names

    def test_kwonly(self):
        source = "def foo(*, key): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        names = get_function_param_names(func)
        assert "key" in names

    def test_kwarg(self):
        source = "def foo(**kwargs): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        names = get_function_param_names(func)
        assert "kwargs" in names

    def test_all_params(self):
        source = "def foo(a, *args, key, **kwargs): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        names = get_function_param_names(func)
        assert names == ["a", "args", "key", "kwargs"]

    def test_no_params(self):
        source = "def foo(): pass"
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        assert get_function_param_names(func) == []


class TestFunctionHasIsinstanceGuard:
    def test_has_guard(self):
        source = """
def foo(x):
    if isinstance(x, str):
        return x.upper()
"""
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        assert function_has_isinstance_guard(func) is True

    def test_no_guard(self):
        source = """
def foo(x):
    return x + 1
"""
        tree = ast.parse(source)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        assert function_has_isinstance_guard(func) is False


class TestGetCallerObject:
    def test_attribute_call(self):
        source = "obj.method()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        assert get_caller_object(call) == "obj"

    def test_non_attribute_call(self):
        source = "func()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        assert get_caller_object(call) is None

    def test_nested_attribute(self):
        source = "a.b.method()"
        tree = ast.parse(source)
        call = [n for n in ast.walk(tree) if isinstance(n, ast.Call)][0]
        result = get_caller_object(call)
        assert result is None
