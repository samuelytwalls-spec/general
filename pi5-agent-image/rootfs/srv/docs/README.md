# /srv/docs

Put documents here for PiAgent's document search (retrieval-augmented
generation). Plain text, Markdown, reStructuredText, HTML, CSV, JSON, YAML,
TOML, INI/conf, logs and source files are indexed. PDFs are indexed when
`pdftotext` is installed (`sudo apt install poppler-utils`).

- The index refreshes every 30 minutes and 3 minutes after boot. Only new or
  changed files are re-embedded.
- To index right away: `piagent index`
- To search without the agent: `piagent search "your question"`
- The agent uses the `search_docs` tool automatically when a question is
  about your documents, and names the files it used.

This directory is writable by the `piagent` group (your login user is a
member), so you can copy files in with `scp` or `rsync` as that user.

Embeddings come from bge-small-en-v1.5 (384 dimensions) running in
llama.cpp, and are stored with sqlite-vec in /var/lib/piagent/rag.db.
