"""Manual ingestion verification tools, separate from application code.

``validate_ingestion.py`` calls ``backend.ingestion.service`` to parse a real
file and optionally export or persist its JSON. ``audit_extraction.py`` compares
a saved canonical JSON with native source text. The sibling ``test_*.py`` files
hold automated regression tests. Run these tools from the JLR project root.
``validate_corpus.py`` inventories the data folder, runs bounded validation
subprocesses, and records coverage, structural counts, failures and unsupported
formats under ``output/corpus-validation/``.
"""
