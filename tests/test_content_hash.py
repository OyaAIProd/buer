import tempfile, os
from buer import parse


def _defs(code, suffix=".py"):
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
        f.write(code); path = f.name
    try:
        return {d.qualified_name: d for d in parse.extract_defines(path)}
    finally:
        os.unlink(path)


def test_content_hash_detects_body_refactor():
    # 函数体重构：签名/调用相近，但控制流不同 → content_hash 必须不同
    v1 = (
        "def f(host, hostname):\n"
        "    host = host.lstrip('.')\n"
        "    if hostname == host:\n"
        "        return True\n"
        "    return False\n"
    )
    v2 = (
        "def f(host, hostname):\n"
        "    if ':' in host:\n"
        "        h, p = host.rsplit(':', 1)\n"
        "        if hostname == h:\n"
        "            return True\n"
        "    return False\n"
    )
    a = _defs(v1)["f"].content_hash
    b = _defs(v2)["f"].content_hash
    assert a != b, "body refactor must change content_hash"


def test_content_hash_ignores_formatting():
    v1 = "def f(x):\n    if x == 1:\n        return True\n    return False\n"
    v2 = "def f(x):\n    if x==1:   return True\n    return False\n"  # 不同空白/合并行
    assert _defs(v1)["f"].content_hash == _defs(v2)["f"].content_hash, (
        "formatting must not change content_hash"
    )


def test_content_hash_ignores_comments():
    v1 = "def f(x):\n    return x + 1\n"
    v2 = "def f(x):\n    # add one\n    return x + 1  # inline comment\n"
    assert _defs(v1)["f"].content_hash == _defs(v2)["f"].content_hash, (
        "comments must not change content_hash"
    )
