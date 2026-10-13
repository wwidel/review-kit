"""bin/detect.py: each detector fires on its pattern and stays quiet on the near-misses.

The near-misses are the false positives found when the detectors were measured on real
repositories: library defaults that are already deterministic, stateless transformers, a
seed set earlier in the same scope, framework methods whose signature is not theirs to choose,
a docstring that mentions `.backward()`, a compressed file that is committed.
"""
import textwrap

import pytest

from conftest import load

detect = load("detect")


def scan(tmp_path, files, only=None, words=None):
    n = 0
    while (tmp_path / f"r{n}").exists():
        n += 1
    root = tmp_path / f"r{n}"        # a fresh tree per scan
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(textwrap.dedent(text))
    return detect.run(root, {only} if only else None, words)


def kinds(hits):
    return sorted(h.kind for h in hits)


# ---------------------------------------------------------------- unseeded

@pytest.mark.parametrize("code,expected", [
    ("from sklearn.cluster import KMeans\nKMeans(n_clusters=3)\n", ["seedable-call-without-seed"]),
    ("from sklearn.cluster import KMeans\nKMeans(n_clusters=3, random_state=0)\n", []),
    ("from sklearn.model_selection import KFold\nKFold(5, shuffle=True)\n",
     ["seedable-call-without-seed"]),
    ("from sklearn.model_selection import KFold\nKFold(5)\n", []),
    ("from xgboost import XGBClassifier\nXGBClassifier()\n", []),
    ("import numpy as np\nfrom sklearn.cluster import KMeans\nnp.random.seed(0)\nKMeans(3)\n", []),
    ("df.sample(n=5)\n", ["seedable-call-without-seed"]),
    ("df.sample(n=5, random_state=1)\n", []),
    ("import numpy as np\n#np.random.seed(1234)\n", ["commented-out-seed"]),
    ("import numpy as np\nx = np.random.rand(3)\n", ["global-rng-unseeded-file"]),
    ("import numpy as np\nnp.random.seed(0)\nx = np.random.rand(3)\n", []),
])
def test_unseeded_python(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "unseeded")) == expected


TRAIN = """
from torch.utils.data import DataLoader
{imp}
def main():
    loader = DataLoader(ds, shuffle=True)
    loss.backward()
if __name__ == "__main__":
    main()
"""


def test_unseeded_torch_training_and_dataloader(tmp_path):
    hits = scan(tmp_path, {"train.py": TRAIN.format(imp="")}, "unseeded")
    assert kinds(hits) == ["unseeded-dataloader", "unseeded-torch-training"]


def test_a_seed_in_an_imported_sibling_module_counts(tmp_path):
    hits = scan(tmp_path, {"pkg/train.py": TRAIN.format(imp="import utils"),
                           "pkg/utils.py": "import torch\ntorch.manual_seed(0)\n"}, "unseeded")
    assert hits == []


def test_a_seeding_module_of_the_same_name_elsewhere_does_not_count(tmp_path):
    hits = scan(tmp_path, {"a/train.py": TRAIN.format(imp="import utils"),
                           "a/utils.py": "x = 1\n",
                           "b/utils.py": "import torch\ntorch.manual_seed(0)\n"}, "unseeded")
    assert "unseeded-torch-training" in kinds(hits)


def test_backward_in_a_docstring_is_not_training(tmp_path):
    code = 'def f():\n    """\n    >>> loss.backward()\n    """\nif __name__ == "__main__":\n    f()\n'
    assert scan(tmp_path, {"u.py": code}, "unseeded") == []


def test_r_script_sampling_without_set_seed(tmp_path):
    assert kinds(scan(tmp_path, {"a.R": "x <- sample(1:10)\n"}, "unseeded")) == \
        ["r-script-without-set-seed"]
    assert scan(tmp_path, {"b.R": "set.seed(1)\nx <- sample(1:10)\n"}, "unseeded") == []


# ---------------------------------------------------------------- fit-outside-fold

@pytest.mark.parametrize("code,expected", [
    ("from sklearn.preprocessing import StandardScaler\ns = StandardScaler()\n"
     "x_test = s.fit_transform(x_test)\n", ["fit-on-heldout"]),
    ("from sklearn.preprocessing import StandardScaler\ns = StandardScaler()\n"
     "s.fit(np.vstack((train, test_feats)))\n", ["fit-on-train-and-test"]),
    ("from sklearn.feature_extraction.text import TfidfVectorizer\nv = TfidfVectorizer()\n"
     "a = v.fit_transform(left)\nb = v.fit_transform(right)\nsim = cosine(a, b)\n",
     ["refit-on-different-data"]),
    ("from sklearn.feature_extraction.text import TfidfVectorizer\nv = TfidfVectorizer()\n"
     "a = v.fit_transform(train_text)\nb = v.fit_transform(test_text)\n",
     ["fit-on-heldout", "refit-on-different-data"]),
    ("from sklearn.preprocessing import StandardScaler\ns = StandardScaler()\n"
     "a = s.fit_transform(df[['age']])\nb = s.fit_transform(df[['income']])\n", []),
    ("from sklearn.feature_extraction.text import TfidfVectorizer\n"
     "a = TfidfVectorizer().fit_transform(df.title)\nb = TfidfVectorizer().fit_transform(df.body)\n",
     []),
    ("from sklearn.preprocessing import Normalizer\na = Normalizer().fit_transform(x1)\n"
     "b = Normalizer().fit_transform(x2)\n", []),
    ("from sklearn.preprocessing import StandardScaler\nfrom sklearn.model_selection import KFold\n"
     "s = StandardScaler()\nX = s.fit_transform(X)\nkf = KFold(5)\n"
     "for tr, va in kf.split(X):\n    pass\n", ["fit-before-split"]),
    ("from sklearn.preprocessing import StandardScaler\ns = StandardScaler()\n"
     "X = s.fit_transform(X)\nwords = text.split(' ')\n", []),
    ("test.loc[test['a'] == 0, 'b'] = test['b'].mean()\n", ["heldout-statistic"]),
    ("train.loc[train['a'] == 0, 'b'] = train['b'].mean()\n", []),
])
def test_fit_outside_fold(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "fit-outside-fold")) == expected


def test_fit_inside_a_called_function_before_the_split(tmp_path):
    code = """
    from sklearn.preprocessing import MinMaxScaler
    from sklearn.model_selection import KFold
    def prep(df):
        s = MinMaxScaler()
        return s.fit_transform(df)
    X = prep(df)
    kf = KFold(5, shuffle=True, random_state=0)
    for tr, va in kf.split(X):
        pass
    """
    assert kinds(scan(tmp_path, {"a.py": code}, "fit-outside-fold")) == ["fit-before-split"]


# ---------------------------------------------------------------- resample-before-split

def test_duplicating_rows_before_the_split(tmp_path):
    code = ("from sklearn.model_selection import train_test_split\n"
            "neg = pd.concat([neg, neg])\na, b = train_test_split(neg, random_state=0)\n")
    assert kinds(scan(tmp_path, {"a.py": code}, "resample-before-split")) == \
        ["resample-before-split"]


def test_resampling_after_the_split_is_fine(tmp_path):
    code = ("from sklearn.model_selection import train_test_split\n"
            "a, b = train_test_split(df, random_state=0)\na = pd.concat([a, a])\n")
    assert scan(tmp_path, {"a.py": code}, "resample-before-split") == []


# ---------------------------------------------------------------- ignored-setting

@pytest.mark.parametrize("code,expected", [
    ("class D:\n    def __init__(self, labels_file='a.csv'):\n        self.labels_file = 'labels.csv'\n",
     ["parameter-overwritten"]),
    ("def f(a, threshold):\n    return a\n", ["parameter-unused"]),
    ("def f(a, **_):\n    return a\n", []),
    ("import torch\nclass M(torch.nn.Module):\n    def forward(self, x, labels=None):\n"
     "        return x\n", []),
    ("class C:\n    def __exit__(self, t, v, tb):\n        return False\n", []),
    ("def train():\n    for e in range(3):\n        use_cache = True\n        if use_cache and e < 2:\n"
     "            pass\n", ["constant-condition"]),
    ("class T:\n    def __init__(self):\n        self.unit = 'KM'\n    def f(self, x):\n"
     "        if self.unit == 'km':\n            return x\n", ["case-mismatch-comparison"]),
    ("import sys\nif sys.argv[1] == '-a':\n    pass\nelif sys.argv[1] == '-b':\n    pass\n",
     ["argv-dispatch-without-else"]),
    ("import sys\nif sys.argv[1] == '-a':\n    pass\nelse:\n    sys.exit(2)\n", []),
])
def test_ignored_setting(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "ignored-setting")) == expected


def test_a_config_key_no_code_reads(tmp_path):
    hits = scan(tmp_path, {"cfg.yml": "lr: 0.1\nunused_knob: 3\n", "a.py": "x = cfg['lr']\n"},
                "ignored-setting")
    assert [h.message for h in hits] == ["config key 'unused_knob' is read by no code in the "
                                         "repository"]


# ---------------------------------------------------------------- missing-producer

def test_a_read_nothing_writes(tmp_path):
    hits = scan(tmp_path, {"a.py": "import pandas as pd\nx = pd.read_csv('feats.csv')\n"},
                "missing-producer")
    assert kinds(hits) == ["read-without-writer"]


def test_a_near_miss_writer_is_named(tmp_path):
    hits = scan(tmp_path, {"a.py": "x = pd.read_csv('features.csv')\n",
                           "b.py": "df.to_csv('features_v2.csv')\n"}, "missing-producer")
    assert "features_v2.csv instead" in hits[0].message and hits[0].confidence == "high"


@pytest.mark.parametrize("files", [
    {"a.py": "x = pd.read_csv('feats.csv')\n", "b.py": "df.to_csv('out/feats.csv')\n"},
    {"a.py": "x = pd.read_csv('train.csv')\n"},
    {"a.py": "x = pd.read_csv('data/v2.csv.gz')\n", "data/v2.csv.gz": b"\x1f\x8b"},
    {"a.py": "x = pd.read_csv('feats.csv')\n", "README.md": "Download feats.csv first.\n"},
    {"run.sh": "java -jar tool.jar out_file=preds.csv\n", "a.py": "p = pd.read_csv('preds.csv')\n"},
])
def test_inputs_that_are_accounted_for(tmp_path, files):
    assert scan(tmp_path, files, "missing-producer") == []


# ---------------------------------------------------------------- regex-corrupts-words

def test_a_rule_without_boundaries_rewrites_words(tmp_path):
    code = "import re\ntext = re.sub('and', ' & ', text, flags=re.IGNORECASE)\n"
    hits = scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["thousand", "the"])
    assert kinds(hits) == ["rewrites-inside-words"] and "thousand" in hits[0].message


def test_a_rule_with_boundaries_is_fine(tmp_path):
    code = "import re\ntext = re.sub(r'\\band\\b', '&', text)\n"
    assert scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["thousand"]) == []


def test_pandas_replace_with_metacharacters_and_no_regex_flag(tmp_path):
    code = "df['f'] = df['f'].str.replace('foo bar.', 'foo')\n"
    assert kinds(scan(tmp_path, {"a.py": code}, "regex-corrupts-words")) == \
        ["str-replace-regex-default"]
    code = "df['f'] = df['f'].str.replace('foo bar.', 'foo', regex=False)\n"
    assert scan(tmp_path, {"a.py": code}, "regex-corrupts-words") == []


# ---------------------------------------------------------------- cannot-run

@pytest.mark.parametrize("code,expected", [
    ("%matplotlib inline\nx = 1\n", ["syntax-error"]),
    ("x = f(a=1 b=2)\n", ["syntax-error"]),
    ("def f():\n    return metric.compute()\n", ["undefined-name"]),
    ("import operator\nk = operator.itemgetter(1)\n", []),
    ("from helpers import *\ny = thing\n", []),
    ("import yaml\nc = yaml.load(open('c.yml'))\n", ["yaml-load-without-loader"]),
    ("import yaml\nc = yaml.load(open('c.yml'), Loader=yaml.SafeLoader)\n", []),
])
def test_cannot_run(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "cannot-run")) == expected


# ---------------------------------------------------------------- duplicate-code

def test_a_function_defined_twice(tmp_path):
    code = "def f(x):\n    return 1\n\ndef f(x):\n    return 2\n"
    assert kinds(scan(tmp_path, {"a.py": code}, "duplicate-code")) == ["redefined"]


def test_identical_bodies_and_duplicated_blocks(tmp_path):
    body = "".join(f"    v{i} = x + {i}\n" for i in range(14)) + "    return v0\n"
    hits = scan(tmp_path, {"a.py": "def f(x):\n" + body, "b.py": "def g(x):\n" + body},
                "duplicate-code")
    assert set(kinds(hits)) == {"identical-function-bodies", "duplicated-block"}


def test_duplicated_imports_are_not_duplicated_logic(tmp_path):
    imports = "".join(f"import mod{i}\n" for i in range(15))
    assert scan(tmp_path, {"a.py": imports, "b.py": imports}, "duplicate-code") == []


# ---------------------------------------------------------------- hard-coded-path

@pytest.mark.parametrize("code,expected", [
    ("p = '~/data/input/train.json'\n", ["absolute-path"]),
    ("p = 'D:/python/data'\n", ["absolute-path"]),
    ("p = 'data/train.csv'\n", []),
    ("import os\nos.chdir('/home/me/proj')\n", ["chdir-literal"]),
])
def test_hard_coded_path(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "hard-coded-path")) == expected


def test_a_worker_reading_a_global_set_only_under_main(tmp_path):
    code = """
    from multiprocessing import Pool
    TARGET = None
    def work(x):
        return TARGET + x
    def helper(x):
        return TARGET
    if __name__ == "__main__":
        TARGET = "out"
        Pool(2).map(work, [1])
    """
    hits = scan(tmp_path, {"a.py": code}, "hard-coded-path")
    assert [(h.kind, "work()" in h.message) for h in hits] == [("fork-only-global", True)]


def test_setwd_in_r(tmp_path):
    assert kinds(scan(tmp_path, {"a.R": "setwd('D:/work/project')\n"}, "hard-coded-path")) == \
        ["absolute-path"]


# ---------------------------------------------------------------- findings for a review

def scoped(tmp_path, files):
    """A committed repository and its scope manifest, as a run would have them."""
    from conftest import git, run
    import json
    root = tmp_path / "proj"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    d = tmp_path / "run" / "R1"
    d.mkdir(parents=True)
    r = run("review.py", "scope", "--repo", root, "--out", d, cwd=root)
    assert r.returncode == 0, r.stderr
    return root, d, json.loads((d / "manifest.json").read_text())


def static_findings(tmp_path, files):
    import json
    from conftest import run
    root, d, manifest = scoped(tmp_path, files)
    r = run("detect.py", "--manifest", d / "manifest.json", "--findings",
            d / "findings-static.json", cwd=root)
    assert r.returncode == 0, r.stderr
    return root, d, manifest, json.loads((d / "findings-static.json").read_text())


FILES = {
    "a.py": "from sklearn.cluster import KMeans\nk1 = KMeans(3)\nk2 = KMeans(4)\n"
            "def f(a, unused):\n    return a\n",
    "b.py": "import yaml\nc = yaml.load(open('c.yml'))\n",
    "README.md": "# p\n",
}


def test_the_findings_file_satisfies_the_contract(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, FILES)
    review = load("review")
    assert review.validate_findings(doc, manifest, str(root)) == []
    assert doc["reviewer"] == "static" and doc["run_id"] == "R1"
    assert all(f["confidence"] == "likely" for f in doc["findings"])


def test_one_finding_per_kind_and_file_and_imprecise_kinds_held_back(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, FILES)
    kinds_ = sorted((f["file"], f["detector_kind"]) for f in doc["findings"])
    assert kinds_ == [("a.py", "seedable-call-without-seed"), ("b.py", "yaml-load-without-loader")]
    seed = next(f for f in doc["findings"] if f["file"] == "a.py")
    assert seed["lines"] == "2,3" and "(lines 2, 3)" in seed["claim"]
    assert doc["denominators"]["held_back"] == 1          # the unused parameter
    assert [s["section"] for s in doc["sections_skipped"]] == ["parameter-unused"]


def test_only_files_in_the_review_window_are_reported(tmp_path):
    import json
    from conftest import run
    root, d, manifest = scoped(tmp_path, FILES)
    manifest["files"] = [f for f in manifest["files"] if f["path"] != "b.py"]
    (d / "manifest.json").write_text(json.dumps(manifest))
    run("detect.py", "--manifest", d / "manifest.json", "--findings", d / "findings-static.json",
        cwd=root)
    doc = json.loads((d / "findings-static.json").read_text())
    assert {f["file"] for f in doc["findings"]} == {"a.py"}


def test_findings_needs_a_manifest(tmp_path):
    from conftest import run
    r = run("detect.py", "--repo", tmp_path, "--findings", tmp_path / "f.json")
    assert r.returncode == 2 and "--manifest" in r.stderr


# ---------------------------------------------------------------- regressions from review

def test_an_invalid_replacement_template_does_not_crash_the_run(tmp_path):
    code = ("import re\na = re.sub(r'ou', r'\\1', t)\nb = re.sub(r'ou', r'\\d', t)\n"
            "c = re.sub(r'(?P<x>ou)', r'\\g<y>', t)\n")
    assert scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["house"]) == []


def test_a_byte_order_mark_is_not_a_syntax_error(tmp_path):
    hits = scan(tmp_path, {"a.py": "\ufeffimport os\nprint(os.sep)\n"}, "cannot-run")
    assert hits == []


def test_line_numbers_ignore_unicode_line_separators(tmp_path):
    code = 'S = "a\u2028b\x0cc"\nfrom sklearn.cluster import KMeans\nKMeans(3)\n'
    (hit,) = scan(tmp_path, {"a.py": code}, "unseeded")
    assert hit.line == 3 and hit.evidence == "KMeans(3)"


def test_async_with_and_match_blocks_give_one_hit_each(tmp_path):
    code = textwrap.dedent("""
    from sklearn.preprocessing import StandardScaler
    s = StandardScaler()
    async def f():
        async with lock:
            x_test = s.fit_transform(x_test)
    def g(v):
        match v:
            case 1:
                y_test = s.fit_transform(y_test)
    """)
    hits = scan(tmp_path, {"a.py": code}, "fit-outside-fold")
    assert sorted((h.kind, h.line) for h in hits) == [("fit-on-heldout", 6), ("fit-on-heldout", 10)]


@pytest.mark.parametrize("code", [
    "test['pred'] = test[['p1', 'p2']].mean(axis=1)\n",
    "val_df['m'] = val_df[cols].max(axis='columns')\n",
    "s.fit(df[df['is_test'] == 0])\n",
    "s.fit(X[~test_mask])\n",
    "from sklearn.manifold import TSNE\nemb = TSNE(2).fit_transform(X_test)\n",
    "from sklearn.preprocessing import LabelEncoder\nfrom sklearn.model_selection import "
    "train_test_split\ny = LabelEncoder().fit_transform(y)\na, b = train_test_split(X, y, "
    "random_state=0)\n",
])
def test_fit_outside_fold_near_misses(tmp_path, code):
    code = "from sklearn.preprocessing import StandardScaler\ns = StandardScaler()\n" + code
    assert scan(tmp_path, {"a.py": code}, "fit-outside-fold") == []


def test_a_repository_function_named_like_a_library_one_is_not_a_fitter(tmp_path):
    code = textwrap.dedent("""
    import json
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    def load(df):
        return StandardScaler().fit_transform(df)
    cfg = json.load(open('c.json'))
    a, b = train_test_split(df, random_state=0)
    """)
    assert scan(tmp_path, {"a.py": code}, "fit-outside-fold") == []


def test_a_dataloader_seeded_by_the_entry_point_that_imports_it(tmp_path):
    files = {"pkg/data.py": "from torch.utils.data import DataLoader\n"
                            "def make(ds):\n    return DataLoader(ds, shuffle=True)\n",
             "pkg/train.py": "import torch\nimport data\ntorch.manual_seed(0)\n"}
    assert scan(tmp_path, files, "unseeded") == []


@pytest.mark.parametrize("code", [
    "from random import shuffle\nshuffle(x)\n",
    "from scipy.signal import resample\ny = resample(x, 100)\n",
    "from sklearn.model_selection import train_test_split\na, b = train_test_split(X, shuffle=False)\n",
    "import numpy as np\nfrom sklearn.cluster import KMeans\nnp.random.seed(0)\n"
    "def f():\n    return KMeans(3)\n",
    "from sklearn.model_selection import KFold\nseed_everything(42)\nKFold(5, shuffle=True)\n",
    "import numpy as np\nnp.random.seed(42)\n# np.random.seed(0)  # old value\n",
    "# we call np.random.seed(0) in main()\nx = 1\n",
])
def test_unseeded_near_misses(tmp_path, code):
    assert scan(tmp_path, {"a.py": code}, "unseeded") == []


def test_sklearn_shuffle_without_a_seed_is_flagged(tmp_path):
    code = "from sklearn.utils import shuffle\nX, y = shuffle(X, y)\n"
    assert kinds(scan(tmp_path, {"a.py": code}, "unseeded")) == ["seedable-call-without-seed"]


def test_a_commented_set_seed_in_r_does_not_seed(tmp_path):
    hits = scan(tmp_path, {"a.R": "# set.seed(1)\nx <- sample(1:10)\n"}, "unseeded")
    assert kinds(hits) == ["r-script-without-set-seed"]


@pytest.mark.parametrize("code", [
    "def f():\n    verbose, n = parse()\n    verbose = False\n    if verbose:\n        pass\n",
    "def f():\n    debug = False\n    if (debug := get()):\n        pass\n",
    "def f():\n    ok = False\n    with ctx() as ok:\n        pass\n    if ok:\n        pass\n",
    "def f():\n    e = False\n    try:\n        pass\n    except Exception as e:\n        pass\n"
    "    if e:\n        pass\n",
    "class C:\n    def __init__(self):\n        self.mode = 'RGB'\n"
    "def load(mode='RGB'):\n    mode = mode.lower()\n    if mode == 'rgb':\n        return 1\n",
])
def test_ignored_setting_near_misses(tmp_path, code):
    assert scan(tmp_path, {"a.py": code}, "ignored-setting") == []


def test_a_nested_function_condition_is_reported_once(tmp_path):
    code = "def outer():\n    def inner():\n        flag = True\n        if flag:\n            pass\n"
    assert kinds(scan(tmp_path, {"a.py": code}, "ignored-setting")) == ["constant-condition"]


@pytest.mark.parametrize("code,expected", [
    ("import pandas as pd\nfrom sklearn.model_selection import train_test_split\n"
     "d = df.resample('D').mean()\na, b = train_test_split(d, random_state=0)\n", []),
    ("from sklearn.model_selection import train_test_split\nd = df.sample(n=100, random_state=1)\n"
     "a, b = train_test_split(d, random_state=0)\n", []),
    ("from sklearn.model_selection import train_test_split\n"
     "d = df.sample(frac=2, replace=True, random_state=1)\na, b = train_test_split(d, random_state=0)\n",
     ["resample-before-split"]),
])
def test_resample_variants(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "resample-before-split")) == expected


@pytest.mark.parametrize("files", [
    {"a.py": "import h5py\nh5py.File('feats.h5', 'w')\nx = h5py.File('feats.h5')\n"},
    {"a.py": "pl.read_csv('out.csv')\n", "b.py": "frame.write_csv('out.csv')\n"},
    {"a.py": "x = np.load('m.npy')\n", "b.py": "Path('m.npy').write_bytes(b)\n"},
    {"tests/test_io.py": "x = pd.read_parquet('made_by_code.parquet')\n"},
])
def test_missing_producer_near_misses(tmp_path, files):
    assert scan(tmp_path, files, "missing-producer") == []


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0,
                    reason="root reads a file whatever its mode")
def test_an_unreadable_document_does_not_crash_the_run(tmp_path):
    root = tmp_path / "r0"
    root.mkdir()
    (root / "a.py").write_text("x = pd.read_csv('feats.csv')\n")
    (root / "NOTES.md").write_text("feats.csv\n")
    (root / "NOTES.md").chmod(0)
    try:
        hits = detect.run(root, {"missing-producer"})
    finally:
        (root / "NOTES.md").chmod(0o644)
    assert kinds(hits) == ["read-without-writer"]


@pytest.mark.parametrize("code", [
    "import re\nt = re.sub('[a-z0-9]', 'i', t)\n",          # a deliberate per-character mask
    "import re\nt = re.sub(r'ing\\b', '', t)\n",            # anchored at the word's end
    "import re\nt = re.sub('ou', 'ou', t)\n",                # rewrites nothing
    "import re\nt = re.sub('x*', '', t)\n",                  # matches the empty string
    "df['f'] = df['f'].str.replace('.', ',')\n",             # one character: literal in pandas < 2
])
def test_regex_near_misses(tmp_path, code):
    assert scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["house", "running"]) == []


def test_from_re_import_sub_and_positional_flags(tmp_path):
    import re as _re
    code = "from re import sub\nimport re\nt = sub('AND', ' & ', t, 0, re.IGNORECASE)\n"
    hits = scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["thousand"])
    assert kinds(hits) == ["rewrites-inside-words"]


@pytest.mark.parametrize("code,expected", [
    ("def f():\n    pass\nreturn 1\n", ["syntax-error"]),       # only compile() sees this
    ("x = '''\n%s rows\n'''\ny = (\n", ["syntax-error"]),       # not a magic: a string
    ("v = {}\nmatch v:\n    case {'a': 1, **rest}:\n        print(rest)\n", []),
    ("class C:\n    name = __qualname__\n", []),
    ("try:\n    pass\nexcept WindowsError:\n    pass\n", []),
    ("y = undefined_thing\nexec('undefined_thing = 1')\n", []),   # exec can define names
])
def test_cannot_run_cases(tmp_path, code, expected):
    hits = scan(tmp_path, {"a.py": code}, "cannot-run")
    assert kinds(hits) == expected
    if expected and "rows" in code:
        assert "magic" not in hits[0].message and hits[0].line == 4


def test_an_ipython_magic_is_named(tmp_path):
    (hit,) = scan(tmp_path, {"a.py": "x = 1\n%matplotlib inline\n"}, "cannot-run")
    assert hit.line == 2 and "IPython magic" in hit.message


def test_redefinition_used_in_between_is_not_called_dead(tmp_path):
    code = "def f():\n    return 1\nf()\ndef f():\n    return 2\n"
    (hit,) = scan(tmp_path, {"a.py": code}, "duplicate-code")
    assert "dead" not in hit.message and hit.evidence == "def f():"


def test_a_class_redefined_inside_a_class(tmp_path):
    code = "class A:\n    class B:\n        x = 1\n    class B:\n        x = 2\n"
    (hit,) = scan(tmp_path, {"a.py": code}, "duplicate-code")
    assert hit.kind == "redefined" and hit.evidence == "class B:"


def test_interface_methods_and_decorated_stubs_are_skipped(tmp_path):
    code = textwrap.dedent("""
    import abc
    class Base(abc.ABC):
        @abc.abstractmethod
        def run(self, cfg):
            return None
        @property
        def size(self, unused=None):
            return 1
    """)
    assert scan(tmp_path, {"a.py": code}, "ignored-setting") == []


def test_torch_training_needs_an_entry_point(tmp_path):
    code = "def step(loss):\n    loss.backward()\n"
    assert scan(tmp_path, {"lib.py": code}, "unseeded") == []


def test_an_argv_check_without_elif_is_not_a_dispatch(tmp_path):
    code = "import sys\nif len(sys.argv) > 1:\n    print(sys.argv[1])\n"
    assert scan(tmp_path, {"a.py": code}, "ignored-setting") == []


def test_untracked_files_named_by_the_manifest_are_scanned(tmp_path):
    import json
    from conftest import run
    root, d, manifest = scoped(tmp_path, {"a.py": "x = 1\n"})
    (root / "new.py").write_text("from sklearn.cluster import KMeans\nKMeans(3)\n")
    manifest["files"].append({"path": "new.py"})
    (d / "manifest.json").write_text(json.dumps(manifest))
    run("detect.py", "--manifest", d / "manifest.json", "--findings", d / "findings-static.json",
        cwd=root)
    doc = json.loads((d / "findings-static.json").read_text())
    assert [f["file"] for f in doc["findings"]] == ["new.py"]


def test_medium_hits_are_sent_as_speculative(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, {
        "a.py": "import pandas as pd\nfrom sklearn.model_selection import train_test_split\n"
                "df = pd.DataFrame()\nd = df.sample(frac=2, replace=True, random_state=1)\n"
                "a, b = train_test_split(d, random_state=0)\n"})
    (f,) = doc["findings"]
    assert f["detector_kind"] == "resample-before-split" and f["confidence"] == "speculative"


def test_undefined_names_in_one_file_are_one_finding(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, {
        "a.py": "x = alpha + 1\ny = beta + 2\nz = alpha\n", "b.py": "w = gamma\n"})
    by_file = {f["file"]: f for f in doc["findings"]}
    assert set(by_file) == {"a.py", "b.py"}
    assert "alpha, beta" in by_file["a.py"]["claim"] and by_file["a.py"]["lines"] == "1,2"
    assert by_file["b.py"]["claim"] == "'gamma' is used but never defined or imported in this file"


def test_two_kinds_on_the_same_line_are_one_finding(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, {
        "a.py": "from sklearn.feature_extraction.text import TfidfVectorizer\n"
                "train_txt, test_txt = [], []\n"
                "v = TfidfVectorizer()\na = v.fit_transform(train_txt)\n"
                "b = v.fit_transform(test_txt)\n"})
    (f,) = doc["findings"]
    assert sorted(f["detector_kinds"]) == ["fit-on-heldout", "refit-on-different-data"]
    assert f["lines"] == "5" and "held-out" in f["claim"] and "again" in f["claim"]


def test_distinct_messages_of_one_kind_stay_separate(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, {
        "a.py": "from sklearn.cluster import KMeans\nfrom sklearn.decomposition import "
                "TruncatedSVD\nk = KMeans(3)\ns = TruncatedSVD(2)\n"})
    assert len(doc["findings"]) == 2


# ---------------------------------------------------------------- the self-review's cases
# Each was a false positive or a false negative reproduced on the detectors, or an exemption
# that no test pinned (a mutation removing it left the suite green).

@pytest.mark.parametrize("code,expected", [
    # A numpy seed does not seed torch's generator.
    ("import numpy as np\nfrom torch.utils.data import DataLoader\nnp.random.seed(0)\n"
     "loader = DataLoader(ds, shuffle=True)\n", ["unseeded-dataloader"]),
    ("from torch.utils.data import DataLoader\nloader = DataLoader(ds, shuffle=True, generator=g)\n",
     []),
    # A docstring that mentions a seeding call does not seed.
    ('"""Call torch.manual_seed(0) before training."""\n'
     "from torch.utils.data import DataLoader\nloader = DataLoader(ds, shuffle=True)\n",
     ["unseeded-dataloader"]),
    # A variable whose name starts with "random" is not the random module.
    ("random_rows = df.copy()\nr = random_rows.sample(n=5)\n", ["seedable-call-without-seed"]),
])
def test_unseeded_review_cases(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "unseeded")) == expected


def test_a_library_import_does_not_link_a_local_module_of_the_same_name(tmp_path):
    files = {"train.py": "from torch.utils.data import DataLoader\n"
                         "loader = DataLoader(ds, shuffle=True)\n",
             "pkg/data.py": "import torch\ntorch.manual_seed(0)\n"}
    assert kinds(scan(tmp_path, files, "unseeded")) == ["unseeded-dataloader"]


@pytest.mark.parametrize("code,expected", [
    ("from sklearn.preprocessing import StandardScaler\ns = StandardScaler()\n"
     "X_train_val = s.fit_transform(X_train_val)\n", []),
    ("from sklearn.preprocessing import StandardScaler\nimport numpy as np\ns = StandardScaler()\n"
     "s.fit(np.concatenate([X_train, X_test]))\n", ["fit-on-train-and-test"]),
    ("from sklearn.preprocessing import Normalizer\nn = Normalizer()\n"
     "x_test = n.fit_transform(x_test)\n", []),
])
def test_fit_outside_fold_review_cases(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "fit-outside-fold")) == expected


def test_a_same_named_function_in_another_file_is_not_a_fitter(tmp_path):
    files = {"a/prep.py": "from sklearn.preprocessing import StandardScaler\n"
                          "def preprocess(X):\n    return StandardScaler().fit_transform(X)\n",
             "b/run.py": "from sklearn.model_selection import train_test_split\n"
                         "def preprocess(df):\n    return df.dropna()\n"
                         "X = preprocess(df)\na, b = train_test_split(X, random_state=0)\n"}
    assert scan(tmp_path, files, "fit-outside-fold") == []
    # Imported from the module that fits, the same call is a fit before the split.
    files["b/run.py"] = ("from sklearn.model_selection import train_test_split\n"
                         "from prep import preprocess as pp\n"
                         "X = pp(df)\na, b = train_test_split(X, random_state=0)\n")
    files["b/prep.py"] = files.pop("a/prep.py")
    assert kinds(scan(tmp_path, files, "fit-outside-fold")) == ["fit-before-split"]


@pytest.mark.parametrize("code,expected", [
    # Overwritten before the first read: the caller's value never matters.
    ("def train(data, mode='fast'):\n    mode = 'slow'\n    return run(data, mode)\n",
     ["parameter-overwritten"]),
    # A default filled in after a check reads the parameter first.
    ("def train(data, mode=None):\n    if mode is None:\n        mode = 'fast'\n"
     "    return run(data, mode)\n", []),
    # An override for one case is not the caller's value ignored.
    ("def train(data, mode, debug):\n    if debug:\n        mode = 'slow'\n"
     "    return run(data, mode)\n", []),
    # A nested function's assignment does not make the outer constant variable.
    ("def f():\n    flag = True\n    def g():\n        flag = compute()\n        return flag\n"
     "    if flag:\n        return 1\n", ["constant-condition"]),
])
def test_ignored_setting_review_cases(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "ignored-setting")) == expected


def test_an_elif_chain_on_argv_is_reported_once(tmp_path):
    code = ("import sys\nif sys.argv[1] == 'a':\n    f()\nelif sys.argv[1] == 'b':\n    g()\n"
            "elif sys.argv[1] == 'c':\n    h()\n")
    assert kinds(scan(tmp_path, {"a.py": code}, "ignored-setting")) == \
        ["argv-dispatch-without-else"]


def test_a_workflow_file_is_not_a_config_nobody_reads(tmp_path):
    files = {".github/workflows/ci.yml": "jobs:\n  test:\n    runs-on: ubuntu-latest\n",
             "a.py": "x = 1\n"}
    assert scan(tmp_path, files, "ignored-setting") == []


@pytest.mark.parametrize("files,expected", [
    # A documented input is not overridden by a writer of another file type.
    ({"a.py": "x = pd.read_csv('test_labels.csv')\ny.to_pickle('test.pkl')\n",
      "README.md": "Download test_labels.csv first.\n"}, []),
    ({"a.py": "x = pd.read_csv('feats.csv')\n",
      "b.py": "from pathlib import Path\nwith Path('feats.csv').open('w') as f:\n    f.write('a')\n"},
     []),
])
def test_missing_producer_review_cases(tmp_path, files, expected):
    assert kinds(scan(tmp_path, files, "missing-producer")) == expected


def test_overload_stubs_are_not_redefinitions(tmp_path):
    code = ("from typing import overload\n@overload\ndef f(x: int) -> int: ...\n"
            "@overload\ndef f(x: str) -> str: ...\ndef f(x):\n    return x\n")
    assert scan(tmp_path, {"a.py": code}, "duplicate-code") == []


@pytest.mark.parametrize("code,expected", [
    ("import yaml\nc = yaml.load(open('c.yml'), yaml.SafeLoader)\n", []),
])
def test_cannot_run_review_cases(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "cannot-run")) == expected


def test_a_syntax_error_names_the_interpreter(tmp_path):
    (h,) = scan(tmp_path, {"a.py": "x = f(a=1 b=2)\n"}, "cannot-run")
    assert f"a syntax error under Python {detect.THIS_PYTHON}" in h.message
    assert h.confidence == "high"


def test_syntax_a_newer_python_accepts_is_not_called_certain(tmp_path, monkeypatch):
    monkeypatch.setattr(detect, "_compiles_under", lambda text: "3.99")
    (h,) = scan(tmp_path, {"a.py": "x = f(a=1 b=2)\n"}, "cannot-run")
    assert "Python 3.99 compiles it" in h.message and h.confidence == "medium"


def test_compiles_under_asks_the_newer_interpreters(monkeypatch):
    import sys
    monkeypatch.setattr(detect, "_newer_pythons", lambda: [("3.99", sys.executable)])
    assert detect._compiles_under("x = 1\n") == "3.99"
    assert detect._compiles_under("x = (\n") is None


def test_a_file_too_deep_to_parse_is_held_back_not_a_crash(tmp_path, monkeypatch):
    import ast

    def deep(*a, **k):
        raise RecursionError("maximum recursion depth exceeded during compilation")

    class DeepAst:          # detect.py's view of the ast module only, not pytest's
        parse = staticmethod(deep)

        def __getattr__(self, name):
            return getattr(ast, name)
    monkeypatch.setattr(detect, "ast", DeepAst())
    monkeypatch.setattr(detect, "compile", deep, raising=False)
    hits = scan(tmp_path, {"a.py": "x = 1\n"}, "cannot-run")
    assert kinds(hits) == ["unparsed-file"] and "unparsed-file" not in detect.SENT


def test_a_long_generated_expression_does_not_crash_the_run(tmp_path):
    code = "x = " + " + ".join(["'a'"] * 5000) + "\n"
    assert set(kinds(scan(tmp_path, {"a.py": code}))) <= {"unparsed-file"}


@pytest.mark.parametrize("code,expected", [
    ("p = '~/.config/tool/settings.json'\n", []),
    ("name = 'x'\np = f'~/{name}'\n", []),
    ("name = 'x'\np = f'/home/alice/{name}.csv'\n", ["absolute-path"]),
])
def test_hard_coded_path_review_cases(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "hard-coded-path")) == expected


def test_two_paths_on_one_line_are_both_in_the_claim(tmp_path):
    root, d, manifest, doc = static_findings(tmp_path, {
        "a.py": "PATHS = ['/home/alice/a.csv', '/home/alice/b.csv']\n"})
    (f,) = doc["findings"]
    assert "a.csv" in f["claim"] and "b.csv" in f["claim"]


def test_an_unknown_detector_name_is_an_error(tmp_path):
    from conftest import run
    (tmp_path / "a.py").write_text("x = 1\n")
    r = run("detect.py", "--repo", tmp_path, "--only", "cannot_run")
    assert r.returncode == 2 and "unknown detector" in r.stderr
    r = run("detect.py", "--repo", tmp_path, "--only", "cannot-run")
    assert r.returncode == 0 and r.stdout.splitlines() == ["cannot-run: 0 hit(s)"]


def test_merged_kinds_take_the_judgment_fix_certainty():
    hs = [detect.Hit("ignored-setting", "case-mismatch-comparison", "a.py", 3, "m1", "q", "high"),
          detect.Hit("ignored-setting", "constant-condition", "a.py", 3, "m2", "q", "high")]
    (group,) = detect._group(hs)
    f = detect._finding(1, group, None)
    assert f["fix_certainty"] == "judgment" and f["claim"] == "m1; m2"
    assert sorted(f["detector_kinds"]) == ["case-mismatch-comparison", "constant-condition"]


def test_detect_and_fingerprint_number_lines_alike(tmp_path):
    import json
    from conftest import run
    root, d, manifest, doc = static_findings(tmp_path, {
        "a.py": "from sklearn.cluster import KMeans\n\x0c# section\nk = KMeans(3)\n"})
    p = d / "findings-static.json"
    r = run("review.py", "fingerprint", "--findings", p, "--manifest", d / "manifest.json")
    assert r.returncode == 0, r.stderr
    (f,) = json.loads(p.read_text())["findings"]
    assert f["line"] == 3 and f["fingerprint_anchor_line"] == "k = KMeans(3)"
