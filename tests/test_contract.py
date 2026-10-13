"""The skills and docs drive review.py by command line, and FINDINGS.md defines what reviewers
write. A renamed flag or a drifted value would otherwise fail only mid-audit, after the
tokens are spent."""
import re
import shlex

import pytest

from conftest import KIT, load

review = load("review")
DOCS = sorted(KIT.glob("skills/*/SKILL.md")) + [KIT / f for f in (
    "README.md", "DESIGN.md", "CLAUDE.md.snippet", "FINDINGS.md", "tests/README.md")]


def documented_commands():
    """Every `review.py <command> --flag …` line in the docs, placeholders filled in."""
    found = []
    for doc in DOCS:
        text = doc.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            for m in re.finditer(r"review\.py\s+([a-z][a-z-]*)(.*)", line):
                rest = re.split(r"\s#\s|`|\||;|&&|\)", m.group(2))[0]
                if "--" not in rest:
                    continue      # prose that names a command, not an invocation
                rest = re.sub(r"<[^>]*>", "X", rest)
                rest = re.sub(r"\$\([^)]*\)|\$\{\w+\}|\$\w+", "X", rest)
                rest = re.sub(r"\d?>\s*/dev/null|2>&1", "", rest)
                found.append((doc.relative_to(KIT).as_posix(), m.group(1), rest.strip()))
    return found


COMMANDS = documented_commands()


def test_the_docs_contain_the_pipeline():
    assert {c for _, c, _ in COMMANDS} >= {
        "preflight", "store", "runs", "residue", "scope", "machine", "characterize", "probe",
        "estimate", "validate", "fingerprint", "audit", "cluster", "match", "coverage",
        "carryover", "summarize", "ledger"}


@pytest.mark.parametrize("doc,cmd,rest", COMMANDS, ids=[f"{d}:{c}" for d, c, _ in COMMANDS])
def test_every_documented_command_line_parses(doc, cmd, rest):
    argv = [cmd, *shlex.split(rest)]
    try:
        review.build_parser().parse_args(argv)
    except SystemExit as e:
        pytest.fail(f"{doc}: `review.py {' '.join(argv)}` is rejected by argparse ({e.code})")


def enum_in(text, field):
    """The values FINDINGS.md lists for a field, from its `a|b|c` spelling."""
    m = re.search(rf'"{field}":\s*"([a-z|-]+)"', text)
    return m.group(1).split("|") if m else None


def test_findings_md_and_validate_agree_on_values():
    text = (KIT / "FINDINGS.md").read_text()
    for name in ("severity", "confidence", "class", "fix_certainty"):
        assert f"`{name}`" in text or f'"{name}"' in text, name
    for value in review.SEVERITIES + review.CONFIDENCES + review.CLASSES \
            + review.FIX_CERTAINTIES + review.EVIDENCE_KINDS:
        assert re.search(rf"\b{re.escape(value)}\b", text), f"{value} is not in FINDINGS.md"


def test_findings_md_documents_every_required_field():
    text = (KIT / "FINDINGS.md").read_text()
    for k in review.REQUIRED_TOP + review.REQUIRED_FINDING:
        assert f'"{k}"' in text, f"FINDINGS.md never shows the required field {k!r}"


def test_reviewer_legs_match_the_skills():
    legs = {p.parent.name[len("review-"):] for p in KIT.glob("skills/review-*/SKILL.md")}
    assert set(review.REVIEWER_LEGS) <= legs


def documented_detect_commands():
    found = []
    for doc in DOCS:
        text = doc.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            for m in re.finditer(r"detect\.py(\s+--.*)", line):
                rest = re.split(r"\s#\s|`|\||;|&&|\)", m.group(1))[0]
                rest = re.sub(r"<[^>]*>", "X", rest)
                rest = re.sub(r"\$\([^)]*\)|\$\{\w+\}|\$\w+", "X", rest)
                found.append((doc.relative_to(KIT).as_posix(), rest.strip()))
    return found


DETECT_COMMANDS = documented_detect_commands()


def test_the_skill_documents_the_static_checks():
    assert any(d == "skills/repo-audit/SKILL.md" for d, _ in DETECT_COMMANDS)


@pytest.mark.parametrize("doc,rest", DETECT_COMMANDS, ids=[d for d, _ in DETECT_COMMANDS])
def test_every_documented_detect_command_parses(doc, rest):
    detect = load("detect")
    try:
        detect.build_parser().parse_args(shlex.split(rest))
    except SystemExit as e:
        pytest.fail(f"{doc}: `detect.py {rest}` is rejected by argparse ({e.code})")
