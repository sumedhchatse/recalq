"""assert-based self-check for knowledge-source sync (folder + git) into
the org knowledge base — needs Redis up, no LLM calls.
Run: python3 cache_layer/test_knowledge.py"""
import os
import time
import tempfile
import subprocess
import memlayer
import knowledge
import documents
import sop_layer
import guardrails

TAG = f"test-ks-{int(time.time())}"
knowledge.CLONE_DIR = tempfile.mkdtemp()


def _docs(name):
    return {d["filename"]: d for d in documents.list_documents(memlayer.r, sop_layer.SOP_NAMESPACE)
            if d["filename"].startswith(name + ":")}


def _sync(src):
    return knowledge.sync_source(src, memlayer.r, memlayer.embedder, guardrails)


def _cleanup(name):
    for d in _docs(name).values():
        documents.delete_document(memlayer.r, sop_layer.SOP_NAMESPACE, d["doc_id"])
    memlayer.r.delete(knowledge.STATE_KEY + name)


def test_folder_add_update_remove():
    name = TAG + "-dir"
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "ops"))
    os.makedirs(os.path.join(d, "node_modules"))
    open(os.path.join(d, "restart.md"), "w").write("To restart the zorblax service run zorblax-ctl restart.")
    open(os.path.join(d, "ops", "backup.txt"), "w").write("Backups of zorblax run nightly at 02:00.")
    open(os.path.join(d, "image.png"), "wb").write(b"\x89PNG")           # not included
    open(os.path.join(d, "node_modules", "x.md"), "w").write("vendored")  # skipped dir
    src = {"name": name, "path": d, "category": "sop"}
    try:
        c = _sync(src)
        assert (c["added"], c["updated"], c["removed"], c["errors"]) == (2, 0, 0, []), c
        docs = _docs(name)
        assert set(docs) == {f"{name}:restart.md", f"{name}:ops/backup.txt"}, docs
        assert docs[f"{name}:restart.md"]["category"] == "sop"

        assert _sync(src)["unchanged"] == 2  # nothing changed -> nothing re-embedded

        open(os.path.join(d, "restart.md"), "w").write("To restart zorblax use systemctl restart zorblax.")
        os.remove(os.path.join(d, "ops", "backup.txt"))
        c = _sync(src)
        assert (c["updated"], c["removed"], c["unchanged"]) == (1, 1, 0), c
        assert set(_docs(name)) == {f"{name}:restart.md"}  # one version, not two

        hits = documents.find_relevant_chunks(memlayer.r, memlayer.embedder, None,
                                              "how do I restart zorblax", sop_layer.SOP_NAMESPACE)
        assert any("systemctl restart zorblax" in h["text"] for h in hits), hits
    finally:
        _cleanup(name)


def test_git_source_clones_then_pulls():
    name = TAG + "-git"
    repo = tempfile.mkdtemp()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    open(os.path.join(repo, "wiki.md"), "w").write("The quux cluster lives in rack 7.")
    git("add", "-A"); git("commit", "-qm", "one")
    src = {"name": name, "git": repo}
    try:
        assert _sync(src)["added"] == 1
        open(os.path.join(repo, "faq.md"), "w").write("Quux on-call rotates weekly.")
        git("add", "-A"); git("commit", "-qm", "two")
        c = _sync(src)
        assert (c["added"], c["unchanged"]) == (1, 1), c
    finally:
        _cleanup(name)


def test_sync_all_report_and_unknown_source():
    cfg = os.path.join(tempfile.mkdtemp(), "client.yaml")
    d = tempfile.mkdtemp()
    open(os.path.join(d, "a.md"), "w").write("alpha")
    name = TAG + "-cfg"
    os.makedirs(os.path.join(os.path.dirname(cfg), "docs"))
    open(os.path.join(os.path.dirname(cfg), "docs", "b.md"), "w").write("beta")
    open(cfg, "w").write(f"knowledge_sources:\n  - name: {name}\n    path: {d}\n"
                         f"  - name: {name}-rel\n    path: docs\n"
                         f"  - name: {TAG}-missing\n    path: /nonexistent/dir\n")
    try:
        out = knowledge.sync_all(cfg, memlayer.r, memlayer.embedder, guardrails)
        assert f"{name}: +1 new" in out and f"{TAG}-missing: failed" in out, out
        assert f"{name}-rel: +1 new" in out, out  # relative path = next to client.yaml
        assert "No knowledge source named" in knowledge.sync_all(cfg, memlayer.r, memlayer.embedder,
                                                                 guardrails, only="nope")
    finally:
        _cleanup(name)
        _cleanup(name + "-rel")


def test_markdown_chunks_by_section():
    md = ("# Ops\nintro\n\n## Backups\nrun backup.sh nightly\n```\n# not a heading\n```\n"
          "### Restore\nrun restore.sh\n## Logs\ntail the log\n")
    chunks = documents.chunk_markdown(md)
    assert chunks[0] == "Ops\nintro", chunks
    assert chunks[1].startswith("Ops > Backups\n") and "# not a heading" in chunks[1], chunks
    assert chunks[2] == "Ops > Backups > Restore\nrun restore.sh", chunks
    assert chunks[3] == "Ops > Logs\ntail the log", chunks  # deeper heading popped


def test_keyword_boost_ignores_common_words():
    texts = ["recalq restore the backup", "recalq intro", "recalq logs", "recalq tests"]
    boost = documents._keyword_boost("how do I restore a recalq backup", texts)
    assert boost[0] == documents.KEYWORD_WEIGHT and all(b == 0 for b in boost[1:]), boost


if __name__ == "__main__":
    test_markdown_chunks_by_section()
    test_keyword_boost_ignores_common_words()
    test_folder_add_update_remove()
    test_git_source_clones_then_pulls()
    test_sync_all_report_and_unknown_source()
    print("ok")
