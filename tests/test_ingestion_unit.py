"""Unit tests for core pure helpers in ingestion module."""

import sys
import types
import unittest


def _install_dependency_stubs() -> None:
    """Install minimal module stubs required to import `ingestion`."""
    chromadb_module = types.ModuleType("chromadb")
    chromadb_module.PersistentClient = object
    sys.modules.setdefault("chromadb", chromadb_module)

    chromadb_utils = types.ModuleType("chromadb.utils")
    sys.modules.setdefault("chromadb.utils", chromadb_utils)

    embedding_functions_module = types.ModuleType(
        "chromadb.utils.embedding_functions"
    )
    embedding_functions_module.OpenAIEmbeddingFunction = object
    sys.modules.setdefault(
        "chromadb.utils.embedding_functions", embedding_functions_module
    )

    dotenv_module = types.ModuleType("dotenv")
    dotenv_module.load_dotenv = lambda *args, **kwargs: None
    sys.modules.setdefault("dotenv", dotenv_module)

    neo4j_module = types.ModuleType("neo4j")
    neo4j_module.GraphDatabase = types.SimpleNamespace(
        driver=lambda *a, **k: None
    )
    neo4j_module.Driver = object
    sys.modules.setdefault("neo4j", neo4j_module)

    neo4j_exceptions = types.ModuleType("neo4j.exceptions")

    class _Neo4jException(Exception):
        pass

    neo4j_exceptions.AuthError = _Neo4jException
    neo4j_exceptions.Neo4jError = _Neo4jException
    neo4j_exceptions.ServiceUnavailable = _Neo4jException
    sys.modules.setdefault("neo4j.exceptions", neo4j_exceptions)

    github_loader_module = types.ModuleType("scripts.github_loader")
    github_loader_module.GitHubLoader = object
    sys.modules.setdefault("scripts.github_loader", github_loader_module)

    pypdf_module = types.ModuleType("pypdf")
    pypdf_module.PdfReader = object
    sys.modules.setdefault("pypdf", pypdf_module)

    pypdf_errors = types.ModuleType("pypdf.errors")

    class _PdfReadError(Exception):
        pass

    pypdf_errors.PdfReadError = _PdfReadError
    sys.modules.setdefault("pypdf.errors", pypdf_errors)

    openpyxl_module = types.ModuleType("openpyxl")
    openpyxl_module.load_workbook = lambda *args, **kwargs: None
    sys.modules.setdefault("openpyxl", openpyxl_module)

    openpyxl_utils = types.ModuleType("openpyxl.utils")
    sys.modules.setdefault("openpyxl.utils", openpyxl_utils)

    openpyxl_utils_exceptions = types.ModuleType("openpyxl.utils.exceptions")

    class _InvalidFileException(Exception):
        pass

    openpyxl_utils_exceptions.InvalidFileException = _InvalidFileException
    sys.modules.setdefault(
        "openpyxl.utils.exceptions",
        openpyxl_utils_exceptions,
    )


_install_dependency_stubs()

from ingestion import KDBIngestor


class TestKDBIngestorHelpers(unittest.TestCase):
    """Validate deterministic and edge-case behavior of chunking helpers."""

    def setUp(self) -> None:
        """Create a lightweight ingestor instance without external clients."""
        self.ingestor = KDBIngestor.__new__(KDBIngestor)
        self.ingestor.chunk_size = 10
        self.ingestor.chunk_overlap = 2
        self.ingestor.max_embedding_tokens = 2
        self.ingestor.sentence_window_size = 2
        self.ingestor.sentence_overlap = 1
        self.ingestor.paragraph_window_size = 2
        self.ingestor.paragraph_overlap = 1
        self.ingestor.code_line_window = 3
        self.ingestor.code_line_overlap = 1
        self.ingestor.chunk_strategy = "char_overlap"

    def test_normalize_id_removes_invalid_chars(self) -> None:
        """Normalize strips symbols and keeps a safe identifier."""
        value = self.ingestor._normalize_id("Repo Demo: versión/1")
        self.assertEqual(value, "Repo_Demo_versi_n_1")

    def test_estimate_tokens_for_empty_and_text(self) -> None:
        """Token estimation returns 0 for empty and >=1 for non-empty."""
        self.assertEqual(self.ingestor._estimate_tokens(""), 0)
        self.assertEqual(self.ingestor._estimate_tokens("abcd"), 1)
        self.assertEqual(self.ingestor._estimate_tokens("abcdefgh"), 2)

    def test_split_char_overlap_with_params(self) -> None:
        """Chunking with overlap should produce stepped windows."""
        chunks = self.ingestor._split_char_overlap_with_params(
            text="abcdefghij",
            chunk_size=4,
            chunk_overlap=2,
        )
        self.assertEqual(chunks, ["abcd", "cdef", "efgh", "ghij", "ij"])

    def test_enforce_embedding_token_limit_splits_long_text(self) -> None:
        """Long text should be split when exceeding embedding token cap."""
        parts = self.ingestor._enforce_embedding_token_limit("abcdefghijklm")
        self.assertGreater(len(parts), 1)

    def test_split_text_dispatch_sentence_window(self) -> None:
        """Dispatcher should route to sentence-window splitter."""
        self.ingestor.chunk_strategy = "sentence_window"
        text = "Uno. Dos. Tres."
        chunks = self.ingestor._split_text(text)
        self.assertEqual(chunks, ["Uno. Dos.", "Dos. Tres.", "Tres."])

    def test_split_text_fallback_to_char_overlap(self) -> None:
        """Unknown strategy should fallback to char-overlap chunking."""
        self.ingestor.chunk_strategy = "unknown"
        chunks = self.ingestor._split_text("abcdefghij")
        self.assertEqual(chunks, ["abcdefghij", "ij"])

    def test_chunk_documents_skips_empty_content(self) -> None:
        """Raw docs with empty page_content should be ignored safely."""
        docs = self.ingestor._chunk_documents(
            [
                {"page_content": "   ", "metadata": {"source": "a.txt"}},
                {"page_content": "", "metadata": {"source": "b.txt"}},
            ]
        )
        self.assertEqual(docs, [])

    def test_chunk_documents_sets_metadata_fields(self) -> None:
        """Chunking pipeline should enrich metadata and positions."""
        self.ingestor.chunk_strategy = "char_overlap"
        self.ingestor.max_embedding_tokens = 1000
        raw_docs = [
            {
                "page_content": "abcdefghij",
                "metadata": {"source": "doc1.txt", "file_type": "text"},
            }
        ]

        docs = self.ingestor._chunk_documents(
            raw_docs,
            collection_name="kdb_test",
        )

        self.assertTrue(docs)
        first = docs[0]
        metadata = first["metadata"]
        self.assertEqual(metadata["source"], "doc1.txt")
        self.assertEqual(metadata["collection"], "kdb_test")
        self.assertEqual(metadata["chunk_strategy"], "char_overlap")
        self.assertIn("graph_chunk_id", metadata)
        self.assertIn("parent_id", metadata)

    def test_chunk_documents_keeps_incremental_positions(self) -> None:
        """Chunks from same source must keep increasing position metadata."""
        self.ingestor.max_embedding_tokens = 1000
        raw_docs = [
            {
                "page_content": "abcdefghij",
                "metadata": {"source": "doc2.txt", "file_type": "text"},
            },
            {
                "page_content": "klmnopqrst",
                "metadata": {"source": "doc2.txt", "file_type": "text"},
            },
        ]

        docs = self.ingestor._chunk_documents(
            raw_docs,
            collection_name="kdb_test",
        )
        positions = [d["metadata"]["position"] for d in docs]

        self.assertTrue(positions)
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(positions[0], 0)


if __name__ == "__main__":
    unittest.main()
