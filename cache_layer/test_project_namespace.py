"""assert-based self-check for project_namespace() — pure function, but
importing memlayer still needs Redis/the embedding service up (same as
running the CLI itself). Run: python3 cache_layer/test_project_namespace.py"""
import os
import tempfile
import memlayer


def test_same_path_same_namespace():
    with tempfile.TemporaryDirectory() as d:
        assert memlayer.project_namespace(d) == memlayer.project_namespace(d)


def test_different_paths_different_namespace():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        assert memlayer.project_namespace(a) != memlayer.project_namespace(b)


def test_same_basename_different_parent_still_differs():
    """The exact bug scenario: two directories named the same thing in
    different places must not collide just because the human-readable
    prefix matches."""
    with tempfile.TemporaryDirectory() as root:
        a = os.path.join(root, "sub1", "api")
        b = os.path.join(root, "sub2", "api")
        os.makedirs(a)
        os.makedirs(b)
        assert memlayer.project_namespace(a) != memlayer.project_namespace(b)


def test_namespace_is_redis_key_safe():
    with tempfile.TemporaryDirectory() as root:
        weird = os.path.join(root, "My Project!! (v2)")
        os.makedirs(weird)
        ns = memlayer.project_namespace(weird)
        assert ns == memlayer._ns(ns), ns  # already normalized, _ns() is a no-op on it
        assert " " not in ns and ":" not in ns, ns


def test_relative_and_absolute_paths_to_same_dir_match():
    with tempfile.TemporaryDirectory() as d:
        cwd = os.getcwd()
        try:
            os.chdir(d)
            assert memlayer.project_namespace(".") == memlayer.project_namespace(d)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    test_same_path_same_namespace()
    test_different_paths_different_namespace()
    test_same_basename_different_parent_still_differs()
    test_namespace_is_redis_key_safe()
    test_relative_and_absolute_paths_to_same_dir_match()
    print("ok")
