"""Tests for the AST extraction core.

The central claim of this rewrite is that the recovered definitions *are* the
game's own Ren'Py code, not a paraphrase.  ``test_extracted_definitions_match_loader_exactly``
is what holds that claim honest: it re-unparses both the original loader.py and
the extracted source and compares them normalised, so any drift in the
extraction logic fails loudly instead of silently producing a subtly wrong
reader.
"""

from __future__ import annotations

import ast
import io
import sys
import textwrap
import unittest
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from renpy_unpack import _fallbacks  # noqa: E402
from renpy_unpack.ast_extract import (  # noqa: E402
    SourceIndex,
    extract_module,
)

from test_support import sdk_loaders  # noqa: E402

REQUIRED = (
    "RPAv3ArchiveHandler",
    "RPAv2ArchiveHandler",
    "RPAv1ArchiveHandler",
    "ArchiveHandlers",
    "index_archives",
    "load_from_archive",
    "archives",
    "arc_files",
)

FALLBACKS = _fallbacks.sources()


def real_loaders() -> list[Path]:
    return sdk_loaders()


def _sdk_source() -> str:
    loaders = real_loaders()
    if not loaders:
        raise unittest.SkipTest("no Ren'Py SDK loader.py available")
    return loaders[0].read_text(encoding="utf-8-sig")


def sdk_source() -> str:
    """The stock ``loader.py`` source, or skip just the calling test.

    Deliberately a call, not a module-level constant.  Evaluating this at import
    time raised ``SkipTest`` while the module was being imported, and unittest then
    skips the *entire module* -- which silently discarded every SDK-independent
    test in this file (about half of it) on any machine without Ren'Py installed.
    That is most machines, including CI.
    """
    return _sdk_source()

from rpa_factory import DISGUISED_LOADER, EXTRA_HANDLER_LOADER  # noqa: E402


def snippet_of(source: str, node: ast.AST) -> str:
    """Return the source text of *node*, ending at its last line of code."""
    lines = source.splitlines()
    start = node.lineno - 1
    end = (getattr(node, "end_lineno", None) or node.lineno) - 1
    return "\n".join(lines[start : end + 1])


def _normalise_body(text: str) -> list[str]:
    """Dedent *text*, reflow it through ``ast.unparse``, and drop blank lines.

    ``ast.unparse`` is applied to the snippet on its own so the result does not
    depend on how deeply it was nested in the file it came from.
    """
    tree = ast.parse(textwrap.dedent(text))
    node = tree.body[0]
    assert isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign)), node
    return [line for line in ast.unparse(node).splitlines() if line.strip()]


# A loader.py written the way Ren'Py 6.x did: no @staticmethod, read_index takes
# self, RWopsIO defined locally, and `loads` bound by a plain assignment.
LEGACY_LOADER = '''
import io
import zlib
import pickle

loads = pickle.loads


class RWopsIO(object):
    def __init__(self, f, mode="rb"):
        self.f = f

    @staticmethod
    def from_buffer(data, name=""):
        return io.BytesIO(data)

    @staticmethod
    def from_split(a, b, name=""):
        return io.BytesIO(a.read() + b.read())


class ArchiveHandlers(object):
    def __init__(self):
        self.exts = {}
        self.peek = {}

    def append(self, handler):
        candidates = []
        header_sizes = []

        for header in handler.get_supported_headers():
            candidates.append((header, handler))
            header_sizes.append(len(header))

        peek = max(header_sizes)

        for ext in handler.get_supported_extensions():
            self.exts.setdefault(ext, []).extend(candidates)
            self.peek[ext] = max(self.peek.get(ext, 0), peek)

    def spec(self, ext):
        return self.peek[ext], self.exts[ext]


archive_handlers = ArchiveHandlers()


class RPAv3ArchiveHandler(object):
    def get_supported_extensions(self):
        return [".rpa"]

    def get_supported_headers(self):
        return ["RPA-3.0 "]

    def read_index(self, infile):
        l = infile.read(40)
        offset = int(l[8:24], 16)
        key = int(l[25:33], 16)
        infile.seek(offset)
        index = loads(zlib.decompress(infile.read()))

        for k in index.keys():
            index[k] = [(o ^ key, d ^ key) for o, d in index[k]]

        return index


archive_handlers.append(RPAv3ArchiveHandler)

arc_files = []
archives = []


def index_archives():
    arc_files.sort(reverse=True)
    archives[:] = []

    for stem, ext, fn in arc_files:
        with open(fn, "rb") as f:
            peek, handlers = archive_handlers.spec(ext)
            file_header = f.read(peek)

            for header, handler in handlers:
                if not file_header.startswith(header):
                    continue

                f.seek(0, 0)
                index = handler.read_index(f)
                archives.append((fn, index))
                break


def load_from_archive(name):
    for afn, index in archives:
        if name not in index:
            continue

        offset, dlen = index[name][0]
        if offset is None:
            return None

        return io.BufferedReader(RWopsIO(afn, "rb"))
'''


@unittest.skipUnless(real_loaders(), "no Ren'Py SDK loader.py available")
class TestExtractionAgainstRealLoader(unittest.TestCase):
    """Extraction must reproduce the SDK's real code byte-for-byte, modulo format."""

    @classmethod
    def setUpClass(cls):
        cls.loader = real_loaders()[0]
        cls.source = cls.loader.read_text(encoding="utf-8-sig")
        cls.result = extract_module(
            cls.source,
            filename=str(cls.loader),
            required=REQUIRED,
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        cls.tree = ast.parse(cls.source)

    def original_node(self, name: str) -> ast.stmt:
        """Find the loader.py statement that defines *name* (def, class or assign)."""
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
                return node
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if name in targets:
                    return node
        self.fail(f"{name} not found in the original loader.py")

    def test_extracted_definitions_match_loader_exactly(self):
        """The recovered text must be the loader's own code, not a paraphrase.

        Both sides are pushed through ``ast.unparse`` first, because the emitted
        source is generated by ``ast.unparse`` and therefore normalises quote
        style and drops comments -- neither of which changes behaviour.  What the
        comparison *does* catch is any structural drift: a lost statement, a
        reordered branch, or the indentation changes that broke the previous
        text-offset approach.

        The generated module nests every definition inside ``build()``, adding one
        indentation level, so the snippets are dedented before comparing.  Plain
        string comparison would otherwise fail on docstring reflow alone.
        """
        emitted = self._emitted_definitions()

        for name in REQUIRED:
            self.assertIn(name, emitted, f"{name} is missing from the generated module")
            expected = _normalise_body(snippet_of(self.source, self.original_node(name)))
            actual = _normalise_body(emitted[name])
            self.assertEqual(expected, actual, f"{name} was not reproduced faithfully")

    def _emitted_definitions(self) -> dict[str, str]:
        """Text of each required definition in the generated module."""
        module = ast.parse(self.result.source)
        build = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "build"
        )
        found = {}
        for node in build.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in REQUIRED:
                found[node.name] = snippet_of(self.result.source, node)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in REQUIRED:
                    found[target.id] = snippet_of(self.result.source, node)
        return found

    def test_all_handlers_recovered(self):
        for name in ("RPAv1ArchiveHandler", "RPAv2ArchiveHandler", "RPAv3ArchiveHandler"):
            self.assertIn(name, self.result.generated_names)

    def test_rwopsio_is_not_taken_from_loader(self):
        # loader.py imports RWopsIO from renpy.pygame.rwobject, so a local
        # definition must not be used even though the name appears in the file.
        self.assertIn("RWopsIO", self.result.shim_names)

    def test_missing_names_is_empty(self):
        self.assertEqual(self.result.missing_names, [])

    def test_generated_module_prunes_unused_shims(self):
        self.assertNotIn("RenpyConfig", self.result.shim_names)

        # Only modules the surviving definitions actually reference get copied.
        generated = ast.parse(self.result.source)
        imported = set()
        for node in generated.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertEqual(imported, {"io", "zlib"}, imported)
        self.assertNotIn("threading", imported)
        self.assertNotIn("unicodedata", imported)

    def test_generated_module_has_no_syntax_warning(self):
        # A Windows path inside the module docstring used to raise
        # SyntaxWarning: invalid escape sequence.
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(self.result.source, "<generated>", "exec")

    def test_global_state_is_hoisted_out_of_build(self):
        """Issue #1, re-created: a ``global`` read before any write must not break.

        The reporter's ``loader.py`` had ``old_config_archives = None`` at module
        level and an ``index_archives()`` that read it before writing it::

            def index_archives():
                global old_config_archives
                if old_config_archives == renpy.config.archives:
                    return
                old_config_archives = list(renpy.config.archives)

        The old hand-assembled ``core.py`` dropped the initialiser.  The AST
        extractor found it -- and emitted it as a local of ``build()``, one scope
        too deep: a ``global`` statement resolves against the *module* namespace,
        so the copy raised the very same NameError.  Stock 8.5.2 never shows this
        because the only functions there that use ``global`` (``auto_init``,
        ``auto_quit``, ``auto_thread_function``, ``check_autoreload``) fall outside
        the readers' dependency closure.
        """
        probe = (
            "\n\nold_config_archives = None\n\n\n"
            "def _reindex_probe():\n"
            "    global old_config_archives\n"
            "    if old_config_archives == renpy.config.archives:\n"
            "        return False\n"
            "    old_config_archives = list(renpy.config.archives)\n"
            "    return True\n"
        )
        result = extract_module(
            self.source + probe,
            filename=str(self.loader),
            required=(*REQUIRED, "_reindex_probe"),
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        self.assertIn("_reindex_probe", result.generated_names)

        # The initialiser must be a module-level statement, not a build() local.
        module_level = [
            target.id
            for node in ast.parse(result.source).body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        self.assertIn("old_config_archives", module_level)

        # And the recovered function must actually work, twice: the first call
        # re-indexes, the second short-circuits -- Ren'Py's own semantics.
        generated: dict = {}
        exec(compile(result.source, "<generated>", "exec"), generated)
        namespace = generated["build"]()
        first = namespace["_reindex_probe"]()
        second = namespace["_reindex_probe"]()
        self.assertTrue(first)
        self.assertFalse(second)

    def test_global_declared_names_are_collected(self):
        from renpy_unpack.ast_extract import _global_declared_names

        tree = ast.parse(
            "a = 1\nb = 2\n\n"
            "def f():\n    global a\n    return a\n\n"
            "def g():\n    global a, b\n    return a + b\n"
        )
        self.assertEqual(_global_declared_names(tree), frozenset({"a", "b"}))
        self.assertEqual(_global_declared_names(ast.parse("c = 3\n")), frozenset())


class TestExtractionFallbacks(unittest.TestCase):
    """A loader.py missing a piece degrades to a shim, and says so."""

    def test_legacy_loader_resolves_rwopsio_from_source(self):
        # This loader defines RWopsIO itself, but it is still a forced shim: the
        # runtime replacement is the one with the tested windowing behaviour.
        result = extract_module(
            LEGACY_LOADER,
            filename="<legacy>",
            required=REQUIRED,
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        self.assertIn("RWopsIO", result.shim_names)
        compile(result.source, "<generated>", "exec")

        # This loader only ships a v3 handler, so v1/v2 are legitimately absent
        # -- and reported as such rather than silently faked.
        self.assertEqual(
            sorted(result.missing_names), ["RPAv1ArchiveHandler", "RPAv2ArchiveHandler"]
        )

    def test_legacy_loader_preserves_missing_staticmethod_style(self):
        # The point of AST extraction: a loader whose read_index takes `self`
        # must keep taking `self`.  An offset-based cut produced code that only
        # worked for one style.
        result = extract_module(
            LEGACY_LOADER,
            filename="<legacy>",
            required=REQUIRED,
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        self.assertIn("def read_index(self, infile):", result.source)

        namespace = {}
        exec(compile(result.source, "<generated>", "exec"), namespace)
        built = namespace["build"]()
        handler = built["RPAv3ArchiveHandler"]()
        self.assertTrue(callable(handler.read_index))

    def test_shim_is_reported_with_its_dependency_chain(self):
        loader = LEGACY_LOADER.replace("loads = pickle.loads", "")
        result = extract_module(
            loader,
            filename="<legacy>",
            required=REQUIRED,
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        self.assertIn("loads", result.shim_names)
        self.assertTrue(
            any("RPAv3ArchiveHandler -> loads" in note for note in result.notes),
            result.notes,
        )

    def test_unresolvable_required_name_is_reported(self):
        result = extract_module(
            "x = 1\n",
            filename="<empty>",
            required=("index_archives",),
            fallbacks={},
            forced_shims=frozenset(),
        )
        self.assertIn("index_archives", result.missing_names)
        self.assertEqual(result.source, "")

    def test_nothing_recovered_yields_an_empty_result_not_a_crash(self):
        # extract_module reports; it is build_reader()'s job to treat an empty
        # result as fatal, so the diagnostics stay inspectable.
        result = extract_module(
            "x = 1\n",
            filename="<empty>",
            required=("index_archives",),
            fallbacks={},
            forced_shims=frozenset(),
        )
        self.assertEqual(result.generated_names, [])


class TestHandlerDiscovery(unittest.TestCase):
    """Archive handlers must be discovered from the loader, not assumed.

    A modified loader can register formats beyond the stock RPA v1/v2/v3 -- one
    real release adds an AES-256-CTR encrypted type.  Those handlers are only
    reachable through their registration call, so a dependency walk starting at
    ``index_archives`` never finds them.
    """

    def test_registered_handlers_are_found_in_order(self):
        from renpy_unpack.ast_extract import handler_names

        found = handler_names(LEGACY_LOADER)
        self.assertEqual(found, ["RPAv3ArchiveHandler"])

    def test_extra_handler_beyond_the_stock_three_is_found(self):
        from renpy_unpack.ast_extract import handler_names

        found = handler_names(EXTRA_HANDLER_LOADER)
        self.assertEqual(found, ["ZXEncryptedArchiveHandler", "RPAv3ArchiveHandler"])

    def test_file_open_callbacks_append_is_not_a_handler(self):
        # `file_open_callbacks.append(load_from_filesystem)` is a different
        # registry holding a plain function, not archive handlers.  Counting it
        # drags loader.py's whole helper graph into the closure -- it once pulled
        # in `import DownloadNeeded`, which does not exist outside a game.
        from renpy_unpack.ast_extract import handler_names

        found = handler_names(EXTRA_HANDLER_LOADER)
        for not_a_handler in ("load_from_filesystem", "load_from_archive"):
            self.assertNotIn(not_a_handler, found)

        stock = handler_names(sdk_source())
        self.assertEqual(
            stock,
            ["RPAv3ArchiveHandler", "RPAv2ArchiveHandler", "RPAv1ArchiveHandler"],
            "the stock loader registers exactly three formats and nothing else",
        )

    def test_handler_list_is_not_polluted_by_non_classes(self):
        """Only classes count, so loop targets and plain functions stay out.

        The historical bug accepted any name passed to any ``.append()``, which
        swept in `stem`/`prefix` (loop targets) and the `file_open_callbacks`
        entries.  Those then showed up as failed extractions of things that are
        not definitions at all.
        """
        source = (
            "class RealHandler:\n"
            "    pass\n"
            "\n"
            "def a_function():\n"
            "    pass\n"
            "\n"
            "archive_handlers.append(RealHandler)\n"
            "archive_handlers.append(a_function)\n"
            "\n"
            "for stem, ext in arc_files:\n"
            "    other.append(stem)\n"
        )
        from renpy_unpack.ast_extract import handler_names

        self.assertEqual(handler_names(source), ["RealHandler"])

    def test_missing_handler_name_is_reported_not_treated_as_a_definition(self):
        from renpy_unpack.ast_extract import extract_module

        result = extract_module(
            sdk_source(),
            filename="<sdk>",
            required=(*REQUIRED, "prefix"),
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        # `prefix` is a function parameter, not a definition: it belongs in
        # missing_names rather than being invented or silently ignored.
        self.assertIn("prefix", result.missing_names)

    def test_unreadable_handler_degrades_instead_of_failing(self):
        """A handler that cannot be extracted costs one format, not the run.

        A real release registers an AES-encrypted format whose decryption lives in
        a compiled native module.  The handler is emitted but unusable, and the
        tool still has to read the unencrypted archives in the same install.
        """
        from renpy_unpack.core import build_reader, prepare_reader

        # Drop the extra handler from the emitted source, emulating "extracted but
        # unusable", then confirm the reader still comes up on the stock handlers.
        namespace, extraction = build_reader(
            EXTRA_HANDLER_LOADER, filename="<extra>"
        )
        self.assertIn("ZXEncryptedArchiveHandler", namespace)

        del namespace["ZXEncryptedArchiveHandler"]
        reader = prepare_reader(namespace, extraction)
        self.assertIn("ZXEncryptedArchiveHandler", " ".join(extraction.notes))
        self.assertEqual(
            [handler.__name__ for handler in reader.handlers],
            ["RPAv3ArchiveHandler"],
        )

    def test_extraction_reaches_the_extra_handler(self):
        result = extract_module(
            EXTRA_HANDLER_LOADER,
            filename="<extra>",
            required=REQUIRED,
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        self.assertIn("ZXEncryptedArchiveHandler", result.generated_names)
        self.assertIn("ZXEncryptedArchiveHandler", result.handler_names)

    def test_build_returns_every_emitted_definition(self):
        """`build()` must hand back everything it emitted.

        It once returned a hand-written list of interesting names, so an extra
        handler was extracted and emitted correctly and then dropped on the floor
        -- it looked like the loader could not read that format at all.
        """
        result = extract_module(
            EXTRA_HANDLER_LOADER,
            filename="<extra>",
            required=REQUIRED,
            fallbacks=FALLBACKS,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
        namespace: dict = {}
        exec(compile(result.source, "<generated>", "exec"), namespace)
        built = namespace["build"]()

        missing = [name for name in result.generated_names if name not in built]
        self.assertEqual(missing, [], "build() dropped extracted definitions")

    def test_stock_loader_registers_exactly_the_three_formats(self):
        from renpy_unpack.ast_extract import handler_names

        found = handler_names(sdk_source())
        self.assertEqual(
            found,
            ["RPAv3ArchiveHandler", "RPAv2ArchiveHandler", "RPAv1ArchiveHandler"],
        )

    def test_declared_extensions_come_from_the_handlers(self):
        """Extensions are declared in the source, so they must be read from it.

        A repacked release renames its archives -- one real game calls them
        ``.dll`` and hides them among system libraries -- so assuming ``.rpa``
        finds nothing on a game that is full of archives.
        """
        from renpy_unpack.ast_extract import archive_extensions

        self.assertEqual(
            archive_extensions(sdk_source()), [".rpa", ".rpi"]
        )
        self.assertEqual(archive_extensions(DISGUISED_LOADER), [".dll", ".rpi"])

    def test_headers_are_read_as_bytes_literals(self):
        """Headers are ``b"..."``, not ``"..."``.

        Scanning only for str literals yields no headers at all, which silently
        disables the header sniff and lets a genuine ``steam_api.dll`` through as an
        archive.
        """
        from renpy_unpack.ast_extract import archive_headers

        stock = archive_headers(sdk_source())
        self.assertIn(b"RPA-3.0 ", stock)
        self.assertIn(b"RPA-2.0 ", stock)
        self.assertIn(b"x\x9c", stock)

        self.assertIn(b"ILOVEYOU", archive_headers(DISGUISED_LOADER))

    def test_extension_and_header_scans_ignore_unrelated_methods(self):
        # `_method_literals` must not pick up strings from elsewhere in the class,
        # such as a docstring or an unrelated attribute.
        from renpy_unpack.ast_extract import archive_extensions, archive_headers

        source = (
            "class RPAv9ArchiveHandler:\n"
            '    """Handles .txt files and says RPA-9.0 in prose."""\n'
            '    archive_extension = ".zip"\n'
            "\n"
            "    @staticmethod\n"
            "    def get_supported_extensions():\n"
            '        return [".rpa"]\n'
            "\n"
            "    @staticmethod\n"
            "    def get_supported_headers():\n"
            '        return [b"RPA-9.0 "]\n'
            "\n"
            "\n"
            "archive_handlers.append(RPAv9ArchiveHandler)\n"
        )
        self.assertEqual(archive_extensions(source), [".rpa"])
        self.assertIn(b"RPA-9.0 ", archive_headers(source))
        self.assertNotIn(b".txt", archive_headers(source))


class TestSourceIndex(unittest.TestCase):
    def test_indexes_guarded_definitions(self):
        source = "import renpy\n\nif renpy.android:\n    def guarded():\n        return 1\n"
        index = SourceIndex.build(source)
        self.assertEqual(index.resolve("guarded"), "function")

    def test_classifies_each_definition_kind(self):
        index = SourceIndex.build(LEGACY_LOADER)
        self.assertEqual(index.resolve("index_archives"), "function")
        self.assertEqual(index.resolve("RPAv3ArchiveHandler"), "class")
        self.assertEqual(index.resolve("archive_handlers"), "assignment")
        self.assertEqual(index.resolve("io"), "module")
        self.assertEqual(index.resolve("loads"), "assignment")
        self.assertEqual(index.resolve("nope"), "missing")

    def test_annotation_stripping_tolerates_modern_syntax(self):
        from renpy_unpack.core import strip_annotations

        modern = "def f(a: dict[str, int], b: list[int] = []) -> dict[str, int]:\n    return {}\n"
        stripped = strip_annotations(modern)
        self.assertNotIn("dict[str, int]", stripped)
        compile(stripped, "<stripped>", "exec")


class TestRuntimeShims(unittest.TestCase):
    def test_rwopsio_window_read(self):
        from renpy_unpack.runtime_shims import RWopsIO

        window = io.BufferedReader(RWopsIO(__file__, "rb", base=4, length=6))
        try:
            self.assertEqual(len(window.read()), 6)
        finally:
            window.close()

    def test_rwopsio_rejects_unset_length_correctly(self):
        # With length=None the window is the rest of the file, and tells/seek
        # must agree with what was actually read.
        from renpy_unpack.runtime_shims import RWopsIO

        raw = RWopsIO(__file__, "rb", base=0)
        self.assertEqual(len(raw.read()), len(Path(__file__).read_bytes()))
        self.assertEqual(raw.tell(), len(Path(__file__).read_bytes()))
        raw.seek(2)
        self.assertEqual(raw.tell(), 2)
        raw.close()

    def test_python2_style_index_pickle_is_accepted(self):
        # `_codecs.encode` is emitted by pickle itself when str/bytes are
        # round-tripped through protocol 2, so it must not trip the allowlist.
        import pickle

        from renpy_unpack.runtime_shims import pickle_loads

        payload = {b"game/a.rpy": [(0, 4)], b"game/b.png": [(4, 8, b"headbytes")]}
        self.assertEqual(pickle_loads(pickle.dumps(payload, protocol=2)), payload)

    def test_restricted_unpickler_rejects_classes(self):
        import pickle

        from renpy_unpack.runtime_shims import UnsupportedPickleError, pickle_loads

        # A payload whose reduce() resolves a callable outside the allowlist.
        hostile = pickle.dumps({"k": [(0, 1)], "evil": eval}, protocol=2)
        with self.assertRaises(UnsupportedPickleError):
            pickle_loads(hostile)

    def test_restricted_unpickler_allows_index_shapes(self):
        import pickle

        from renpy_unpack.runtime_shims import pickle_loads

        for payload in (
            {"a.txt": [(10, 20)], "b/c.bin": [(30, 40, b"head")]},
            {b"py2.txt": [(1, 2)]},
        ):
            self.assertEqual(pickle_loads(pickle.dumps(payload, protocol=2)), payload)

    def test_unsafe_mode_bypasses_restriction(self):
        import pickle

        from renpy_unpack.runtime_shims import pickle_loads

        payload = {"k": [(0, 1)]}
        self.assertEqual(pickle_loads(pickle.dumps(payload), unsafe=True), payload)


class TestPathSafety(unittest.TestCase):
    def test_rejects_escapes(self):
        from renpy_unpack.core import is_safe_relative

        for bad in (
            "../evil.txt",
            "a/../../evil.txt",
            "/etc/passwd",
            "C:\\Windows\\system32\\drivers\\etc\\hosts",
            "\\\\server\\share\\x",
            "..",
            "",
            "game/./x",
        ):
            self.assertFalse(is_safe_relative(bad), bad)

    def test_accepts_normal_names(self):
        from renpy_unpack.core import is_safe_relative

        for good in ("game/script.rpy", "images/bg/room 1.png", "a.webp", "x/y/z.dat"):
            self.assertTrue(is_safe_relative(good), good)


if __name__ == "__main__":
    unittest.main(verbosity=2)
