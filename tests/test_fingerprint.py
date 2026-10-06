"""A defect keeps its fingerprint while its code is unchanged, and only then (section 7)."""
import json

from conftest import finding, fingerprints, make_run


def fp_after(tmp_path, repo, content, line, rid, file="a.py"):
    (repo / "a.py").write_text(content)
    d = make_run(tmp_path, rid, repo, findings={"code": [finding("c1", file, line)]})
    return fingerprints(d, "code")[0]


BASE = "import numpy as np\nx = np.random.rand(3)\n"


def test_same_code_moved_down_keeps_its_fingerprint(tmp_path, repo):
    a = fp_after(tmp_path, repo, BASE, 2, "R1")
    b = fp_after(tmp_path, repo, "# a header\n\n" + BASE, 4, "R2")
    assert a == b


def test_whitespace_and_comments_do_not_change_it(tmp_path, repo):
    a = fp_after(tmp_path, repo, BASE, 2, "R1")
    b = fp_after(tmp_path, repo, "import numpy as np\nx  =  np.random.rand(3)   # TODO seed\n",
                 2, "R2")
    assert a == b


def test_changed_code_changes_it(tmp_path, repo):
    a = fp_after(tmp_path, repo, BASE, 2, "R1")
    b = fp_after(tmp_path, repo, "import numpy as np\nx = np.random.rand(4)\n", 2, "R2")
    assert a != b


def test_every_spelling_of_the_file_gives_one_fingerprint_and_one_spelling(tmp_path, repo):
    a = fp_after(tmp_path, repo, BASE, 2, "R1", file="a.py")
    b = fp_after(tmp_path, repo, BASE, 2, "R2", file="./a.py")
    c = fp_after(tmp_path, repo, BASE, 2, "R3", file=str(repo / "a.py"))
    assert a == b == c
    doc = json.loads((tmp_path / "R3" / "findings-code.json").read_text())
    assert doc["findings"][0]["file"] == "a.py"


def test_a_finding_without_a_line_gets_a_stable_claim_based_id(tmp_path, repo):
    def stamp(rid):
        d = make_run(tmp_path, rid, repo, findings={"code": [
            finding("c1", "a.py", None, claim="nothing in this module is seeded")]})
        doc = json.loads((d / "findings-code.json").read_text())["findings"][0]
        return doc["fingerprint"], doc["fingerprint_basis"], doc["fingerprint_anchor"]
    a, basis, anchor = stamp("R1")
    assert stamp("R2")[0] == a and basis == "claim-slug" and anchor == ""
