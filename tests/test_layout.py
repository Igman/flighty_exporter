"""The package's dependency rule, checked by reading its imports.

Like the ignore-rules test, this looks at the repository rather than the
command line's output. Every module of ``src/flighty_exporter`` belongs to one
group (a subpackage, or one of the two top-level groups), and a group may
import only from the groups listed for it in ``ALLOWED``:

- flights imports nothing else from the package (pure domain);
- mail may import only the record types (``flights.records``);
- ollama may import the message type from mail and the record types;
- the top-level core (config, store, progress, tables) may import only
  flights;
- stages and output may import from everything below them, but not from
  each other, and neither imports the command line.

No module imports a name beginning with an underscore from another module,
and there are no import cycles between groups. The imports are read with
``ast``, including ones inside functions, so nothing is executed.

A new module must be placed in a group here on purpose: an unknown top-level
module or subpackage fails the test.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "flighty_exporter"
SOURCE = Path(__file__).resolve().parents[1] / "src" / PACKAGE

SUBPACKAGES = ("flights", "mail", "ollama", "stages", "output")
TOP_LEVEL = {
    "__init__": "core",
    "config": "core",
    "store": "core",
    "progress": "core",
    "tables": "core",
    "cli": "entry",
    "__main__": "entry",
}

ALLOWED: dict[str, set[str]] = {
    "flights": set(),
    "mail": {"flights"},
    "ollama": {"mail", "flights"},
    "core": {"flights"},
    "stages": {"core", "ollama", "mail", "flights"},
    "output": {"core", "ollama", "mail", "flights"},
    "entry": {"output", "stages", "core", "ollama", "mail", "flights"},
}

# Narrower rules inside an allowed edge: (importing group, imported group) ->
# the only modules it may import from, and (if not None) the only names.
NARROW: dict[tuple[str, str], tuple[set[str], set[str] | None]] = {
    ("mail", "flights"): ({"flights.records"}, None),
    ("ollama", "flights"): ({"flights.records"}, None),
    ("ollama", "mail"): ({"mail.reader"}, {"Message"}),
}


@dataclass(frozen=True)
class Import:
    source: str  # importing module, relative to the package ("stages.audit")
    target: str  # imported module, relative to the package ("tables")
    name: str | None  # the imported name, None for a whole module
    line: int


def modules(root: Path) -> dict[str, Path]:
    """Every module of the package at ``root``, by dotted name relative to it
    ("cli", "stages.audit"; a subpackage's ``__init__`` is "stages")."""
    found = {}
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__" and len(parts) > 1:
            parts.pop()
        found[".".join(parts)] = path
    return found


def group_of(module: str) -> str | None:
    head = module.split(".")[0]
    if head in SUBPACKAGES:
        return head
    return None if "." in module else TOP_LEVEL.get(module)


def _resolved(module: str, path: Path, known: dict[str, Path], package: str):
    """(node, imported module, imported name or None, local name) for every
    import of a package module in ``module``'s source, including ones inside
    functions."""
    here = [] if module == "__init__" else module.split(".")
    container = here if path.name == "__init__.py" else here[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(package + "."):
                    target = alias.name[len(package) + 1:] or "__init__"
                    yield node, target, None, alias.asname or alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = container[: len(container) - (node.level - 1)]
                parts = base + (node.module.split(".") if node.module else [])
            elif node.module and (node.module == package or node.module.startswith(package + ".")):
                parts = node.module.split(".")[1:]
            else:
                continue
            target = ".".join(parts) or "__init__"
            for alias in node.names:
                submodule = f"{target}.{alias.name}" if parts else alias.name
                if submodule in known:
                    yield node, submodule, None, alias.asname or alias.name
                else:
                    yield node, target, alias.name, alias.asname or alias.name


def imports(root: Path, package: str = PACKAGE) -> list[Import]:
    """Every import of a package module by a package module."""
    known = modules(root)
    return [
        Import(module, target, name, node.lineno)
        for module, path in known.items()
        for node, target, name, _ in _resolved(module, path, known, package)
    ]


def private_attributes(root: Path, package: str = PACKAGE) -> list[tuple[str, str, int]]:
    """Reads of underscore names through an imported module, e.g.
    ``from . import review`` then ``review._helper``: (module, expression, line)."""
    known = modules(root)
    found = []
    for module, path in known.items():
        bound = {local for _, _, name, local in _resolved(module, path, known, package) if name is None}
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in bound
                and _private(node.attr)
            ):
                found.append((module, f"{node.value.id}.{node.attr}", node.lineno))
    return found


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def rule_violations(root: Path, package: str = PACKAGE) -> list[str]:
    problems = []
    for module in modules(root):
        if group_of(module) is None:
            problems.append(f"{module} is in no group: add it to the layout on purpose")
    for imp in imports(root, package):
        source = group_of(imp.source)
        target = group_of(imp.target)
        if source is None or target is None or source == target:
            continue
        what = imp.target + (f".{imp.name}" if imp.name else "")
        if target not in ALLOWED[source]:
            problems.append(f"{imp.source} (line {imp.line}): {source} may not import {target} ({what})")
            continue
        rule = NARROW.get((source, target))
        if rule is not None:
            allowed_modules, allowed_names = rule
            if imp.target not in allowed_modules or (allowed_names is not None and imp.name not in allowed_names):
                problems.append(
                    f"{imp.source} (line {imp.line}): {source} may import only "
                    f"{', '.join(sorted(allowed_modules))}"
                    f"{' (' + ', '.join(sorted(allowed_names)) + ')' if allowed_names else ''} from {target}, "
                    f"not {what}"
                )
    return problems


def private_imports(root: Path, package: str = PACKAGE) -> list[str]:
    problems = []
    for imp in imports(root, package):
        if imp.name is not None and _private(imp.name) and imp.target != imp.source:
            problems.append(f"{imp.source} (line {imp.line}) imports {imp.name} from {imp.target}")
    for module, expression, line in private_attributes(root, package):
        problems.append(f"{module} (line {line}) reads {expression}")
    return problems


def cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Each cycle in the graph once, as the path that closes it."""
    found: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    state: dict[str, int] = {}  # 1: on the current path, 2: done
    path: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt]
                if frozenset(cycle) not in seen:
                    seen.add(frozenset(cycle))
                    found.append(cycle)
            elif nxt not in state:
                visit(nxt)
        path.pop()
        state[node] = 2

    for node in sorted(edges):
        if node not in state:
            visit(node)
    return found


def group_edges(root: Path, package: str = PACKAGE) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for imp in imports(root, package):
        source = group_of(imp.source)
        target = group_of(imp.target)
        if source and target and source != target:
            edges.setdefault(source, set()).add(target)
    return edges


def module_edges(root: Path, package: str = PACKAGE) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for imp in imports(root, package):
        if imp.target != imp.source:
            edges.setdefault(imp.source, set()).add(imp.target)
    return edges


# -- the package ------------------------------------------------------------------


def test_every_import_follows_the_dependency_rule():
    problems = rule_violations(SOURCE)
    assert not problems, "\n".join(problems)


def test_no_module_imports_an_underscore_name_from_another():
    problems = private_imports(SOURCE)
    assert not problems, "\n".join(problems)


def test_no_import_cycles_between_groups():
    found = cycles(group_edges(SOURCE))
    assert not found, "\n".join(" -> ".join(cycle) for cycle in found)


def test_no_import_cycles_between_modules():
    found = cycles(module_edges(SOURCE))
    assert not found, "\n".join(" -> ".join(cycle) for cycle in found)


def test_the_checks_see_the_package():
    # Guards against a check that passes because it read nothing.
    found = {(imp.source, imp.target) for imp in imports(SOURCE)}
    assert ("stages.audit", "tables") in found
    assert ("ollama.decision", "mail.reader") in found
    assert ("cli", "stages.audit") in found  # from .stages import audit
    assert {"mail", "ollama", "core", "stages", "output", "entry"} <= set(group_edges(SOURCE))


# -- the checks themselves, on a small invented package -----------------------------


def _package(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for sub in SUBPACKAGES:
        (root / sub).mkdir(exist_ok=True)
        (root / sub / "__init__.py").touch()
    return root


def test_a_forbidden_import_is_reported(tmp_path):
    root = _package(
        tmp_path,
        {
            "store.py": "",
            "flights/records.py": "from ..store import Store\n",
            "mail/reader.py": "from ..flights.consolidate import Flight\n",
            "flights/consolidate.py": "",
            "ollama/decision.py": "from ..mail.reader import Mailbox\nfrom ..mail.reader import Message\n",
            "stages/classify.py": "from ..cli import main\n",
            "cli.py": "",
        },
    )
    problems = rule_violations(root)
    assert any("flights.records" in p and "flights may not import core" in p for p in problems)
    assert any("mail.reader" in p and "not flights.consolidate.Flight" in p for p in problems)
    assert any("ollama.decision" in p and "not mail.reader.Mailbox" in p for p in problems)
    assert not any("mail.reader.Message" in p for p in problems)
    assert any("stages.classify" in p and "may not import entry" in p for p in problems)


def test_an_unplaced_module_is_reported(tmp_path):
    root = _package(tmp_path, {"engine.py": "", "screens/__init__.py": ""})
    problems = rule_violations(root)
    assert "engine is in no group: add it to the layout on purpose" in problems
    assert "screens is in no group: add it to the layout on purpose" in problems


def test_an_underscore_import_is_reported(tmp_path):
    root = _package(
        tmp_path,
        {
            "output/review.py": "def _helper(): pass\n",
            "stages/audit.py": "from ..output.review import _helper\n",
            "cli.py": "from .output import review\nreview._helper()\n",
        },
    )
    problems = private_imports(root)
    assert "stages.audit (line 1) imports _helper from output.review" in problems
    assert "cli (line 2) reads review._helper" in problems


def test_a_cycle_is_reported(tmp_path):
    root = _package(
        tmp_path,
        {
            "stages/audit.py": "from ..output.review import write\n",
            "output/review.py": "from ..stages.extract import PASS_NAME\n",
            "stages/extract.py": "",
        },
    )
    assert cycles(group_edges(root)) == [["output", "stages", "output"]]
    assert cycles(module_edges(root)) == []
