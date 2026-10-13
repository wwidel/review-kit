#!/usr/bin/env python3
"""Static detectors for defects common in data-analysis repositories.

Each detector reads code without running it and reports sites, with the evidence that makes
the site suspicious. They differ from `review.py probe`, which lists every site of a kind
(every `.fit`, every seed call) for a reviewer to judge: a detector here fires only on a
specific defective pattern, so most of its hits should be worth a finding on their own.

    python3 detect.py --repo PATH [--json OUT] [--words FILE] [--only NAME,...]
    python3 detect.py --manifest RUN/manifest.json --findings RUN/findings-static.json

The second form is how /repo-audit uses it: the precise kinds, within the run's review tier,
written as a findings document for the reconciler to verify like any reviewer's.

Detectors:

    unseeded               randomness with no seed: a seedable call without its seed
                           argument, global RNG use in a file that never seeds, a
                           commented-out seed, an R script that samples without set.seed
    fit-outside-fold       a transformer fitted on test data, refitted on different data
                           (two bases), or fitted before the data is split into folds
    resample-before-split  rows resampled or duplicated before the train/held-out split
    ignored-setting        a parameter never read or overwritten by a literal, a condition
                           on a constant, a string compared in the wrong case, an argv
                           dispatch with no else, a config key no code reads
    missing-producer       a file the code reads that nothing writes and nothing provides
    regex-corrupts-words   a text-cleaning rule that rewrites the middle of common words
    cannot-run             a syntax error, a name used and never defined, yaml.load
                           without a Loader
    duplicate-code         a function defined twice in one scope, identical bodies, and
                           long duplicated blocks
    hard-coded-path        absolute or home-directory paths, setwd/chdir to a literal, a
                           worker global set only under __main__

Stdlib only. Python 3.10+.
"""
from __future__ import annotations

import argparse, ast, builtins, hashlib, json, os, re, shutil, subprocess, sys, warnings
from dataclasses import dataclass, asdict, field
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "__pycache__", ".venv", "venv",
             ".mypy_cache", ".pytest_cache", ".review", ".ipynb_checkpoints", "site-packages"}
CODE_EXT = {".py", ".r", ".sh", ".yml", ".yaml"}


@dataclass
class Hit:
    detector: str
    kind: str
    file: str
    line: int
    message: str
    evidence: str = ""
    confidence: str = "medium"       # "high": the pattern alone is the defect


@dataclass
class Repo:
    """What every detector receives: each one uses the parts it needs."""
    root: Path
    py: list                    # PyFile per Python file
    others: list                # (relative path, text) for R, shell and YAML files
    words: list | None = None   # common words for regex-corrupts-words


@dataclass
class PyFile:
    rel: str
    text: str
    tree: ast.AST | None
    lines: list = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def repo_files(repo: Path):
    """Tracked files and untracked ones git does not ignore: `scope` puts both in a review."""
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       cwd=repo, capture_output=True)
    if r.returncode == 0 and r.stdout:
        names = [os.fsdecode(x) for x in r.stdout.split(b"\0") if x]
    else:
        names = [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file()]
    out = []
    for n in sorted(set(names)):
        parts = Path(n).parts
        if set(parts[:-1]) & SKIP_DIRS:
            continue
        if (repo / n).is_file():
            out.append(n)
    return out


LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(text):
    """Lines as Python numbers them. `str.splitlines` also breaks on form feeds, U+2028 and
    other separators, which made reported line numbers run past the end of the file."""
    lines = LINE_BREAK.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


_SEGMENTS = {}


def src(node, text):
    """A node's source text. `ast.get_source_segment` re-splits the whole file on every call,
    which made the detectors quadratic in file length; this splits each file once. Column
    offsets in the AST are UTF-8 byte offsets, hence the encoding."""
    end = getattr(node, "end_lineno", None)
    if end is None or getattr(node, "lineno", None) is None:
        return ""
    cached = _SEGMENTS.get(id(text))
    if cached is None or cached[0] is not text:
        if len(_SEGMENTS) > 256:
            _SEGMENTS.clear()
        cached = (text, [line.encode("utf-8") for line in split_lines(text)])
        _SEGMENTS[id(text)] = cached
    lines = cached[1]
    try:
        a, b = node.lineno - 1, end - 1
        if a == b:
            seg = lines[a][node.col_offset:node.end_col_offset]
        else:
            seg = b"\n".join([lines[a][node.col_offset:]] + lines[a + 1:b]
                             + [lines[b][:node.end_col_offset]])
        return seg.decode("utf-8", errors="replace")
    except (IndexError, TypeError):
        return ""


def callee(call: ast.Call) -> str:
    """The called name, last component: `pd.read_csv` -> read_csv, `KMeans` -> KMeans."""
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def dotted(node) -> str:
    """`np.random.seed` for an Attribute chain of Names, else ''."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def kwnames(call: ast.Call):
    return {k.arg for k in call.keywords if k.arg}


def has_kwargs_splat(call: ast.Call):
    return any(k.arg is None for k in call.keywords)


def kw(call: ast.Call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def str_const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
COMPOUND = (ast.For, ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try,
            ast.Match) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


def scopes(tree):
    """(scope node, [its statements]) for the module, each class body and each function.

    Statements of nested blocks — loops, ifs, withs, trys, match cases — belong to the scope
    they sit in; nested definitions are scopes of their own, never part of the outer one.
    """
    def flatten(body):
        for st in sorted(body, key=lambda s: s.lineno):
            if isinstance(st, DEFS):
                continue
            yield st
            inner = []
            for name in ("body", "orelse", "finalbody"):
                inner += [x for x in getattr(st, name, None) or [] if isinstance(x, ast.stmt)]
            for h in getattr(st, "handlers", None) or []:
                inner += h.body
            for case in getattr(st, "cases", None) or []:
                inner += case.body
            yield from flatten(inner)
    yield tree, list(flatten(tree.body))
    for node in ast.walk(tree):
        if isinstance(node, DEFS):
            yield node, list(flatten(node.body))


def calls_in(stmts):
    """Every Call in these statements, by line. A compound statement contributes only its
    header (its block's statements are in the list themselves), so nothing is counted twice."""
    out = []
    for st in stmts:
        nodes = _shallow(st) if isinstance(st, COMPOUND) else ast.walk(st)
        out += [n for n in nodes if isinstance(n, ast.Call)]
    return sorted(out, key=lambda c: (c.lineno, c.col_offset))


def _shallow(st):
    """The nodes of a compound statement's header only."""
    heads = []
    if isinstance(st, (ast.For, ast.AsyncFor)):
        heads = [st.target, st.iter]
    elif isinstance(st, (ast.While, ast.If)):
        heads = [st.test]
    elif isinstance(st, (ast.With, ast.AsyncWith)):
        heads = [i.context_expr for i in st.items]
    elif isinstance(st, ast.Match):
        heads = [st.subject] + [c.guard for c in st.cases if c.guard is not None]
    for h in heads:
        yield from ast.walk(h)


def own_nodes(fn):
    """Every node in a function's body, not descending into nested definitions or lambdas."""
    stack = [n for n in fn.body if not isinstance(n, DEFS)]
    while stack:
        n = stack.pop()
        yield n
        for child in ast.iter_child_nodes(n):
            if not isinstance(child, DEFS + (ast.Lambda,)):
                stack.append(child)


def imported_from(tree):
    """{local name: module} for every `from module import name` and `import module as name`."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                out[a.asname or a.name] = n.module
        elif isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name
    return out


WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(text):
    """Lower-case identifier parts: `x_test`, `testData`, `data_test` all contain 'test'."""
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", text)
    return set(t for t in WORD_SPLIT.split(text.lower()) if t)


# ---------------------------------------------------------------- unseeded

# Callables whose randomness is controlled by an argument, and the names that argument takes.
SEEDED_CALLS = {
    "train_test_split": ("random_state",), "KMeans": ("random_state",),
    "MiniBatchKMeans": ("random_state",), "TruncatedSVD": ("random_state",),
    "RandomForestClassifier": ("random_state",), "RandomForestRegressor": ("random_state",),
    "ExtraTreesClassifier": ("random_state",), "ExtraTreesRegressor": ("random_state",),
    "GradientBoostingClassifier": ("random_state",),
    "GradientBoostingRegressor": ("random_state",),
    "HistGradientBoostingClassifier": ("random_state",),
    "HistGradientBoostingRegressor": ("random_state",),
    "DecisionTreeClassifier": ("random_state",), "DecisionTreeRegressor": ("random_state",),
    "AdaBoostClassifier": ("random_state",), "BaggingClassifier": ("random_state",),
    "IsolationForest": ("random_state",), "MLPClassifier": ("random_state",),
    "MLPRegressor": ("random_state",), "SGDClassifier": ("random_state",),
    "SGDRegressor": ("random_state",), "TSNE": ("random_state",), "NMF": ("random_state",),
    "LatentDirichletAllocation": ("random_state",), "GaussianMixture": ("random_state",),
    "ShuffleSplit": ("random_state",), "StratifiedShuffleSplit": ("random_state",),
    "GroupShuffleSplit": ("random_state",), "resample": ("random_state",),
    "shuffle": ("random_state",), "make_classification": ("random_state",),
    # Not XGBoost, LightGBM or CatBoost: with no seed they use a fixed default, so an omitted
    # seed does not make them vary between runs.
    "RandomUnderSampler": ("random_state",), "RandomOverSampler": ("random_state",),
    "SMOTE": ("random_state",),
}
# Seeded only when shuffling.
SHUFFLED_SPLITTERS = {"KFold", "StratifiedKFold", "GroupKFold", "RepeatedKFold",
                      "RepeatedStratifiedKFold"}
NP_RANDOM_FNS = {"rand", "randn", "randint", "random", "random_sample", "choice", "shuffle",
                 "permutation", "uniform", "normal", "binomial", "poisson", "beta", "gamma",
                 "exponential", "multinomial", "random_integers", "standard_normal"}
PY_RANDOM_FNS = {"random", "randint", "choice", "choices", "shuffle", "sample", "uniform",
                 "gauss", "randrange", "normalvariate"}
TORCH_RANDOM_FNS = {"rand", "randn", "randint", "randperm", "normal", "bernoulli", "multinomial"}
TORCH_SEED = re.compile(r"\b(?:torch\.manual_seed|torch\.cuda\.manual_seed(?:_all)?|"
                        r"seed_everything|seed_all|set_seed|fix_seed|pl\.seed_everything)\s*\(")


def trains_torch(tree):
    """A line that back-propagates a loss, in code: a docstring example does not count, and
    nor does an `optimizer.step()` with no gradient behind it (a scheduler demo)."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                n.func.attr == "backward":
            return n.lineno
    return None

SEED_CALL = re.compile(r"\b(?:np\.random\.seed|numpy\.random\.seed|random\.seed|"
                       r"torch\.manual_seed|torch\.cuda\.manual_seed(?:_all)?|tf\.random\."
                       r"set_seed|set_seed|seed_everything|seed_all|fix_seed|set\.seed)\s*\(")
# A seeding call that has been commented out — the comment starts with the call; a sentence
# that mentions one does not count.
COMMENTED_SEED = re.compile(r"^\s*#\s*" + SEED_CALL.pattern.replace(r"\b", "", 1))
# Calls that seed numpy's global RNG, which sklearn and pandas draw from when not given a seed.
NUMPY_SEEDS = {"np.random.seed", "numpy.random.seed"}
SEED_HELPERS = {"seed_everything", "seed_all", "set_seed", "fix_seed", "set_seeds", "seed_torch"}
R_RANDOM = re.compile(r"\b(sample|runif|rnorm|rbinom|randomForest|xgb\.cv|xgb\.train|"
                      r"createDataPartition|createFolds|kmeans|train)\s*\(")


def _local_imports(f, by_module, local):
    """Repository modules this file imports, by module stem. An absolute import counts only
    when its top-level name is a module or directory of the repository (`local`): `from
    torch.utils.data import DataLoader` does not import the repository's data.py."""
    out = []
    for n in ast.walk(f.tree) if f.tree is not None else []:
        names = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names if a.name.split(".")[0] in local]
        elif isinstance(n, ast.ImportFrom) and n.module and (
                n.level or n.module.split(".")[0] in local):
            names = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
        elif isinstance(n, ast.ImportFrom) and not n.module:     # from . import utils
            names = [a.name for a in n.names]
        for name in names:
            for part in (name.split(".")[-1], name.split(".")[0]):
                cands = by_module.get(part, [])
                # `import utils` means the utils beside this file when there is one; a
                # same-named module elsewhere in the repository is a different module.
                near = [g for g in cands if Path(g.rel).parent == Path(f.rel).parent]
                out += near or (cands if len(cands) == 1 else [])
    return out


def _seeds_torch(f):
    """True if this file's code seeds torch: a seeding call, not a comment or a docstring that
    mentions one. A file that does not parse is judged by its text."""
    if f.tree is None:
        return bool(TORCH_SEED.search(f.text))
    return any(isinstance(c, ast.Call) and (TORCH_SEED.match(dotted(c.func) + "(") or
                                            callee(c) in SEED_HELPERS) for c in ast.walk(f.tree))


def _active_seeds(f):
    """Lines of seeding calls in this file's code (not in comments or strings)."""
    return [c.lineno for c in ast.walk(f.tree) if isinstance(c, ast.Call) and (
        SEED_CALL.match(dotted(c.func) + "(") or callee(c) in SEED_HELPERS)]


def detect_unseeded(r):
    py, others = r.py, r.others
    hits = []
    by_module = {}
    for f in py:
        by_module.setdefault(Path(f.rel).stem, []).append(f)
    local = set(by_module) | {d for f in py for d in Path(f.rel).parts[:-1]}
    importers = {}
    for g in py:
        for m in _local_imports(g, by_module, local):
            importers.setdefault(m.rel, []).append(g)
    seeds_torch = {}

    def torch_seeded(f):
        # This file, the modules it imports, and the files that import it: a DataLoader built
        # in data.py is seeded by the train.py that imports it and calls torch.manual_seed.
        near = [f] + _local_imports(f, by_module, local) + importers.get(f.rel, [])
        for g in near:
            if g.rel not in seeds_torch:
                seeds_torch[g.rel] = _seeds_torch(g)
        return any(seeds_torch[g.rel] for g in near)
    repo_seeds = any(SEED_CALL.search(f.text) for f in py) or any(
        SEED_CALL.search(t) for _, t in others)
    for f in py:
        active = _active_seeds(f) if f.tree is not None else []
        if not active:
            for i, line in enumerate(f.lines, 1):
                if COMMENTED_SEED.match(line):
                    hits.append(Hit("unseeded", "commented-out-seed", f.rel, i,
                                    "a seeding call is commented out, and nothing else in this "
                                    "file seeds", line.strip(), "high"))
        if f.tree is None:
            continue
        file_seeds = bool(active)
        imports = imported_from(f.tree)
        global_uses = []
        # With random_state=None, sklearn and pandas draw from numpy's global RNG, so a numpy
        # seed makes them reproducible: one earlier in the same scope, or one at module level,
        # which runs before any function in the module is called.
        seeded_from = {}
        module_seed = None
        for scope, stmts in scopes(f.tree):
            seed_lines = [c.lineno for c in calls_in(stmts)
                          if dotted(c.func) in NUMPY_SEEDS or callee(c) in SEED_HELPERS]
            if isinstance(scope, ast.Module) and seed_lines:
                module_seed = min(seed_lines)
            for c in calls_in(stmts):
                if seed_lines and min(seed_lines) < c.lineno:
                    seeded_from[id(c)] = min(seed_lines)
        for scope, stmts in scopes(f.tree):
            if module_seed is not None and not isinstance(scope, ast.Module):
                for c in calls_in(stmts):
                    seeded_from[id(c)] = module_seed
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Call):
                continue
            name = callee(node)
            full = dotted(node.func)
            # A numpy seed reproduces numpy and what draws from it, sklearn and pandas; a
            # DataLoader shuffles with torch's generator, which it does not touch.
            if id(node) in seeded_from and name != "DataLoader":
                continue
            if name in SEEDED_CALLS and not has_kwargs_splat(node):
                seeds = SEEDED_CALLS[name]
                sh = kw(node, "shuffle")
                origin = imports.get(full.split(".")[0], "") if full else ""
                if not (kwnames(node) & set(seeds)):
                    if name in ("shuffle", "resample") and not (
                            full.startswith("sklearn") or origin.startswith("sklearn")):
                        pass        # random.shuffle, scipy's resample: they take no seed
                    elif isinstance(sh, ast.Constant) and sh.value is False:
                        pass        # train_test_split(shuffle=False) draws nothing
                    else:
                        hits.append(Hit("unseeded", "seedable-call-without-seed", f.rel,
                                        node.lineno, f"{name}(...) without {' or '.join(seeds)}",
                                        src(node, f.text)[:160], "high"))
            elif name in SHUFFLED_SPLITTERS and not has_kwargs_splat(node):
                sh = kw(node, "shuffle")
                if isinstance(sh, ast.Constant) and sh.value is True \
                        and "random_state" not in kwnames(node):
                    hits.append(Hit("unseeded", "seedable-call-without-seed", f.rel, node.lineno,
                                    f"{name}(shuffle=True) without random_state",
                                    src(node, f.text)[:160], "high"))
            elif name == "sample" and isinstance(node.func, ast.Attribute) and \
                    (kwnames(node) & {"n", "frac"}) and "random_state" not in kwnames(node) \
                    and full.split(".")[0] not in ("random", "np", "numpy"):
                hits.append(Hit("unseeded", "seedable-call-without-seed", f.rel, node.lineno,
                                ".sample(...) without random_state", src(node, f.text)[:160],
                                "high"))
            elif name == "DataLoader" and not has_kwargs_splat(node):
                sh = kw(node, "shuffle")
                shuffles = sh is not None and not (isinstance(sh, ast.Constant) and not sh.value)
                if shuffles and not (kwnames(node) & {"generator", "worker_init_fn"}) and \
                        not torch_seeded(f):
                    hits.append(Hit("unseeded", "unseeded-dataloader", f.rel, node.lineno,
                                    "a shuffling DataLoader, and neither this file nor the "
                                    "modules it imports seed torch", src(node, f.text)[:160],
                                    "high"))
            if (full.startswith(("np.random.", "numpy.random.")) and name in NP_RANDOM_FNS) \
                    or (full.startswith("random.") and full.count(".") == 1
                        and name in PY_RANDOM_FNS) \
                    or (full.startswith("torch.") and full.count(".") == 1
                        and name in TORCH_RANDOM_FNS):
                global_uses.append(node)
        line = trains_torch(f.tree)
        if line and re.search(r"__name__\s*==\s*['\"]__main__", f.text) and not torch_seeded(f):
            hits.append(Hit("unseeded", "unseeded-torch-training", f.rel, line,
                            "this script trains a torch model, and neither it nor the modules "
                            "it imports call torch.manual_seed: initialisation, dropout and "
                            "shuffling differ on every run", f.lines[line - 1].strip()[:160],
                            "high"))
        if global_uses and not file_seeds:
            first = global_uses[0]
            hits.append(Hit("unseeded", "global-rng-unseeded-file", f.rel, first.lineno,
                            f"{len(global_uses)} draw(s) from a global RNG and no seeding call "
                            "in this file" + ("" if repo_seeds else " or anywhere in the "
                                              "repository"),
                            src(first, f.text)[:160], "medium" if repo_seeds else "high"))
    for rel, text in others:
        if not rel.lower().endswith(".r"):
            continue
        uses = [i for i, l in enumerate(split_lines(text), 1)
                if R_RANDOM.search(l) and not l.lstrip().startswith("#")]
        code = "\n".join(l for l in split_lines(text) if not l.lstrip().startswith("#"))
        if uses and "set.seed(" not in code.replace(" ", ""):
            hits.append(Hit("unseeded", "r-script-without-set-seed", rel, uses[0],
                            f"{len(uses)} random call(s) and no set.seed in this R script",
                            split_lines(text)[uses[0] - 1].strip()[:160], "high"))
    return hits


# ---------------------------------------------------------------- fit outside the fold

# A class is taken for a fitted preprocessing step by the end of its name: StandardScaler,
# OneHotEncoder, TfidfVectorizer, TruncatedSVD. Stateless ones are listed too, so they are
# recognised and then excluded (STATELESS below).
TRANSFORMER_SUFFIXES = ("Scaler", "Encoder", "Imputer", "Vectorizer", "PCA", "SVD", "Normalizer",
                        "Transformer", "Discretizer", "Binarizer", "SelectKBest", "NMF",
                        "FactorAnalysis", "LatentDirichletAllocation")


def is_transformer(cls: str) -> bool:
    return bool(cls) and cls.endswith(TRANSFORMER_SUFFIXES)
SPLITTERS = {"train_test_split", "cross_val_score", "cross_validate", "cross_val_predict"}
SPLIT_OBJECTS = SHUFFLED_SPLITTERS | {"ShuffleSplit", "StratifiedShuffleSplit", "GroupShuffleSplit",
                                      "TimeSeriesSplit", "LeaveOneOut", "PredefinedSplit"}
HELDOUT = {"test", "val", "valid", "validation", "holdout", "dev"}
# Fitting these learns nothing from the data, so refitting them is harmless.
STATELESS = {"Normalizer", "FunctionTransformer", "Binarizer", "PolynomialFeatures",
             "HashingVectorizer", "SplineTransformer"}
SPLIT_RECEIVER = {"kf", "skf", "gkf", "fold", "folds", "kfold", "cv", "splitter", "split", "rkf",
                  "sss", "ss", "tscv", "logo", "loo"}
# Encoders of the target or of a fixed category set: fitting them on every row maps labels to
# integers and learns nothing a held-out score depends on.
LABEL_ENCODERS = {"LabelEncoder", "LabelBinarizer", "MultiLabelBinarizer"}
TRAIN = {"train", "training", "tr", "trn"}
SIDES = HELDOUT | TRAIN | {"tst"}


def _heldout_only(name):
    """A name for held-out rows alone: `X_test`, `val_df`; not `X_train_val`, which holds the
    training rows too."""
    tk = tokens(name)
    return bool(tk & HELDOUT) and not tk & TRAIN


def _bound_classes(tree, text):
    """{target source: called name} for every `x = Something(...)` in the file."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for t in node.targets:
                out[src(t, text)] = callee(node.value)
    return out


def _base_name(expr_src):
    """The object an expression starts from: `X_test[cols]` -> X_test, `df[df.a > 0]` -> df."""
    m = re.match(r"\s*(?:self\.)?(\w+)", expr_src)
    return m.group(1) if m else ""


def _sides_only(a, b):
    """True if two argument sources differ only in train/test-style words and digits."""
    def norm(s):
        s = re.sub(r"\d+", "", s)
        return "_".join(w for w in re.split(r"[^A-Za-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", s).lower())
                        if w and w not in SIDES)
    return a != b and norm(a) == norm(b) and bool(
        (tokens(a) | tokens(b)) & SIDES)


def _used_together(names, stmts, after):
    """True if one expression after line `after` reads every name in `names`."""
    for st in stmts:
        if st.lineno <= after:
            continue
        for n in ast.walk(st) if not isinstance(st, COMPOUND) else _shallow(st):
            if isinstance(n, (ast.Call, ast.BinOp, ast.Compare, ast.Tuple, ast.List)):
                read = {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}
                if names <= read:
                    return True
    return False


def _transformer_vars(tree, text):
    """Names (or self.attrs) bound to a transformer instance anywhere in the file."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            cls = callee(node.value)
            if is_transformer(cls):
                for t in node.targets:
                    key = src(t, text)
                    if key:
                        names[key] = cls
    return names


def _splitter_names(tree, text):
    """Names bound to a cross-validation splitter object anywhere in the file."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and \
                callee(node.value) in SPLIT_OBJECTS:
            out |= {src(t, text) for t in node.targets}
    return out


def _split_line(stmts, text, splitters=frozenset()):
    """First line in this scope at which data is split into train/held-out parts. A `.split(`
    counts only on a splitter object: `str.split` is not a data split."""
    for c in calls_in(stmts):
        name = callee(c)
        if name in SPLITTERS:
            return c.lineno
        if name == "split" and isinstance(c.func, ast.Attribute) and c.args:
            recv = c.func.value
            rsrc = src(recv, text)
            if rsrc in splitters or (isinstance(recv, ast.Call) and callee(recv) in SPLIT_OBJECTS) \
                    or (tokens(rsrc) & SPLIT_RECEIVER and not isinstance(c.args[0], ast.Constant)):
                return c.lineno
    return None


def _imported_names(tree):
    """{local name: (module, name in that module)} for `from module import name [as local]`,
    and {"*": [modules]} for star imports."""
    out = {"*": []}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                if a.name == "*":
                    out["*"].append(n.module)
                else:
                    out[a.asname or a.name] = (n.module, a.name)
    return out


def _function_resolver(py):
    """A function that maps (file, bare called name) to the (file, name) of the repository
    function it calls: one defined in that file, or imported from a repository module by
    `from module import name` or `from module import *`. A same-named function in some other
    file is a different function."""
    defined = {}
    by_stem = {}
    for f in py:
        if f.tree is None:
            continue
        by_stem.setdefault(Path(f.rel).stem, []).append(f)
        defined[f.rel] = {n.name for n in ast.walk(f.tree)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    imports = {f.rel: _imported_names(f.tree) for f in py if f.tree is not None}

    def module_file(f, module):
        cands = by_stem.get(module.split(".")[-1], [])
        near = [g for g in cands if Path(g.rel).parent == Path(f.rel).parent]
        return (near or (cands if len(cands) == 1 else [None]))[0]

    def resolve(f, name):
        if name in defined.get(f.rel, ()):
            return (f.rel, name)
        imp = imports.get(f.rel, {})
        if name in imp:
            module, orig = imp[name]
            g = module_file(f, module)
            return (g.rel, orig) if g is not None and orig in defined.get(g.rel, ()) else None
        for module in imp.get("*", []):
            g = module_file(f, module)
            if g is not None and name in defined.get(g.rel, ()):
                return (g.rel, name)
        return None
    return resolve


def _fitting_functions(py, resolve):
    """(file, name) of repository functions whose body fits a transformer (one level of
    calls)."""
    direct = set()
    defs = []
    for f in py:
        if f.tree is None:
            continue
        tv = _transformer_vars(f.tree, f.text)
        for node in ast.walk(f.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.append((f, node))
                for c in ast.walk(node):
                    if isinstance(c, ast.Call) and callee(c) in ("fit", "fit_transform") and \
                            isinstance(c.func, ast.Attribute) and (
                                src(c.func.value, f.text) in tv
                                or callee(c) == "fit_transform"):
                        direct.add((f.rel, node.name))
                        break
    indirect = set()
    for f, node in defs:
        if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
               and resolve(f, c.func.id) in direct for c in ast.walk(node)):
            indirect.add((f.rel, node.name))
    return direct | indirect


def detect_fit_outside_fold(r):
    py = r.py
    hits = []
    resolve = _function_resolver(py)
    fitters = _fitting_functions(py, resolve)
    for f in py:
        if f.tree is None:
            continue
        all_tv = _transformer_vars(f.tree, f.text)
        tv = {k: v for k, v in all_tv.items() if v not in STATELESS}
        bound = _bound_classes(f.tree, f.text)
        splitters = _splitter_names(f.tree, f.text)
        for scope, stmts in scopes(f.tree):
            # A statistic of the held-out rows written back into them: `test[c] = test[c].mean()`.
            for st in stmts:
                if isinstance(st, ast.Assign) and len(st.targets) == 1:
                    tgt_base = _base_name(src(st.targets[0], f.text))
                    for c in ast.walk(st.value):
                        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and
                                callee(c) in ("mean", "median", "std", "mode", "max", "min",
                                              "quantile")):
                            continue
                        axis = kw(c, "axis") if kw(c, "axis") is not None else (
                            c.args[0] if c.args else None)
                        if isinstance(axis, ast.Constant) and axis.value in (1, "columns"):
                            continue    # a row-wise aggregate: no statistic of the rows
                        if tgt_base and _base_name(src(c.func.value, f.text)) == tgt_base and \
                                _heldout_only(tgt_base):
                            hits.append(Hit("fit-outside-fold", "heldout-statistic", f.rel,
                                            st.lineno, f"{tgt_base} is filled with a statistic "
                                            "of itself: the value used on held-out rows is "
                                            "estimated from the rows being scored",
                                            src(st, f.text)[:160], "high"))
                            break
            fits = []          # (call, receiver source, argument source, class or "")
            for c in calls_in(stmts):
                if callee(c) not in ("fit", "fit_transform") or not isinstance(
                        c.func, ast.Attribute) or not c.args:
                    continue
                recv = src(c.func.value, f.text)
                cls = callee(c.func.value) if isinstance(c.func.value, ast.Call) else (
                    all_tv.get(recv) or bound.get(recv, ""))
                if cls in STATELESS or cls in LABEL_ENCODERS:
                    continue
                # A known class that is not a preprocessing step (TSNE, KMeans) is not one;
                # an unknown receiver that fit_transforms is taken to be one.
                if cls[:1].isupper() and not is_transformer(cls):
                    continue
                if recv in tv or is_transformer(cls) or callee(c) == "fit_transform":
                    fits.append((c, recv, src(c.args[0], f.text), cls))
            for c, recv, arg, cls in fits:
                combined = re.search(r"\b(concat\w*|cat|vstack|hstack|append|r_|row_stack)\b|\+",
                                     arg)
                if tokens(arg) & HELDOUT and combined:
                    hits.append(Hit("fit-outside-fold", "fit-on-train-and-test", f.rel,
                                    c.lineno, f"{recv} is fitted on training and held-out "
                                    f"data together ({arg[:60]})", src(c, f.text)[:160],
                                    "medium"))
                elif _heldout_only(_base_name(arg)) and not combined:
                    hits.append(Hit("fit-outside-fold", "fit-on-heldout", f.rel, c.lineno,
                                    f"{recv} is fitted on held-out data ({arg[:60]}); its "
                                    "statistics then shape what is scored",
                                    src(c, f.text)[:160], "high"))
            # One transformer fitted twice on different data, where that matters: the two
            # sides of a split (train and test end up in different bases), or two outputs that
            # are later compared or combined (two text columns vectorised with different weights).
            # Reusing an encoder on unrelated columns is fine.
            by_recv = {}
            for c, recv, arg, cls in fits:
                if not isinstance(c.func.value, ast.Call):
                    by_recv.setdefault(recv, []).append((c, arg))
            for recv, lst in by_recv.items():
                for (c1, a1), (c2, a2) in zip(lst, lst[1:]):
                    if a1 == a2:
                        continue
                    outs = set()
                    for st in stmts:
                        if isinstance(st, ast.Assign) and st.value in (c1, c2) and \
                                len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                            outs.add(st.targets[0].id)
                    if _sides_only(a1, a2) or (len(outs) == 2 and
                                               _used_together(outs, stmts, c2.lineno)):
                        hits.append(Hit("fit-outside-fold", "refit-on-different-data", f.rel,
                                        c2.lineno, f"{recv} is fitted again on different data "
                                        f"({a1[:40]}, then {a2[:40]}): the two results are in "
                                        "different bases; fit once and transform both",
                                        src(c2, f.text)[:160], "high"))
                        break
            # Fitted before the data is split, in this scope or in a function called here.
            split_at = _split_line(stmts, f.text, splitters)
            if split_at is None:
                continue
            for c in calls_in(stmts):
                if c.lineno >= split_at:
                    break
                name = callee(c)
                recv = src(c.func.value, f.text) if isinstance(c.func, ast.Attribute) else ""
                fitted_here = name in ("fit", "fit_transform") and recv in tv and \
                    tv[recv] not in LABEL_ENCODERS
                via_function = isinstance(c.func, ast.Name) and \
                    resolve(f, c.func.id) in fitters
                if fitted_here or via_function:
                    hits.append(Hit("fit-outside-fold", "fit-before-split", f.rel, c.lineno,
                                    ("a transformer is fitted" if fitted_here else
                                     f"{name}() fits a transformer") + f" before the data "
                                    f"is split at line {split_at}", src(c, f.text)[:160],
                                    "medium"))
    return hits


# ---------------------------------------------------------------- resample before split

RESAMPLERS = re.compile(r"(?i)(resample|oversampl|undersampl|smote|fit_resample|"
                        r"random_under|random_over|upsampl|downsampl)")


def detect_resample_before_split(r):
    py = r.py
    hits = []
    for f in py:
        if f.tree is None:
            continue
        splitters = _splitter_names(f.tree, f.text)
        for scope, stmts in scopes(f.tree):
            split_at = _split_line(stmts, f.text, splitters)
            if split_at is None:
                continue
            for c in calls_in(stmts):
                if c.lineno >= split_at:
                    break
                name = callee(c)
                why = None
                time_rule = name == "resample" and isinstance(c.func, ast.Attribute) and (
                    (c.args and str_const(c.args[0]) is not None) or kw(c, "rule") is not None)
                if RESAMPLERS.search(name) and not time_rule:   # df.resample('D') aggregates
                    why = f"{name}() resamples"
                elif name == "concat" and c.args and isinstance(c.args[0], (ast.List, ast.Tuple)):
                    elts = [src(e, f.text) for e in c.args[0].elts]
                    dup = sorted({e for e in elts if elts.count(e) > 1})
                    if dup:
                        why = f"concat duplicates {dup[0]}"
                elif name == "concat" and c.args and isinstance(c.args[0], ast.BinOp) and \
                        isinstance(c.args[0].op, ast.Mult):
                    why = "concat of a repeated list"
                elif name == "sample" and isinstance(c.func, ast.Attribute):
                    # Only with replacement: a subset drawn without it cannot put one row on
                    # both sides of a split.
                    rep = kw(c, "replace")
                    frac = kw(c, "frac")
                    if (isinstance(rep, ast.Constant) and rep.value is True) or (
                            isinstance(frac, ast.Constant) and isinstance(frac.value, (int, float))
                            and frac.value > 1):
                        why = ".sample with replacement"
                if why:
                    hits.append(Hit("resample-before-split", "resample-before-split", f.rel,
                                    c.lineno, f"{why} before the data is split at line "
                                    f"{split_at}: copies can land on both sides, or every fold "
                                    "shares one draw", src(c, f.text)[:160], "medium"))
    return hits


# ---------------------------------------------------------------- ignored settings

INTERFACE_ARGS = {"self", "cls", "args", "kwargs", "_"}


def _reads(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _is_stub(fn):
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    return not body or all(isinstance(s, (ast.Pass, ast.Raise)) or (
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in body)


def _overwritten_before_read(fn, p):
    """The statement of `fn`'s body that assigns a string literal to parameter `p` before any
    read of it, or None. Only a statement at the top of the body counts: under an `if` it is
    an override for one case (`if debug: mode = "slow"`), not the caller's value ignored."""
    for st in fn.body:
        if isinstance(st, ast.Assign) and str_const(st.value) is not None and any(
                isinstance(t, ast.Name) and t.id == p for t in st.targets):
            return st
        if isinstance(st, DEFS):
            continue
        if any(isinstance(n, ast.Name) and n.id == p for n in ast.walk(st)):
            return None         # read (or rebound some other way) first
    return None


def detect_ignored_setting(r):
    py, others = r.py, r.others
    hits = []
    local_classes = {n.name for f in py if f.tree is not None for n in ast.walk(f.tree)
                     if isinstance(n, ast.ClassDef)}
    for f in py:
        if f.tree is None:
            continue
        # Methods whose signature an interface dictates: a framework calls them with
        # arguments they are free to ignore.
        interface = set()
        for cls in [n for n in ast.walk(f.tree) if isinstance(n, ast.ClassDef)]:
            external = any(dotted(b).split(".")[-1] not in local_classes | {"object"}
                           for b in cls.bases)
            for m in cls.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        external or (m.name.startswith("__") and m.name.endswith("__"))):
                    interface.add(m)
        chained = {id(n.orelse[0]) for n in ast.walk(f.tree) if isinstance(n, ast.If)
                   and len(n.orelse) == 1 and isinstance(n.orelse[0], ast.If)}
        # Attributes only: a local name is often normalised before it is compared
        # (`mode = mode.lower()`), so its literal assignments say little about its values.
        literal_assigns = {}       # attribute -> {string literal assigned: other assignments?}
        other_assigns = set()
        for node in ast.walk(f.tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute):
                        if str_const(node.value) is not None:
                            literal_assigns.setdefault(t.attr, set()).add(node.value.value)
                        else:
                            other_assigns.add(t.attr)
        for node in ast.walk(f.tree):
            # Parameters never read, or overwritten by a literal before any read.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_stub(node):
                decorated = {dotted(d) if not isinstance(d, ast.Call) else dotted(d.func)
                             for d in node.decorator_list}
                if decorated & {"abstractmethod", "abc.abstractmethod", "overload",
                                "typing.overload", "property"}:
                    continue
                params = [a.arg for a in node.args.posonlyargs + node.args.args
                          + node.args.kwonlyargs]
                loose = node in interface or node.args.kwarg is not None or (
                    node.name.startswith("__") and node.name.endswith("__"))
                reads = set().union(*(_reads(s) for s in node.body)) if node.body else set()
                for p in params:
                    if p in INTERFACE_ARGS or p.startswith("_"):
                        continue
                    if p in reads:
                        s = _overwritten_before_read(node, p)
                        if s is not None:
                            hits.append(Hit("ignored-setting", "parameter-overwritten", f.rel,
                                            s.lineno, f"parameter {p!r} of {node.name}() is "
                                            "overwritten by a literal before it is first read; "
                                            "the caller's value is ignored",
                                            src(s, f.text)[:160], "high"))
                        continue
                    overwritten = [s for s in ast.walk(node) if isinstance(s, ast.Assign)
                                   and str_const(s.value) is not None and any(
                                       (isinstance(t, ast.Attribute) and t.attr == p) or
                                       (isinstance(t, ast.Name) and t.id == p)
                                       for t in s.targets)]
                    if overwritten:
                        s = overwritten[0]
                        hits.append(Hit("ignored-setting", "parameter-overwritten", f.rel,
                                        s.lineno, f"parameter {p!r} of {node.name}() is never "
                                        f"read; a literal is assigned in its place",
                                        src(s, f.text)[:160], "high"))
                    elif not loose:
                        hits.append(Hit("ignored-setting", "parameter-unused", f.rel,
                                        node.lineno, f"parameter {p!r} of {node.name}() is "
                                        "never read", f"def {node.name}(...)", "medium"))
            # A condition, in a function, on a local that only ever holds one constant. Every
            # other way of binding the name — unpacking, `:=`, `with ... as`, `except ... as`,
            # a loop target, `global` — counts as a different value.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mine = list(own_nodes(node))
                consts = {a.arg: {"<other>"} for a in node.args.posonlyargs + node.args.args
                          + node.args.kwonlyargs}
                simple = {}
                for n in mine:
                    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(
                            n.targets[0], ast.Name) and isinstance(n.value, ast.Constant) and \
                            isinstance(n.value.value, bool):
                        simple[id(n.targets[0])] = n.value.value
                for n in mine:
                    if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                        consts.setdefault(n.id, set()).add(simple.get(id(n), "<other>"))
                    elif isinstance(n, (ast.Global, ast.Nonlocal)):
                        for name in n.names:
                            consts.setdefault(name, set()).add("<other>")
                    elif isinstance(n, ast.ExceptHandler) and n.name:
                        consts.setdefault(n.name, set()).add("<other>")
                for n in mine:
                    if isinstance(n, (ast.If, ast.While, ast.IfExp)):
                        for x in [x for x in ast.walk(n.test) if isinstance(x, ast.Name)]:
                            vals = consts.get(x.id)
                            if vals and len(vals) == 1 and next(iter(vals)) != "<other>":
                                hits.append(Hit("ignored-setting", "constant-condition", f.rel,
                                                n.lineno, f"{x.id!r} is only ever assigned "
                                                f"{next(iter(vals))}, so this condition never "
                                                "changes", src(n.test, f.text)[:160], "high"))
            # A string compared to a value that is only ever assigned in another case.
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(
                    node.ops[0], (ast.Eq, ast.NotEq)):
                left, right = node.left, node.comparators[0]
                lit = str_const(right) if str_const(right) is not None else str_const(left)
                other = left if str_const(right) is not None else right
                key = other.attr if isinstance(other, ast.Attribute) else None
                if lit is not None and key and key in literal_assigns and \
                        key not in other_assigns:
                    vals = literal_assigns[key]
                    if lit not in vals and lit.lower() in {v.lower() for v in vals}:
                        hits.append(Hit("ignored-setting", "case-mismatch-comparison", f.rel,
                                        node.lineno, f"{key} is compared with {lit!r}, but is "
                                        f"only ever assigned {sorted(vals)}; this branch "
                                        "cannot be taken", src(node, f.text)[:160], "high"))
            # An argv dispatch that falls through silently.
            if isinstance(node, ast.If) and id(node) not in chained and \
                    "sys.argv" in src(node.test, f.text):
                tail = node
                while tail.orelse and len(tail.orelse) == 1 and isinstance(tail.orelse[0], ast.If):
                    tail = tail.orelse[0]
                if not tail.orelse and tail is not node:
                    hits.append(Hit("ignored-setting", "argv-dispatch-without-else", f.rel,
                                    node.lineno, "an if/elif chain on sys.argv with no else: "
                                    "an unrecognised argument does nothing and exits 0",
                                    src(node.test, f.text)[:120], "high"))
    # Config keys that no code mentions.
    code_text = "\n".join(f.text for f in py) + "\n" + "\n".join(
        t for r, t in others if r.lower().endswith((".sh", ".r")))
    for rel, text in others:
        if not rel.lower().endswith((".yml", ".yaml")) or Path(rel).parts[0] == ".github":
            continue
        for i, line in enumerate(split_lines(text), 1):
            m = re.match(r"^\s*([A-Za-z_][\w]*)\s*:", line)
            if m and not re.search(r"\b" + re.escape(m.group(1)) + r"\b", code_text):
                hits.append(Hit("ignored-setting", "config-key-unread", rel, i,
                                f"config key {m.group(1)!r} is read by no code in the "
                                "repository", line.strip()[:160], "medium"))
    return hits


# ---------------------------------------------------------------- missing producer

READERS = {"read_csv", "read_table", "read_parquet", "read_pickle", "read_feather", "read_hdf",
           "read_json", "read_excel", "load", "loadtxt", "genfromtxt", "fromfile", "load_npz",
           "imread", "read_pickle", "File"}
WRITERS = {"to_csv", "to_parquet", "to_pickle", "to_feather", "to_hdf", "to_json", "to_excel",
           "save", "savez", "savez_compressed", "savetxt", "dump", "imwrite", "savefig",
           "save_npz", "tofile", "write_csv", "write_parquet", "write_ipc",
           "write_json", "write_ndjson", "write_text", "write_bytes", "save_model", "to_netcdf",
           "to_zarr", "write_table", "savemat", "sink_parquet", "sink_csv"}


def _is_test_file(rel):
    p = Path(rel)
    return bool({"tests", "test"} & set(p.parts[:-1])) or p.name.startswith("test_") \
        or p.stem.endswith("_test")
COMPRESSED = {".gz", ".bz2", ".xz", ".zip", ".zst"}
FILE_TOKEN = re.compile(r"[\w.\-]+\.(?:csv|tsv|txt|json|jsonl|npy|npz|pkl|pickle|pth|pt|bin|"
                        r"h5|hdf5|parquet|feather|zarr|zip|joblib|model|ckpt|yaml|yml|jar)"
                        r"(?:\.(?:gz|bz2|xz|zip|zst))?\b")
R_READ = re.compile(r"\b(?:read\.csv|read_csv|fread|readRDS|read\.table|load)\s*\(\s*['\"]([^'\"]+)")
R_WRITE = re.compile(r"\b(?:write\.csv|fwrite|saveRDS|write\.table|save)\s*\([^'\"]*['\"]([^'\"]+)")
SH_WRITE = re.compile(r"(?:>\s*|(?:-o|--out\w*)[ =]|\b(?:cp|mv)\s+\S+\s+|"
                      r"\b\w*(?:out|pred|save|dest|dst)\w*=)([\w./\-]+)")
# The files a competition or dataset download provides; their absence from the repository is
# expected, documented or not.
CONVENTIONAL_INPUTS = re.compile(r"^(?:train|test|valid|validation|sample_submission|"
                                 r"sample_submission_\w+|submission)\.\w+$", re.I)


def _names_in(node, text):
    """File names in a path expression: literals, f-string parts, joined pieces."""
    out = set()
    for n in ast.walk(node):
        s = str_const(n)
        if s:
            out |= set(FILE_TOKEN.findall(s))
    return out


def _type(name):
    """`train.csv.gz` -> `.csv.gz`, `x.v2.csv` -> `.csv`."""
    sx = [s.lower() for s in Path(name).suffixes]
    return "".join(sx[-2:] if len(sx) > 1 and sx[-1] in COMPRESSED else sx[-1:])


def _base(name):
    """`train.csv.gz` -> `train`."""
    return name[:len(name) - len(_type(name))] if _type(name) else name


def detect_missing_producer(r):
    py, others, repo = r.py, r.others, r.root
    reads, writes = {}, set()
    for f in py:
        if f.tree is None:
            continue
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Call):
                continue
            name = callee(node)
            args = node.args[:1] + [k.value for k in node.keywords
                                    if k.arg in ("path", "fname", "file", "filepath_or_buffer",
                                                 "path_or_buf", "f")]
            if name == "open" and isinstance(node.func, ast.Attribute) and \
                    not dotted(node.func.value):
                # Path('x.csv').open('w'): the path is the receiver and the mode comes first.
                # A module's open (gzip.open, io.open) takes the path first, as below.
                mode = str_const(node.args[0]) if node.args else str_const(kw(node, "mode"))
                if mode is not None and not re.fullmatch(r"[rwxabt+U]+", mode):
                    continue        # ZipFile(...).open('member.csv'): not a mode
                for nm in _names_in(node.func.value, f.text):
                    if mode and any(ch in mode for ch in "wax"):
                        writes.add(nm)
                    else:
                        reads.setdefault(nm, (f.rel, node.lineno))
                continue
            if name in ("open", "File") and node.args:   # File: h5py.File(path, mode)
                mode = str_const(node.args[1]) if len(node.args) > 1 else str_const(kw(node, "mode"))
                target = writes if mode and any(ch in mode for ch in "wax") else None
                for nm in _names_in(node.args[0], f.text):
                    if target is not None:
                        writes.add(nm)
                    else:
                        reads.setdefault(nm, (f.rel, node.lineno))
                continue
            if name in WRITERS:
                for a in node.args + [k.value for k in node.keywords]:
                    writes |= _names_in(a, f.text)
                if isinstance(node.func, ast.Attribute):    # Path('m.npy').write_bytes(...)
                    writes |= _names_in(node.func.value, f.text)
            elif name in READERS and not _is_test_file(f.rel):
                # A test reads what the code under test writes, often through a computed path.
                for a in args:
                    for nm in _names_in(a, f.text):
                        reads.setdefault(nm, (f.rel, node.lineno))
    for rel, text in others:
        low = rel.lower()
        for i, line in enumerate(split_lines(text), 1):
            if line.lstrip().startswith("#"):
                continue
            if low.endswith(".r"):
                for m in R_READ.finditer(line):
                    for nm in FILE_TOKEN.findall(m.group(1)):
                        reads.setdefault(nm, (rel, i))
                for m in R_WRITE.finditer(line):
                    writes |= set(FILE_TOKEN.findall(m.group(1)))
            elif low.endswith(".sh"):
                for m in SH_WRITE.finditer(line):
                    writes |= set(FILE_TOKEN.findall(m.group(1)))
                written_here = {nm for m in SH_WRITE.finditer(line)
                                for nm in FILE_TOKEN.findall(m.group(1))}
                for nm in FILE_TOKEN.findall(line):
                    if nm not in written_here and not re.search(r"(?:wget|curl|kaggle)\b", line):
                        reads.setdefault(nm, (rel, i))
                if re.search(r"\b(?:wget|curl|kaggle\s+\w+\s+download|unzip|tar)\b", line):
                    writes |= set(FILE_TOKEN.findall(line))
    files = repo_files(repo)
    present = {Path(p).name for p in files}
    docs = []
    for p in files:
        if p.lower().endswith((".md", ".txt", ".rst")):
            try:
                if (repo / p).stat().st_size <= 2_000_000:   # not a 128k-line word list
                    docs.append((repo / p).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    docs = "\n".join(docs)
    hits = []
    for nm, (rel, line) in sorted(reads.items(), key=lambda kv: kv[1]):
        if nm in writes or nm in present or Path(nm).suffix in (".yaml", ".yml", ".jar") \
                or CONVENTIONAL_INPUTS.match(nm):
            continue
        documented = re.search(r"(?<![\w.])" + re.escape(nm) + r"(?![\w])", docs)
        # A near-miss writer is a file of the same type whose name contains this one's, or
        # the reverse: features.csv read, features_v2.csv written. test.pkl is not a
        # near-miss for test_labels.csv.
        near = sorted(w for w in writes if w != nm and _type(w) == _type(nm) and (
            _base(w) in _base(nm) or _base(nm) in _base(w)))
        if documented and not near:
            continue        # an input the documentation names: assumed to be downloaded
        msg = f"{nm} is read here, but nothing in the repository writes it and it is not committed"
        if near:
            msg += f"; the code writes {', '.join(near[:3])} instead"
        hits.append(Hit("missing-producer", "read-without-writer", rel, line, msg, nm,
                        "high" if near else "medium"))
    return hits


# ---------------------------------------------------------------- regex corrupts words

DEFAULT_WORDS = ["thousand", "usage", "woman", "big", "status", "human",
                 "jaguar", "pig", "dig", "gig", "fig", "rig", "wig", "sign", "design", "high",
                 "light", "night", "right", "bright", "eight", "weight", "sigma", "usual",
                 "music", "focus", "cause", "because", "pause", "house", "mouse",
                 "manager", "image", "page", "stage", "language", "message", "orange",
                 "student", "company", "people", "question", "answer", "money", "country",
                 "information", "business", "government", "something", "children", "program"]


def _regex_calls(f):
    """(call, pattern, replacement, flags) for literal regex substitutions in a file."""
    imports = imported_from(f.tree)
    for node in ast.walk(f.tree):
        if not isinstance(node, ast.Call):
            continue
        name, full = callee(node), dotted(node.func)
        is_re_sub = full in ("re.sub", "regex.sub") or (
            full == "sub" and imports.get("sub") in ("re", "regex"))
        if name == "sub" and is_re_sub and len(node.args) >= 3:
            pat, rep = str_const(node.args[0]), str_const(node.args[1])
            flags = 0
            # re.sub(pattern, repl, string, count, flags)
            fl = kw(node, "flags") if kw(node, "flags") is not None else (
                node.args[4] if len(node.args) > 4 else None)
            if fl is not None and "IGNORECASE" in src(fl, f.text) or (
                    fl is not None and re.search(r"\bre\.I\b", src(fl, f.text))):
                flags = re.IGNORECASE
            if pat is not None and rep is not None:
                yield node, pat, rep, flags
        elif name == "replace" and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "str" \
                and len(node.args) >= 2:
            pat, rep = str_const(node.args[0]), str_const(node.args[1])
            regex = kw(node, "regex")
            if pat is not None and rep is not None and not (
                    isinstance(regex, ast.Constant) and regex.value is False):
                yield node, pat, rep, 0


def detect_regex_corrupts_words(r):
    py, words = r.py, r.words
    words = words or DEFAULT_WORDS
    hits = []
    for f in py:
        if f.tree is None:
            continue
        for node, pat, rep, flags in _regex_calls(f):
            try:
                rx = re.compile(pat, flags)
            except re.error:
                continue
            if rx.match("") or re.search(r"\\[bB]|\(\?<[!=]", pat):
                continue        # matches nothing, or the author anchored it at word edges
            damaged, lengths = [], set()
            for w in words:
                for form in dict.fromkeys((w, w.capitalize())):   # ordered: same output every run
                    text = f" {form} "
                    m = rx.search(text)
                    if not m or not m.group(0).strip():
                        continue
                    s, e = m.span()
                    # Damage = the match touches letters of the word on either side, so it
                    # rewrites part of a word rather than a whole one.
                    partial = (text[s - 1].isalpha() if s > 0 else False) or (
                        text[e].isalpha() if e < len(text) else False)
                    try:
                        out = rx.sub(rep, text)
                    except (re.error, IndexError, ValueError):
                        out = text      # an invalid replacement: nothing this rule can damage
                    if partial and out != text:
                        damaged.append(f"{form} -> {out.strip()!r}")
                        lengths.add(len(m.group(0)))
                        break
                if len(damaged) >= 3:
                    break
            if damaged and lengths != {1}:      # a one-character rule rewrites on purpose
                hits.append(Hit("regex-corrupts-words", "rewrites-inside-words", f.rel,
                                node.lineno, f"the pattern {pat!r} also matches inside "
                                f"ordinary words: {'; '.join(damaged)}",
                                src(node, f.text)[:160], "high"))
            if callee(node) == "replace" and kw(node, "regex") is None and len(pat) > 1 and \
                    re.search(r"[.()\[\]+*?|^$]", pat):   # pandas < 2 took one character literally
                hits.append(Hit("regex-corrupts-words", "str-replace-regex-default", f.rel,
                                node.lineno, f"{pat!r} has regex metacharacters and no regex=; "
                                "pandas < 2 treats it as a regular expression",
                                src(node, f.text)[:160], "medium"))
    return hits


# ---------------------------------------------------------------- cannot run

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__builtins__",
                                 "__path__", "__loader__", "__package__", "__annotations__",
                                 "display", "get_ipython", "__class__", "__module__",
                                 "__qualname__", "WindowsError"}   # a builtin on Windows


def _bound_names(tree):
    bound = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchAs) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchStar) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bound.add(n.rest)
        elif type(n).__name__ in ("TypeVar", "ParamSpec", "TypeVarTuple"):   # PEP 695
            bound.add(n.name)
    return bound


THIS_PYTHON = "{}.{}".format(*sys.version_info[:2])
_NEWER = []


def _newer_pythons():
    """(version, path) of the interpreters on PATH newer than this one, newest first."""
    if not _NEWER:
        _NEWER.append([(f"3.{m}", shutil.which(f"python3.{m}"))
                       for m in range(20, sys.version_info[1], -1) if shutil.which(f"python3.{m}")])
    return _NEWER[0]


def _compiles_under(text):
    """The newest Python on PATH, newer than this one, that compiles `text`, or None. Syntax
    is version-dependent: `type X = int` and f"{d["k"]}" are errors on 3.11 and valid on
    3.12, and the repository may target the newer one."""
    for version, exe in _newer_pythons():
        try:
            r = subprocess.run([exe, "-c", "import sys; compile(sys.stdin.read(), 'f', 'exec')"],
                               input=text, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return version
    return None


def _syntax_error(f, line, msg, magic=False):
    newer = None if magic else _compiles_under(f.text)
    where = (f"a syntax error under Python {THIS_PYTHON}; Python {newer} compiles it, so it "
             "fails only on older interpreters" if newer else
             f"a syntax error under Python {THIS_PYTHON}")
    return Hit("cannot-run", "syntax-error", f.rel, line,
               ("an IPython magic in a .py file: " if magic else "") + f"{msg} ({where})",
               (f.lines[line - 1].strip() if 0 < line <= len(f.lines) else "")[:160],
               "medium" if newer else "high")


def detect_cannot_run(r):
    py = r.py
    hits = []
    for f in py:
        if f.tree is None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    compile(f.text, f.rel, "exec")
                continue
            except SyntaxError as e:
                # Blame an IPython magic only when the error is on its line: a `%` at the start
                # of a line inside a string is not one.
                at = e.lineno or 1
                magic = 0 < at <= len(f.lines) and bool(re.match(r"^\s*[%!]\w", f.lines[at - 1]))
                hits.append(_syntax_error(f, at, e.msg, magic))
            except ValueError as e:     # a null byte, on Python < 3.12
                hits.append(Hit("cannot-run", "syntax-error", f.rel, 1, str(e), "", "high"))
            except (RecursionError, MemoryError):
                # Held back: a generated file nested too deeply for this parser says nothing
                # about whether Python can run it.
                hits.append(Hit("cannot-run", "unparsed-file", f.rel, 1, "nested too deeply "
                                "for the static checks to parse; not checked", "", "medium"))
            continue
        try:
            # compile() finds what ast.parse lets through: `return` outside a function, a
            # misplaced `from __future__`, `nonlocal` at module level.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(f.text, f.rel, "exec")
        except SyntaxError as e:
            hits.append(_syntax_error(f, e.lineno or 1, e.msg))
        except (ValueError, RecursionError, MemoryError):
            pass
        star = any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
                   for n in ast.walk(f.tree))
        dynamic = re.search(r"\b(exec|globals|locals|vars)\s*\(|__builtins__|setattr\(\s*"
                            r"(sys\.modules|builtins)", f.text)
        if not (star or dynamic):
            bound = _bound_names(f.tree)
            seen = set()
            loads = sorted((n for n in ast.walk(f.tree) if isinstance(n, ast.Name) and
                            isinstance(n.ctx, ast.Load)), key=lambda n: (n.lineno, n.col_offset))
            for n in loads:      # by position: each name is reported where it is first used
                if n.id not in bound and n.id not in BUILTINS and n.id not in seen:
                    seen.add(n.id)
                    hits.append(Hit("cannot-run", "undefined-name", f.rel, n.lineno,
                                    f"{n.id!r} is used but never defined or imported in this "
                                    "file", f.lines[n.lineno - 1].strip()[:160], "high"))
        for n in ast.walk(f.tree):
            if isinstance(n, ast.Call) and dotted(n.func) == "yaml.load" and len(n.args) < 2 \
                    and "Loader" not in kwnames(n):
                hits.append(Hit("cannot-run", "yaml-load-without-loader", f.rel, n.lineno,
                                "yaml.load without Loader: a TypeError on PyYAML >= 6, and "
                                "executes tags on older versions", src(n, f.text)[:160], "high"))
    return hits


# ---------------------------------------------------------------- duplicate code

def detect_duplicate_code(r, min_block=12):
    py, others = r.py, r.others
    hits = []
    bodies = {}
    for f in py:
        if f.tree is None:
            continue
        for scope in [f.tree] + [n for n in ast.walk(f.tree) if isinstance(n, ast.ClassDef)]:
            seen = {}
            for st in scope.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    # The implementation after a run of @overload stubs is undecorated, so
                    # the earlier definition's decorators count too.
                    if st.name in seen and not any(dotted(d).endswith(("setter", "overload",
                                                                         "register"))
                                                   or (isinstance(d, ast.Attribute) and
                                                       d.attr in ("setter", "deleter"))
                                                   for d in st.decorator_list) and not any(
                            dotted(d).endswith("overload")
                            for d in seen[st.name].decorator_list):
                        first = seen[st.name].lineno
                        used_between = any(
                            isinstance(n, ast.Name) and n.id == st.name and
                            isinstance(n.ctx, ast.Load) and first < n.lineno < st.lineno
                            for n in ast.walk(scope))
                        hits.append(Hit("duplicate-code", "redefined", f.rel, first,
                                        f"{st.name} is defined again at line {st.lineno}; "
                                        + ("code between the two uses this definition, and "
                                           "everything after uses the other" if used_between
                                           else "this definition is dead"),
                                        f.lines[first - 1].strip()[:160], "high"))
                    seen[st.name] = st
        for n in ast.walk(f.tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(n.body) >= 3:
                key = hashlib.sha1(ast.dump(ast.Module(body=n.body, type_ignores=[])).encode()
                                   ).hexdigest()
                bodies.setdefault(key, []).append((f.rel, n.lineno, n.name))
    for key, sites in bodies.items():
        if len(sites) > 1:
            first = sites[0]
            for rel, line, name in sites[1:]:
                hits.append(Hit("duplicate-code", "identical-function-bodies", rel, line,
                                f"{name} has the same body as {first[2]} at {first[0]}:"
                                f"{first[1]}", "", "medium"))
    # Long duplicated blocks of normalised lines, across all code files.
    norm_files = []
    for rel, text in [(f.rel, f.text) for f in py] + [(r, t) for r, t in others
                                                       if r.lower().endswith((".r", ".sh"))]:
        lines = []
        for i, l in enumerate(split_lines(text), 1):
            s = re.sub(r"\s+", " ", l.strip())
            if s and not s.startswith(("#", "import ", "from ", "library(", "require(")) \
                    and len(s) > 3:
                lines.append((i, s))
        norm_files.append((rel, lines))
    index = {}
    for fi, (rel, lines) in enumerate(norm_files):
        for j in range(len(lines) - min_block + 1):
            h = hashlib.sha1("\n".join(s for _, s in lines[j:j + min_block]).encode()).hexdigest()
            index.setdefault(h, []).append((fi, j))
    reported = set()
    for h, occ in index.items():
        if len(occ) < 2:
            continue
        (fa, ja), (fb, jb) = occ[0], occ[1]
        if fa == fb and abs(ja - jb) < min_block:
            continue
        key = (fa, ja // min_block, fb, jb // min_block)
        if key in reported or (fa, fb) in {(k[0], k[2]) for k in reported if
                                            abs(k[1] - ja // min_block) <= 1}:
            continue
        reported.add(key)
        ra, la = norm_files[fa][0], norm_files[fa][1][ja][0]
        rb, lb = norm_files[fb][0], norm_files[fb][1][jb][0]
        hits.append(Hit("duplicate-code", "duplicated-block", rb, lb,
                        f"{min_block}+ lines here repeat {ra}:{la} — a change to one copy "
                        "misses the other", "", "medium"))
    return hits


# ---------------------------------------------------------------- hard-coded paths

ABS_PATH = re.compile(r"^(?:~/|/home/|/Users/|/mnt/|/media/|/data/|/root/|/opt/|/srv/|"
                      r"[A-Za-z]:[\\/])")


def detect_hard_coded_path(r):
    py, others = r.py, r.others
    hits = []
    for f in py:
        if f.tree is None:
            continue
        chdir_args = {id(c.args[0]) for c in ast.walk(f.tree) if isinstance(c, ast.Call)
                      and dotted(c.func) == "os.chdir" and c.args}
        for n in ast.walk(f.tree):
            s = str_const(n)
            m = ABS_PATH.match(s) if s else None
            # Not a per-user dot-directory (~/.cache, ~/.config/tool.json): it is where the
            # tool keeps its files on every machine. Not a prefix with nothing after it, the
            # constant part of f"~/{name}", which says nothing about the machine.
            if m and "\n" not in s and len(s) < 300 and id(n) not in chdir_args \
                    and not s.startswith("~/.") and s[m.end():].strip("/\\"):
                hits.append(Hit("hard-coded-path", "absolute-path", f.rel, n.lineno,
                                f"a machine-specific path: {s!r}",
                                f.lines[n.lineno - 1].strip()[:160], "high"))
            if isinstance(n, ast.Call) and dotted(n.func) == "os.chdir" and n.args and \
                    str_const(n.args[0]):
                hits.append(Hit("hard-coded-path", "chdir-literal", f.rel, n.lineno,
                                "os.chdir to a literal path", src(n, f.text)[:160], "high"))
        # A module global set only under __main__, read by a multiprocessing worker: correct
        # only under the fork start method.
        if re.search(r"\b(Pool|Process|ProcessPoolExecutor)\s*\(", f.text):
            top = {t.id for st in f.tree.body if isinstance(st, ast.Assign)
                   for t in st.targets if isinstance(t, ast.Name)}
            main = next((st for st in f.tree.body if isinstance(st, ast.If)
                         and "__main__" in src(st.test, f.text)), None)
            if main:
                set_in_main = {t.id for n in ast.walk(main) if isinstance(n, ast.Assign)
                               for t in n.targets if isinstance(t, ast.Name)} & top
                workers = set()
                for c in ast.walk(f.tree):
                    if isinstance(c, ast.Call) and callee(c) in (
                            "map", "imap", "imap_unordered", "starmap", "apply_async", "apply",
                            "submit", "map_async", "starmap_async", "Process"):
                        for a in list(c.args) + [k.value for k in c.keywords]:
                            for n in ast.walk(a):
                                if isinstance(n, ast.Name):
                                    workers.add(n.id)
                for fn in [n for n in f.tree.body if isinstance(n, ast.FunctionDef)
                           and n.name in workers]:
                    used = _reads(fn) & set_in_main
                    for g in sorted(used):
                        hits.append(Hit("hard-coded-path", "fork-only-global", f.rel, fn.lineno,
                                        f"{fn.name}() reads the global {g!r}, which is only set "
                                        "under __main__: workers started with spawn or "
                                        "forkserver (the default on macOS and Windows, and on "
                                        "Linux from Python 3.14) see its initial value",
                                        f"def {fn.name}", "medium"))
    for rel, text in others:
        for i, line in enumerate(split_lines(text), 1):
            if line.lstrip().startswith("#"):
                continue
            m = re.search(r"\b(setwd|cd)\s*\(?\s*['\"]?((?:~/|/home/|/Users/|[A-Za-z]:[\\/])"
                          r"[^'\")\s]*)", line)
            if m:
                hits.append(Hit("hard-coded-path", "absolute-path", rel, i,
                                f"a machine-specific path: {m.group(2)!r}", line.strip()[:160],
                                "high"))
    return hits


# ---------------------------------------------------------------- driver

DETECTORS = {
    "unseeded": detect_unseeded,
    "fit-outside-fold": detect_fit_outside_fold,
    "resample-before-split": detect_resample_before_split,
    "ignored-setting": detect_ignored_setting,
    "missing-producer": detect_missing_producer,
    "regex-corrupts-words": detect_regex_corrupts_words,
    "cannot-run": detect_cannot_run,
    "duplicate-code": detect_duplicate_code,
    "hard-coded-path": detect_hard_coded_path,
}


def load(repo: Path, extra=()):
    """The repository's code files: tracked and unignored ones, plus `extra` (the files a
    review's manifest names, which may include ignored ones)."""
    names = set(repo_files(repo))
    names |= {p for p in extra if not set(Path(p).parts[:-1]) & SKIP_DIRS
              and (repo / p).is_file()}
    py, others = [], []
    for rel in sorted(names):
        ext = Path(rel).suffix.lower()
        if ext not in CODE_EXT:
            continue
        try:
            # utf-8-sig: a byte-order mark is not part of the code, and ast.parse rejects it.
            text = (repo / rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if ext == ".py":
            try:
                with warnings.catch_warnings():
                    # The analysed code's own warnings (invalid escapes in its strings) are
                    # not this tool's output.
                    warnings.simplefilter("ignore")
                    tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                tree = None
            py.append(PyFile(rel, text, tree, split_lines(text)))
        else:
            others.append((rel, text))
    return py, others


def run(repo: Path, only=None, words=None, extra=()):
    py, others = load(repo, extra)
    r = Repo(repo, py, others, words)
    hits, seen = [], set()
    for name, fn in DETECTORS.items():
        if not only or name in only:
            for h in fn(r):
                key = (h.detector, h.kind, h.file, h.line, h.message)
                if key not in seen:
                    seen.add(key)
                    hits.append(h)
    return sorted(hits, key=lambda h: (h.detector, h.file, h.line, h.kind, h.message))


# ---------------------------------------------------------------- findings for a review

STATIC_REVIEWER = "static"

# The kinds sent to the reconciler as findings, each with how bad the defect is if true, the
# fix, whether that fix is mechanical, and the PRACTICES rule it breaks (None where no rule
# fits). The rest are counted in the findings file and not sent: unused parameters were mostly
# noise on unseen code; unread config keys, duplicated blocks and file-level global RNG use
# were noisy on the audited corpus; identical bodies and the pandas regex default rarely
# matter on their own.
SENT = {
    "seedable-call-without-seed": ("minor", "pass random_state (or seed) explicitly", "mechanical", "T-7"),
    "unseeded-dataloader": ("major", "seed torch at the entry point and pass a seeded generator to the DataLoader", "judgment", "T-7"),
    "unseeded-torch-training": ("major", "seed torch, numpy and random at the entry point, and record the seed with the run", "judgment", "T-7"),
    "commented-out-seed": ("major", "restore the seeding call, or record why the run is deliberately unseeded", "judgment", "T-7"),
    "r-script-without-set-seed": ("minor", "call set.seed before the first random call", "mechanical", "T-7"),
    "fit-on-heldout": ("major", "fit on the training rows only and transform the held-out rows", "judgment", None),
    "fit-on-train-and-test": ("major", "fit on the training rows only, inside the fold", "judgment", None),
    "refit-on-different-data": ("major", "fit once and transform every split with that fit", "judgment", None),
    "fit-before-split": ("major", "move the fit inside the fold loop or after the split", "judgment", None),
    "heldout-statistic": ("minor", "compute the statistic on the training rows and apply it to the held-out rows", "judgment", None),
    "resample-before-split": ("major", "split first, then resample the training side only", "judgment", None),
    "parameter-overwritten": ("major", "use the parameter, or remove it so callers cannot believe it has an effect", "judgment", None),
    "constant-condition": ("minor", "read the setting from configuration, or remove the dead branch", "judgment", None),
    "case-mismatch-comparison": ("minor", "compare against the value actually assigned", "mechanical", None),
    "argv-dispatch-without-else": ("minor", "add an else that prints usage and exits non-zero", "mechanical", None),
    "read-without-writer": ("minor", "commit the script that produces the file, or document where it comes from", "judgment", None),
    "rewrites-inside-words": ("major", "anchor the pattern with word boundaries (\\b) or match whole tokens", "judgment", None),
    "syntax-error": ("major", "fix the syntax so the file can be imported", "mechanical", None),
    "undefined-name": ("major", "define or import the name in this file", "judgment", None),
    "yaml-load-without-loader": ("major", "use yaml.safe_load", "mechanical", "C-6"),
    "redefined": ("minor", "delete the dead definition, or rename one of them", "judgment", "C-4"),
    "absolute-path": ("minor", "read the path from configuration or make it relative to the repository", "judgment", "D-5"),
    "chdir-literal": ("minor", "remove the chdir and use paths relative to the repository", "judgment", "D-5"),
    "fork-only-global": ("minor", "pass the value to the worker as an argument", "judgment", None),
}


SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "trivial": 3}


def _group(hits):
    """Hits gathered into one list per distinct defect.

    1. Hits of one kind in one file that say the same thing (the same unseeded call at two
       lines) are one defect with several sites.
    2. All undefined names in one file are one defect: the file relies on names that some
       other file or session defines, and naming each separately split one cause into many.
    3. Groups from the same detector covering exactly the same lines are one defect seen two
       ways: a vectorizer fitted on test data is also a vectorizer fitted twice.
    """
    groups = {}
    for h in hits:
        key = (h.kind, h.file) if h.kind == "undefined-name" else (
            h.kind, h.file, re.sub(r"\b\d+\b", "#", h.message))
        groups.setdefault(key, []).append(h)
    merged = {}
    for hs in groups.values():
        lines = frozenset(h.line for h in hs)
        merged.setdefault((hs[0].detector, hs[0].file, lines), []).extend(hs)
    return list(merged.values())


def _finding(n, hs, repo):
    hs = sorted(hs, key=lambda h: (SEVERITY_ORDER[SENT[h.kind][0]], h.line))
    first = hs[0]                       # the most severe kind leads
    kinds = list(dict.fromkeys(h.kind for h in hs))
    severity, fix, certainty, rule = SENT[first.kind]
    lines = sorted({h.line for h in hs})
    if kinds == ["undefined-name"]:
        names = list(dict.fromkeys(re.match(r"'([^']+)'", h.message).group(1)
                                   for h in sorted(hs, key=lambda h: h.line)))
        claim = (f"{len(names)} name(s) used in this file are never defined or imported in it: "
                 f"{', '.join(names)} — it can only run where something else defines them"
                 if len(names) > 1 else first.message)
    else:
        # One message per distinct statement: two paths on one line are two, the same
        # unseeded call at lines 4 and 9 is one.
        msgs = {}
        for h in hs:
            msgs.setdefault(re.sub(r"\b\d+\b", "#", h.message), h.message)
        claim = "; ".join(msgs.values())
        if len(lines) > 1:
            claim += f" (lines {', '.join(map(str, lines))})"
        if len(kinds) > 1:
            fix = "; ".join(dict.fromkeys(SENT[k][1] for k in kinds))
    quote = first.evidence
    if (not quote or kinds == ["undefined-name"]) and repo is not None:
        try:
            quote = split_lines((Path(repo) / first.file).read_text(
                encoding="utf-8-sig", errors="replace"))[first.line - 1].strip()
        except (OSError, IndexError):
            pass
    return {
        "id": f"static-{n:03d}", "rule_id": rule, "file": first.file, "line": lines[0],
        "lines": ",".join(map(str, lines)), "claim": claim, "class": "defect",
        "evidence": {"kind": "quote", "quote": quote or first.message},
        # A detector's "high" means the pattern alone is the defect; "medium", that it often
        # is. Neither is more than a claim the reconciler has yet to check.
        "severity": severity,
        "confidence": "likely" if any(h.confidence == "high" for h in hs) else "speculative",
        "fix": fix, "fix_certainty": certainty if len(kinds) == 1 else "judgment",
        "detector": first.detector, "detector_kind": first.kind,
        **({"detector_kinds": kinds} if len(kinds) > 1 else {}),
    }


def to_findings(hits, manifest, run_id=None, repo=None):
    """A findings document in the contract's shape, one finding per distinct defect.

    Only files in the manifest's review tier are reported, so a `--since` window holds; the
    detectors still read the whole repository, because a missing producer or an undefined
    name is decided by files outside the window.
    """
    review = {f["path"] for f in manifest.get("files", [])}
    sent, held_back = [], {}
    for h in hits:
        if h.file not in review:
            continue
        if h.kind not in SENT:
            held_back[h.kind] = held_back.get(h.kind, 0) + 1
            continue
        sent.append(h)
    groups = sorted(_group(sent), key=lambda hs: (hs[0].file, min(h.line for h in hs),
                                                   hs[0].kind, hs[0].message))
    findings = [_finding(n, hs, repo) for n, hs in enumerate(groups, 1)]
    code = sorted(p for p in review if Path(p).suffix.lower() in CODE_EXT)
    return {
        "reviewer": STATIC_REVIEWER, "run_id": run_id,
        "scope": {"mode": manifest.get("mode"), "base": manifest.get("base"),
                  "files_in_scope": len(review), "files_read": code, "files_queried": []},
        "machine": {},
        "denominators": {"files_scanned": len(code), "hits": len(hits),
                         "hits_in_scope": len(sent) + sum(held_back.values()),
                         "held_back": sum(held_back.values()), "findings": len(findings)},
        "sections_skipped": [{"section": kind, "reason": f"{n} hit(s) of a kind that is "
                              "counted but not sent as findings"}
                             for kind, n in sorted(held_back.items())],
        "not_reviewed": [{"path": p, "reason": "static checks read code only (.py, .R, .sh, "
                          "YAML)"} for p in sorted(review - set(code))],
        "findings": findings,
    }


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="the repository to scan (default: the manifest's repo)")
    ap.add_argument("--json", help="write every hit to this file as JSON")
    ap.add_argument("--findings", help="write the precise hits in the review's manifest scope to "
                                       "this file as a findings document (needs --manifest)")
    ap.add_argument("--manifest", help="the run's manifest.json, for --findings")
    ap.add_argument("--words", help="a word list, one per line, for regex-corrupts-words "
                                    "(default: a small built-in list)")
    ap.add_argument("--only", help="comma-separated detector names")
    return ap


def main():
    ap = build_parser()
    a = ap.parse_args()
    if a.findings and not a.manifest:
        ap.error("--findings needs --manifest")
    manifest = json.loads(Path(a.manifest).read_text()) if a.manifest else None
    if not a.repo and not manifest:
        ap.error("give --repo, or --manifest to take it from")
    repo = Path(a.repo or manifest["repo"]).resolve()
    words = None
    if a.words:
        words = [w.strip() for w in Path(a.words).read_text().splitlines()
                 if w.strip().isalpha() and len(w.strip()) > 2]
    extra = [x["path"] for x in (manifest or {}).get("files", []) + (manifest or {}).get(
        "reference", [])]
    only = {x.strip() for x in a.only.split(",") if x.strip()} if a.only else None
    if only and only - set(DETECTORS):
        ap.error(f"unknown detector(s): {', '.join(sorted(only - set(DETECTORS)))}; "
                 f"choose from {', '.join(DETECTORS)}")
    hits = run(repo, only, words, extra)
    if a.json:
        Path(a.json).write_text(json.dumps([asdict(h) for h in hits], indent=1))
    if a.findings:
        out = Path(a.findings)
        doc = to_findings(hits, manifest, run_id=out.parent.name, repo=repo)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.replace(out)
        d = doc["denominators"]
        print(f"static: {len(doc['findings'])} finding(s) from {d['hits_in_scope']} hit(s) in "
              f"scope ({d['held_back']} of kinds counted but not sent) -> {out}")
        return
    by = {}
    for h in hits:
        by.setdefault(h.detector, []).append(h)
    for name in DETECTORS:
        if only and name not in only:
            continue
        lst = by.get(name, [])
        print(f"{name}: {len(lst)} hit(s)")
        for h in lst[:40]:
            print(f"  [{h.confidence}] {h.file}:{h.line}  {h.message}")
        if len(lst) > 40:
            print(f"  … {len(lst) - 40} more (see --json)")


if __name__ == "__main__":
    main()
