"""No bridge may reference a name it never defines -- the 500 nobody greps for.

Measured 2026-09-29, 21:48: three bridges (lingxi, codely, xhx) answered every
`stream: true` request with `500 Internal Server Error`, while /health, /v1/models
and the non-streaming probe stayed green -- so the fleet verifier happily
reported them REAL. Codex CLI always streams, so those three bridges were
unusable in Codex while every automated check said fine.

The cause was a missing import: each file used `StreamingResponse` and imported
`JSONResponse, Response` instead. A missing import is invisible to `py_compile`
(it only fails when that line executes) and invisible to any health check.

The same audit previously caught `_ChannelRetry`, raised in the WorkBuddy
bridge but never defined anywhere. This test keeps the audit in CI: it parses
every bridge/tool module, resolves names through the real scope chain (locals,
enclosing scopes, module globals, builtins) and fails on anything unresolved.
"""
import ast
import builtins
import os

import pytest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
AUDIT_ROOTS = ("bridges", "tools", "opencodex")

BUILTIN_NAMES = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__debug__", "__class__", "__module__", "__qualname__",
    "__dict__", "__annotations__", "__path__", "__all__",
}


class _Scope:
    def __init__(self, node, parent, kind):
        self.node = node
        self.parent = parent
        self.kind = kind
        self.bound = set()
        self.children = []
        self.loads = []


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for elt in target.elts:
            out.extend(_target_names(elt))
        return out
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _bind_params(scope, node):
    args = node.args
    for group in (args.posonlyargs, args.args, args.kwonlyargs):
        for arg in group:
            scope.bound.add(arg.arg)
    for arg in (args.vararg, args.kwarg):
        if arg:
            scope.bound.add(arg.arg)


def _collect(node, scope):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            sub = _Scope(child, scope, "function")
            _bind_params(sub, child)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.bound.add(child.name)
            scope.children.append(sub)
            _collect(child, sub)
        elif isinstance(child, ast.ClassDef):
            scope.bound.add(child.name)
            sub = _Scope(child, scope, "class")
            scope.children.append(sub)
            _collect(child, sub)
        elif isinstance(child, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            sub = _Scope(child, scope, "comprehension")
            for generator in child.generators:
                sub.bound.update(_target_names(generator.target))
            scope.children.append(sub)
            _collect(child, sub)
        else:
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    scope.bound.update(_target_names(target))
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                scope.bound.update(_target_names(child.target))
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                scope.bound.update(_target_names(child.target))
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        scope.bound.update(_target_names(item.optional_vars))
            elif isinstance(child, ast.ExceptHandler):
                if child.name:
                    scope.bound.add(child.name)
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    scope.bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name != "*":
                        scope.bound.add(alias.asname or alias.name)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                scope.bound.update(child.names)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                scope.loads.append((child.lineno, child.id))
            _collect(child, scope)


def _unresolved(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    root = _Scope(tree, None, "module")
    _collect(tree, root)
    found = []

    def walk(scope):
        chain = []
        cursor = scope
        while cursor is not None:
            chain.append(cursor)
            cursor = cursor.parent
        for lineno, name in scope.loads:
            if name in BUILTIN_NAMES:
                continue
            if any(name in ancestor.bound for ancestor in chain):
                continue
            found.append((path, lineno, name, scope.kind))
        for sub in scope.children:
            walk(sub)

    walk(root)
    return found


def _modules():
    for root_name in AUDIT_ROOTS:
        root = os.path.join(KIT, root_name)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in sorted(filenames):
                if filename.endswith(".py") and ".bak" not in filename:
                    yield os.path.join(dirpath, filename)


def test_every_name_is_defined_somewhere():
    problems = []
    for path in sorted(_modules()):
        try:
            problems.extend(_unresolved(path))
        except SyntaxError as exc:
            problems.append((path, exc.lineno or 0, "<syntax error: %s>" % exc.msg, "module"))
    assert not problems, "undefined names (missing import / never-defined symbol):\n" + "\n".join(
        "  %s:%s  %r  (%s scope)" % (p, l, n, k) for p, l, n, k in problems)


def test_bridges_that_stream_import_streaming_response():
    """Pin the exact regression: construct it, import it -- in the same file."""
    offenders = []
    for path in sorted(_modules()):
        if os.path.basename(path) == os.path.basename(__file__):
            continue  # this file only mentions the name inside a string
        source = open(path, encoding="utf-8").read()
        if "StreamingResponse(" not in source:
            continue
        imported = set()
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, ast.ImportFrom) and node.module and "responses" in node.module:
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update((alias.asname or alias.name).split(".")[-1] for alias in node.names)
        if "StreamingResponse" not in imported:
            offenders.append(os.path.relpath(path, KIT))
    assert not offenders, "constructs StreamingResponse without importing it: %s" % offenders


def test_audit_actually_sees_the_bridges():
    """Guard the guard: an empty walk must not pass as a green audit."""
    counted = list(_modules())
    assert len(counted) >= 30, "audit looked at only %d modules" % len(counted)
    names = [os.path.basename(p) for p in counted]
    for expected in ("core.py", "lingxi_bridge.py", "codely_bridge.py", "xhx_bridge.py"):
        assert expected in names, "audit stopped covering %s" % expected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
