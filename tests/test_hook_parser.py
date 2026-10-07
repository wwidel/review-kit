"""What the hook credits as read, and what audit then concludes (plan section 4).

Every "credits nothing" case is also run through audit, because a command that credits
nothing but still counts as a reading command turns a false claim into UNRESOLVED (exit 3),
not UNSUPPORTED (exit 1). The floor cases at the end are credited although little or none of
the file reaches the reviewer; DESIGN.md documents them. They are pinned so a change to any
of them is deliberate.
"""
import os
import time

import pytest

from conftest import audit_case, load

h = load("log_file_access")


@pytest.fixture
def tree(repo):
    (repo / "b.py").write_text("y = 2\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "README.md").write_text("docs\n")
    (repo / "src" / "README.md").write_text("src\n")
    return repo


def credited(cmd, repo, cwd=None):
    paths, state = h.bash_paths(cmd, str(cwd or repo))
    return sorted(os.path.relpath(p, repo) for p in paths), state


CREDITS = [
    ("cat a.py", ["a.py"]),
    ("cd src && cat m.py", ["src/m.py"]),
    ("(cd src; cat m.py)", ["src/m.py"]),
    ("(cd src); cat a.py", ["a.py"]),
    ("for f in a b; do cat -n $f.py; done", ["a.py", "b.py"]),
    ("cat {a,b}.py", ["a.py", "b.py"]),
    ("cat src/*.py *.py", ["a.py", "b.py", "src/m.py"]),
    ("find src -name '*.py' -exec cat {} \\;", ["src/m.py"]),
    ("find src -name '*.py' | xargs cat", ["src/m.py"]),
    ("find src -name '*.py' | sort | xargs cat", ["src/m.py"]),
    ("git show HEAD:src/m.py", ["src/m.py"]),
    ("git blame a.py", ["a.py"]),
    ("git log -p -- a.py", ["a.py"]),
    ("git -C src blame m.py", ["src/m.py"]),
    ("python3 - <<'EOF'\nprint(open('a.py').read())\nEOF", ["a.py"]),
    ("python3 -c \"print(open('a.py').read())\"", ["a.py"]),
    ("cat a.py >&2", ["a.py"]),
    ("cat a.py > /dev/stderr", ["a.py"]),
    ("cat a.py | head", ["a.py"]),
    ("cat < a.py", ["a.py"]),
    ("sort a.py", ["a.py"]),
    ("rg -L y b.py", ["b.py"]),
    ("echo '#' && cat a.py", ["a.py"]),
    ("if true; then cat a.py; fi", ["a.py"]),
    ("for f in a.py; do for g in b.py; do cat $g; done | wc -l; cat $f; done", ["a.py"]),
]


@pytest.mark.parametrize("cmd,expected", CREDITS)
def test_credits_the_file(tree, cmd, expected):
    assert credited(cmd, tree)[0] == expected


# (command, a file it must NOT be credited with, whether it counts as a reading command)
NOTHING = [
    ("wc -l a.py", "a.py", False),
    ("ls a.py; echo cat", "a.py", False),
    ("md5sum a.py", "a.py", False),
    ("wc -l < a.py", "a.py", False),
    ("cat a.py | wc -l", "a.py", True),
    ("cat a.py > /tmp/x", "a.py", True),
    ("cat a.py | python3 -c 0", "a.py", True),
    ("{ cat a.py; } > /dev/null", "a.py", True),
    ("cat a.py >& /dev/null", "a.py", True),
    ("grep -l x a.py", "a.py", True),
    ("grep -q x a.py", "a.py", True),
    ("grep -c x a.py", "a.py", True),
    ("rg -l y b.py", "b.py", True),
    ("cmp -s a.py b.py", "a.py", True),
    ("head -n 0 a.py", "a.py", True),
    ("git diff --stat -- a.py", "a.py", True),
    ("git log -- a.py", "a.py", False),
    ("sort -o out.txt a.py", "a.py", True),
    ("cat a.py # b.py", "b.py", True),
    ("for f in a.py b.py; do cat $f; done | wc -l", "a.py", True),
    ("for f in a.py; do cat $f; done > /dev/null", "a.py", True),
    ("if true; then cat a.py; fi > /dev/null", "a.py", True),
    ("for f in a.py; do for g in b.py; do cat $g; done; cat $f; done | wc -l", "b.py", True),
    ("while read l; do echo $l; done < a.py", "a.py", True),
]


@pytest.mark.parametrize("cmd,target,reads", NOTHING)
def test_credits_nothing_and_audit_says_what_it_should(tmp_path, tree, cmd, target, reads):
    paths, state = credited(cmd, tree)
    assert target not in paths
    assert bool(state["reads"]) is reads
    rec = {"tool": "Bash", "agent": "x", "paths": [str(tree / p) for p in paths],
           "cmd": cmd, "cwd": str(tree), "reads": bool(state["reads"])}
    r = audit_case(tmp_path, tree, [target], [rec])
    # A reading command that names the file leaves it for a person to settle; anything else
    # is a claim nothing supports.
    named = reads and target in cmd
    assert r.returncode == (3 if named else 1), r.stdout


def test_a_grep_or_glob_path_is_never_credited(tree):
    for tool in ("Grep", "Glob"):
        assert h.paths_from(tool, {"path": str(tree / "a.py"), "pattern": "x"}) == set()


def test_command_substitution(tree):
    assert credited("x=$(cat a.py)", tree)[0] == ["a.py"]
    paths, state = credited("x=`cat a.py`", tree)
    assert paths == [] and not state["reads"]   # a real read audit will call unsupported


def test_unparseable_input_still_counts_as_a_read(tree):
    paths, state = credited("cat 'a.py", tree)
    assert paths == [] and state["reads"]


# ---------------------------------------------------------------- the documented floor

FLOOR = [
    "grep -n NO_SUCH_STRING a.py",      # prints nothing
    "python3 a.py",                     # shows output, not source
    "diff a.py a.py",                   # identical: prints nothing
    "cmp a.py b.py",                    # only the first difference
    "tail -n +9999 a.py",               # past the end
    "cat a.py | sed d",
    "sed -n 0p a.py",
    "awk 'NR==0' a.py",
    "false && cat a.py",                # never runs
    "true || cat a.py",
    "if false; then cat a.py; fi",
    "head -n1 a.py",                    # partial reads
    "sed -n 1p a.py",
    "cut -c1 a.py",
]


@pytest.mark.parametrize("cmd", FLOOR)
def test_floor_cases_are_credited(tree, cmd):
    assert "a.py" in credited(cmd, tree)[0]


def test_floor_python_comment_mentioning_a_file(tree):
    assert "b.py" in credited("python3 -c \"1 # b.py\"", tree)[0]


def test_floor_a_path_mentioned_in_a_reading_command_is_unresolved(tmp_path, tree):
    rec = {"tool": "Bash", "agent": "x", "paths": [str(tree / "a.py")],
           "cmd": "echo src/README.md; cat a.py", "cwd": str(tree), "reads": True}
    assert audit_case(tmp_path, tree, ["src/README.md"], [rec]).returncode == 3


def test_but_a_same_named_file_elsewhere_does_not_rescue_a_claim(tmp_path, tree):
    rec = {"tool": "Bash", "agent": "x", "paths": [str(tree / "docs" / "README.md")],
           "cmd": "cat docs/README.md", "cwd": str(tree), "reads": True}
    assert audit_case(tmp_path, tree, ["src/README.md"], [rec]).returncode == 1


# ---------------------------------------------------------------- the work limit

def test_a_deep_glob_that_matches_nothing_is_bounded(tmp_path):
    root = tmp_path / "deep"
    for i in range(8):
        for j in range(8):
            for k in range(8):
                (root / f"a{i}" / f"b{j}" / f"c{k}").mkdir(parents=True)
    t = time.monotonic()
    paths, state = h.bash_paths(f"cat {root}/*/*/*/*/*/zzz.py", str(tmp_path))
    # Generous: the deadline bounds the parse, and a loaded machine slows the rest.
    assert time.monotonic() - t < h.DEADLINE_S + 2
    assert paths == set()


def test_directory_listings_are_charged_to_the_budget(tmp_path, monkeypatch):
    root = tmp_path / "wide"
    for i in range(30):
        (root / f"d{i}").mkdir(parents=True)
    monkeypatch.setattr(h, "BUDGET", 10)
    paths, state = h.bash_paths(f"cat {root}/*/zzz.py", str(tmp_path))
    assert state.get("truncated") is True


def test_the_deadline_stops_a_slow_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "DEADLINE_S", -1)
    paths, state = h.bash_paths("cat a.py", str(tmp_path))
    assert state.get("truncated") is True


def test_more_than_500_matches_are_cut_and_marked(tmp_path):
    for i in range(510):
        (tmp_path / f"f{i:03}.py").write_text("x\n")
    paths, state = h.bash_paths("cat *.py", str(tmp_path))
    assert len(paths) == 500 and state.get("truncated") is True
