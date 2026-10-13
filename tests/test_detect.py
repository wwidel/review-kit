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
     "a = v.fit_transform(q1)\nb = v.fit_transform(q2)\n", ["refit-on-different-data"]),
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
    ("class D:\n    def __init__(self, train_csv='t.csv'):\n        self.train_csv = 'train.csv'\n",
     ["parameter-overwritten"]),
    ("def f(a, threshold):\n    return a\n", ["parameter-unused"]),
    ("def f(a, **_):\n    return a\n", []),
    ("import torch\nclass M(torch.nn.Module):\n    def forward(self, x, labels=None):\n"
     "        return x\n", []),
    ("class C:\n    def __exit__(self, t, v, tb):\n        return False\n", []),
    ("def train():\n    for e in range(3):\n        warmup = True\n        if warmup and e < 2:\n"
     "            pass\n", ["constant-condition"]),
    ("class T:\n    def __init__(self):\n        self.space = 'RGB'\n    def f(self, x):\n"
     "        if self.space == 'rgb':\n            return x\n", ["case-mismatch-comparison"]),
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
    hits = scan(tmp_path, {"a.py": "x = pd.read_csv('x_train.csv')\n",
                           "b.py": "df.to_csv('x_train_kcore.csv')\n"}, "missing-producer")
    assert "x_train_kcore.csv instead" in hits[0].message and hits[0].confidence == "high"


@pytest.mark.parametrize("files", [
    {"a.py": "x = pd.read_csv('feats.csv')\n", "b.py": "df.to_csv('out/feats.csv')\n"},
    {"a.py": "x = pd.read_csv('train.csv')\n"},
    {"a.py": "x = pd.read_csv('data/v2.csv.gz')\n", "data/v2.csv.gz": b"\x1f\x8b"},
    {"a.py": "x = pd.read_csv('feats.csv')\n", "README.md": "Download feats.csv first.\n"},
    {"run.sh": "java -jar s.jar pred_file=preds.csv\n", "a.py": "p = pd.read_csv('preds.csv')\n"},
])
def test_inputs_that_are_accounted_for(tmp_path, files):
    assert scan(tmp_path, files, "missing-producer") == []


# ---------------------------------------------------------------- regex-corrupts-words

def test_a_rule_without_boundaries_rewrites_words(tmp_path):
    code = "import re\ntext = re.sub('U\\\\.?S\\\\.?A', ' America ', text, flags=re.IGNORECASE)\n"
    hits = scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["thousand", "the"])
    assert kinds(hits) == ["rewrites-inside-words"] and "thousand" in hits[0].message


def test_a_rule_with_boundaries_is_fine(tmp_path):
    code = "import re\ntext = re.sub(r'\\bUSA\\b', 'America', text)\n"
    assert scan(tmp_path, {"a.py": code}, "regex-corrupts-words", words=["thousand"]) == []


def test_pandas_replace_with_metacharacters_and_no_regex_flag(tmp_path):
    code = "df['f'] = df['f'].str.replace('pets ok.', 'pets')\n"
    assert kinds(scan(tmp_path, {"a.py": code}, "regex-corrupts-words")) == \
        ["str-replace-regex-default"]
    code = "df['f'] = df['f'].str.replace('pets ok.', 'pets', regex=False)\n"
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
    ("p = '~/Kaggle/input/train.json'\n", ["absolute-path"]),
    ("p = 'D:/python/data'\n", ["absolute-path"]),
    ("p = 'data/train.csv'\n", []),
    ("import os\nos.chdir('/home/me/proj')\n", ["absolute-path", "chdir-literal"]),
])
def test_hard_coded_path(tmp_path, code, expected):
    assert kinds(scan(tmp_path, {"a.py": code}, "hard-coded-path")) == expected


def test_a_worker_reading_a_global_set_only_under_main(tmp_path):
    code = """
    from multiprocessing import Pool
    DST = None
    def work(x):
        return DST + x
    def helper(x):
        return DST
    if __name__ == "__main__":
        DST = "out"
        Pool(2).map(work, [1])
    """
    hits = scan(tmp_path, {"a.py": code}, "hard-coded-path")
    assert [(h.kind, "work()" in h.message) for h in hits] == [("fork-only-global", True)]


def test_setwd_in_r(tmp_path):
    assert kinds(scan(tmp_path, {"a.R": "setwd('D:/kaggle/project')\n"}, "hard-coded-path")) == \
        ["absolute-path"]
