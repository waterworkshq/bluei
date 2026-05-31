import pytest

from bluei.engine.structural_hash import (
    compute_structural_hash,
    fuzzy_structural_match,
    extract_node_sequence,
    extract_operator_sequence,
    sequence_similarity,
)


def test_decorator_staticmethod():
    code = (
        "class Foo:\n"
        "    @staticmethod\n"
        "    def bar(x):\n"
        "        return x + 1\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) == 16


def test_decorator_classmethod():
    code = (
        "class Foo:\n"
        "    @classmethod\n"
        "    def bar(cls, x):\n"
        "        return x\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_decorator_property():
    code = (
        "class Foo:\n"
        "    @property\n"
        "    def bar(self):\n"
        "        return 42\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_stacked_decorators():
    code = (
        "def deco(f):\n"
        "    return f\n"
        "\n"
        "class Foo:\n"
        "    @deco\n"
        "    @staticmethod\n"
        "    def bar(x):\n"
        "        return x\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) == 16


def test_async_function_def():
    code = (
        "async def fetch(url):\n"
        "    return url\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_async_for_loop():
    code = (
        "async def process(items):\n"
        "    async for item in items:\n"
        "        pass\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_async_with_context():
    code = (
        "async def run():\n"
        "    async with open('f') as f:\n"
        "        pass\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_await_expression():
    code = (
        "async def go():\n"
        "    x = await foo()\n"
        "    return x\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_walrus_operator():
    code = (
        "if (n := len(data)) > 10:\n"
        "    print(n)\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_type_hints_function():
    code = (
        "def greet(name: str) -> str:\n"
        "    return 'hello ' + name\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_type_hints_complex():
    code = (
        "from typing import Optional, List\n"
        "def foo(x: int, y: Optional[List[str]]) -> bool:\n"
        "    return True\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_f_string():
    code = (
        "name = 'world'\n"
        "greeting = f'hello {name}'\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_raw_string():
    code = "pattern = r'\\d+'\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_byte_string():
    code = "data = b'hello'\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_list_comprehension():
    code = "squares = [x ** 2 for x in range(10)]\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_dict_comprehension():
    code = "mapping = {k: v for k, v in items}\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_set_comprehension():
    code = "uniques = {x for x in data if x > 0}\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_generator_expression():
    code = "total = sum(x for x in range(100))\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_lambda_function():
    code = "add = lambda a, b: a + b\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_lambda_in_sort():
    code = "items.sort(key=lambda x: x[1])\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_try_except_else_finally():
    code = (
        "try:\n"
        "    x = risky()\n"
        "except ValueError:\n"
        "    x = 0\n"
        "except Exception:\n"
        "    x = -1\n"
        "else:\n"
        "    x += 1\n"
        "finally:\n"
        "    cleanup()\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_try_except_bare():
    code = (
        "try:\n"
        "    do_thing()\n"
        "except:\n"
        "    pass\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_context_manager_with():
    code = (
        "with open('file.txt') as f:\n"
        "    data = f.read()\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_multiple_context_managers():
    code = (
        "with open('a') as fa, open('b') as fb:\n"
        "    pass\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_star_import():
    code = "from os.path import *\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_relative_import():
    code = "from ..utils import helper\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_regular_import():
    code = "import os\nimport sys\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_class_with_properties_and_vars():
    code = (
        "class MyClass:\n"
        "    class_var = 0\n"
        "\n"
        "    def __init__(self, val):\n"
        "        self.instance_var = val\n"
        "\n"
        "    @property\n"
        "    def value(self):\n"
        "        return self.instance_var\n"
        "\n"
        "    @value.setter\n"
        "    def value(self, v):\n"
        "        self.instance_var = v\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_nested_functions():
    code = (
        "def outer(x):\n"
        "    def inner(y):\n"
        "        return x + y\n"
        "    return inner(1)\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_nested_classes():
    code = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def method(self):\n"
        "            pass\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_hash_stability_same_code():
    code = "def foo(x, y):\n    return x + y\n"
    h1 = compute_structural_hash(code, "python")
    h2 = compute_structural_hash(code, "python")
    assert h1 == h2


def test_hash_differs_for_different_structure():
    code1 = "def foo(x):\n    return x\n"
    code2 = "def foo(x, y):\n    return x + y\n"
    h1 = compute_structural_hash(code1, "python")
    h2 = compute_structural_hash(code2, "python")
    assert h1 != h2


def test_hash_normalizes_variable_names():
    code1 = "def foo(alpha):\n    return alpha + 1\n"
    code2 = "def foo(beta):\n    return beta + 1\n"
    h1 = compute_structural_hash(code1, "python")
    h2 = compute_structural_hash(code2, "python")
    assert h1 == h2


def test_hash_normalizes_string_literals():
    code1 = "x = 'hello world'\n"
    code2 = "x = 'goodbye moon'\n"
    h1 = compute_structural_hash(code1, "python")
    h2 = compute_structural_hash(code2, "python")
    assert h1 == h2


def test_hash_normalizes_numeric_literals():
    code1 = "x = 42\n"
    code2 = "x = 99\n"
    h1 = compute_structural_hash(code1, "python")
    h2 = compute_structural_hash(code2, "python")
    assert h1 == h2


def test_syntax_error_falls_back_to_text_hash():
    code = "def foo(:\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) == 16


def test_fuzzy_match_identical_returns_one():
    code = "def foo(x):\n    return x + 1\n"
    score = fuzzy_structural_match(code, code, "python")
    assert score == 1.0


def test_fuzzy_match_similar_high_score():
    code1 = "def foo(x):\n    return x + 1\n"
    code2 = "def foo(x):\n    return x + 2\n"
    score = fuzzy_structural_match(code1, code2, "python")
    assert score > 0.8


def test_fuzzy_match_different_low_score():
    code1 = "def foo(x):\n    return x + 1\n"
    code2 = "class Bar:\n    pass\n"
    score = fuzzy_structural_match(code1, code2, "python")
    assert score < 0.5


def test_extract_node_sequence_python():
    code = "x = 1\ny = 2\n"
    seq = extract_node_sequence(code, "python")
    assert len(seq) > 0
    assert "Assign" in seq


def test_extract_operator_sequence_python():
    code = "x = a + b * c\n"
    ops = extract_operator_sequence(code, "python")
    assert "Add" in ops
    assert "Mult" in ops


def test_sequence_similarity_identical():
    assert sequence_similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0


def test_sequence_similarity_both_empty():
    assert sequence_similarity([], []) == 1.0


def test_sequence_similarity_one_empty():
    assert sequence_similarity(["a"], []) == 0.0
    assert sequence_similarity([], ["a"]) == 0.0


def test_text_hash_for_non_python():
    code = "function foo() { return 1; }"
    h = compute_structural_hash(code, "javascript")
    assert isinstance(h, str) and len(h) == 16


def test_annassign_with_type_hint():
    code = "x: int = 42\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_annassign_no_value():
    code = "x: int\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_bool_op_and():
    code = "if x and y:\n    pass\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_bool_op_or():
    code = "if x or y:\n    pass\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_chained_comparison():
    code = "if 0 < x < 10:\n    pass\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_raise_from():
    code = "raise ValueError('bad') from exc\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_dict_unpacking():
    code = "{**a, **b}\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_set_literal():
    code = "s = {1, 2, 3}\n"
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0


def test_empty_code():
    h = compute_structural_hash("", "python")
    assert isinstance(h, str) and len(h) > 0


def test_decorator_on_standalone_function():
    code = (
        "@my_decorator\n"
        "def hello():\n"
        "    pass\n"
    )
    h = compute_structural_hash(code, "python")
    assert isinstance(h, str) and len(h) > 0
