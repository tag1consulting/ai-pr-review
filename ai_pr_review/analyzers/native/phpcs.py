"""Native Python implementation of the phpcs analyzer.

Replaces analyzers/run-phpcs.sh. Invokes phpcs directly via subprocess and
converts its JSON output to Finding instances.

Trust boundary (#1042): the workspace is untrusted PR content, and a phpcs
ruleset is not inert data. It can set ``installed_paths``, ``<arg
name="bootstrap">``, ``<autoload>`` and path-style ``<rule ref>`` entries,
all of which make phpcs load PHP from the workspace. phpcs therefore never
reads the repo ruleset. This module reads the ruleset itself as data, keeps
an allowlisted subset (which files to lint, which exclude patterns, which
installed standards and sniffs, and per-rule tweaks), and gives phpcs a
freshly written ruleset built from that subset. This is the same approach as
the empty trusted ``--configuration`` file in ``phpstan.py`` (#739).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ai_pr_review.analyzers._env import tool_env
from ai_pr_review.analyzers.native._paths import strip_workspace_prefix
from ai_pr_review.analyzers.native._phpcs_patterns import (
    _relative_pattern_matches,  # noqa: F401 (re-export, tests import it here)
)
from ai_pr_review.analyzers.native._phpcs_ruleset import (
    _build_ruleset_xml,
    _find_repo_ruleset,
    _in_ruleset_scope,
    _parse_repo_ruleset,
    _pattern_ok,  # noqa: F401 (re-export, tests import it here)
    _RepoRuleset,
)
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles, escapes_workspace

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "phpcs"
_TIMEOUT_SECS = 120
_PHP_EXTENSIONS = {".php", ".module", ".inc", ".theme", ".install", ".profile"}

# Sniff categories (the second dot-separated part of a sniff code, as in
# ``Squiz.Commenting.FunctionComment.Missing``) that only report style. phpcs
# marks many of them ERROR, which would otherwise make a missing docblock
# High. Precedent for an in-code map: ``golangci_lint._HIGH_SEVERITY_LINTERS``.
_STYLE_SNIFF_CATEGORIES = frozenset({"Commenting", "WhiteSpace", "Formatting", "NamingConventions"})

# phpcs's --standard flag only accepts full ruleset names (or a path to a
# ruleset.xml). It cannot mix a named standard with a bare sniff code
# (`--standard=PSR12,Squiz.Commenting.FunctionComment` errors: "the
# Squiz.Commenting.FunctionComment coding standard is not installed").
# Verified empirically against phpcs 4.0.4. A generated ruleset.xml with a
# `<rule ref>` for each is the way to add a single sniff on top of a named
# standard.
_PSR12_PLUS_DOCBLOCK_RULESET = """<?xml version="1.0"?>
<ruleset name="PSR12PlusDocblock">
    <description>PSR12 with Squiz function-comment sniffs added.</description>
    <rule ref="PSR12"/>
    <rule ref="Squiz.Commenting.FunctionComment"/>
</ruleset>
"""
_PSR12_REFS = ("PSR12", "Squiz.Commenting.FunctionComment")

# Correctness-only sniffs with no code-style opinion (no indentation, tabs,
# line length, naming, or brace placement). Used when a repo ruleset names a
# standard that the image does not have (for example WordPress). PSR12 is the
# wrong fallback there: it requires spaces, so tab-indented code gets a
# finding on almost every line. All seven names verified against phpcs 4.0.4
# with `phpcs -e` (#1042).
_STYLE_NEUTRAL_REFS = (
    "Generic.PHP.Syntax",
    "Generic.CodeAnalysis.EmptyStatement",
    "Generic.CodeAnalysis.UnconditionalIfStatement",
    "Generic.CodeAnalysis.JumbledIncrementer",
    "Generic.PHP.DeprecatedFunctions",
    "Squiz.PHP.Eval",
    "Squiz.PHP.NonExecutableCode",
)

# phpcs 4.x exit codes are a bitmask (PHPCSStandards/PHP_CodeSniffer#184,
# src/Util/ExitCode.php): 0=clean, 1=fixable violations, 2=non-fixable
# violations, 3=both. All four mean "phpcs completed a normal run; the JSON
# output is trustworthy." Fatal/config errors use 16 (PROCESS_ERROR) or 64
# (REQUIREMENTS_NOT_MET) instead, which never overlap this range. This
# differs from phpcs 3.x, where 2 meant "some fixable issue exists" and a
# genuine config error was not representable via this simple 0/1/2 scheme.
_SUCCESS_RETURNCODES = frozenset({0, 1, 2, 3})

_MAX_COMPOSER_BYTES = 1024 * 1024

_DRUPAL_EXTENSIONS = frozenset({".module", ".install", ".theme", ".profile"})
# Drupal 7 modules describe themselves with a plain `<name>.info` file (no
# `.info.yml`, often no composer.json). `.info` is also used by unrelated tools,
# so the file counts only when it has both keys every Drupal 7 info file needs.
_MAX_INFO_BYTES = 64 * 1024
_D7_NAME_RE = re.compile(rb"^\s*name\s*=", re.MULTILINE)
_D7_CORE_RE = re.compile(rb"^\s*core\s*=", re.MULTILINE)
# Ships inside phpcs itself but is not listed by `phpcs -i`.
_ALWAYS_INSTALLED = frozenset({"Generic"})


@dataclass
class _Plan:
    """What to run: a named standard or a generated ruleset, plus scope.

    A generated ruleset is stored as its inputs, the base refs and the
    allowlisted repo ruleset. The XML is built only when phpcs needs it.
    `static_xml` is the one fixed ruleset text, for the PSR12 default with no
    repo ruleset. It holds no repo data.
    """

    label: str
    remediation_standard: str
    named_standard: str | None = None
    base_refs: tuple[str, ...] = ()
    # The allowlisted subset of the repo ruleset. None when the plan carries
    # nothing from the repo.
    ruleset: _RepoRuleset | None = None
    files: list[str] = field(default_factory=list)
    drupal: bool = False
    static_xml: str | None = None

    @property
    def ruleset_xml(self) -> str | None:
        """The generated ruleset for this plan. None for a named standard."""
        if self.named_standard is not None:
            return None
        if self.static_xml is not None:
            return self.static_xml
        return _build_ruleset_xml(self.base_refs, self.ruleset)

    @property
    def repo_rules(self) -> bool:
        """True when the generated ruleset carries rules or patterns from the repo.

        Those can name a sniff that this phpcs version lacks, so a process
        error is retried once without them.
        """
        return self.ruleset is not None


def _installed_standards() -> set[str]:
    """Return the standard names phpcs lists, or an empty set when it cannot be asked."""
    try:
        with _trusted_cwd() as cwd:
            result = subprocess.run(
                ["phpcs", "-i"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=cwd,
                env=tool_env(),
            )
    except subprocess.TimeoutExpired:
        logger.warning("[ai-pr-review] WARNING: phpcs -i timed out detecting standard; falling back to PSR12.")
        return set()
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs -i failed: %s; falling back to PSR12.", exc)
        return set()
    if result.returncode != 0 or not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: phpcs -i exited %d with no usable output; "
            "the installed standards are unknown, so no repo standard can be honored.",
            result.returncode,
        )
        return set()
    text = result.stdout
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    elif " are " in text:
        text = text.split(" are ", 1)[-1]
    names = {t for t in re.split(r"[,\s]+", text) if t and t != "and"}
    return names | _ALWAYS_INSTALLED


@contextmanager
def _trusted_cwd() -> Iterator[str]:
    """Yield an empty temp directory to use as phpcs's working directory.

    phpcs resolves some ruleset references against its working directory. Run
    from the workspace, a repo file could be loaded as a ruleset, and a ruleset
    can run PHP (`bootstrap`, `autoload`). From an empty directory, nothing from
    the repo can resolve (#1042).
    """
    with tempfile.TemporaryDirectory(prefix="phpcs-cwd-") as directory:
        yield directory


def _is_drupal7_info(path: Path) -> bool:
    """Is *path* a Drupal 7 `.info` file? Reads at most 64 KiB, as data, never a symlink."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as fh:
            data = fh.read(_MAX_INFO_BYTES)
    except OSError:
        return False
    return bool(_D7_NAME_RE.search(data) and _D7_CORE_RE.search(data))


def _dir_has_drupal_info(directory: Path) -> bool:
    try:
        if any(directory.glob("*.info.yml")):
            return True
        return any(_is_drupal7_info(p) for p in directory.glob("*.info"))
    except OSError:
        return False


def _looks_like_drupal(php_files: list[str]) -> bool:
    """Does the repo show Drupal signals? Reads file names, `*.info` files and composer.json as data."""
    for f in php_files:
        if Path(f).suffix in _DRUPAL_EXTENSIONS:
            return True
    composer = Path("composer.json")
    if composer.is_file() and not composer.is_symlink():
        try:
            with composer.open("rb") as fh:
                data = json.loads(fh.read(_MAX_COMPOSER_BYTES))
        except (OSError, ValueError) as exc:
            logger.warning("[ai-pr-review] WARNING: could not read composer.json as a Drupal signal: %s", exc)
            data = None
        if isinstance(data, dict):
            if str(data.get("type", "")).startswith("drupal-"):
                return True
            for key in ("require", "require-dev"):
                deps = data.get(key)
                if isinstance(deps, dict) and any(str(k).startswith("drupal/core") for k in deps):
                    return True
    cwd = Path.cwd()
    checked: set[Path] = set()
    for f in php_files:
        directory = Path(f).parent
        if directory.is_absolute():
            try:
                directory = directory.relative_to(cwd)
            except ValueError:
                checked_dirs = [directory]
            else:
                checked_dirs = [directory, *directory.parents]
        else:
            checked_dirs = [directory, *directory.parents]
        for d in checked_dirs:
            if d in checked:
                continue
            checked.add(d)
            if _dir_has_drupal_info(d):
                return True
    return False


def _plan_standard(php_files: list[str]) -> _Plan | None:
    """Choose what to run. None when the repo ruleset puts every changed file out of scope."""
    installed = _installed_standards()
    ruleset_path = _find_repo_ruleset()
    if ruleset_path is not None:
        ruleset = _parse_repo_ruleset(ruleset_path, installed)
        if ruleset is None:
            logger.warning(
                "[ai-pr-review] WARNING: %s could not be used; linting with the style-neutral fallback.",
                ruleset_path.name,
            )
            return _Plan(
                label="style-neutral fallback",
                remediation_standard=ruleset_path.name,
                base_refs=_STYLE_NEUTRAL_REFS,
                files=php_files,
            )
        scoped = [f for f in php_files if _in_ruleset_scope(f, ruleset)]
        if not scoped:
            logger.info("[ai-pr-review] phpcs: no changed file is inside the <file> scope of %s; skipping.", ruleset.name)
            return None
        if ruleset.missing_standards:
            logger.warning(
                "[ai-pr-review] WARNING: %s names standard(s) not installed in this image: %s. "
                "Linting with the style-neutral fallback plus the installed rules it names.",
                ruleset.name, ", ".join(ruleset.missing_standards),
            )
            return _Plan(
                label="style-neutral fallback",
                remediation_standard=ruleset.name,
                base_refs=_STYLE_NEUTRAL_REFS,
                ruleset=ruleset,
                files=scoped,
            )
        if ruleset.has_standard_ref:
            return _Plan(
                label="repo ruleset",
                remediation_standard=ruleset.name,
                ruleset=ruleset,
                files=scoped,
                drupal=any(r.get("ref", "").startswith("Drupal") for r in ruleset.rules),
            )
        # A ruleset with no standard of its own: keep its scope and excludes
        # and pick the default standard below.
        php_files = scoped
        default_extra: _RepoRuleset | None = ruleset
    else:
        default_extra = None

    if "Drupal" in installed and _looks_like_drupal(php_files):
        if default_extra is None:
            return _Plan(
                label="Drupal,DrupalPractice",
                remediation_standard="Drupal,DrupalPractice",
                named_standard="Drupal,DrupalPractice",
                files=php_files,
                drupal=True,
            )
        return _Plan(
            label="Drupal,DrupalPractice",
            remediation_standard=default_extra.name,
            base_refs=("Drupal", "DrupalPractice"),
            ruleset=default_extra,
            files=php_files,
            drupal=True,
        )
    if default_extra is not None:
        return _Plan(
            label="PSR12",
            remediation_standard=default_extra.name,
            base_refs=_PSR12_REFS,
            ruleset=default_extra,
            files=php_files,
        )
    return _Plan(label="PSR12", remediation_standard="PSR12", static_xml=_PSR12_PLUS_DOCBLOCK_RULESET, files=php_files)


@contextmanager
def _standard_arg(plan: _Plan) -> Iterator[str]:
    """Return the value to pass as --standard for *plan*.

    A named standard passes through unchanged. A generated ruleset is written
    to a temp file that phpcs reads. The temp file never lives in the
    workspace.
    """
    xml = plan.ruleset_xml
    if xml is None:
        yield plan.named_standard or ""
        return
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", prefix="phpcs-ruleset-", delete=True
    ) as f:
        f.write(xml)
        f.flush()
        yield f.name


def _invoke_phpcs(plan: _Plan) -> subprocess.CompletedProcess[str] | None:
    """Run phpcs for *plan*. None on timeout or when phpcs cannot start."""
    try:
        # Absolute paths, because phpcs runs from an empty directory.
        absolute_files = [os.path.abspath(f) for f in plan.files]
        with _standard_arg(plan) as standard_arg, _trusted_cwd() as cwd:
            return subprocess.run(
                [
                    "phpcs",
                    "--report=json",
                    f"--standard={standard_arg}",
                    "--extensions=php,module,inc,theme,install,profile",
                    "-q",
                    "--",
                    *absolute_files,
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECS,
                cwd=cwd,
                env=tool_env(),
            )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs timed out after %ss; skipping.", exc.timeout)
        return None
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs failed to start: %s", exc)
        return None


def _is_style_sniff(source_rule: str) -> bool:
    """Is *source_rule* (``Squiz.Commenting.FunctionComment.Missing``) a style-only sniff?"""
    parts = source_rule.split(".")
    return len(parts) >= 2 and parts[1] in _STYLE_SNIFF_CATEGORIES


def _severity(msg_type: str, source_rule: str) -> str:
    """Map a phpcs message to a severity, rating style-only sniffs Low."""
    if _is_style_sniff(source_rule):
        return "Low"
    return "High" if msg_type == "ERROR" else "Medium"


def _remediation(plan: _Plan, path: str) -> str:
    if plan.drupal:
        return f"See https://www.drupal.org/docs/develop/standards or fix with: phpcs --standard={plan.remediation_standard} {path}"
    return f"Fix the violation named above. Check locally with: phpcs --standard={plan.remediation_standard} {path}"


def _run_phpcs(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run phpcs on changed PHP files and return Finding instances."""
    php_files = [f for f in changed_files.php if Path(f).is_file() and Path(f).suffix in _PHP_EXTENSIONS]
    escaping = [f for f in php_files if escapes_workspace(f)]
    if escaping:
        logger.warning(
            "[ai-pr-review] WARNING: phpcs skips %d changed file(s) that resolve outside the workspace: %s",
            len(escaping), ", ".join(escaping[:5]),
        )
        php_files = [f for f in php_files if f not in escaping]
    if not php_files:
        return []

    if not shutil.which("phpcs"):
        logger.warning("[ai-pr-review] WARNING: phpcs not found; skipping.")
        return []

    plan = _plan_standard(php_files)
    if plan is None:
        return []

    result = _invoke_phpcs(plan)
    if result is None:
        return []

    if result.returncode not in _SUCCESS_RETURNCODES and plan.repo_rules:
        # A rule or sniff code the repo ruleset names may not exist in this
        # phpcs version (a phpcs 3 sniff that phpcs 4 removed, for example).
        # Retry once with the style-neutral fallback and none of the repo
        # rules, instead of returning no findings at all.
        logger.warning(
            "[ai-pr-review] WARNING: phpcs rejected the generated ruleset (exit %d); "
            "retrying with the style-neutral fallback. The repo rules are dropped and "
            "the repo's top-level exclude patterns are kept. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        assert plan.ruleset is not None  # repo_rules is True only with a ruleset
        plan = dataclasses.replace(
            plan,
            label="style-neutral fallback",
            base_refs=_STYLE_NEUTRAL_REFS,
            ruleset=dataclasses.replace(
                plan.ruleset,
                files=(),
                rules=(),
                has_standard_ref=False,
                missing_standards=(),
            ),
            drupal=False,
        )
        result = _invoke_phpcs(plan)
        if result is None:
            return []

    if result.returncode not in _SUCCESS_RETURNCODES:
        logger.warning(
            "[ai-pr-review] WARNING: phpcs exited %d (process/config error); standard=%r may not be usable. "
            "stderr: %s",
            result.returncode, plan.label, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: phpcs exited %d with no JSON output; treating it as no findings. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: phpcs produced unexpected output structure; skipping.")
        return []

    findings: list[Finding] = []

    for file_path, file_data in (data.get("files") or {}).items():
        if not isinstance(file_data, dict):
            continue
        normalized_path = strip_workspace_prefix(file_path)

        for msg in file_data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type", "")
            source_rule = msg.get("source") or ""
            severity = _severity(msg_type, source_rule)
            try:
                findings.append(
                    Finding(
                        severity=severity,  # type: ignore[arg-type]
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=normalized_path,
                        line=msg.get("line") or None,
                        finding=f"{source_rule}: {msg.get('message', '')}",
                        remediation=_remediation(plan, normalized_path),
                        category="lint",
                        style_only=_is_style_sniff(source_rule),
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: phpcs dropped malformed finding: %s; msg=%r",
                    exc, repr(msg)[:200],
                )

    return findings
