from scripts.check_architecture_boundaries import find_violations


def test_architecture_boundaries_have_no_regressions() -> None:
    assert find_violations() == []
