"""Clone detection: identical textbook solutions are weak evidence, distinctive structure is strong."""
import re

from app.analyzer.plagiarism_engine import (
    apply_collusion_corroboration,
    detect_cross_question_collusion,
    normalize_c_tokens,
    run_question_plagiarism_matrix,
)
from app.schemas import CloneMatch

TEXTBOOK = """#include <stdio.h>
int main() {
    int n, i, sum = 0;
    scanf("%d", &n);
    for (i = 1; i <= n; i++) {
        sum = sum + i;
    }
    printf("%d\\n", sum);
    return 0;
}
"""

QUIRKS = """    int checksum = 17;
    checksum = checksum * 3 + n;
    checksum = checksum ^ (n << 2);
    if (checksum > 1000) { checksum = checksum - 1000; }
    int spare = checksum + sum;
    printf("%d\\n", spare % 7);
"""


def rename(code, mapping):
    for old, new in mapping.items():
        code = re.sub(rf"\b{old}\b", new, code)
    return code


def textbook_variant(k):
    return rename(TEXTBOOK, {"n": f"n{k}", "i": f"i{k}", "sum": f"total{k}"})


def quirky(k):
    base = rename(TEXTBOOK, {"n": f"n{k}", "i": f"i{k}", "sum": f"total{k}"})
    extra = rename(QUIRKS, {"n": f"n{k}", "sum": f"total{k}", "checksum": f"chk{k}", "spare": f"sp{k}"})
    return base.replace("    return 0;", extra + "    return 0;")


def match_between(clone_map, a, b):
    return next((m for m in clone_map[a] if m.partner_roll_no == b), None)


def test_renaming_identifiers_does_not_change_the_canonical_tokens():
    a, _, _ = normalize_c_tokens(textbook_variant(1))
    b, _, _ = normalize_c_tokens(textbook_variant(2))
    assert a == b


def test_for_and_while_are_normalised_to_the_same_token():
    toks, _, _ = normalize_c_tokens("int main() { for (;;) {} while (1) {} }")
    assert toks.count("LOOP") == 2


def test_common_textbook_solution_is_weak_evidence():
    submissions = {f"s{k}": textbook_variant(k) for k in range(1, 7)}
    clone_map, _ = run_question_plagiarism_matrix("Q1", submissions)
    matches = [m for ms in clone_map.values() for m in ms]
    assert matches, "identical structures should still be listed for context"
    assert all(m.strength == "WEAK" for m in matches)
    assert all(m.cluster_size == 6 for m in matches)


def test_distinctive_shared_structure_is_strong():
    submissions = {f"s{k}": textbook_variant(k) for k in range(1, 6)}
    submissions["x1"] = quirky(11)
    submissions["x2"] = quirky(12)
    clone_map, _ = run_question_plagiarism_matrix("Q1", submissions)
    m = match_between(clone_map, "x1", "x2")
    assert m is not None and m.strength == "STRONG"
    assert m.exclusive_percent >= 20


def test_unrelated_programs_are_not_matched():
    other = "#include <stdio.h>\nint main() {\n    char s[20];\n    scanf(\"%s\", s);\n    int len = 0;\n    while (s[len]) { len = len + 1; }\n    printf(\"%d\\n\", len);\n    return 0;\n}\n"
    clone_map, _ = run_question_plagiarism_matrix("Q1", {"a": TEXTBOOK, "b": other})
    assert clone_map["a"] == [] and clone_map["b"] == []


def test_identical_comments_make_a_match_strong():
    commented = TEXTBOOK.replace("    int n,", "    // my own lucky trick for the sum, do not copy this\n    int n,")
    submissions = {f"s{k}": textbook_variant(k) for k in range(1, 5)}
    submissions["c1"] = rename(commented, {"n": "m1"})
    submissions["c2"] = rename(commented, {"n": "m2"})
    clone_map, _ = run_question_plagiarism_matrix("Q1", submissions)
    m = match_between(clone_map, "c1", "c2")
    assert m is not None and m.strength == "STRONG"


# ------------------------------------------------------------------ cross-question corroboration
def make_match(roll_partner, qid, sim=100.0, cluster=2, strength="WEAK"):
    return CloneMatch(partner_roll_no=roll_partner, question_id=qid, similarity_percent=sim, jaccard_percent=sim,
                      sequence_percent=sim, cluster_size=cluster, strength=strength)


def pair_maps(cluster, questions=("Q1", "Q2")):
    return {q: {"a": [make_match("b", q, cluster=cluster)], "b": [make_match("a", q, cluster=cluster)]} for q in questions}


def test_near_exact_pair_repeating_across_questions_is_corroborated():
    maps = pair_maps(cluster=2)
    assert apply_collusion_corroboration(maps, n_students=15) == 1
    assert all(m.strength == "STRONG" and m.corroborated for q in maps.values() for ms in q.values() for m in ms)
    pairs = detect_cross_question_collusion(maps)
    assert len(pairs) == 1 and (pairs[0].roll_a, pairs[0].roll_b) == ("a", "b") and pairs[0].shared_questions == ["Q1", "Q2"]


def test_matches_inside_large_clusters_do_not_corroborate():
    maps = pair_maps(cluster=9)          # everyone wrote it this way
    assert apply_collusion_corroboration(maps, n_students=15) == 0
    assert all(m.strength == "WEAK" for q in maps.values() for ms in q.values() for m in ms)
    assert detect_cross_question_collusion(maps) == []


def test_a_single_question_is_not_enough_for_corroboration():
    maps = pair_maps(cluster=2, questions=("Q1",))
    assert apply_collusion_corroboration(maps, n_students=15) == 0


def test_collusion_counts_only_strong_matches():
    maps = {"Q1": {"a": [make_match("b", "Q1", strength="STRONG")], "b": [make_match("a", "Q1", strength="STRONG")]},
            "Q2": {"a": [make_match("b", "Q2", strength="WEAK")], "b": [make_match("a", "Q2", strength="WEAK")]}}
    assert detect_cross_question_collusion(maps, min_shared_questions=2) == []


def test_prose_plagiarism_and_stylometric_shift_detection():
    from app.analyzer.plagiarism_engine import detect_stylometric_anomalies
    p1 = "To detect a cycle in a directed graph we run depth first search with a recursion stack tracking grey and black vertices in linear O(V+E) time."
    p2 = "To detect a cycle in a directed graph we run depth first search with a recursion stack tracking grey and black vertices in linear O(V+E) time."
    p3 = "Kahn topological sorting uses indegree counts and a queue of zero indegree nodes to order vertices."
    langs = {"a": "text", "b": "text", "c": "text"}
    cmap, _ = run_question_plagiarism_matrix("Q1", {"a": p1, "b": p2, "c": p3}, languages_by_roll=langs)
    m = match_between(cmap, "a", "b")
    assert m is not None and m.similarity_percent >= 95.0

    # Stylometric shift: Q1 uses tabs + snake_case + 0 comments; Q2 uses spaces + camelCase + heavy comments
    q1_code = "int main() {\n\tint my_val = 0;\n\tint total_sum = 1;\n\tfor (int idx_i = 0; idx_i < 10; idx_i++) {\n\t\ttotal_sum += idx_i;\n\t\tmy_val += total_sum;\n\t}\n\treturn my_val;\n}\n"
    q2_code = "// Step 1: initialize variables\n// Step 2: loop over items\n// Step 3: compute final result\n// Step 4: return value\nint main() {\n    int myVal = 0;\n    int totalSum = 1;\n    for (int idxI = 0; idxI < 10; idxI++) {\n        totalSum += idxI;\n        myVal += totalSum;\n    }\n    return myVal;\n}\n"
    notes, warnings = detect_stylometric_anomalies({"s1": {"Q1": {"code": q1_code, "language": "c"}, "Q2": {"code": q2_code, "language": "c"}}})
    assert ("s1", "Q1") in notes and warnings
