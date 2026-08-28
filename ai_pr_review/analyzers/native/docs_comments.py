"""Native analyzers for doc-comment/signature mismatch and missing docs.

Backs two AnalyzerSpec registrations:
  - docs-api-check: a documented @param-family tag names a parameter that
    does not exist in the signature, or vice versa. Medium severity —
    near-zero false-positive rate measured against this repo (see
    docs/adr/0001-tree-sitter-not-node-for-doc-mismatch.md).
  - docs-missing-check: a newly-added public function/method has no
    preceding doc comment at all. Low severity, diff-gated to symbols
    genuinely added in this diff (not pre-existing undocumented code).

Python uses the already-installed `ruff` binary (--isolated, so results
never depend on the consumer's own ruff config). Go uses a dedicated
`golangci-lint --enable-only=godoclint` invocation, independent of the
general golangci-lint analyzer (native/golangci_lint.py), for the same
--isolated reason. Every other supported language (JS/TS/Java/Kotlin/C#/
Ruby/C++/Scala) is covered by one shared tree-sitter traversal — see
ADR-0001 for why this is a tree-sitter engine rather than a Node/ESLint
layer. PHP is deliberately excluded: phpcs already covers doc-comment
mismatch on both the Drupal and PSR12 paths (native/phpcs.py).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ai_pr_review.context.treesitter import _attr_or_call
from ai_pr_review.diff.linemap import LineRef, parse_added_lines
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_TIMEOUT_SECS = 120

_API_SOURCE = "docs-api-check"
_MISSING_SOURCE = "docs-missing-check"
_TS_ENGINE_CONFIDENCE = 80
_RUFF_CONFIDENCE = 90

# ---------------------------------------------------------------------------
# Tree-sitter engine: shared traversal for the @param-family languages.
#
# Node-type names below were verified against real parses this session with
# tree-sitter-language-pack 1.8.1 for every listed language — not guessed
# from grammar docs. PHP, Python, and Go are intentionally absent (handled
# by phpcs, ruff, and a dedicated golangci-lint invocation respectively).
# ---------------------------------------------------------------------------

# manifest.py's detect_language() labels, lowercased, mapped to the
# tree-sitter-language-pack grammar name. ".ts"/".tsx" need separate
# grammars (tsx handles JSX-in-TypeScript) even though detect_language()
# collapses both to the single "TypeScript" label, so this table is keyed
# by extension for the JS/TS family and by language label for the rest.
_EXT_TO_GRAMMAR: dict[str, str] = {
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
}
_LANGUAGE_LABEL_TO_GRAMMAR: dict[str, str] = {
    "java": "java",
    "kotlin": "kotlin",
    "csharp": "csharp",
    "ruby": "ruby",
    "c++": "cpp",
    "scala": "scala",
}

_FUNCTION_NODE_TYPES: dict[str, frozenset[str]] = {
    "javascript": frozenset({"function_declaration", "method_definition"}),
    "typescript": frozenset({"function_declaration", "method_definition", "method_signature"}),
    "tsx": frozenset({"function_declaration", "method_definition", "method_signature"}),
    "java": frozenset({"method_declaration"}),
    "kotlin": frozenset({"function_declaration"}),
    "csharp": frozenset({"method_declaration"}),
    "ruby": frozenset({"method"}),
    "cpp": frozenset({"function_definition"}),
    "scala": frozenset({"function_definition"}),
}

_PARAM_LIST_NODE_TYPE: dict[str, str] = {
    "javascript": "formal_parameters",
    "typescript": "formal_parameters",
    "tsx": "formal_parameters",
    "java": "formal_parameters",
    "kotlin": "function_value_parameters",
    "csharp": "parameter_list",
    "ruby": "method_parameters",
    "cpp": "parameter_list",
    "scala": "parameters",
}

# Node kinds whose text names an identifier directly (JS bare params, Ruby
# params) as opposed to a wrapper node (formal_parameter, parameter,
# parameter_declaration) that has an identifier among its own children.
_IDENTIFIER_KINDS = frozenset({"identifier", "simple_identifier"})

# A parameter wrapper resolves to "no single name" for these kinds —
# destructuring and rest patterns don't have one JSDoc-addressable name.
# Skipping (not guessing) matches this repo's stated preference for
# under- rather than over-reporting on ambiguous cases.
_UNRESOLVABLE_PARAM_KINDS = frozenset({"object_pattern", "array_pattern", "rest_pattern"})

# Node kinds that mark visibility as non-public. Presence of any of these
# words in a modifier/access-specifier node's text means "not public";
# absence means public, which is the correct default for every listed
# language except Java (package-private-by-default) — erring toward
# checking a package-private Java method is the safer failure mode for a
# Low-severity, non-blocking finding, so it is not special-cased here.
_NON_PUBLIC_MARKERS = frozenset({"private", "protected"})

# C++ and Ruby toggle visibility via a preceding sibling rather than a
# modifier on the function itself (an access_specifier "public:"/"private:"
# section in C++; a bare `private`/`protected` call in Ruby).
_ACCESS_SPECIFIER_KINDS = frozenset({"access_specifier"})


def _kind(node: object) -> str:
    k = _attr_or_call(node, "kind", None) or _attr_or_call(node, "type", "")
    return k if isinstance(k, str) else ""


def _children(node: object) -> list[object]:
    count = _attr_or_call(node, "child_count", 0)
    if not isinstance(count, int):
        return []
    out = []
    for i in range(count):
        try:
            child = node.child(i)  # type: ignore[attr-defined]
        except (TypeError, AttributeError):
            continue
        if child is not None:
            out.append(child)
    return out


def _node_text(node: object, src_bytes: bytes) -> str:
    sb = _attr_or_call(node, "start_byte", None)
    eb = _attr_or_call(node, "end_byte", None)
    if isinstance(sb, int) and isinstance(eb, int):
        return src_bytes[sb:eb].decode(errors="replace")
    return ""


def _start_byte(node: object) -> int:
    sb = _attr_or_call(node, "start_byte", None)
    return sb if isinstance(sb, int) else -1


def _sibling_index(siblings: list[object], node: object) -> int | None:
    """Find *node*'s position among *siblings* by start_byte, not identity.

    This tree-sitter binding returns a fresh wrapper object from every
    accessor call (`.child(i)`, `.parent`, a prior traversal step), and
    those wrappers implement neither `is` nor `==` as value equality —
    verified empirically this session (two wrappers for the provably same
    underlying node, same start_byte, compare unequal both ways). A plain
    `list.index()` on tree-sitter nodes therefore always raises/misses;
    matching on start_byte (unique per node in a single parse) is the
    reliable substitute.
    """
    target = _start_byte(node)
    if target < 0:
        return None
    for i, sib in enumerate(siblings):
        if _start_byte(sib) == target:
            return i
    return None


def _start_line(node: object) -> int:
    """1-based line number of *node*'s first line."""
    sp = _attr_or_call(node, "start_position", None) or _attr_or_call(node, "start_point", None)
    if isinstance(sp, tuple) and sp and isinstance(sp[0], int):
        return sp[0] + 1
    if sp is not None:
        row = _attr_or_call(sp, "row", 0)
        if isinstance(row, int):
            return row + 1
    return 1


def _param_name(node: object, src_bytes: bytes) -> str | None:
    """Return a parameter's name, or None if it has no single addressable name."""
    kind = _kind(node)
    if kind in _IDENTIFIER_KINDS:
        return _node_text(node, src_bytes)
    if kind in _UNRESOLVABLE_PARAM_KINDS:
        return None
    if kind == "assignment_pattern":
        # JS default value: `name = default`. First child is the name (or a
        # nested unresolvable pattern, e.g. `{a, b} = {}`).
        for child in _children(node):
            return _param_name(child, src_bytes)
        return None
    # Wrapper node (formal_parameter, parameter, parameter_declaration,
    # required_parameter, optional_parameter): the name is whichever direct
    # child is an identifier — verified this session that every listed
    # language puts exactly one such child in the wrapper, regardless of
    # whether the type comes before (Java, C#, C++) or after (Kotlin,
    # Scala, TypeScript) the name.
    for child in _children(node):
        if _kind(child) in _IDENTIFIER_KINDS:
            return _node_text(child, src_bytes)
    return None


def _find_param_list(func_node: object, param_list_kind: str) -> object | None:
    """Find the parameter-list node for *func_node*.

    Usually a direct child. C++ nests it one level deeper inside a
    function_declarator (verified this session: `function_definition ->
    function_declarator -> parameter_list`), so direct children whose kind
    contains "declarator" are searched one level further.
    """
    for child in _children(func_node):
        if _kind(child) == param_list_kind:
            return child
    for child in _children(func_node):
        if "declarator" in _kind(child):
            for grandchild in _children(child):
                if _kind(grandchild) == param_list_kind:
                    return grandchild
    return None


def _signature_param_names(func_node: object, param_list_kind: str, src_bytes: bytes) -> list[str] | None:
    """Return the function's parameter names, or None if any is unresolvable.

    Returning None (rather than a partial list) is deliberate: a function
    with even one destructured/rest parameter has an incompletely-known
    signature, and guessing which @param tags correspond to the resolvable
    remainder risks false positives this repo has consistently scoped away
    from (see the merge/dedup and diff-scope design elsewhere in this
    codebase, which favor under- over over-reporting).
    """
    param_list = _find_param_list(func_node, param_list_kind)
    if param_list is None:
        return []
    names: list[str] = []
    for child in _children(param_list):
        kind = _kind(child)
        if kind in ("(", ")", ","):
            continue
        name = _param_name(child, src_bytes)
        if name is None:
            return None
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# @param-family tag extraction from doc-comment text.
#
# One regex covers JSDoc/JavaDoc/KDoc (`@param {type} name`), YARD
# (`@param name [type]`), and bare (`@param name`) orderings: it only
# requires an optional `{...}` type BEFORE the captured name, and does not
# require anything specific to follow, so a `[type]` after the name (YARD)
# is simply left unconsumed rather than needing a second alternation.
# A separate regex covers C# XML doc comments (`<param name="x">`), an
# entirely different tag syntax.
# ---------------------------------------------------------------------------
_JSDOC_PARAM_RE = re.compile(r"@param\s+(?:\{[^}]*\}\s+)?\[?(\w+)\]?")
_XML_DOC_PARAM_RE = re.compile(r'<param\s+name\s*=\s*"(\w+)"')


def _doc_param_names(comment_text: str) -> set[str]:
    names = set(_JSDOC_PARAM_RE.findall(comment_text))
    names.update(_XML_DOC_PARAM_RE.findall(comment_text))
    return names


_MAX_WRAPPER_CLIMB = 4


def _preceding_comment_text(func_node: object, src_bytes: bytes) -> str | None:
    """Return the text of the doc comment immediately preceding *func_node*, if any.

    Adjacency in the AST (no other node between them) is the "attached"
    definition used here, matching how ruff's own D-rules treat a Python
    docstring as attached only when it is the first statement — the same
    "immediately preceding, no blank-line-spanning gap" semantics, just
    expressed as sibling adjacency rather than statement position.

    Climbs through "sole-child wrapper" ancestors when the current node has
    nothing before it at its own level. Verified necessary for Ruby: a
    `def` is wrapped in a `body_statement` node, and a preceding `#`-comment
    is a sibling of that wrapper under `class`, not a sibling of the `def`
    itself. Only climbs when the current node is genuinely first among its
    siblings (idx == 0) — if there is already a definitive non-comment
    sibling immediately before it, that answer is not overridden by
    climbing further.

    Collects every contiguous comment sibling immediately before the
    attachment point, not just the nearest one — verified necessary for
    Ruby (`# @param a` / `# @param b` as two consecutive single-line
    comment nodes) and C# (`/// <param ...>` XML doc lines, same shape).
    """
    node = func_node
    for _ in range(_MAX_WRAPPER_CLIMB):
        parent = _attr_or_call(node, "parent", None)
        if parent is None:
            return None
        siblings = _children(parent)
        idx = _sibling_index(siblings, node)
        if idx is None:
            return None
        if idx == 0:
            node = parent
            continue
        if "comment" not in _kind(siblings[idx - 1]):
            return None
        comment_nodes = [siblings[idx - 1]]
        i = idx - 2
        while i >= 0 and "comment" in _kind(siblings[i]):
            comment_nodes.append(siblings[i])
            i -= 1
        comment_nodes.reverse()
        return "\n".join(_node_text(c, src_bytes) for c in comment_nodes)
    return None


def _is_public(func_node: object, language: str, src_bytes: bytes) -> bool:
    """Return whether *func_node* looks like public API by language convention.

    Absence of an explicit private/protected marker means public for every
    listed language except Java (package-private by default) — treated as
    public anyway here, since flagging an extra package-private method is
    the safer failure mode for a Low-severity, non-blocking finding than
    silently skipping real API surface.
    """
    name_node = None
    for child in _children(func_node):
        kind = _kind(child)
        if kind in ("modifiers", "modifier"):
            text = _node_text(child, src_bytes).lower()
            if any(marker in text for marker in _NON_PUBLIC_MARKERS):
                return False
        if kind in _IDENTIFIER_KINDS or kind == "property_identifier":
            name_node = child
    if name_node is not None:
        name = _node_text(name_node, src_bytes)
        if name.startswith("_") or name.startswith("#"):
            return False
    if language == "cpp":
        # Toggled by the nearest preceding access_specifier sibling within
        # the same field_declaration_list, not a modifier on the node
        # itself — verified this session.
        parent = _attr_or_call(func_node, "parent", None)
        if parent is not None:
            siblings = _children(parent)
            idx = _sibling_index(siblings, func_node)
            if idx is not None:
                for i in range(idx - 1, -1, -1):
                    if _kind(siblings[i]) in _ACCESS_SPECIFIER_KINDS:
                        return "public" in _node_text(siblings[i], src_bytes).lower()
    return True


def _parse_file(path: Path, grammar: str) -> tuple[object, bytes] | None:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        logger.warning(
            "[ai-pr-review] WARNING: tree-sitter-language-pack unavailable; "
            "docs-api-check/docs-missing-check skipped for %s. Cause: %s",
            path, exc,
        )
        return None
    try:
        parser = get_parser(grammar)
    except Exception as exc:
        logger.warning("[ai-pr-review] WARNING: could not load tree-sitter grammar %r: %s", grammar, exc)
        return None
    try:
        src = path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: could not read %s: %s", path, exc)
        return None
    src_bytes = src.encode()
    try:
        tree = parser.parse(src)
    except (TypeError, AttributeError):
        try:
            # Dual error-code list: CI's mypy job doesn't install the
            # optional tree-sitter-language-pack extra, so parser.parse is
            # untyped (Any) there and the arg-type ignore is unused in that
            # environment — same reasoning as context/treesitter.py's
            # identical fallback.
            tree = parser.parse(src_bytes)  # type: ignore[arg-type,unused-ignore]
        except Exception as exc:
            logger.warning("[ai-pr-review] WARNING: tree-sitter parse error for %s: %s", path, exc)
            return None
    except Exception as exc:
        logger.warning("[ai-pr-review] WARNING: tree-sitter parse error for %s: %s", path, exc)
        return None
    root = _attr_or_call(tree, "root_node", None)
    if root is None:
        return None
    return root, src_bytes


def _walk_functions(node: object, function_kinds: frozenset[str], out: list[object]) -> None:
    if _kind(node) in function_kinds:
        out.append(node)
    for child in _children(node):
        _walk_functions(child, function_kinds, out)


def _grammar_for_file(path: Path) -> str | None:
    ext = path.suffix.lstrip(".").lower()
    if ext in _EXT_TO_GRAMMAR:
        return _EXT_TO_GRAMMAR[ext]
    from ai_pr_review.languages import detect_language

    label = detect_language(ext).lower()
    return _LANGUAGE_LABEL_TO_GRAMMAR.get(label)


def _tree_sitter_api_findings(path: str) -> list[Finding]:
    p = Path(path)
    grammar = _grammar_for_file(p)
    if grammar is None:
        return []
    parsed = _parse_file(p, grammar)
    if parsed is None:
        return []
    root, src_bytes = parsed

    function_kinds = _FUNCTION_NODE_TYPES[grammar]
    param_list_kind = _PARAM_LIST_NODE_TYPE[grammar]
    functions: list[object] = []
    _walk_functions(root, function_kinds, functions)

    findings: list[Finding] = []
    for func in functions:
        comment_text = _preceding_comment_text(func, src_bytes)
        if comment_text is None:
            continue
        doc_names = _doc_param_names(comment_text)
        if not doc_names:
            continue
        sig_names = _signature_param_names(func, param_list_kind, src_bytes)
        if sig_names is None:
            continue
        sig_set = set(sig_names)
        line = _start_line(func)

        for extra in sorted(doc_names - sig_set):
            findings.append(
                Finding(
                    severity="Medium",
                    confidence=_TS_ENGINE_CONFIDENCE,
                    source=_API_SOURCE,
                    category="docs",
                    file=path,
                    line=line,
                    finding=f"Documented parameter '{extra}' is not in the function's signature.",
                    remediation="Update the doc comment to match the current signature.",
                )
            )
        for missing in sorted(sig_set - doc_names):
            findings.append(
                Finding(
                    severity="Medium",
                    confidence=_TS_ENGINE_CONFIDENCE,
                    source=_API_SOURCE,
                    category="docs",
                    file=path,
                    line=line,
                    finding=f"Parameter '{missing}' is not documented in the function's doc comment.",
                    remediation="Add the missing parameter to the doc comment.",
                )
            )
    return findings


def _tree_sitter_missing_findings(path: str, added_lines: set[LineRef]) -> list[Finding]:
    p = Path(path)
    grammar = _grammar_for_file(p)
    if grammar is None:
        return []
    parsed = _parse_file(p, grammar)
    if parsed is None:
        return []
    root, src_bytes = parsed

    function_kinds = _FUNCTION_NODE_TYPES[grammar]
    functions: list[object] = []
    _walk_functions(root, function_kinds, functions)

    findings: list[Finding] = []
    for func in functions:
        line = _start_line(func)
        if LineRef(path, line) not in added_lines:
            continue
        if not _is_public(func, grammar, src_bytes):
            continue
        if _preceding_comment_text(func, src_bytes) is not None:
            continue
        findings.append(
            Finding(
                severity="Low",
                confidence=_TS_ENGINE_CONFIDENCE,
                source=_MISSING_SOURCE,
                category="docs",
                file=path,
                line=line,
                finding="New public function/method has no doc comment.",
                remediation="Add a doc comment describing this function's purpose and parameters.",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Python path: ruff --isolated (never inherits the consumer's own ruff
# config — that is the whole reason this is a separate invocation from the
# general "ruff" analyzer in native/ruff.py, which deliberately does honor
# consumer config).
# ---------------------------------------------------------------------------


def _run_ruff_isolated(py_files: list[str], select: str) -> list[dict[str, object]] | None:
    if not shutil.which("ruff"):
        logger.warning("[ai-pr-review] WARNING: ruff not found; skipping.")
        return None
    try:
        result = subprocess.run(
            [
                "ruff", "check", "--isolated", "--preview", "--no-cache", "--exit-zero",
                f"--select={select}", "--output-format=json", "--", *py_files,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: ruff timed out after %ss; skipping.", exc.timeout)
        return None
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: ruff failed to start: %s", exc)
        return None
    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: ruff exited %d; skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return None
    if not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: ruff produced non-JSON output: %s", exc)
        return None
    if not isinstance(data, list):
        logger.warning("[ai-pr-review] WARNING: ruff produced unexpected output structure; skipping.")
        return None
    return [item for item in data if isinstance(item, dict)]


def _ruff_filename_and_row(item: dict[str, object]) -> tuple[str, int | None]:
    filename_raw = item.get("filename")
    filename = filename_raw if isinstance(filename_raw, str) else ""
    location = item.get("location")
    row_raw = location.get("row") if isinstance(location, dict) else None
    row = row_raw if isinstance(row_raw, int) else None
    return filename, row


def _python_api_findings(py_files: list[str]) -> list[Finding]:
    items = _run_ruff_isolated(py_files, "D417,DOC102,DOC202,DOC403,DOC502")
    if not items:
        return []
    findings: list[Finding] = []
    for item in items:
        filename, row = _ruff_filename_and_row(item)
        try:
            findings.append(
                Finding(
                    severity="Medium",
                    confidence=_RUFF_CONFIDENCE,
                    source=_API_SOURCE,
                    category="docs",
                    file=filename,
                    line=row,
                    finding=f"{item.get('code', '')}: {item.get('message', '')}",
                    remediation="Update the docstring to match the current signature.",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: docs-api-check dropped malformed ruff item: %s; item=%r",
                exc, repr(item)[:200],
            )
    return findings


def _python_missing_findings(py_files: list[str], added_lines: set[LineRef]) -> list[Finding]:
    items = _run_ruff_isolated(py_files, "D101,D102,D103")
    if not items:
        return []
    findings: list[Finding] = []
    for item in items:
        filename, row = _ruff_filename_and_row(item)
        if row is None or LineRef(filename, row) not in added_lines:
            continue
        try:
            findings.append(
                Finding(
                    severity="Low",
                    confidence=_RUFF_CONFIDENCE,
                    source=_MISSING_SOURCE,
                    category="docs",
                    file=filename,
                    line=row,
                    finding=f"{item.get('code', '')}: {item.get('message', '')}",
                    remediation="Add a docstring describing this symbol.",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: docs-missing-check dropped malformed ruff item: %s; item=%r",
                exc, repr(item)[:200],
            )
    return findings


# ---------------------------------------------------------------------------
# Go path: a dedicated golangci-lint invocation scoped to godoclint only,
# independent of the general golangci-lint analyzer (native/golangci_lint.py)
# for the same --isolated-style reason ruff gets a second invocation here.
# godoclint checks Go doc-comment presence and form only — Go doc comments
# have no @param syntax, so there is no mismatch direction to check; this
# only feeds docs-missing-check, never docs-api-check.
# ---------------------------------------------------------------------------


def _find_go_module_root(go_files: list[str]) -> Path | None:
    candidate = Path(go_files[0]).resolve().parent
    while True:
        if (candidate / "go.mod").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _go_missing_findings(go_files: list[str], added_lines: set[LineRef]) -> list[Finding]:
    target_files = [f for f in go_files if Path(f).is_file()]
    if not target_files:
        return []
    if not shutil.which("golangci-lint"):
        logger.warning("[ai-pr-review] WARNING: golangci-lint not found; skipping docs-missing-check for Go.")
        return []
    module_root = _find_go_module_root(target_files)
    if module_root is None:
        logger.warning("[ai-pr-review] WARNING: could not find go.mod; docs-missing-check skipped for Go.")
        return []

    seen: set[str] = set()
    patterns: list[str] = []
    for f in target_files:
        rel = Path(f).resolve().relative_to(module_root)
        pkg_dir = str(rel.parent)
        if pkg_dir not in seen:
            seen.add(pkg_dir)
            patterns.append(f"./{pkg_dir}/...")

    # godoclint's rules ship in tiers; enabling the linter only turns on the
    # "Basic" tier (doc-comment FORM checks: start-with-name, etc), never
    # "require-doc" (the presence check this analyzer needs) — that is
    # "Strict" tier, opt-in only. Verified empirically this session against
    # golangci-lint 2.13.1: --enable-only=godoclint alone produces zero
    # findings even on a fully undocumented exported function. A config
    # file is the only way to turn on a per-linter rule in golangci-lint
    # v2 — there is no CLI flag for it.
    #
    # The config file must live INSIDE module_root, not an unrelated temp
    # directory: verified empirically that golangci-lint computes each
    # issue's reported Pos.Filename relative to the --config file's own
    # directory when that directory differs from the lint target's module
    # root, producing unusable paths like "../../home/x/repo/main.go"
    # instead of "main.go" — which would silently break the diff-gating
    # lookup below. The JSON *output* path has no such constraint (only
    # verified to affect the config file's location).
    with tempfile.NamedTemporaryFile(
        mode="w", dir=str(module_root), suffix=".yml", prefix=".ai-pr-review-godoclint-", delete=True
    ) as config_file:
        config_file.write(
            'version: "2"\n'
            "linters:\n"
            "  settings:\n"
            "    godoclint:\n"
            "      enable:\n"
            "        - require-doc\n"
        )
        config_file.flush()

        with tempfile.TemporaryDirectory(prefix="godoclint-") as tmpdir:
            json_path = Path(tmpdir) / "output.json"
            try:
                result = subprocess.run(
                    [
                        "golangci-lint", "run",
                        "--enable-only=godoclint",
                        f"--config={config_file.name}",
                        f"--output.json.path={json_path}",
                        "--issues-exit-code=0",
                        *patterns,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT_SECS,
                    cwd=str(module_root),
                )
            except subprocess.TimeoutExpired as exc:
                logger.warning("[ai-pr-review] WARNING: godoclint timed out after %ss; skipping.", exc.timeout)
                return []
            except OSError as exc:
                logger.warning("[ai-pr-review] WARNING: godoclint failed to start: %s", exc)
                return []

            if result.returncode not in (0, 1):
                logger.warning(
                    "[ai-pr-review] WARNING: godoclint exited %d; skipping. stderr: %s",
                    result.returncode, result.stderr[:200],
                )
                return []

            if not json_path.exists() or not json_path.stat().st_size:
                return []

            try:
                data = json.loads(json_path.read_text())
            except json.JSONDecodeError as exc:
                logger.warning("[ai-pr-review] WARNING: godoclint produced non-JSON output: %s", exc)
                return []

    if not isinstance(data, dict):
        return []
    issues = data.get("Issues") or []
    if not isinstance(issues, list):
        return []

    resolved_cwd = Path(".").resolve()
    if module_root != resolved_cwd:
        try:
            prefix = str(module_root.relative_to(resolved_cwd)) + "/"
        except ValueError:
            prefix = str(module_root) + "/"
    else:
        prefix = ""

    findings: list[Finding] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        pos = item.get("Pos") or {}
        filename = pos.get("Filename") or ""
        line = pos.get("Line")
        full_path = prefix + filename
        if not isinstance(line, int) or LineRef(full_path, line) not in added_lines:
            continue
        try:
            findings.append(
                Finding(
                    severity="Low",
                    confidence=_TS_ENGINE_CONFIDENCE,
                    source=_MISSING_SOURCE,
                    category="docs",
                    file=full_path,
                    line=line,
                    finding=f"godoclint: {item.get('Text', '')}",
                    remediation="Add or fix the doc comment for this exported symbol.",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: docs-missing-check dropped malformed godoclint item: %s; item=%r",
                exc, repr(item)[:200],
            )
    return findings


# ---------------------------------------------------------------------------
# Public entrypoints (AnalyzerSpec.native_fn).
# ---------------------------------------------------------------------------

_TS_ENGINE_EXTENSIONS = frozenset({"js", "jsx", "ts", "tsx", "java", "kt", "kts", "cs", "rb", "rake", "gemspec", "c", "h", "cpp", "hpp", "cc", "cxx", "scala", "sbt"})


def _run_docs_api_check(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Doc-comment/signature mismatch across Python and the @param-family languages."""
    findings: list[Finding] = []

    py_files = [f for f in changed_files.python if Path(f).is_file()]
    if py_files:
        findings.extend(_python_api_findings(py_files))

    ts_files = [
        f for f in changed_files.source
        if Path(f).is_file() and Path(f).suffix.lstrip(".").lower() in _TS_ENGINE_EXTENSIONS
    ]
    for f in ts_files:
        findings.extend(_tree_sitter_api_findings(f))

    return findings


def _run_docs_missing_check(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Missing docs on newly-added public symbols, diff-gated to added lines."""
    try:
        diff_text = diff_file.read_text(errors="replace")
    except OSError:
        diff_text = ""
    added_lines = parse_added_lines(diff_text)
    if not added_lines:
        return []

    findings: list[Finding] = []

    py_files = [f for f in changed_files.python if Path(f).is_file()]
    if py_files:
        findings.extend(_python_missing_findings(py_files, added_lines))

    if changed_files.go:
        findings.extend(_go_missing_findings(changed_files.go, added_lines))

    ts_files = [
        f for f in changed_files.source
        if Path(f).is_file() and Path(f).suffix.lstrip(".").lower() in _TS_ENGINE_EXTENSIONS
    ]
    for f in ts_files:
        findings.extend(_tree_sitter_missing_findings(f, added_lines))

    return findings
