import os
import time

from piagent.llm import LlamaClient
from piagent.rag import DocIndex, chunk_text, extract_text


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def test_chunking_respects_size_and_overlap():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    chunks = chunk_text(text, size=400, overlap=80)
    assert len(chunks) > 5
    assert all(len(c) <= 400 for c in chunks)
    # consecutive chunks share some text
    assert any(chunks[i][-30:].split()[-1] in chunks[i + 1] for i in range(len(chunks) - 1))


def test_long_paragraph_is_split():
    chunks = chunk_text("x" * 50 + " " + "y" * 3000, size=500, overlap=50)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks).count("y") >= 3000


def test_html_extraction(tmp_path):
    p = tmp_path / "a.html"
    p.write_text("<html><style>.x{}</style><body><h1>Title</h1><p>Hello <b>world</b></p><script>bad()</script></body></html>")
    text = extract_text(str(p))
    assert "Hello world" in text and "bad()" not in text and ".x{}" not in text


def test_binary_files_are_skipped(tmp_path):
    p = tmp_path / "b.txt"
    p.write_bytes(b"abc\x00def")
    assert extract_text(str(p)) is None


def test_index_search_update_remove(cfg):
    docs = cfg.rag.docs_dir
    write(f"{docs}/garden.md", "# Garden\n\nTomatoes need sun and regular watering in summer.")
    write(f"{docs}/net/wifi.txt", "The office wifi network is called PiNet and uses WPA3.")
    write(f"{docs}/ignored.bin", "not indexed because of the extension")
    write(f"{docs}/.hidden.md", "hidden files are skipped")
    index = DocIndex(cfg.rag, LlamaClient(cfg.llm))

    stats = index.index()
    assert stats["added"] == 2 and stats["chunks"] == 2, stats

    hits = index.search("what is the wifi network called", k=2)
    assert hits[0]["path"] == "net/wifi.txt"
    assert "PiNet" in hits[0]["text"]
    assert hits[0]["score"] > hits[1]["score"]

    # unchanged files are skipped
    assert index.index()["unchanged"] == 2

    # modified file is re-embedded
    time.sleep(0.01)
    write(f"{docs}/garden.md", "# Garden\n\nCarrots grow well in sandy soil.")
    stats = index.index()
    assert stats["updated"] == 1 and stats["unchanged"] == 1
    assert index.search("sandy soil carrots", k=1)[0]["path"] == "garden.md"

    # deleted file is dropped
    os.remove(f"{docs}/net/wifi.txt")
    stats = index.index()
    assert stats["removed"] == 1
    assert index.stats()["documents"] == 1
    assert all(h["path"] != "net/wifi.txt" for h in index.search("wifi network", k=5))


def test_search_before_indexing_returns_nothing(cfg):
    index = DocIndex(cfg.rag, LlamaClient(cfg.llm))
    assert index.search("anything") == []
