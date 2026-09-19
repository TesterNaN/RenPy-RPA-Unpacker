"""Extract the RPA archive machinery from a game's ``renpy/loader.py`` via AST.

Why AST instead of the string surgeries this project used to rely on: Ren'Py has
shipped several incompatible spellings of the same code over the years.  The
handler methods are plain functions in some versions and ``@staticmethod`` in
others, ``RWopsIO`` lived inside ``loader.py`` before it moved to
``renpy.pygame.rwobject``, and function order and comments change freely.  An
offset-based cut silently produces a half-function that happens to import.

This module instead:

1. parses ``loader.py`` with :mod:`ast`;
2. locates the definitions it needs *by name*, keeping the original node object
   (so the real source is reproduced faithfully, decorators and all);
3. walks the definitions' referenced names to compute a dependency closure, so
   a handler that needs ``loads`` pulls in ``loads``;
4. prunes a small shim registry down to only the names that cannot be resolved
   from the source at all (``RWopsIO``, ``loads``, ``renpy``);
5. emits one self-contained module with :func:`ast.unparse`.

The result is structurally correct by construction rather than by luck, and it
adapts to a Ren'Py version the author never saw.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

__all__ = [
    "ExtractionError",
    "ExtractionResult",
    "SourceIndex",
    "arc_files_fields",
    "archive_extensions",
    "archive_headers",
    "extract_module",
    "handler_names",
]


class ExtractionError(RuntimeError):
    """The target source could not be used to build an archive reader."""


# ---------------------------------------------------------------------------
# Names that must never be treated as an extractable dependency.
# ---------------------------------------------------------------------------

def _builtin_names() -> frozenset[str]:
    """Every name Python provides in a fresh module scope.

    Resolving one of these against the source would be actively harmful.  The
    motivating case: ``class RPAv3ArchiveHandler(object):`` reads the name
    ``object``, which is not defined in loader.py, so a naive closure walk would
    try to supply it -- and because ``build()`` nests every definition inside one
    function, the injected parameter shadowed the builtin and turned the handler
    class into ``type``.  Builtins must be left alone because Python already
    provides them.
    """
    import builtins as _builtins

    return frozenset(dir(_builtins))


_AMBIENT_NAMES = _builtin_names() | {
    "__name__",
    "__file__",
    "__doc__",
    "__builtins__",
    "self",
    "cls",
    "True",
    "False",
    "None",
}


def _collect_load_names(node: ast.AST | None, *, annotations: bool = True) -> set[str]:
    """Return every name *node* reads, across all of its nested scopes."""
    if node is None:
        return set()

    names: set[str] = set()

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.Name):
            # Load means "this code reads this name".
            if isinstance(current.ctx, ast.Load):
                names.add(current.id)
        elif isinstance(current, ast.Attribute):
            # Only the *root* of an attribute chain matters: `renpy.config` reads
            # the global `renpy`.
            root = current
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and isinstance(root.ctx, ast.Load):
                names.add(root.id)
        elif isinstance(current, ast.Global | ast.Nonlocal):
            names.update(current.names)
        elif isinstance(current, ast.Call) and not annotations:
            pass

        for child in ast.iter_child_nodes(current):
            if not annotations and isinstance(child, ast.arg):
                # Skip the annotation, keep the parameter default if any lives
                # under it (defaults hang off the FunctionDef, not the arg).
                continue
            visit(child)

    if isinstance(node, ast.ClassDef):
        # Class *bases* are evaluated eagerly at class-creation time, so they are
        # the real hazards; annotations inside the body may be lazy.
        for base in node.bases:
            visit(base)
        for keyword in node.keywords:
            visit(keyword)
        for decorator in node.decorator_list:
            visit(decorator)
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(stmt)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue  # docstring
            else:
                visit(stmt)
        return names - _AMBIENT_NAMES

    visit(node)
    return names - _AMBIENT_NAMES


# ---------------------------------------------------------------------------
# Source indexing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ModuleAssignment:
    """A module-level assignment that extracted code may depend on."""

    name: str
    node: ast.stmt
    line: int
    #: Names read by the assignment's value, e.g. ``ArchiveHandlers`` for
    #: ``archive_handlers = ArchiveHandlers()``.
    dependencies: frozenset[str]


@dataclass
class SourceIndex:
    """A name -> AST node index over one ``loader.py``."""

    tree: ast.Module
    source: str
    filename: str
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    assignments: dict[str, _ModuleAssignment] = field(default_factory=dict)
    imported_modules: set[str] = field(default_factory=set)
    imported_names: dict[str, str] = field(default_factory=dict)
    order: dict[str, int] = field(default_factory=dict)
    #: Archive handlers the loader registers, in the order it registers them.
    registered_handlers: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, source: str, filename: str = "<loader.py>") -> SourceIndex:
        index = cls(tree=ast.parse(source, filename=filename), source=source, filename=filename)
        index._scan(index.tree.body, nested=False)
        index.registered_handlers = handler_names(source)
        return index

    def _scan(self, body: Sequence[ast.stmt], *, nested: bool) -> None:
        for position, stmt in enumerate(body):
            self._scan_statement(stmt, position, nested=nested)

    def _scan_statement(self, stmt: ast.stmt, position: int, *, nested: bool) -> None:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".")[0]
                self.imported_modules.add(bound)
                self.order.setdefault(bound, position)
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                bound = alias.asname or alias.name
                self.imported_names[bound] = f"{stmt.module}.{alias.name}"
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not nested:
                self.functions.setdefault(stmt.name, stmt)
                self.order.setdefault(stmt.name, position)
        elif isinstance(stmt, ast.ClassDef):
            if not nested:
                self.classes.setdefault(stmt.name, stmt)
                self.order.setdefault(stmt.name, position)
            # Nested classes are only reachable through their parent.
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            else:
                targets, value = ([stmt.target], stmt.value)
            if value is None:
                return
            dependencies = frozenset(_collect_load_names(value))
            for target in targets:
                for name in self._target_names(target):
                    self.assignments.setdefault(
                        name,
                        _ModuleAssignment(
                            name=name,
                            node=stmt,
                            line=stmt.lineno,
                            dependencies=dependencies,
                        ),
                    )
                    self.order.setdefault(name, position)
        elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            # Definitions guarded by `if renpy.android:` or wrapped in try/except
            # are still real definitions; recurse without promoting nested ones.
            for child_body in self._block_bodies(stmt):
                self._scan(child_body, nested=nested)

    @staticmethod
    def _target_names(target: ast.expr) -> Iterable[str]:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                yield from SourceIndex._target_names(element)

    @staticmethod
    def _block_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
        bodies: list[list[ast.stmt]] = []
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(stmt, attr, None)
            if isinstance(block, list):
                bodies.append(block)
        handlers = getattr(stmt, "handlers", None)
        if handlers:
            bodies.extend(handler.body for handler in handlers)
        return bodies

    def resolve(self, name: str) -> str:
        """How *name* can be satisfied: definition kind, ``module``, ``imported``, or ``missing``."""
        if name in self.functions:
            return "function"
        if name in self.classes:
            return "class"
        if name in self.assignments:
            return "assignment"
        if name in self.imported_modules:
            return "module"
        if name in self.imported_names:
            return "imported"
        return "missing"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """The generated module plus a report of how it was built."""

    source: str
    generated_names: list[str]
    shim_names: list[str]
    missing_names: list[str]
    #: Names registered by a module-level ``archive_handlers.append(...)`` call.
    #: These are the handlers the loader actually offers, which is not a fixed set
    #: -- modified loaders add their own (e.g. an AES-encrypted archive format).
    handler_names: list[str] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"  from loader.py : {', '.join(self.generated_names) or '(none)'}"]
        if self.shim_names:
            lines.append(f"  runtime shims  : {', '.join(self.shim_names)}")
        if self.missing_names:
            lines.append(f"  NOT FOUND      : {', '.join(self.missing_names)}")
        for note in self.notes:
            lines.append(f"  note           : {note}")
        return "\n".join(lines)


def arc_files_fields(source: str, function_name: str = "index_archives") -> list[str]:
    """Return the field names ``function_name`` unpacks out of ``arc_files``.

    Ren'Py has changed this tuple over time -- 3-tuples of
    ``(stem, ext, filename)`` in older releases, 5-tuples carrying the accepted
    headers and extensions in newer ones.  Since the code that consumes the
    tuple is right there in the module, its shape is read from the loop target
    instead of being guessed.  An empty list means no such loop was found.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the source already compiled
        return []

    target: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            target = node
            break
    if target is None:
        return []

    for node in ast.walk(target):
        if not isinstance(node, ast.For):
            continue
        if not _iterates_arc_files(node.iter):
            continue
        return _collect_target_names(node.target)
    return []


def _iterates_arc_files(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "arc_files"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        # e.g. `for entry in sorted(arc_files):`
        return any(_iterates_arc_files(arg) for arg in node.args)
    return False


def _registered_handler_name(node: ast.stmt, classes: set[str]) -> str | None:
    """Return the handler class from ``<registry>.append(CLASS)``, if that is what it is."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None

    call = node.value
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "append":
        return None
    if not call.args:
        return None

    # Narrow by receiver: the registry is named something like
    # `archive_handlers`, and anything else named `.append` is not our business.
    receiver = func.value
    if not isinstance(receiver, ast.Name) or "handler" not in receiver.id.lower():
        return None

    argument = call.args[0]
    if isinstance(argument, ast.Name) and argument.id in classes:
        return argument.id
    return None


def _method_literals(source: str, class_name: str, method_name: str) -> list[object]:
    """String *and bytes* literals inside ``class_name.method_name``, in source order.

    Both matter: extensions are ``".rpa"`` but headers are ``b"RPA-3.0 "``, and a
    str-only scan silently yields no headers at all -- which made a real
    ``steam_api.dll`` look like an archive.

    Deliberately not a "find the node named X" walk: ``ast.walk`` is
    breadth-first, so it matches the decorator/argument scaffolding before the
    body and returns nothing useful.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the source already compiled
        return []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != method_name:
                continue
            found: list[object] = []
            for inner in ast.walk(item):
                if isinstance(inner, ast.Constant) and isinstance(
                    inner.value, (str, bytes)
                ):
                    found.append(inner.value)
            return found
    return []


def archive_extensions(source: str) -> list[str]:
    """File extensions the loader treats as archives, as it declares them.

    Ren'Py decides what is an archive by asking each handler for
    ``get_supported_extensions()``, so the extension is *declared in the source*
    and not necessarily ``.rpa``.  One real game renames its archives to ``.dll``
    and its handler returns ``[".dll"]``; another returns ``[".rpa"]`` plus an
    encrypted format.  Hardcoding ``.rpa``/``.rpi`` therefore misses archives
    entirely, which is exactly what happened until this existed.
    """
    found: list[str] = []
    for name in _handlers_in_source_order(source):
        for value in _method_literals(source, name, "get_supported_extensions"):
            if isinstance(value, str) and value.startswith(".") and value not in found:
                found.append(value)
    return found


def archive_headers(source: str) -> list[bytes]:
    """Header byte strings the loader's handlers accept, as literals."""
    headers: list[bytes] = []
    for name in _handlers_in_source_order(source):
        for value in _method_literals(source, name, "get_supported_headers"):
            if isinstance(value, bytes):
                encoded = value
            elif isinstance(value, str):
                encoded = value.encode("latin-1", "replace")
            else:  # pragma: no cover - guarded by the caller's type check
                continue
            if encoded and encoded not in headers:
                headers.append(encoded)
    return headers


def _handlers_in_source_order(source: str) -> list[str]:
    names = handler_names(source)
    if names:
        return names
    # Fall back to any class that looks like a handler.
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return []
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and "handler" in node.name.lower()
    ]


def handler_names(source: str) -> list[str]:
    """Class names registered via ``archive_handlers.append(...)``, in load order.

    The set of archive handlers is *not* fixed.  A modified loader may register
    extra ones -- one real game adds an AES-256-CTR encrypted format alongside the
    stock RPA v1/v2/v3 handlers -- and those handlers are not referenced by name
    anywhere else, so a dependency walk starting from ``index_archives`` would
    never reach them.  The registration calls are the only record of what the
    loader supports, so they are read directly.

    Matching is deliberately narrow.  Ren'Py also calls
    ``file_open_callbacks.append(...)``, and plenty of unrelated code calls
    ``.append(...)`` on loop variables, so accepting every ``append`` call drags
    most of ``loader.py`` into the closure.  Only appends into a registry whose
    name mentions "handler" count, and the argument must name a *class* defined
    in this module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the source already compiled
        return []

    classes = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        name = _registered_handler_name(node, classes)
        if name is not None and name not in found:
            found.append(name)
    return found


def _collect_target_names(node: ast.expr) -> list[str]:
    """Names bound by an assignment/loop target, e.g. ``a, b = x, y``."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in node.elts:
            names.extend(_collect_target_names(element))
        return names
    return []


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _emit_definition(name: str, node: ast.stmt) -> str:
    """Unparse a definition for embedding inside a function, with the AST link."""
    ast.fix_missing_locations(node)
    # Line numbers in the copy describe THIS file, so drop the original link.
    try:
        ast.increment_lineno(node, -node.lineno)
    except Exception:  # pragma: no cover - defensive
        pass
    return _indent(ast.unparse(node))


def _emit_shim(name: str, body: str) -> str:
    return (
        f"    # --- shim: {name} (not definable from loader.py) ---\n"
        + _indent(body.strip("\n"))
        + "\n"
    )


class _Extractor:
    def __init__(
        self,
        index: SourceIndex,
        fallbacks: Mapping[str, Callable[[], str]],
        required: Sequence[str],
        forced_shims: frozenset[str] = frozenset(),
    ):
        self.index = index
        self.fallbacks = fallbacks
        self.required = list(required)
        self.forced_shims = forced_shims
        self._required_seen = set(required)
        #: Shim source blocks, emitted before the definitions that use them.
        self.shim_blocks: list[str] = []
        #: Extracted-definition source blocks, in dependency order.
        self.definition_blocks: list[str] = []
        self.emitted_assignments: set[int] = set()
        self.generated: list[str] = []
        self.shims: list[str] = []
        self.missing: list[str] = []
        self.provenance: dict[str, str] = {}
        self.notes: list[str] = []
        self._done: set[str] = set()
        self._in_progress: set[str] = set()
        self._imports_needed: set[str] = set()
        self._globals: set[str] = set()

    # -- driver ------------------------------------------------------------

    def run(self) -> ExtractionResult:
        # Handler classes are only reachable through their registration call, so
        # seed them before walking the dependency graph.
        for name in self.index.registered_handlers:
            if name not in self._required_seen:
                self._required_seen.add(name)
                self.required.append(name)

        for name in self.required:
            self._want(name, required=True, chain=[name])

        # Whether an empty result is fatal depends on the caller: a diagnostics
        # tool wants the report, while the CLI must refuse to run.  Recording it
        # in missing_names lets build_reader() make that call with a good message.
        source = self._build_module() if self.generated else ""
        return ExtractionResult(
            source=source,
            generated_names=list(self.generated),
            shim_names=list(self.shims),
            missing_names=list(self.missing),
            handler_names=list(self.index.registered_handlers),
            provenance=dict(self.provenance),
            notes=list(self.notes),
        )

    # -- emission ----------------------------------------------------------

    def _want(self, name: str, *, required: bool, chain: list[str]) -> None:
        if name in self._done or name in _AMBIENT_NAMES:
            return
        if name in self._in_progress:
            # A genuine import cycle (rare in loader.py); the name will already
            # exist at call time, so ordering it here is enough.
            return

        if name in self.forced_shims:
            self._use_fallback(name, required=required, chain=chain)
            return

        kind = self.index.resolve(name)

        if kind == "module":
            self._imports_needed.add(name)
            self.provenance[name] = "import"
            self._done.add(name)
            return

        if kind == "imported":
            self._imports_needed.add(name)
            self.provenance[name] = f"import ({self.index.imported_names[name]})"
            self._done.add(name)
            return

        if kind == "missing":
            self._use_fallback(name, required=required, chain=chain)
            return

        self._in_progress.add(name)
        try:
            if kind == "function":
                self._emit_function(name, self.index.functions[name])
            elif kind == "class":
                self._emit_class(name, self.index.classes[name])
            elif kind == "assignment":
                self._emit_assignment(name, self.index.assignments[name])
        finally:
            self._in_progress.discard(name)

        self._done.add(name)
        self.generated.append(name)
        self.provenance[name] = kind

    def _use_fallback(self, name: str, *, required: bool, chain: list[str]) -> None:
        provider = self.fallbacks.get(name)
        if provider is None:
            self._done.add(name)
            if required and name not in self.missing:
                self.missing.append(name)
                self.notes.append(
                    f"{name} is neither defined in loader.py nor available as a "
                    f"shim (reached via {' -> '.join(chain)})"
                )
            return

        # A shim's own dependencies must exist before the shim is emitted.
        body = provider()
        for dependency in sorted(_collect_load_names(ast.parse(body))):
            if dependency != name:
                self._want(dependency, required=False, chain=[*chain, dependency])

        self._done.add(name)
        self.shims.append(name)
        # Shims are unconditional, so they can all live at the top of the
        # generated function where the definitions that read them will find them.
        self.shim_blocks.append(_emit_shim(name, body))
        self.provenance[name] = "shim"
        self.notes.append(
            f"{name} is not defined in {self.index.filename}; used the built-in "
            f"shim (reached via {' -> '.join(chain)})"
        )

    def _emit_function(self, name: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dependency in self._dependencies(node):
            self._want(dependency, required=False, chain=[name, dependency])
        self.definition_blocks.append(_emit_definition(name, node) + "\n")

    def _emit_class(self, name: str, node: ast.ClassDef) -> None:
        for dependency in self._dependencies(node):
            self._want(dependency, required=False, chain=[name, dependency])
        self.definition_blocks.append(_emit_definition(name, node) + "\n")

    def _emit_assignment(self, name: str, assignment: _ModuleAssignment) -> None:
        # A single statement such as ``a, b = c, d`` binds several names; emit it
        # once, the first time any of its names is requested.
        marker = id(assignment.node)
        if marker in self.emitted_assignments:
            self.definition_blocks.append(
                f"    # ({name} is bound by the assignment above)\n"
            )
            return
        self.emitted_assignments.add(marker)

        for dependency in sorted(assignment.dependencies):
            self._want(dependency, required=False, chain=[name, dependency])
        self.definition_blocks.append(_emit_definition(name, assignment.node) + "\n")

    def _dependencies(self, node: ast.stmt) -> list[str]:
        return sorted(_collect_load_names(node))

    # -- module assembly ---------------------------------------------------

    def _build_module(self) -> str:
        # The provenance line lands inside a docstring, so the path must be a
        # valid literal: an unescaped Windows path would raise a SyntaxWarning
        # the moment the --write-core output is re-parsed or imported.
        origin = self.index.filename.replace("\\", "\\\\")
        lines = [
            '"""Generated by renpy_unpack from a Ren\'Py loader.py -- do not edit.',
            "",
            f"Archive readers recovered from: {origin}",
            "Each definition below is the game's own Ren'Py code, located by AST name",
            "lookup rather than by text offsets.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
        ]

        for name in sorted(self._imports_needed):
            lines.append(f"import {name}")

        lines.extend(
            [
                "",
                "",
                "def build():",
                '    """Return the namespace of extracted archive readers."""',
            ]
        )

        if self.shim_blocks:
            lines.append("    # --- runtime shims (names absent from loader.py) ---")
            lines.extend(block.rstrip("\n") for block in self.shim_blocks)
            lines.append("")

        lines.extend(block.rstrip("\n") for block in self.definition_blocks)

        locals_used = self._locals_needed
        if locals_used:
            lines.append("")
            lines.append("    return {")
            for name in locals_used:
                lines.append(f'        "{name}": {name},')
            lines.append("    }")
        else:  # pragma: no cover - only an empty extraction lands here
            lines.append("    return {}")

        return "\n".join(lines) + "\n"

    @property
    def _locals_needed(self) -> list[str]:
        """Names ``build()`` hands back to the caller.

        Everything the extractor actually emitted is returned.  An earlier version
        kept a hand-written list of interesting names, which silently dropped any
        definition outside it -- a modified loader's extra archive handler was
        extracted and emitted correctly and then never returned, so it looked
        unusable.  Ordering is a display concern only, so the well-known names come
        first and everything else follows in emission order.
        """
        available = list(dict.fromkeys([*self.generated, *self.shims]))

        preferred = [
            "RPAv3ArchiveHandler",
            "RPAv2ArchiveHandler",
            "RPAv1ArchiveHandler",
            "ArchiveHandlers",
            "archive_handlers",
            "index_archives",
            "load_from_archive",
            "load",
            "walkdir",
            "get_prefixes",
            "transfn",
            "load_from_filesystem",
            "archives",
            "arc_files",
            "lower_map",
            "loads",
            "renpy",
            "RenpyConfig",
            "RWopsIO",
        ]
        head = [name for name in preferred if name in available]
        tail = [name for name in available if name not in head]
        return head + tail


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_module(
    loader_source: str,
    *,
    filename: str = "<loader.py>",
    required: Sequence[str],
    fallbacks: Mapping[str, Callable[[], str]],
    forced_shims: frozenset[str] = frozenset(),
) -> ExtractionResult:
    """Build a self-contained module exposing the requested archive readers.

    *required* lists the top-level names to recover; *fallbacks* supplies source
    for names that cannot be recovered from *loader_source* itself, and
    *forced_shims* names those that must always come from *fallbacks*.

    Python 3.9 compatible on purpose: the generated module starts with
    ``from __future__ import annotations`` so modern annotations inside a
    *newer* loader.py never get evaluated on an older interpreter.
    """
    index = SourceIndex.build(loader_source, filename=filename)
    return _Extractor(index, fallbacks, required, forced_shims).run()
