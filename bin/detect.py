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

Detectors (and the taxonomy category each targets):

    unseeded               C1  randomness with no seed: a seedable call without its seed
                               argument, global RNG use in a file that never seeds, a
                               commented-out seed, an R script that samples without set.seed
    fit-outside-fold       A2  a transformer fitted on test data, refitted on different data
                               (two bases), or fitted before the data is split into folds
    resample-before-split  A1  rows resampled or duplicated before the train/held-out split
    ignored-setting        B5  a parameter never read or overwritten by a literal, a condition
                               on a constant, a string compared in the wrong case, an argv
                               dispatch with no else, a config key no code reads
    missing-producer       C2  a file the code reads that nothing writes and nothing provides
    regex-corrupts-words   B3  a text-cleaning rule that rewrites the middle of common words
    cannot-run             E1  a syntax error, a name used and never defined, yaml.load
                               without a Loader
    duplicate-code         E2  a function defined twice in one scope, identical bodies, and
                               long duplicated blocks
    hard-coded-path        F3  absolute or home-directory paths, setwd/chdir to a literal, a
                               worker global set only under __main__

Stdlib only. Python 3.10+.
"""
from __future__ import annotations

import argparse, ast, builtins, hashlib, json, os, re, subprocess, sys, warnings
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
    r = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True)
    if r.returncode == 0 and r.stdout:
        names = [os.fsdecode(x) for x in r.stdout.split(b"\0") if x]
    else:
        names = [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file()]
    out = []
    for n in sorted(names):
        parts = Path(n).parts
        if set(parts[:-1]) & SKIP_DIRS:
            continue
        if (repo / n).is_file():
            out.append(n)
    return out


def src(node, text):
    try:
        return ast.get_source_segment(text, node) or ""
    except Exception:
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


def scopes(tree):
    """(scope node, [statements in source order, nested blocks included, nested defs excluded])."""
    def flatten(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield st
            for name in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(st, name, None)
                if isinstance(inner, list):
                    yield from flatten([x for x in inner if isinstance(x, ast.stmt)]
                                       + [y for x in inner if isinstance(x, ast.ExceptHandler)
                                          for y in x.body])
    yield tree, list(flatten(tree.body))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, list(flatten(node.body))


def calls_in(stmts):
    """Every Call in these statements, without descending into nested defs, by line."""
    out = []
    for st in stmts:
        for node in ast.walk(st) if not isinstance(st, (ast.For, ast.While, ast.If, ast.With,
                                                         ast.Try)) else _shallow(st):
            if isinstance(node, ast.Call):
                out.append(node)
    return sorted(out, key=lambda c: (c.lineno, c.col_offset))


def _shallow(st):
    """The nodes of a compound statement's header only: its body is flattened separately."""
    heads = []
    if isinstance(st, ast.For):
        heads = [st.target, st.iter]
    elif isinstance(st, (ast.While, ast.If)):
        heads = [st.test]
    elif isinstance(st, ast.With):
        heads = [i.context_expr for i in st.items]
    for h in heads:
        yield from ast.walk(h)


WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(text):
    """Lower-case identifier parts: `x_test`, `testData`, `data_test` all contain 'test'."""
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", text)
    return set(t for t in WORD_SPLIT.split(text.lower()) if t)


# ---------------------------------------------------------------- C1 unseeded

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
    """The first line that back-propagates a loss, in code: a docstring example does not
    count, and nor does an `optimizer.step()` with no gradient behind it (a scheduler demo)."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                n.func.attr == "backward":
            return n.lineno
    return None

SEED_CALL = re.compile(r"\b(?:np\.random\.seed|numpy\.random\.seed|random\.seed|"
                       r"torch\.manual_seed|torch\.cuda\.manual_seed(?:_all)?|tf\.random\."
                       r"set_seed|set_seed|seed_everything|seed_all|fix_seed|set\.seed)\s*\(")
COMMENTED_SEED = re.compile(r"^\s*#.*" + SEED_CALL.pattern)
R_RANDOM = re.compile(r"\b(sample|runif|rnorm|rbinom|randomForest|xgb\.cv|xgb\.train|"
                      r"createDataPartition|createFolds|kmeans|train)\s*\(")


def _local_imports(f, by_module):
    """Repository modules this file imports, by module stem."""
    out = []
    for n in ast.walk(f.tree) if f.tree is not None else []:
        names = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            names = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names = [a.name for a in n.names]
        for name in names:
            for part in (name.split(".")[-1], name.split(".")[0]):
                cands = by_module.get(part, [])
                # `import utils` means the utils beside this file when there is one; a
                # same-named module elsewhere in the repository is a different module.
                near = [g for g in cands if Path(g.rel).parent == Path(f.rel).parent]
                out += near or (cands if len(cands) == 1 else [])
    return out


def detect_unseeded(r):
    py, others = r.py, r.others
    hits = []
    by_module = {}
    for f in py:
        by_module.setdefault(Path(f.rel).stem, []).append(f)

    def torch_seeded(f):
        return any(TORCH_SEED.search(g.text) for g in [f] + _local_imports(f, by_module))
    repo_seeds = any(SEED_CALL.search(f.text) for f in py) or any(
        SEED_CALL.search(t) for _, t in others)
    for f in py:
        for i, line in enumerate(f.lines, 1):
            if COMMENTED_SEED.match(line):
                hits.append(Hit("unseeded", "commented-out-seed", f.rel, i,
                                "a seeding call is commented out", line.strip(), "high"))
        if f.tree is None:
            continue
        file_seeds = bool(SEED_CALL.search(f.text))
        global_uses = []
        # With random_state=None, sklearn and pandas draw from numpy's global RNG, so a numpy
        # seed earlier in the same function or module body does make them reproducible.
        seeded_from = {}
        for scope, stmts in scopes(f.tree):
            seed_lines = [c.lineno for c in calls_in(stmts)
                          if dotted(c.func) in ("np.random.seed", "numpy.random.seed")]
            for c in calls_in(stmts):
                if seed_lines and min(seed_lines) < c.lineno:
                    seeded_from[id(c)] = min(seed_lines)
        for node in ast.walk(f.tree):
            if not isinstance(node, ast.Call) or id(node) in seeded_from:
                continue
            name = callee(node)
            full = dotted(node.func)
            if name in SEEDED_CALLS and not has_kwargs_splat(node):
                seeds = SEEDED_CALLS[name]
                if not (kwnames(node) & set(seeds)):
                    if name == "shuffle" and full.startswith(("np.random", "random", "numpy")):
                        pass        # the global-RNG shuffle, handled below
                    elif name in ("shuffle", "resample") and not full.startswith(
                            ("sklearn", "utils")) and "." in full:
                        pass        # some other library's shuffle
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
                    and not full.startswith(("random", "np", "numpy")):
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
        uses = [i for i, l in enumerate(text.splitlines(), 1)
                if R_RANDOM.search(l) and not l.lstrip().startswith("#")]
        if uses and "set.seed(" not in text.replace(" ", ""):
            hits.append(Hit("unseeded", "r-script-without-set-seed", rel, uses[0],
                            f"{len(uses)} random call(s) and no set.seed in this R script",
                            text.splitlines()[uses[0] - 1].strip()[:160], "high"))
    return hits


# ---------------------------------------------------------------- A2 fit outside the fold

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


def _fitting_functions(py):
    """Names of repository functions whose body fits a transformer (one level of calls)."""
    direct = set()
    defs = {}
    for f in py:
        if f.tree is None:
            continue
        tv = _transformer_vars(f.tree, f.text)
        for node in ast.walk(f.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append(node)
                for c in ast.walk(node):
                    if isinstance(c, ast.Call) and callee(c) in ("fit", "fit_transform") and \
                            isinstance(c.func, ast.Attribute) and (
                                src(c.func.value, f.text) in tv
                                or callee(c) == "fit_transform"):
                        direct.add(node.name)
                        break
    indirect = set()
    for name, nodes in defs.items():
        for node in nodes:
            if any(isinstance(c, ast.Call) and callee(c) in direct for c in ast.walk(node)):
                indirect.add(name)
    return direct | indirect


def detect_fit_outside_fold(r):
    py = r.py
    hits = []
    fitters = _fitting_functions(py)
    for f in py:
        if f.tree is None:
            continue
        tv = _transformer_vars(f.tree, f.text)
        tv = {k: v for k, v in tv.items() if v not in STATELESS}
        splitters = _splitter_names(f.tree, f.text)
        for scope, stmts in scopes(f.tree):
            # A statistic of the held-out rows written back into them: `test[c] = test[c].mean()`.
            for st in stmts:
                if isinstance(st, ast.Assign) and len(st.targets) == 1:
                    tgt = src(st.targets[0], f.text)
                    tgt_base = re.match(r"\w+", tgt)
                    stats = [c for c in ast.walk(st.value) if isinstance(c, ast.Call) and
                             callee(c) in ("mean", "median", "std", "mode", "max", "min",
                                           "quantile") and isinstance(c.func, ast.Attribute)]
                    for c in stats:
                        base = re.match(r"\w+", src(c.func.value, f.text))
                        if tgt_base and base and tgt_base.group(0) == base.group(0) and \
                                tokens(tgt_base.group(0)) & HELDOUT:
                            hits.append(Hit("fit-outside-fold", "heldout-statistic", f.rel,
                                            st.lineno, f"{tgt_base.group(0)} is filled with a "
                                            "statistic of itself: the value used on held-out "
                                            "rows is estimated from the rows being scored",
                                            src(st, f.text)[:160], "high"))
                            break
            fits = []          # (call, receiver source, argument source)
            for c in calls_in(stmts):
                if callee(c) in ("fit", "fit_transform") and isinstance(c.func, ast.Attribute) \
                        and c.args:
                    recv = src(c.func.value, f.text)
                    inline = callee(c.func.value) if isinstance(c.func.value, ast.Call) else ""
                    is_tf = recv in tv or callee(c) == "fit_transform" or is_transformer(inline)
                    if inline in STATELESS or _transformer_vars(f.tree, f.text).get(recv) in STATELESS:
                        is_tf = False
                    if is_tf:
                        fits.append((c, recv, src(c.args[0], f.text)))
            for c, recv, arg in fits:
                combined = re.search(r"\b(concat|vstack|hstack|append|r_|row_stack)\b|\+", arg)
                if tokens(arg) & HELDOUT and combined:
                    hits.append(Hit("fit-outside-fold", "fit-on-train-and-test", f.rel,
                                    c.lineno, f"{recv} is fitted on training and held-out "
                                    f"data together ({arg[:60]})", src(c, f.text)[:160],
                                    "medium"))
                elif tokens(arg) & HELDOUT and not re.search(r"train", arg, re.I):
                    hits.append(Hit("fit-outside-fold", "fit-on-heldout", f.rel, c.lineno,
                                    f"{recv} is fitted on held-out data ({arg[:60]}); its "
                                    "statistics then shape what is scored",
                                    src(c, f.text)[:160], "high"))
            # The same transformer, or a fresh one of the same class, fitted twice in one
            # scope on different data: the two outputs live in different bases.
            by_key = {}
            for c, recv, arg in fits:
                key = tv.get(recv, recv)
                by_key.setdefault(key, []).append((c, arg))
            for key, lst in by_key.items():
                args = {a for _, a in lst}
                if len(lst) >= 2 and len(args) >= 2:
                    c = lst[1][0]
                    hits.append(Hit("fit-outside-fold", "refit-on-different-data", f.rel,
                                    c.lineno, f"{key} is fitted again on different data "
                                    f"({', '.join(sorted(a[:40] for a in args))}); "
                                    "transform with the first fit instead",
                                    src(c, f.text)[:160], "high"))
            # Fitted before the data is split, in this scope or in a function called here.
            split_at = _split_line(stmts, f.text, splitters)
            if split_at is None:
                continue
            for c in calls_in(stmts):
                if c.lineno >= split_at:
                    break
                name = callee(c)
                fitted_here = name in ("fit", "fit_transform") and isinstance(
                    c.func, ast.Attribute) and src(c.func.value, f.text) in tv
                if fitted_here or (name in fitters and name not in ("fit", "fit_transform")):
                    hits.append(Hit("fit-outside-fold", "fit-before-split", f.rel, c.lineno,
                                    ("a transformer is fitted" if fitted_here else
                                     f"{name}() fits a transformer") + f" before the data "
                                    f"is split at line {split_at}", src(c, f.text)[:160],
                                    "medium"))
    return hits


# ---------------------------------------------------------------- A1 resample before split

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
                if RESAMPLERS.search(name):
                    why = f"{name}() resamples"
                elif name == "concat" and c.args and isinstance(c.args[0], (ast.List, ast.Tuple)):
                    elts = [src(e, f.text) for e in c.args[0].elts]
                    dup = sorted({e for e in elts if elts.count(e) > 1})
                    if dup:
                        why = f"concat duplicates {dup[0]}"
                    elif isinstance(c.args[0], ast.BinOp):
                        why = "concat of a repeated list"
                elif name == "concat" and c.args and isinstance(c.args[0], ast.BinOp) and \
                        isinstance(c.args[0].op, ast.Mult):
                    why = "concat of a repeated list"
                elif name == "sample" and isinstance(c.func, ast.Attribute):
                    rep = kw(c, "replace")
                    frac = kw(c, "frac")
                    if (isinstance(rep, ast.Constant) and rep.value is True) or (
                            isinstance(frac, ast.Constant) and isinstance(frac.value, (int, float))
                            and frac.value > 1):
                        why = ".sample with replacement"
                    elif kw(c, "n") is not None:
                        why = ".sample(n=...) draws a subset"
                if why:
                    hits.append(Hit("resample-before-split", "resample-before-split", f.rel,
                                    c.lineno, f"{why} before the data is split at line "
                                    f"{split_at}: copies can land on both sides, or every fold "
                                    "shares one draw", src(c, f.text)[:160], "medium"))
    return hits


# ---------------------------------------------------------------- B5 ignored settings

INTERFACE_ARGS = {"self", "cls", "args", "kwargs", "_"}


def _reads(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _is_stub(fn):
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    return not body or all(isinstance(s, (ast.Pass, ast.Raise)) or (
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in body)


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
        literal_assigns = {}       # attribute or name -> set of string literals assigned
        for node in ast.walk(f.tree):
            if isinstance(node, ast.Assign) and str_const(node.value) is not None:
                for t in node.targets:
                    key = t.attr if isinstance(t, ast.Attribute) else (
                        t.id if isinstance(t, ast.Name) else None)
                    if key:
                        literal_assigns.setdefault(key, set()).add(node.value.value)
            elif isinstance(node, ast.keyword) and node.arg and str_const(node.value) is not None:
                literal_assigns.setdefault(node.arg, set()).add(node.value.value)
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
                    if p in INTERFACE_ARGS or p.startswith("_") or p in reads:
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
            # A condition on a variable that only ever holds one constant.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                body_nodes = [n for n in ast.walk(node)]
                consts = {}
                for n in body_nodes:
                    if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                        for t in targets:
                            if isinstance(t, ast.Name):
                                val = n.value if isinstance(n, ast.Assign) else None
                                v = val.value if isinstance(val, ast.Constant) and isinstance(
                                    val.value, bool) else "<other>"
                                consts.setdefault(t.id, set()).add(v)
                    elif isinstance(n, (ast.For, ast.With, ast.comprehension)):
                        tgt = getattr(n, "target", None)
                        for t in ast.walk(tgt) if tgt is not None else []:
                            if isinstance(t, ast.Name):
                                consts.setdefault(t.id, set()).add("<other>")
                    elif isinstance(n, (ast.Global, ast.Nonlocal)):
                        for name in n.names:
                            consts.setdefault(name, set()).add("<other>")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for a in node.args.args + node.args.kwonlyargs:
                        consts.setdefault(a.arg, set()).add("<other>")
                for n in body_nodes:
                    if isinstance(n, (ast.If, ast.While, ast.IfExp)):
                        names = [x for x in ast.walk(n.test) if isinstance(x, ast.Name)]
                        for x in names:
                            vals = consts.get(x.id)
                            if vals and len(vals) == 1 and next(iter(vals)) != "<other>" and \
                                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
                key = other.attr if isinstance(other, ast.Attribute) else (
                    other.id if isinstance(other, ast.Name) else None)
                if lit is not None and key and key in literal_assigns:
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
        for i, line in enumerate(text.splitlines(), 1):
            m = re.match(r"^\s*([A-Za-z_][\w]*)\s*:", line)
            if m and not re.search(r"\b" + re.escape(m.group(1)) + r"\b", code_text):
                hits.append(Hit("ignored-setting", "config-key-unread", rel, i,
                                f"config key {m.group(1)!r} is read by no code in the "
                                "repository", line.strip()[:160], "medium"))
    return hits


# ---------------------------------------------------------------- C2 missing producer

READERS = {"read_csv", "read_table", "read_parquet", "read_pickle", "read_feather", "read_hdf",
           "read_json", "read_excel", "load", "loadtxt", "genfromtxt", "fromfile", "load_npz",
           "imread", "read_pickle", "File"}
WRITERS = {"to_csv", "to_parquet", "to_pickle", "to_feather", "to_hdf", "to_json", "to_excel",
           "save", "savez", "savez_compressed", "savetxt", "dump", "imwrite", "savefig",
           "save_npz", "tofile", "write_pred_csv"}
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
            if name == "open" and node.args:
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
            elif name in READERS:
                for a in args:
                    for nm in _names_in(a, f.text):
                        reads.setdefault(nm, (f.rel, node.lineno))
    for rel, text in others:
        low = rel.lower()
        for i, line in enumerate(text.splitlines(), 1):
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
    present = {Path(p).name for p in repo_files(repo)}
    docs = "\n".join((repo / p).read_text(encoding="utf-8", errors="replace")
                     for p in repo_files(repo) if p.lower().endswith((".md", ".txt", ".rst")))
    hits = []
    for nm, (rel, line) in sorted(reads.items(), key=lambda kv: kv[1]):
        if nm in writes or nm in present or Path(nm).suffix in (".yaml", ".yml", ".jar") \
                or CONVENTIONAL_INPUTS.match(nm):
            continue
        documented = re.search(r"(?<![\w.])" + re.escape(nm) + r"(?![\w])", docs)
        near = sorted(w for w in writes if w != nm and (
            Path(w).stem in Path(nm).stem or Path(nm).stem in Path(w).stem))
        if documented and not near:
            continue        # an input the documentation names: assumed to be downloaded
        msg = f"{nm} is read here, but nothing in the repository writes it and it is not committed"
        if near:
            msg += f"; the code writes {', '.join(near[:3])} instead"
        hits.append(Hit("missing-producer", "read-without-writer", rel, line, msg, nm,
                        "high" if near else "medium"))
    return hits


# ---------------------------------------------------------------- B3 regex corrupts words

DEFAULT_WORDS = ["thousand", "usage", "woman", "big", "washer", "dryer", "status", "human",
                 "jaguar", "pig", "dig", "gig", "fig", "rig", "wig", "sign", "design", "high",
                 "light", "night", "right", "bright", "eight", "weight", "sigma", "usual",
                 "music", "focus", "cause", "because", "pause", "house", "mouse", "busa",
                 "manager", "image", "page", "stage", "language", "message", "orange",
                 "student", "company", "people", "question", "answer", "money", "country",
                 "information", "business", "government", "something", "children", "program"]


def _regex_calls(f):
    """(call, pattern, replacement, flags) for literal regex substitutions in a file."""
    for node in ast.walk(f.tree):
        if not isinstance(node, ast.Call):
            continue
        name, full = callee(node), dotted(node.func)
        if name == "sub" and full in ("re.sub", "regex.sub") and len(node.args) >= 3:
            pat, rep = str_const(node.args[0]), str_const(node.args[1])
            flags = 0
            fl = kw(node, "flags")
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
            if rx.match(""):
                continue
            damaged = []
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
                    if partial and rx.sub(rep, text) != text:
                        damaged.append(f"{form} -> {rx.sub(rep, text).strip()!r}")
                        break
                if len(damaged) >= 3:
                    break
            if damaged:
                hits.append(Hit("regex-corrupts-words", "rewrites-inside-words", f.rel,
                                node.lineno, f"the pattern {pat!r} also matches inside "
                                f"ordinary words: {'; '.join(damaged)}",
                                src(node, f.text)[:160], "high"))
            if dotted(node.func) not in ("re.sub", "regex.sub") and \
                    kw(node, "regex") is None and re.search(r"[.()\[\]+*?|^$]", pat):
                hits.append(Hit("regex-corrupts-words", "str-replace-regex-default", f.rel,
                                node.lineno, f"{pat!r} has regex metacharacters and no regex=; "
                                "pandas < 2 treats it as a regular expression",
                                src(node, f.text)[:160], "medium"))
    return hits


# ---------------------------------------------------------------- E1 cannot run

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__builtins__",
                                 "__path__", "__loader__", "__package__", "__annotations__",
                                 "display", "get_ipython", "__class__"}


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
    return bound


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
                magic = next((i for i, l in enumerate(f.lines, 1)
                              if re.match(r"^\s*[%!]\w", l)), None)
                hits.append(Hit("cannot-run", "syntax-error", f.rel, magic or e.lineno or 1,
                                ("an IPython magic in a .py file: " if magic else "")
                                + f"{e.msg}", (f.lines[(magic or e.lineno or 1) - 1].strip()
                                               if f.lines else "")[:160], "high"))
            except ValueError as e:
                hits.append(Hit("cannot-run", "syntax-error", f.rel, 1, str(e), "", "high"))
            continue
        star = any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
                   for n in ast.walk(f.tree))
        dynamic = re.search(r"\b(exec|globals|locals|vars)\s*\(|__builtins__|setattr\(\s*"
                            r"(sys\.modules|builtins)", f.text)
        if not (star or dynamic):
            bound = _bound_names(f.tree)
            seen = set()
            for n in ast.walk(f.tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and \
                        n.id not in bound and n.id not in BUILTINS and n.id not in seen:
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


# ---------------------------------------------------------------- E2 duplicate code

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
                    if st.name in seen and not any(dotted(d).endswith(("setter", "overload",
                                                                         "register"))
                                                   or (isinstance(d, ast.Attribute) and
                                                       d.attr in ("setter", "deleter"))
                                                   for d in st.decorator_list):
                        hits.append(Hit("duplicate-code", "redefined", f.rel, seen[st.name],
                                        f"{st.name} is defined again at line {st.lineno}; "
                                        "this definition is dead", f"def {st.name}", "high"))
                    seen[st.name] = st.lineno
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
        for i, l in enumerate(text.splitlines(), 1):
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


# ---------------------------------------------------------------- F3 hard-coded paths

ABS_PATH = re.compile(r"^(?:~/|/home/|/Users/|/mnt/|/media/|/data/|/root/|/opt/|/srv/|"
                      r"[A-Za-z]:[\\/])")


def detect_hard_coded_path(r):
    py, others = r.py, r.others
    hits = []
    for f in py:
        if f.tree is None:
            continue
        for n in ast.walk(f.tree):
            s = str_const(n)
            if s and ABS_PATH.match(s) and "\n" not in s and len(s) < 300:
                hits.append(Hit("hard-coded-path", "absolute-path", f.rel, n.lineno,
                                f"a machine-specific path: {s!r}", s, "high"))
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
                                        "under __main__: worker processes started with spawn "
                                        "(macOS; Linux from Python 3.14) see its initial value",
                                        f"def {fn.name}", "medium"))
    for rel, text in others:
        for i, line in enumerate(text.splitlines(), 1):
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


def load(repo: Path):
    py, others = [], []
    for rel in repo_files(repo):
        ext = Path(rel).suffix.lower()
        if ext not in CODE_EXT:
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ext == ".py":
            try:
                with warnings.catch_warnings():
                    # The analysed code's own warnings (invalid escapes in its strings) are
                    # not this tool's output.
                    warnings.simplefilter("ignore")
                    tree = ast.parse(text)
            except (SyntaxError, ValueError):
                tree = None
            py.append(PyFile(rel, text, tree, text.splitlines()))
        else:
            others.append((rel, text))
    return py, others


def run(repo: Path, only=None, words=None):
    py, others = load(repo)
    r = Repo(repo, py, others, words)
    hits = []
    for name, fn in DETECTORS.items():
        if not only or name in only:
            hits += fn(r)
    return sorted(hits, key=lambda h: (h.detector, h.file, h.line))


# ---------------------------------------------------------------- findings for a review

STATIC_REVIEWER = "static"

# The kinds precise enough to send to the reconciler as findings, each with how bad the
# defect is if true, the fix, and whether that fix is mechanical. The rest — unused
# parameters, unread config keys, duplicated blocks and identical bodies, file-level global RNG
# use, the pandas regex default — were mostly noise on unseen code, so they are counted in the
# findings file and not sent.
SENT = {
    "seedable-call-without-seed": ("minor", "pass random_state (or seed) explicitly", "mechanical", "T-7"),
    "unseeded-dataloader": ("major", "seed torch at the entry point and pass a seeded generator to the DataLoader", "judgment", "T-7"),
    "unseeded-torch-training": ("major", "seed torch, numpy and random at the entry point, and record the seed with the run", "judgment", "T-7"),
    "commented-out-seed": ("major", "restore the seeding call, or record why the run is deliberately unseeded", "judgment", "T-7"),
    "r-script-without-set-seed": ("minor", "call set.seed before the first random call", "mechanical", "T-7"),
    "fit-on-heldout": ("major", "fit on the training rows only and transform the held-out rows", "judgment", "T-4"),
    "fit-on-train-and-test": ("major", "fit on the training rows only, inside the fold", "judgment", "T-4"),
    "refit-on-different-data": ("major", "fit once and transform every split with that fit", "judgment", "T-4"),
    "fit-before-split": ("major", "move the fit inside the fold loop or after the split", "judgment", "T-4"),
    "heldout-statistic": ("minor", "compute the statistic on the training rows and apply it to the held-out rows", "judgment", "T-4"),
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


def to_findings(hits, manifest, run_id=None, repo=None):
    """A findings document in the contract's shape, one finding per (kind, file).

    Only files in the manifest's review tier are reported, so a `--since` window holds; the
    detectors still read the whole repository, because a missing producer or an undefined
    name is decided by files outside the window.
    """
    review = {f["path"] for f in manifest.get("files", [])}
    groups, held_back = {}, {}
    for h in hits:
        if h.file not in review:
            continue
        if h.kind not in SENT:
            held_back[h.kind] = held_back.get(h.kind, 0) + 1
            continue
        groups.setdefault((h.kind, h.file), []).append(h)
    findings = []
    for n, ((kind, file), hs) in enumerate(sorted(groups.items(), key=lambda kv: (
            kv[0][1], min(h.line for h in kv[1]), kv[0][0])), 1):
        hs = sorted(hs, key=lambda h: h.line)
        first = hs[0]
        severity, fix, certainty, rule = SENT[kind]
        lines = sorted({h.line for h in hs})
        quote = first.evidence
        if not quote and repo is not None:
            try:
                quote = (Path(repo) / file).read_text(errors="replace").splitlines()[first.line - 1].strip()
            except (OSError, IndexError):
                quote = ""
        claim = first.message + (f" (and {len(hs) - 1} more site(s) in this file of the same "
                                 f"kind: lines {', '.join(map(str, lines[1:]))})" if len(hs) > 1 else "")
        findings.append({
            "id": f"static-{n:03d}", "rule_id": rule, "file": file, "line": first.line,
            "lines": ",".join(map(str, lines)), "claim": claim, "class": "defect",
            "evidence": {"kind": "quote", "quote": quote or first.message},
            "severity": severity, "confidence": "likely", "fix": fix,
            "fix_certainty": certainty, "detector": first.detector, "detector_kind": kind,
        })
    code = sorted(p for p in review if Path(p).suffix.lower() in CODE_EXT)
    return {
        "reviewer": STATIC_REVIEWER, "run_id": run_id,
        "scope": {"mode": manifest.get("mode"), "base": manifest.get("base"),
                  "files_in_scope": len(review), "files_read": code, "files_queried": []},
        "machine": {},
        "denominators": {"files_scanned": len(code), "hits": len(hits),
                         "hits_in_scope": sum(len(v) for v in groups.values())
                         + sum(held_back.values()),
                         "held_back": sum(held_back.values()), "findings": len(findings)},
        "sections_skipped": [{"section": kind, "reason": f"{n} hit(s) held back: imprecise on "
                              "unseen code, so not sent as findings"}
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
    hits = run(repo, set(a.only.split(",")) if a.only else None, words)
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
              f"scope ({d['held_back']} held back as imprecise) -> {out}")
        return
    by = {}
    for h in hits:
        by.setdefault(h.detector, []).append(h)
    for name in DETECTORS:
        lst = by.get(name, [])
        print(f"{name}: {len(lst)} hit(s)")
        for h in lst[:40]:
            print(f"  [{h.confidence}] {h.file}:{h.line}  {h.message}")
        if len(lst) > 40:
            print(f"  … {len(lst) - 40} more (see --json)")


if __name__ == "__main__":
    main()
