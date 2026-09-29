"""Slow work must not run on the event loop.

An `async def` route body runs *on the event loop*. A blocking call inside one
does not merely make that request slow, it stops the whole application: every
other page, for every user, until it returns. With an extraction that is two
to four minutes of total unavailability, and the symptom is indistinguishable
from the server being down - including, cruelly, the settings page you would
go to in order to choose a faster model.

This is easy to reintroduce, because the blocking version looks completely
normal and works perfectly in every test that exercises one request at a
time. So it is checked structurally instead: heavy work may be *passed to*
`run_in_threadpool`, but never *called* in an async route.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROUTES = sorted(pathlib.Path("forge/web/routes").glob("*.py"))

# Calls that block for a long time: a model call, Blender, a key check, a
# catalogue fetch. Written as the dotted source spelling.
BLOCKING = {
    "extract.run",
    "runner.build_now",
    "verify",
    "openrouter.catalog",
}


def dotted(node: ast.AST) -> str:
    """`a.b.c(...)` -> "a.b.c"; `f(...)` -> "f"."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def offenders(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            # Only a *call* blocks. Passing the function object to
            # run_in_threadpool is an ast.Name/Attribute, not an ast.Call, so
            # the correct form is invisible to this check by construction.
            if isinstance(node, ast.Call) and dotted(node.func) in BLOCKING:
                found.append(f"{path.name}:{node.lineno} "
                             f"async {func.name}() calls "
                             f"{dotted(node.func)}() on the event loop")
    return found


@pytest.mark.parametrize("path", ROUTES, ids=lambda p: p.name)
def test_no_route_blocks_the_event_loop(path):
    problems = offenders(path)
    assert not problems, (
        "Blocking work called directly in an async route. This freezes every "
        "other request for the duration. Wrap it:\n"
        "    result = await run_in_threadpool(the_slow_thing, arg, arg)\n\n"
        + "\n".join(problems)
    )


def test_the_check_would_actually_catch_a_regression(tmp_path):
    """A structural test that cannot fail is worse than no test."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def analyse():\n"
        "    result = extract.run(1, 2, 3)\n"
        "    return result\n"
    )
    assert offenders(bad), "the check must notice a bare blocking call"

    good = tmp_path / "good.py"
    good.write_text(
        "async def analyse():\n"
        "    result = await run_in_threadpool(extract.run, 1, 2, 3)\n"
        "    return result\n"
    )
    assert not offenders(good), "the threaded form must pass"
