#!/usr/bin/env python3
"""PostToolUse hook: append every file a tool touched to that repository's access log.

Wired once, globally, with no configuration (see review-kit/README.md). It writes to
`~/.review-kit/<repo>-<hash>/review/access.jsonl` for the repository the tool ran in: the
payload's `cwd`, or its nearest ancestor with a store, so a `cd` into a subdirectory keeps
logging. It **writes nothing at all where no directory on the way up has a store**, so it is
inert outside the repositories the kit was installed for, and nothing is ever created inside
the repository under review.

Reviewers cannot see or write this log, so `review.py audit` can compare it against what a
reviewer *claimed* to read. This is the only check in the kit that a lying agent cannot defeat
from inside its own context: the record is written by the harness, not by the agent. `audit`
ignores records older than the run it is checking, because an append-only log would otherwise
let a read from weeks ago vouch for a file this run never opened.

Never fails the tool call — a broken hook must not break the session.
"""
import fnmatch, json, os, re, shlex, sys, time, datetime
from pathlib import Path

# A path-like token inside an inline script or heredoc body. The extension may run to 12
# characters (`.gitignore`, `.safetensors`) and must END the match: with `{1,6}` and nothing
# after it, `src/.gitignore` was logged as `src/.gitign`.
PATHY = re.compile(r"(?:^|[\s'\"=(])((?:/|\./|\.\./)?[\w./\-]*\.[A-Za-z0-9]{1,12})(?![\w./\-])")

# Commands that return file CONTENT. wc, md5sum and sha256sum return a count or a digest
# and are deliberately absent: a reviewer that read nothing could otherwise pass the
# scope-honesty check with a single `wc -l $(ls *.py)`. ls, echo and rm are absent for the
# same reason — naming a file is not reading it.
READ_CMDS = {"cat", "head", "tail", "sed", "awk", "less", "more", "grep", "egrep", "fgrep",
             "rg", "python", "python3", "node", "jq", "diff", "nl", "strings", "xxd", "od",
             "tac", "bat", "cut", "sort", "uniq", "zcat", "cmp"}
# Used only when a command cannot be tokenised at all, to decide whether it is worth keeping.
READERS = re.compile(r"(?:^|[\s;&|(])(" + "|".join(sorted(READ_CMDS)) + r")(?=\s|$)")

# Of those, the ones that dump every file they are given. Only these may have a glob, a brace
# list or a `find` expanded on their behalf: `cat src/*.py` prints every file, `grep foo
# src/*.py` prints the matching lines of some, and counting a search as a read of everything
# it touched is the failure the comment on Grep below describes.
DUMP_CMDS = {"cat", "head", "tail", "nl", "less", "more", "tac", "bat", "strings", "xxd", "od"}

# A pipe stage that passes what it receives on to the reviewer. `cat a.py | head` shows the
# file; `cat a.py | wc -l` shows a number, and must not count as reading a.py.
# An interpreter downstream may print anything or nothing, so it does not count as passing
# content on: `cat src/*.py | python3 -c 0` shows nothing of those files.
PASS_CMDS = (READ_CMDS - {"python", "python3", "node", "awk"}) \
    | {"tee", "column", "fold", "fmt", "expand", "tr", "rev", "less", "more"}

# Words that open or close a shell construct rather than name a command.
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "while", "until", "!", "time",
            "{", "}", "case", "esac", "sudo", "env", "command", "exec", "nice", "timeout"}
LOOPS = {"for", "while", "until", "select"}
SEPARATORS = {";", "&&", "||", "|", "&", ";;", "|&"}
REDIRECTS = {"<", ">", ">>", ">&", "&>", "&>>", ">|", "<<", "<<-", "<<<", "<&"}
OPERATORS = SEPARATORS | REDIRECTS | {"(", ")"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
INTERPRETER = re.compile(r"(?:^|[\s/;&|(])(python3?|node)\b")
VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")
BRACE = re.compile(r"(?<!\$)\{([^{}$]*,[^{}$]*)\}")

# Work the parser may do for one command. The hook runs on every tool call; a nested loop over
# a large glob once took minutes, the harness killed the hook, and nothing was logged at all.
# Glob expansion is charged per directory listed, not only per match: a deep pattern that
# matches nothing (`/usr/*/*/*/*/*/zzz.py`) listed thousands of directories for free and
# took seconds. The deadline is the backstop for any slow path nobody has thought of.
BUDGET = 5000
DEADLINE_S = 0.5
MAX_PATHS = 2000
# Commands that close a compound block, and the words that open one: a redirect or pipe
# after `done`, `fi` or `esac` applies to every command inside the block.
CLOSERS = {"done": LOOPS, "fi": {"if"}, "esac": {"case"}}
LEADERS = {"do", "then", "else", "!", "{", "time"}


class OutOfBudget(Exception):
    pass


def spend(state, n=1):
    """Charge the parse for some work; stop it when the budget or the deadline runs out."""
    state["budget"][0] -= n
    if state["budget"][0] <= 0 or time.monotonic() > state.get("deadline", float("inf")):
        raise OutOfBudget


def strip_comment(line: str) -> str:
    """A shell line without its comment. `#` starts one only at the start of a word and
    outside quotes, so `cat a.py # b.py` reads a.py alone and `echo '#'` is untouched."""
    quote, prev, i = None, " ", 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'":
            prev = line[i + 1] if i + 1 < len(line) else ""
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (prev.isspace() or prev in ";&|()"):
            return line[:i]
        prev = ch
        i += 1
    return line


def looks_like_path(s: str, root: str) -> bool:
    """`json.load` and `np.array` match the path regex; a dotted call is not a file.

    Accept only what carries a separator or actually exists on disk. An audit built on
    false positives would vouch for files nobody opened, which is the failure this log
    exists to catch.
    """
    if "/" in s:
        return True
    try:
        return (Path(root) / s).exists()
    except OSError:
        return False


def store_for(start):
    """The review store of the nearest directory at or above `start` that has one, or None.

    Walking up is what keeps a `cd` from switching the log off. Claude Code runs the hook in
    the session's current directory, and the version that looked only there wrote nothing
    once an agent had run `cd src`: the reads that followed were never logged, and `audit`
    would have reported them as claims nobody could support.

    The path is walked as written first, which is what lets a symlinked subdirectory
    (`data -> /mnt/data`) find the repository it sits in; then with symlinks resolved, which
    is what finds a repository reached through an alias of its root. Anything below an
    installed repository logs to its store, a nested git checkout or a vendored copy
    included, because those files are inside the tree being reviewed.

    The slug comes from the kit's own `review.py` rather than a copy, so the hook and every
    other stage can never disagree about where a repository's store is. Importing it costs
    about 2ms against the ~8ms this process already spends starting Python.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "review_kit_review", Path(__file__).resolve().parent / "review.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        written = Path(os.path.abspath(start))
        resolved = written.resolve()
        for top in (written, resolved) if resolved != written else (written,):
            for d in (top, *top.parents):
                store = mod.store_path(d)
                if store.is_dir():
                    return store
    except Exception:
        pass
    return None


def strip_heredocs(cmd: str):
    """Split a command into its shell text and the heredoc bodies fed to an interpreter.

    A heredoc body is data, not shell, and tokenising it as shell turns every quote in it into
    a parse failure. A body fed to python or node is a script, swept for the paths it names;
    any other body — `cat > notes.txt <<EOF` — is text being written, and naming a file in it
    reads nothing.
    """
    shell, bodies, lines, i = [], [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        shell.append(line)
        delims = [m.group(2) for m in HEREDOC.finditer(line)]
        i += 1
        for d in delims:
            body = []
            while i < len(lines) and lines[i].strip() != d:
                body.append(lines[i])
                i += 1
            i += 1
            if INTERPRETER.search(line):
                bodies.append("\n".join(body))
    return "\n".join(shell), bodies


def simple_commands(text: str):
    """Tokenise shell text into simple commands and subshell markers.

    Each command is {"words", "inputs", "hidden", "pipe"}: `inputs` are `< file` reads,
    `hidden` says its stdout went to a file or /dev/null rather than back to the reviewer,
    and `pipe` that it feeds the next command. `(` and `)` come through as markers so a `cd`
    inside a subshell does not outlive it; `{ ...; }` and `( ... )` are groups, and a redirect
    or pipe after one applies to every command inside it.
    """
    text = "\n".join(strip_comment(x) for x in text.split("\n"))
    lex = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    out, cur, pending = [], None, None
    opens, groups, piped_groups = [], {}, set()   # groups: close index -> open index

    def new():
        return {"words": [], "inputs": [], "hidden": False, "pipe": False}

    def tokens():
        # shlex runs adjacent punctuation together: `);`, `)|`, `;(`. Split such a run into
        # the operators it is made of, longest first, or `(cd src); cat a.py` keeps the cd.
        ops = sorted(OPERATORS, key=len, reverse=True)
        for tok in lex:
            if tok and tok not in OPERATORS and set(tok) <= set("();<>|&"):
                while tok:
                    op = next((o for o in ops if tok.startswith(o)), tok[0])
                    yield op
                    tok = tok[len(op):]
            else:
                yield tok

    cur = new()
    for tok in tokens():
        if pending:
            op, fd = pending
            pending = None
            if op in ("<", "<&") and fd in (None, "0"):
                cur["inputs"].append(tok)
            elif ((op in (">", ">>", ">|") and fd in (None, "1")
                   and tok not in ("/dev/stdout", "/dev/stderr", "/dev/tty"))
                  or op in ("&>", "&>>")
                  # `>& file` sends stdout and stderr to the file; `>&2` only moves stdout
                  # to stderr, which the reviewer still sees.
                  or (op == ">&" and fd in (None, "1") and not tok.isdigit()
                      and tok not in ("-", "/dev/stdout", "/dev/stderr", "/dev/tty"))):
                if not cur["words"] and not cur["inputs"] and out \
                        and len(out) - 1 in groups:
                    # `{ cat a; cat b; } > /dev/null` hides the whole group.
                    for x in out[groups[len(out) - 1]:]:
                        if isinstance(x, dict):
                            x["hidden"] = True
                else:
                    cur["hidden"] = True
            continue
        if tok in REDIRECTS:
            fd = None
            if cur["words"] and cur["words"][-1].isdigit():
                fd = cur["words"].pop()
            if tok == ">&" and fd in (None, "1"):
                pass                  # `>&2`: stdout to stderr, still shown to the reviewer
            pending = (tok, fd)
            continue
        # `{` and `}` open and close a group only where a command starts; elsewhere they are
        # ordinary words, as in `find … -exec cat {} \;`.
        group_brace = tok in ("{", "}") and not cur["words"] and not cur["inputs"]
        if tok in SEPARATORS or tok in ("(", ")") or group_brace:
            if cur["words"] or cur["inputs"]:
                cur["pipe"] = tok in ("|", "|&")
                out.append(cur)
            elif tok in ("|", "|&") and out and len(out) - 1 in groups:
                piped_groups.add(len(out) - 1)
            cur = new()
            if tok in ("(", "{"):
                opens.append(len(out))
                out.append(tok)
            elif tok in (")", "}"):
                if opens:
                    groups[len(out)] = opens.pop()
                out.append(tok)
            continue
        cur["words"].append(tok)
    if cur["words"] or cur["inputs"]:
        out.append(cur)
    # A stage is visible only if its output reaches the reviewer: not redirected away, and,
    # if piped, into a stage that passes content on and is itself visible.
    def sink_hides(k):
        nxt = next((x for x in out[k + 1:] if isinstance(x, dict)), None)
        if nxt is None or nxt["hidden"] or not nxt["words"]:
            return True
        name = os.path.basename(nxt["words"][0])
        if name == "xargs":
            return False
        return name not in PASS_CMDS or quiet(name, nxt["words"][1:])

    for k in range(len(out) - 1, -1, -1):
        c = out[k]
        if k in piped_groups and sink_hides(k):
            for x in out[groups[k]:k]:
                if isinstance(x, dict):
                    x["hidden"] = True
        if isinstance(c, dict) and c["pipe"] and sink_hides(k):
            c["hidden"] = True
        if isinstance(c, dict) and c["hidden"] and len(c["words"]) == 1 \
                and c["words"][0] in CLOSERS:
            # `for …; do cat $f; done | wc -l` and `if …; fi > /dev/null` hide the whole
            # block, as `{ …; }` does; only the closing word used to be hidden.
            start = block_start(out, k)
            if start is not None:
                for x in out[start:k]:
                    if isinstance(x, dict):
                        x["hidden"] = True
    return out


def block_start(out, k):
    """Index of the command that opens the block `out[k]` (a lone `done`/`fi`/`esac`) closes."""
    closer = out[k]["words"][0]
    depth = 0
    for i in range(k - 1, -1, -1):
        x = out[i]
        # A nested opener follows a keyword in the same simple command: `do for g in …`.
        words = [w for w in x["words"] if w not in LEADERS] if isinstance(x, dict) else []
        if not words:
            continue
        w = words[0]
        if w == closer:
            depth += 1
        elif w in CLOSERS[closer]:
            if depth == 0:
                return i
            depth -= 1
    return None


def substitute(word, env):
    def rep(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))
    w = VAR.sub(rep, word)
    if w.startswith("~"):
        w = os.path.expanduser(w)
    return w


def braces(word, limit=64):
    """`src/{a,b}.py` → [src/a.py, src/b.py]. Unnested lists only; ranges are left alone."""
    words = [word]
    for _ in range(4):
        nxt = []
        for w in words:
            m = BRACE.search(w)
            if not m:
                nxt.append(w)
                continue
            nxt += [w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(",")]
        if nxt == words:
            break
        words = nxt[:limit]
    return words


def resolve(word, cwd):
    """An existing file this word names, relative to the shell's cwd at that point, or None."""
    if not word or word.startswith("-") or "$" in word or "\0" in word:
        return None
    try:
        q = Path(word) if word.startswith("/") else Path(cwd) / word
        return str(q.resolve()) if q.is_file() else None
    except (OSError, ValueError):
        return None


def expand_glob(word, cwd, state):
    """The files a shell glob names, sorted as the shell would, at most 500 of them.

    Expanded one path component at a time, with every directory listing charged to the
    parse. `glob.iglob` charged only for what it matched, so a deep pattern that matched
    nothing could list every directory under `/usr` before returning, for free.
    """
    pat = word if word.startswith("/") else os.path.join(cwd, word)
    parts = Path(pat).parts
    cands = [parts[0]]
    for comp in parts[1:]:
        nxt = []
        for c in cands:
            if not any(ch in comp for ch in "*?["):
                spend(state)
                q = os.path.join(c, comp)
                if os.path.lexists(q):
                    nxt.append(q)
                continue
            try:
                names = os.listdir(c)
            except OSError:
                continue
            spend(state, 1 + len(names) // 200)
            nxt += [os.path.join(c, n) for n in sorted(names)
                    if (comp.startswith(".") or not n.startswith("."))
                    and fnmatch.fnmatchcase(n, comp)]
        cands = nxt
    hits = []
    for g in cands:
        if len(hits) == 500:
            # Recorded, so `audit` knows this parse did not see every file the glob matched.
            state["truncated"] = True
            break
        spend(state)
        if os.path.isfile(g):
            hits.append(str(Path(g).resolve()))
    return hits


def find_matches(args, cwd, state):
    """The files `find DIR... [-type f] [-name P] [-maxdepth N]` would print, or None.

    Only that predicate subset is emulated; anything else returns None and the read is left
    for `audit` to report as unresolved, rather than guessed at.
    """
    dirs, i, name, iname, depth = [], 0, None, None, None
    while i < len(args) and not args[i].startswith("-"):
        dirs.append(args[i])
        i += 1
    while i < len(args):
        a = args[i]
        if a in ("-exec", "-execdir"):
            break
        if a in ("-name", "-iname", "-maxdepth", "-type") and i + 1 < len(args):
            v = args[i + 1]
            if a == "-name":
                name = v
            elif a == "-iname":
                iname = v.lower()
            elif a == "-maxdepth":
                if not v.isdigit():
                    return None
                depth = int(v)
            elif v != "f":
                return None
            i += 2
            continue
        if a in ("-print", "-print0"):
            i += 1
            continue
        return None
    hits = []
    for d in dirs or ["."]:
        base = Path(d) if d.startswith("/") else Path(cwd) / d
        for root, sub, files in os.walk(base):
            spend(state)
            rel_depth = len(Path(root).relative_to(base).parts)
            if depth is not None and rel_depth >= depth:
                sub[:] = []
            sub[:] = [s for s in sub if s not in (".git", "node_modules", ".venv", "venv")]
            for f in files:
                spend(state)
                if depth is not None and rel_depth + 1 > depth:
                    continue
                if name and not fnmatch.fnmatchcase(f, name):
                    continue
                if iname and not fnmatch.fnmatchcase(f.lower(), iname):
                    continue
                hits.append(str((Path(root) / f).resolve()))
    return hits


def quiet(name, args):
    """True if this invocation prints no file content: `grep -l`, `grep -q`, `cmp -s`, `head -c0`."""
    if name in ("grep", "egrep", "fgrep", "rg"):
        # For rg, -L is --follow (symlinks), not --files-without-match as it is for grep.
        letters = set("qlc") if name == "rg" else set("qlLc")
        for a in args:
            if a in ("--quiet", "--silent", "--count", "--files-with-matches",
                     "--files-without-match"):
                return True
            if a.startswith("-") and not a.startswith("--") and set(a[1:]) & letters \
                    and not re.search(r"[0-9]", a):
                return True
    if name == "cmp" and any(a in ("-s", "--silent", "--quiet") for a in args):
        return True
    if name == "sort" and any(a == "-o" or a.startswith("--output") or
                              (a.startswith("-o") and len(a) > 2) for a in args):
        # `sort -o out a.py` writes to a file, exactly like `sort a.py > out`.
        return True
    if name in ("head", "tail"):
        joined = " ".join(args)
        if re.search(r"(?:^|\s)-[nc]\s*0(?:\s|$)", joined):
            return True
    return False


def credit_args(name, args, cwd, out, state):
    if name in ("python", "python3", "node"):
        # The script and paths named in -c code; trailing argv is data passed to it, and
        # `python3 -c 0 a.py b.py` reads neither.
        for k, w in enumerate(args):
            if w in ("-c", "-e") and k + 1 < len(args):
                for m in PATHY.findall(args[k + 1]):
                    hit = resolve(m, cwd)
                    if hit:
                        out.add(hit)
                return
            if w == "-m":
                return
            if not w.startswith("-"):
                hit = resolve(w, cwd)
                if hit:
                    out.add(hit)
                return
        return
    for w in args:
        for v in braces(w):
            if name in DUMP_CMDS and any(c in v for c in "*?["):
                out.update(expand_glob(v, cwd, state))
                continue
            hit = resolve(v, cwd)
            if hit:
                out.add(hit)
            elif name == "awk" and re.search(r"[\s'\"(]", v):
                for m in PATHY.findall(v):
                    hit = resolve(m, cwd)
                    if hit:
                        out.add(hit)


def git_root(cwd):
    p = Path(cwd)
    for q in (p, *p.parents):
        if (q / ".git").exists():
            return str(q)
    return cwd


def git_read(args, cwd, out, state, hidden):
    """`git diff/show/blame -- f` and `git log -p -- f` print a file's content or its changes.

    `git show REV:path` names the path after the colon, relative to the repository root.
    `git log` without -p prints commit messages only, and `git grep` is a search; neither
    credits anything.
    """
    k = 0
    while k < len(args) and args[k].startswith("-"):
        if args[k] == "-C" and k + 1 < len(args):
            # `git -C src show …` runs as if from src: names resolve there.
            cwd = os.path.join(cwd, args[k + 1])
        k += 2 if args[k] in ("-C", "-c") else 1
    if k >= len(args):
        return
    sub, rest = args[k], args[k + 1:]
    if sub not in ("diff", "show", "blame", "log", "annotate"):
        return
    if sub == "log" and not any(a in ("-p", "--patch", "-u") for a in rest):
        return
    state["reads"] = True
    if hidden or any(a in ("--stat", "--name-only", "--name-status", "--shortstat", "--numstat",
                           "--quiet", "--exit-code", "-s", "--no-patch") for a in rest):
        return
    for a in rest:
        if a.startswith("-"):
            continue
        if ":" in a:
            hit = resolve(a.split(":", 1)[1], git_root(cwd))
        else:
            hit = resolve(a, cwd)
        if hit:
            out.add(hit)


def walk(cmds, cwd, env, out, state, depth=0):
    """Follow a list of simple commands the way the shell would, as far as reading goes.

    Tracks `cd`/`pushd`, subshells, `VAR=value`, and unrolls `for VAR in LIST; do ...; done`
    over its list. Each of these hid real reads from the previous version, which resolved
    every bare name against the repository root: `cd src && cat -n train.py` logged nothing,
    and nor did `for f in a.py b.py; do cat -n $f; done`. Only a command whose output reaches
    the reviewer is credited.
    """
    stack, i, prev_find = [], 0, None
    while i < len(cmds):
        c = cmds[i]
        i += 1
        if c == "(":
            stack.append(cwd)
            continue
        if c == ")":
            cwd = stack.pop() if stack else cwd
            continue
        if c in ("{", "}"):
            continue               # a brace group runs in the current shell: cd persists
        spend(state)
        found, prev_find = prev_find, None
        words = [substitute(w, env) for w in c["words"]]
        while words and words[0] in KEYWORDS:
            words = words[1:]
        if words and words[0] == "for" and len(words) >= 3 and words[2] == "in" and depth < 3:
            var, items = words[1], []
            for w in words[3:]:
                for v in braces(w):
                    if any(ch in v for ch in "*?["):
                        items += [os.path.relpath(g, cwd) for g in expand_glob(v, cwd, state)]
                    else:
                        items.append(v)
            body, level = [], 1
            while i < len(cmds):
                x = cmds[i]
                i += 1
                if isinstance(x, dict):
                    ws = [w for w in x["words"] if w not in ("do", "then", "!", "{")]
                    if ws and ws[0] in LOOPS:
                        level += 1
                    if ws and ws[0] == "done":
                        level -= 1
                        if level == 0:
                            break
                body.append(x)
            if len(items) > 500:
                state["truncated"] = True
            for item in items[:500]:
                walk(body, cwd, {**env, var: item}, out, state, depth + 1)
            continue
        if c["inputs"] and words and words[0] in CLOSERS:
            # `while read l; do echo $l; done < a.py` may print the file or nothing; which,
            # only running it would tell. A reading command, so audit leaves it to a person.
            state["reads"] = True
        if words and words[0] == "done":
            words = words[1:]
        while words and re.fullmatch(r"\w+=.*", words[0]):
            name, _, value = words[0].partition("=")
            if len(words) == 1:
                env[name] = value
            words = words[1:]
        if not words:
            continue
        name = os.path.basename(words[0])
        args = words[1:]
        # `< file` is shown only to a command that passes what it reads on: `cat < a.py`
        # shows a.py, `wc -l < a.py` shows a number, and `while read l; do …; done < a.py`
        # shows whatever the loop prints, which may be nothing. All three used to credit it.
        if c["inputs"] and not c["hidden"] and name in PASS_CMDS and not quiet(name, args):
            for r in c["inputs"]:
                hit = resolve(substitute(r, env), cwd)
                if hit:
                    out.add(hit)
        if name in ("cd", "pushd"):
            target = args[0] if args else os.path.expanduser("~")
            if target != "-":
                q = Path(target) if target.startswith("/") else Path(cwd) / target
                if q.is_dir():
                    cwd = str(q.resolve())
            continue
        if name == "find":
            if "-exec" in args or "-execdir" in args:
                k = args.index("-exec") if "-exec" in args else args.index("-execdir")
                inner = os.path.basename(args[k + 1]) if k + 1 < len(args) else ""
                if inner in READ_CMDS:
                    state["reads"] = True
                if inner in DUMP_CMDS and not c["hidden"]:
                    hits = find_matches(args[:k], cwd, state)
                    if hits:
                        out.update(hits)
            elif c["pipe"]:
                prev_find = find_matches(args, cwd, state)
            continue
        if found is not None and c["pipe"] and name in ("sort", "uniq", "tr", "tee"):
            prev_find = found      # `find … | sort | xargs cat`: the list passes through intact
            continue
        if name == "git":
            git_read(args, cwd, out, state, c["hidden"])
            continue
        if name == "xargs":
            k = 0
            while k < len(args) and args[k].startswith("-"):
                k += 2 if args[k] in ("-n", "-I", "-P", "-L", "-d", "-s") else 1
            inner = os.path.basename(args[k]) if k < len(args) else ""
            if inner in READ_CMDS:
                state["reads"] = True
            if inner in DUMP_CMDS and found and not c["hidden"]:
                out.update(found)
            continue
        if name not in READ_CMDS:
            continue
        state["reads"] = True
        if c["hidden"] or quiet(name, args):
            continue
        credit_args(name, args, cwd, out, state)
    return cwd


def bash_paths(cmd: str, cwd: str):
    """(files a Bash command showed the reviewer, whether it ran a reading command). Never raises."""
    out, state = set(), {"budget": [BUDGET], "reads": False,
                         "deadline": time.monotonic() + DEADLINE_S}
    try:
        text, bodies = strip_heredocs(cmd)
        if bodies:
            state["reads"] = True
        for b in bodies:
            for m in PATHY.findall(b):
                hit = resolve(m, cwd)
                if hit:
                    out.add(hit)
        walk(simple_commands(text), cwd, {}, out, state)
    except OutOfBudget:
        state["truncated"] = True
    except Exception:
        # Unbalanced quotes and the like: credit nothing, but keep the command if it looks
        # like a read, so `audit` can report its files as unresolved rather than unsupported.
        state["reads"] = state["reads"] or bool(READERS.search(cmd))
    return out, state


def paths_from(tool, inp, cwd=None, state=None):
    out = set()
    # `path` on a Grep or Glob is the directory searched, not a file whose contents came back.
    # Counting it would let one grep vouch for every file under it — the opposite of what
    # this log is for, and contrary to the note below.
    keys = ("file_path", "notebook_path") if tool in ("Grep", "Glob") \
        else ("file_path", "path", "notebook_path")
    for k in keys:
        if isinstance(inp.get(k), str):
            out.add(inp[k])
    if isinstance(inp.get("command"), str):
        # Only files given to a command whose output came back. The previous version swept
        # every path-like token in any command that mentioned a reader anywhere, so
        # `wc -l a.py | sort` or `ls a.py; echo cat` "read" a.py.
        paths, st = bash_paths(inp["command"], cwd or os.getcwd())
        out |= paths
        if state is not None:
            state.update(st)
    # A Grep or Glob pattern is deliberately ignored: searching a tree is not reading any
    # particular file, and counting it as one would let a single grep "support" a claim to
    # have read everything it matched.
    return {p for p in out if p and not p.startswith("-")}


def filtered(paths, cwd):
    """Plausible paths, one spelling each, all absolute. A relative path is relative to the
    directory the tool ran in — the payload's `cwd` — not to the repository root: after
    `cd src`, a Read of `m.py` means src/m.py."""
    out = set()
    for p in paths:
        if not looks_like_path(p, cwd):
            continue
        if not p.startswith("/"):
            try:
                q = Path(cwd) / p
                p = str(q.resolve()) if q.is_file() else os.path.normpath(str(q))
            except (OSError, ValueError):
                p = os.path.normpath(os.path.join(cwd, p))
        out.add(p)
    return sorted(out)


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(ev, dict):
        return
    tool = ev.get("tool_name", "")
    if tool not in ("Read", "Bash", "NotebookEdit", "Edit", "Write", "Grep", "Glob"):
        return
    inp = ev.get("tool_input") or {}
    # Where the tool ran: the payload says, and the hook process's own directory is only the
    # fallback. REVIEW_ACCESS_ROOT, if set, overrides both as the place to start looking.
    cwd = ev.get("cwd") or os.getcwd()
    logdir = store_for(os.environ.get("REVIEW_ACCESS_ROOT") or cwd)
    if logdir is None:
        # No store here or above, so this is not a repository anyone installed the kit for.
        # Writing anyway is how an earlier version littered a `.review/` into every directory
        # a session ever touched — and worse, that bare directory then read as an in-repo
        # install and put a whole run inside the repository being audited.
        return
    try:
        state = {}
        paths = filtered(paths_from(tool, inp, cwd, state), cwd)
        rec = {"t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "tool": tool, "agent": ev.get("agent_id") or ev.get("session_id"),
               "paths": paths[:MAX_PATHS]}
        if len(paths) > MAX_PATHS or state.get("truncated"):
            # Recorded, not hidden: `audit` must not mistake a cut-short parse for a complete one.
            rec["truncated"] = True
        cmd = inp.get("command")
        if tool == "Bash" and isinstance(cmd, str):
            # The raw command and where it ran, so `audit` can tell a file named in a command
            # this parser could not follow from a file that appears in no command at all —
            # and so a human can settle the difference by reading it. Written for reading
            # commands even when no path resolved: that is exactly the case worth seeing.
            rec["cwd"] = cwd
            rec["cmd"] = cmd[:4000]
            rec["reads"] = bool(state.get("reads"))
        if rec["paths"] or rec.get("reads"):
            with (logdir / "access.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception as e:
        # Still never break the session — but leave a trace. A silently failed hook and an
        # honest reviewer that read nothing produce the same empty log, and `audit` then
        # accuses the reviewer.
        try:
            (logdir / "access.errors").open("a", encoding="utf-8").write(
                f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {type(e).__name__}: {e}\n")
        except Exception:
            pass


if __name__ == "__main__":
    main()
