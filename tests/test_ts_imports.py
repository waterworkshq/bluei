"""Tests for TypeScript/JavaScript import extraction (Slice 2c).

Covers ESM import forms, CommonJS require, side-effect imports, dedup,
and the dispatch from extract_imports_touched.
"""

from bluei.engine.structural_hash import extract_imports_touched
from bluei.engine.structural_hash.python import _extract_ts_imports


def test_esm_default_import():
    assert _extract_ts_imports("import x from 'react';") == ["react"]


def test_esm_named_import_double_quotes():
    assert _extract_ts_imports('import { useState } from "react";') == ["react"]


def test_esm_namespace_import():
    assert _extract_ts_imports("import * as fs from 'fs';") == ["fs"]


def test_side_effect_import():
    assert _extract_ts_imports("import './polyfill.js';") == ["./polyfill.js"]


def test_commonjs_require():
    assert _extract_ts_imports("const fs = require('fs');") == ["fs"]


def test_commonjs_destructure_require():
    assert _extract_ts_imports('const { readFileSync } = require("fs");') == ["fs"]


def test_mixed_imports_deduplicated():
    code = """\
import React from 'react';
import { useState, useEffect } from "react";
import * as utils from './utils';
const path = require('path');
const { join } = require("path");
import 'core-js/stable';
"""
    assert _extract_ts_imports(code) == sorted(
        {"./utils", "core-js/stable", "path", "react"}
    )


def test_no_imports_returns_empty():
    assert _extract_ts_imports("const x = 1; console.log(x);") == []


def test_extract_imports_touched_dispatches_ts():
    code = "import x from 'react';\nconst fs = require('fs');\n"
    assert extract_imports_touched(code, language="typescript") == ["fs", "react"]


def test_extract_imports_touched_dispatches_js_aliases():
    code = "import x from 'react';\n"
    for lang in ("javascript", "tsx", "jsx"):
        assert extract_imports_touched(code, language=lang) == ["react"]


def test_python_dispatch_unchanged():
    code = "import os\nfrom pathlib import Path\n"
    assert extract_imports_touched(code, language="python") == ["os", "pathlib"]


def test_unknown_language_returns_empty():
    assert extract_imports_touched("import x from 'react';", language="go") == []
