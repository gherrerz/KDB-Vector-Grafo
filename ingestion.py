"""Ingesta de documentos a ChromaDB y, opcionalmente, a Neo4j."""

import os
import re
import uuid
import logging
from typing import Any, Callable
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j import Driver
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable
from scripts.github_loader import GitHubLoader

# simple loaders to avoid langchain_community
from pypdf import PdfReader
from pypdf.errors import PdfReadError
import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

# Cargar variables de entorno (API KEY)
load_dotenv()


LOGGER = logging.getLogger(__name__)


class KDBIngestor:
    """Ingestor principal para indexación vectorial y estructural."""

    def __init__(
        self,
        data_path: str,
        db_path: str,
        chroma_client: Any | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """
        Inicializa el ingestor de la base de conocimientos.
        data_path: Carpeta donde están los documentos fuente.
        db_path: Carpeta donde se guardará la base vectorial.
        chroma_client: Cliente ChromaDB existente (opcional).
        Si no se proporciona, se crea uno nuevo.
        """
        self.data_path = data_path
        self.db_path = db_path
        self.progress_callback = progress_callback
        # Usar cliente existente si se proporciona, sino crear uno nuevo
        if chroma_client is not None:
            self.client = chroma_client
        else:
            self.client = chromadb.PersistentClient(path=self.db_path)
        self.embedding_fn = embedding_functions.OpenAIEmbeddingFunction(
            api_key=os.getenv("OPENAI_API_KEY"),
            model_name="text-embedding-3-small"
        )
        # Modo multi-colección (small / large / code)
        self.enable_multi_collection = True
        # self.enable_multi_collection = False

        self.collection_profiles = [
            {
                "name": "kdb_small",
                "strategy": "sentence_window",
                "chunk_size": 700,
                "chunk_overlap": 120,
                "sentence_window_size": 3,
                "sentence_overlap": 1,
                "for_graph": False
            },
            {
                "name": "kdb_large",
                "strategy": "char_overlap",
                "chunk_size": 1800,
                "chunk_overlap": 220,
                "for_graph": True
            },
            {
                "name": "kdb_code",
                "strategy": "code_aware",
                "chunk_size": 1400,
                "chunk_overlap": 200,
                "code_line_window": 120,
                "code_line_overlap": 30,
                "for_graph": False
            }
        ]

        # Configuración de chunking (elige UNA estrategia descomentando una
        # línea)
        # self.chunk_strategy = "char_overlap"
        # self.chunk_strategy = "sentence_window"
        # self.chunk_strategy = "paragraph_window"
        # self.chunk_strategy = "heading_window"
        self.chunk_strategy = "code_aware"

        # Parámetros generales de chunking
        self.chunk_size = 1000
        self.chunk_overlap = 200
        self.sentence_window_size = 6
        self.sentence_overlap = 2
        self.paragraph_window_size = 3
        self.paragraph_overlap = 1
        self.code_line_window = 80
        self.code_line_overlap = 20

        self.max_batch_tokens = 7000
        self.max_batch_items = 100
        self.max_embedding_tokens = 8000
        self.max_batch_chars = 18000
        self.max_embedding_chars = 12000

        self.text_extensions = {
            ".txt",
            ".md",
            ".markdown",
            ".rst",
            ".log",
            ".csv",
            ".json",
            ".yaml",
            ".yml",
            ".xml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".properties",
            ".gradle",
            ".kts",
            ".html",
            ".css",
            ".scss",
            ".less",
        }
        self.code_extensions = {
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".java",
            ".cs",
            ".go",
            ".rs",
            ".cpp",
            ".c",
            ".h",
            ".hpp",
            ".php",
            ".rb",
            ".kt",
            ".swift",
            ".scala",
            ".sql",
            ".sh",
            ".ps1",
            ".bat",
            ".vue",
            ".svelte",
            ".dart",
            ".r",
            ".m",
            ".mm",
        }
        self.special_text_filenames = {
            "dockerfile",
            "makefile",
            "pom.xml",
            "build.gradle",
            "settings.gradle",
            "gradle.properties",
            "gradlew",
            "gradlew.bat",
        }

        self.neo4j_uri = os.getenv("NEO4J_URI", "")
        self.neo4j_user = os.getenv("NEO4J_USER", "")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "")
        self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.neo4j_driver = self._init_neo4j_driver()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        """Emite eventos de progreso hacia la UI si hay callback."""
        if not self.progress_callback:
            return
        try:
            data = {"event": event, **payload}
            self.progress_callback(data)
        except (TypeError, ValueError, RuntimeError) as exc:
            LOGGER.debug("No se pudo emitir progreso (%s): %s", event, exc)

    def _is_bad_request_error(self, exc: Exception) -> bool:
        """Detecta BadRequestError sin depender del import directo."""
        return exc.__class__.__name__ == "BadRequestError"

    def _init_neo4j_driver(self) -> Driver | None:
        """Inicializa y valida la conexión de Neo4j."""
        if not (self.neo4j_uri and self.neo4j_user and self.neo4j_password):
            LOGGER.info(
                "ℹ️ Neo4j no configurado. Se ejecutará "
                "solo indexación vectorial."
            )
            return None
        try:
            driver = GraphDatabase.driver(
                self.neo4j_uri,
                auth=(self.neo4j_user, self.neo4j_password)
            )
            with driver.session(database=self.neo4j_database) as session:
                session.run("RETURN 1")
            LOGGER.info("✅ Conexión con Neo4j establecida.")
            return driver
        except (AuthError, ServiceUnavailable, Neo4jError) as exc:
            LOGGER.warning("⚠️ No se pudo conectar a Neo4j: %s", exc)
            return None

    def _normalize_id(self, text: str) -> str:
        """Normaliza texto para usarlo como prefijo de identificadores."""
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", text)
        return normalized.strip("_")[:120] or "documento"

    def _index_graph(self, docs: list[dict[str, Any]]) -> None:
        """Indexa documentos y relaciones de continuidad en Neo4j."""
        if not self.neo4j_driver:
            return

        by_source = {}
        for item in docs:
            source = item.get("metadata", {}).get("source", "desconocido")
            by_source.setdefault(source, []).append(item)

        try:
            with self.neo4j_driver.session(
                database=self.neo4j_database
            ) as session:
                session.run(
                    "CREATE CONSTRAINT document_name_unique IF NOT EXISTS "
                    "FOR (d:Document) REQUIRE d.name IS UNIQUE"
                )
                session.run(
                    "CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS "
                    "FOR (c:Chunk) REQUIRE c.id IS UNIQUE"
                )

                for source, source_docs in by_source.items():
                    source_docs = sorted(
                        source_docs,
                        key=lambda x: x.get("metadata", {}).get("position", 0)
                    )

                    chunks_payload = []
                    for d in source_docs:
                        metadata = d.get("metadata", {})
                        chunks_payload.append({
                            "id": metadata.get("graph_chunk_id"),
                            "text": d.get("page_content", ""),
                            "position": metadata.get("position", 0)
                        })

                    rels_payload = []
                    for i in range(len(chunks_payload) - 1):
                        rels_payload.append({
                            "from_id": chunks_payload[i]["id"],
                            "to_id": chunks_payload[i + 1]["id"]
                        })

                    session.run(
                        "MATCH (d:Document {name: $source}) DETACH DELETE d",
                        source=source
                    )

                    session.run(
                        "MERGE (d:Document {name: $source})",
                        source=source
                    )

                    session.run(
                        """
                        UNWIND $rows AS row
                        MATCH (d:Document {name: $source})
                        MERGE (c:Chunk {id: row.id})
                        SET c.text = row.text,
                            c.position = row.position,
                            c.source = $source
                        MERGE (d)-[:HAS_CHUNK]->(c)
                        """,
                        source=source,
                        rows=chunks_payload
                    )

                    if rels_payload:
                        session.run(
                            """
                            UNWIND $rels AS rel
                            MATCH (c1:Chunk {id: rel.from_id})
                            MATCH (c2:Chunk {id: rel.to_id})
                            MERGE (c1)-[:NEXT]->(c2)
                            """,
                            rels=rels_payload
                        )
            LOGGER.info("✅ Indexación estructural en Neo4j completada.")
        except Neo4jError as exc:
            LOGGER.warning("⚠️ Error indexando en Neo4j: %s", exc)

    def close(self) -> None:
        """Cierra conexiones abiertas de recursos externos."""
        if self.neo4j_driver:
            self.neo4j_driver.close()

    def _estimate_tokens(self, text: str) -> int:
        """Estima tokens de forma conservadora según longitud."""
        if not text:
            return 0
        # Estimación conservadora (aprox. 1 token cada 4 caracteres)
        return max(1, len(text) // 4)

    def _split_char_overlap_with_params(
            self,
            text: str,
            chunk_size: int,
            chunk_overlap: int) -> list[str]:
        """Fragmenta texto por tamaño fijo con solapamiento configurable."""
        if not text:
            return []
        chunks = []
        start = 0
        safe_chunk_size = max(1, chunk_size)
        safe_overlap = max(0, min(chunk_overlap, safe_chunk_size - 1))
        step = max(1, safe_chunk_size - safe_overlap)
        while start < len(text):
            end = min(len(text), start + safe_chunk_size)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start += step
        return chunks

    def _enforce_embedding_token_limit(self, text: str) -> list[str]:
        """Garantiza que un fragmento no supere el límite estimado."""
        if not text:
            return []
        max_embedding_chars = getattr(
            self,
            "max_embedding_chars",
            self.max_embedding_tokens * 4,
        )
        if (
            self._estimate_tokens(text) <= self.max_embedding_tokens
            and len(text) <= max_embedding_chars
        ):
            return [text]

        max_chars = min(self.max_embedding_tokens * 4, max_embedding_chars)
        safe_overlap = min(self.chunk_overlap, max_chars // 5)
        return self._split_char_overlap_with_params(
            text, max_chars, safe_overlap)

    def _upsert_in_batches(
        self,
        collection: Any,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        """Realiza upsert en lotes para evitar límites de token/tamaño."""
        batch_texts = []
        batch_metadatas = []
        batch_ids = []
        batch_tokens = 0
        batch_chars = 0
        total = len(texts)
        inserted = 0
        failed = 0

        self._emit_progress(
            "upsert_start",
            total_chunks=total,
            max_batch_tokens=self.max_batch_tokens,
            max_batch_chars=self.max_batch_chars,
            max_embedding_tokens=self.max_embedding_tokens,
            max_embedding_chars=self.max_embedding_chars,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )

        def upsert_with_retry(
            docs: list[str],
            metas: list[dict[str, Any]],
            doc_ids: list[str],
            depth: int = 0,
        ) -> tuple[int, int]:
            """Inserta lote y reintenta dividiendo cuando hay BadRequestError."""
            if not docs:
                return 0, 0
            try:
                collection.upsert(documents=docs, metadatas=metas, ids=doc_ids)
                return len(docs), 0
            except Exception as exc:  # pragma: no cover - depende de API externa
                if self._is_bad_request_error(exc) and len(docs) > 1 and depth < 6:
                    mid = len(docs) // 2
                    self._emit_progress(
                        "upsert_split_batch",
                        batch_size=len(docs),
                        depth=depth,
                        reason="BadRequestError",
                    )
                    ok_left, fail_left = upsert_with_retry(
                        docs[:mid],
                        metas[:mid],
                        doc_ids[:mid],
                        depth + 1,
                    )
                    ok_right, fail_right = upsert_with_retry(
                        docs[mid:],
                        metas[mid:],
                        doc_ids[mid:],
                        depth + 1,
                    )
                    return ok_left + ok_right, fail_left + fail_right

                if self._is_bad_request_error(exc):
                    self._emit_progress(
                        "upsert_bad_request",
                        batch_size=len(docs),
                        depth=depth,
                        error=str(exc),
                        sample_id=doc_ids[0] if doc_ids else "",
                    )
                else:
                    self._emit_progress(
                        "upsert_error",
                        batch_size=len(docs),
                        depth=depth,
                        error=str(exc),
                    )
                LOGGER.warning("⚠️ Error en upsert de lote: %s", exc)
                return 0, len(docs)

        def flush_batch() -> None:
            """Envía el lote acumulado al vector store y reinicia buffers."""
            nonlocal batch_texts, batch_metadatas, batch_ids
            nonlocal batch_tokens, batch_chars, inserted, failed
            if not batch_texts:
                return
            current_batch_size = len(batch_texts)
            current_batch_tokens = batch_tokens
            current_batch_chars = batch_chars
            ok_count, fail_count = upsert_with_retry(
                batch_texts,
                batch_metadatas,
                batch_ids,
            )
            inserted += ok_count
            failed += fail_count
            LOGGER.info(
                "   ↳ Upsert batch procesado: %s OK, %s fallo "
                "(acumulado %s/%s)",
                ok_count,
                fail_count,
                inserted,
                total,
            )
            self._emit_progress(
                "upsert_batch_result",
                batch_size=current_batch_size,
                batch_tokens=current_batch_tokens,
                batch_chars=current_batch_chars,
                inserted=inserted,
                failed=failed,
                total_chunks=total,
                progress=(inserted + failed) / max(1, total),
            )
            batch_texts = []
            batch_metadatas = []
            batch_ids = []
            batch_tokens = 0
            batch_chars = 0

        def append_item(
            text: str,
            metadata: dict[str, Any],
            doc_id: str,
        ) -> None:
            """Agrega un ítem al lote; si excede tamaño lo divide."""
            nonlocal batch_tokens
            nonlocal batch_chars
            item_tokens = self._estimate_tokens(text)
            item_chars = len(text)
            max_embedding_chars = getattr(
                self,
                "max_embedding_chars",
                self.max_embedding_tokens * 4,
            )

            if item_chars > max_embedding_chars:
                safe_overlap = min(self.chunk_overlap, max_embedding_chars // 5)
                pieces = self._split_char_overlap_with_params(
                    text,
                    max_embedding_chars,
                    safe_overlap,
                )
                LOGGER.info(
                    "↳ Chunk largo dividido por caracteres en %s partes. "
                    "id=%s",
                    len(pieces),
                    doc_id,
                )
                self._emit_progress(
                    "chunk_split_chars",
                    original_chars=item_chars,
                    parts=len(pieces),
                    source=metadata.get("source", ""),
                    doc_id=doc_id,
                )
                for idx, piece in enumerate(pieces):
                    piece_id = f"{doc_id}::char_part::{idx}"
                    append_item(piece, metadata, piece_id)
                return

            if item_tokens > self.max_batch_tokens:
                max_chars = min(self.max_batch_tokens * 3, max_embedding_chars)
                safe_overlap = min(self.chunk_overlap, max_chars // 5)
                pieces = self._split_char_overlap_with_params(
                    text,
                    max_chars,
                    safe_overlap,
                )
                LOGGER.info(
                    "↳ Chunk grande dividido en %s partes. id=%s",
                    len(pieces),
                    doc_id,
                )
                self._emit_progress(
                    "chunk_split_tokens",
                    original_tokens=item_tokens,
                    parts=len(pieces),
                    source=metadata.get("source", ""),
                    doc_id=doc_id,
                )
                for idx, piece in enumerate(pieces):
                    piece_id = f"{doc_id}::part::{idx}"
                    append_item(piece, metadata, piece_id)
                return

            would_exceed_tokens = (
                batch_tokens + item_tokens) > self.max_batch_tokens
            would_exceed_items = len(batch_texts) >= self.max_batch_items
            max_batch_chars = getattr(self, "max_batch_chars", 18000)
            would_exceed_chars = (
                batch_chars + item_chars) > max_batch_chars

            if would_exceed_tokens or would_exceed_items or would_exceed_chars:
                flush_batch()

            batch_texts.append(text)
            batch_metadatas.append(metadata)
            batch_ids.append(doc_id)
            batch_tokens += item_tokens
            batch_chars += item_chars

        for text, metadata, doc_id in zip(texts, metadatas, ids):
            append_item(text, metadata, doc_id)

        flush_batch()
        self._emit_progress(
            "upsert_end",
            inserted=inserted,
            failed=failed,
            total_chunks=total,
        )

    def _get_chunking_state(self) -> dict[str, int | str]:
        """Devuelve snapshot de la configuración actual de chunking."""
        return {
            "chunk_strategy": self.chunk_strategy,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "sentence_window_size": self.sentence_window_size,
            "sentence_overlap": self.sentence_overlap,
            "paragraph_window_size": self.paragraph_window_size,
            "paragraph_overlap": self.paragraph_overlap,
            "code_line_window": self.code_line_window,
            "code_line_overlap": self.code_line_overlap,
        }

    def _restore_chunking_state(self, state: dict[str, Any]) -> None:
        """Restaura configuración de chunking desde un snapshot previo."""
        self.chunk_strategy = state.get("chunk_strategy", self.chunk_strategy)
        self.chunk_size = state.get("chunk_size", self.chunk_size)
        self.chunk_overlap = state.get("chunk_overlap", self.chunk_overlap)
        self.sentence_window_size = state.get(
            "sentence_window_size", self.sentence_window_size)
        self.sentence_overlap = state.get(
            "sentence_overlap", self.sentence_overlap)
        self.paragraph_window_size = state.get(
            "paragraph_window_size", self.paragraph_window_size)
        self.paragraph_overlap = state.get(
            "paragraph_overlap", self.paragraph_overlap)
        self.code_line_window = state.get(
            "code_line_window", self.code_line_window)
        self.code_line_overlap = state.get(
            "code_line_overlap", self.code_line_overlap)

    def _apply_profile_settings(self, profile: dict[str, Any]) -> None:
        """Aplica parámetros de un perfil de colección al estado actual."""
        self.chunk_strategy = profile.get("strategy", self.chunk_strategy)
        for key in [
            "chunk_size",
            "chunk_overlap",
            "sentence_window_size",
            "sentence_overlap",
            "paragraph_window_size",
            "paragraph_overlap",
            "code_line_window",
                "code_line_overlap"]:
            if key in profile:
                setattr(self, key, profile[key])

    def _build_vector_id(self, metadata: dict[str, Any], text: str) -> str:
        """Construye ID determinístico por fuente, estrategia y posición."""
        source = metadata.get("source", "desconocido")
        strategy = metadata.get("chunk_strategy", "unknown")
        position = metadata.get("position", 0)
        fingerprint = f"{source}|{strategy}|{position}|{text[:120]}"
        return uuid.uuid5(uuid.NAMESPACE_URL, fingerprint).hex

    def _split_char_overlap(self, text: str) -> list[str]:
        """Fragmenta con la configuración global de tamaño y overlap."""
        return self._split_char_overlap_with_params(
            text, self.chunk_size, self.chunk_overlap)

    def _split_sentence_window(self, text: str) -> list[str]:
        """Agrupa oraciones en ventanas con solapamiento entre ventanas."""
        if not text:
            return []
        sentences = [s.strip() for s in re.split(
            r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return self._split_char_overlap(text)
        chunks = []
        i = 0
        step = max(1, self.sentence_window_size - self.sentence_overlap)
        while i < len(sentences):
            chunk = " ".join(
                sentences[i:i + self.sentence_window_size]).strip()
            if chunk:
                chunks.append(chunk)
            i += step
        return chunks

    def _split_paragraph_window(self, text: str) -> list[str]:
        """Agrupa párrafos en ventanas con solapamiento entre grupos."""
        if not text:
            return []
        paragraphs = [p.strip()
                      for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if not paragraphs:
            return self._split_char_overlap(text)
        chunks = []
        i = 0
        step = max(1, self.paragraph_window_size - self.paragraph_overlap)
        while i < len(paragraphs):
            chunk = "\n\n".join(
                paragraphs[i:i + self.paragraph_window_size]).strip()
            if chunk:
                chunks.append(chunk)
            i += step
        return chunks

    def _split_heading_window(self, text: str) -> list[str]:
        """Segmenta por encabezados y ajusta tamaño máximo por fragmento."""
        if not text:
            return []
        heading_pattern = (
            r"(?m)(?=^\s{0,3}#{1,6}\s+|^\s*\d+[\.)]\s+|"
            r"^[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{8,}$)"
        )
        sections = [s.strip() for s in re.split(
            heading_pattern, text) if s.strip()]
        if len(sections) <= 1:
            return self._split_paragraph_window(text)

        chunks = []
        current = ""
        for section in sections:
            candidate = (current + "\n\n" +
                         section).strip() if current else section
            if len(candidate) <= self.chunk_size:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                if len(section) <= self.chunk_size:
                    current = section
                else:
                    chunks.extend(self._split_char_overlap(section))
                    current = ""
        if current:
            chunks.append(current)
        return chunks

    def _split_code_aware(self, text: str) -> list[str]:
        """Segmenta código respetando fronteras sintácticas y tamaño máximo."""
        if not text:
            return []
        lines = text.splitlines()
        if not lines:
            return []

        code_boundary_pattern = re.compile(
            r"^\s*(def\s+|class\s+|async\s+def\s+|function\s+|"
            r"async\s+function\s+|public\s+|private\s+|protected\s+|"
            r"interface\s+|enum\s+|struct\s+|"
            r"impl\s+|fn\s+|export\s+class\s+|export\s+function\s+|"
            r"if\s+__name__\s*==\s*['\"]__main__['\"])")

        boundaries = [0]
        for idx, line in enumerate(lines[1:], start=1):
            if code_boundary_pattern.search(line):
                boundaries.append(idx)
        boundaries.append(len(lines))

        blocks = []
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            block_text = "\n".join(lines[start:end]).strip()
            if block_text:
                blocks.append(block_text)

        if not blocks:
            blocks = [text]

        chunks = []
        for block in blocks:
            if len(block) <= self.chunk_size:
                chunks.append(block)
                continue

            block_lines = block.splitlines()
            line_start = 0
            line_step = max(1, self.code_line_window - self.code_line_overlap)
            while line_start < len(block_lines):
                line_end = min(len(block_lines), line_start +
                               self.code_line_window)
                piece = "\n".join(block_lines[line_start:line_end]).strip()
                if piece:
                    if len(piece) <= self.chunk_size:
                        chunks.append(piece)
                    else:
                        chunks.extend(self._split_char_overlap(piece))
                line_start += line_step
        return chunks

    def _split_text_with_strategy(self, text: str, strategy: str) -> list[str]:
        """Aplica una estrategia de chunking explícita al texto."""
        if strategy == "sentence_window":
            return self._split_sentence_window(text)
        if strategy == "paragraph_window":
            return self._split_paragraph_window(text)
        if strategy == "heading_window":
            return self._split_heading_window(text)
        if strategy == "code_aware":
            return self._split_code_aware(text)
        return self._split_char_overlap(text)

    def _split_text(self, text: str) -> list[str]:
        """Despacha el texto al algoritmo de chunking configurado."""
        return self._split_text_with_strategy(text, self.chunk_strategy)

    def _should_include_document_in_collection(
        self,
        metadata: dict[str, Any],
        collection_name: str,
    ) -> bool:
        """Define si un documento debe indexarse en la colección destino."""
        file_type = (metadata.get("file_type", "") or "").lower()
        if collection_name == "kdb_code":
            return file_type == "code"
        return True

    def _resolve_dynamic_chunk_strategy(
        self,
        metadata: dict[str, Any],
        collection_name: str,
    ) -> str:
        """Selecciona estrategia de chunking dinámica por tipo de documento."""
        base_strategy = self.chunk_strategy
        file_type = (metadata.get("file_type", "") or "").lower()
        source = (metadata.get("source", "") or "").lower()
        ext = os.path.splitext(source)[1]

        if collection_name == "kdb_code" or file_type == "code":
            return "code_aware"

        if file_type == "pdf":
            return "sentence_window"

        if file_type == "excel":
            return "paragraph_window"

        if ext in {".md", ".markdown", ".rst"}:
            return "heading_window"

        if ext in {
            ".json",
            ".yaml",
            ".yml",
            ".xml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".properties",
        }:
            return "paragraph_window"

        return base_strategy

    def _chunk_documents(
        self,
        raw_docs: list[dict[str, Any]],
        collection_name: str = "kdb_principal",
    ) -> list[dict[str, Any]]:
        """Convierte documentos crudos en chunks listos para indexación."""
        docs: list[dict[str, Any]] = []
        source_positions: dict[str, int] = {}
        source_cursors: dict[str, int] = {}
        for raw_idx, d in enumerate(raw_docs):
            text = d.get("page_content", "") or ""
            text = text.strip()
            if not text:
                continue

            metadata_base = d.get("metadata", {})
            if not self._should_include_document_in_collection(
                metadata_base,
                collection_name,
            ):
                continue

            source = metadata_base.get("source", "desconocido")
            source_positions.setdefault(source, 0)
            source_cursors.setdefault(source, 0)
            source_id = self._normalize_id(source)
            effective_strategy = self._resolve_dynamic_chunk_strategy(
                metadata_base,
                collection_name,
            )
            file_type = metadata_base.get("file_type", "text")
            repository = self._infer_repository_from_source(source)
            language = self._infer_language_from_source(source, file_type)

            chunks = self._split_text_with_strategy(text, effective_strategy)
            for chunk in chunks:
                safe_chunks = self._enforce_embedding_token_limit(chunk)
                for safe_chunk in safe_chunks:
                    position = source_positions[source]
                    graph_chunk_id = f"{source_id}::{position}"
                    parent_id = f"{source_id}::raw::{raw_idx}"
                    line_start, line_end, next_cursor = self._locate_chunk_line_span(
                        text,
                        safe_chunk,
                        search_start=source_cursors[source],
                    )
                    source_cursors[source] = next_cursor
                    symbol_meta = self._resolve_symbol_metadata(
                        safe_chunk,
                        source,
                        file_type,
                    )
                    metadata = {
                        **metadata_base,
                        "doc_id": graph_chunk_id,
                        "repository": repository,
                        "file_path": source,
                        "language": language,
                        "symbol_name": symbol_meta.get("symbol_name", ""),
                        "symbol_type": symbol_meta.get("symbol_type", ""),
                        "chunk_type": symbol_meta.get("chunk_type", "paragraph"),
                        "line_start": line_start,
                        "line_end": line_end,
                        "position": position,
                        "graph_chunk_id": graph_chunk_id,
                        "chunk_strategy": effective_strategy,
                        "collection": collection_name,
                        "parent_id": parent_id
                    }
                    docs.append(
                        {"page_content": safe_chunk, "metadata": metadata})
                    source_positions[source] += 1
        return docs

    def _is_supported_source_file(self, file_name: str) -> bool:
        """Determina si el archivo es soportado por extensión o nombre."""
        ext = os.path.splitext(file_name)[1].lower()
        normalized_name = file_name.lower()
        return (
            ext in self.text_extensions
            or ext in self.code_extensions
            or normalized_name in self.special_text_filenames
        )

    def _detect_file_type(self, file_name: str) -> str:
        """Clasifica archivo como code/text para metadatos."""
        ext = os.path.splitext(file_name)[1].lower()
        if ext in self.code_extensions:
            return "code"
        return "text"

    def _infer_repository_from_source(self, source: str) -> str:
        """Infiere repositorio lógico desde la ruta relativa del archivo."""
        if not source:
            return "desconocido"
        parts = [p for p in source.replace("\\", "/").split("/") if p]
        return parts[0] if parts else "desconocido"

    def _infer_language_from_source(self, source: str, file_type: str) -> str:
        """Infiere lenguaje principal a partir de extensión y tipo."""
        ext = os.path.splitext((source or "").lower())[1]
        ext_to_lang = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".jsx": "javascript",
            ".java": "java",
            ".cs": "csharp",
            ".go": "go",
            ".rs": "rust",
            ".cpp": "cpp",
            ".c": "c",
            ".h": "c",
            ".hpp": "cpp",
            ".php": "php",
            ".rb": "ruby",
            ".kt": "kotlin",
            ".swift": "swift",
            ".scala": "scala",
            ".sql": "sql",
            ".sh": "bash",
            ".ps1": "powershell",
            ".json": "json",
            ".yml": "yaml",
            ".yaml": "yaml",
            ".xml": "xml",
            ".toml": "toml",
            ".md": "markdown",
            ".rst": "rst",
            ".txt": "text",
            ".pdf": "pdf",
            ".xlsx": "excel",
            ".xls": "excel",
        }
        if ext in ext_to_lang:
            return ext_to_lang[ext]
        if file_type == "code":
            return "code"
        if file_type == "excel":
            return "excel"
        if file_type == "pdf":
            return "pdf"
        return "text"

    def _resolve_symbol_metadata(
        self,
        chunk_text: str,
        source: str,
        file_type: str,
    ) -> dict[str, str]:
        """Resuelve nombre/tipo de símbolo y tipo de chunk por heurística."""
        if not chunk_text:
            return {
                "symbol_name": Path(source).stem or "desconocido",
                "symbol_type": "module",
                "chunk_type": "function_body" if file_type == "code" else "paragraph",
            }

        if file_type == "code":
            class_match = re.search(
                r"(?m)^\s*(?:class|interface|enum|struct)\s+([A-Za-z_][A-Za-z0-9_]*)",
                chunk_text,
            )
            if class_match:
                return {
                    "symbol_name": class_match.group(1),
                    "symbol_type": "class",
                    "chunk_type": "class_body",
                }

            function_match = re.search(
                r"(?m)^\s*(?:async\s+def|def|function|fn)\s+([A-Za-z_][A-Za-z0-9_]*)",
                chunk_text,
            )
            if function_match:
                return {
                    "symbol_name": function_match.group(1),
                    "symbol_type": "function",
                    "chunk_type": "function_body",
                }

            return {
                "symbol_name": Path(source).stem or "desconocido",
                "symbol_type": "module",
                "chunk_type": "dependency_summary",
            }

        heading_match = re.search(r"(?m)^\s*#{1,6}\s+(.+)$", chunk_text)
        if heading_match:
            return {
                "symbol_name": heading_match.group(1).strip()[:120],
                "symbol_type": "section",
                "chunk_type": "docstring",
            }

        return {
            "symbol_name": Path(source).stem or "desconocido",
            "symbol_type": "document",
            "chunk_type": "paragraph",
        }

    def _locate_chunk_line_span(
        self,
        full_text: str,
        chunk_text: str,
        search_start: int = 0,
    ) -> tuple[int, int, int]:
        """Ubica líneas aproximadas de un chunk dentro del texto original."""
        if not full_text or not chunk_text:
            return 1, 1, search_start

        idx = full_text.find(chunk_text, search_start)
        if idx < 0:
            idx = full_text.find(chunk_text)
        if idx < 0:
            line_start = max(1, full_text.count("\n", 0, search_start) + 1)
            line_end = line_start + max(0, chunk_text.count("\n"))
            return line_start, line_end, search_start

        line_start = full_text.count("\n", 0, idx) + 1
        line_end = line_start + max(0, chunk_text.count("\n"))
        next_cursor = idx + len(chunk_text)
        return line_start, line_end, next_cursor

    def _extract_code_entities(
        self,
        raw_docs: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Extrae entidades de código y relaciones de dependencia básicas."""
        code_docs = [
            d for d in raw_docs if d.get("metadata", {}).get("file_type") == "code"
        ]
        if not code_docs:
            return [], []

        entity_rows: list[dict[str, Any]] = []
        dependency_rows: list[dict[str, str]] = []
        entities_by_file: dict[str, list[dict[str, Any]]] = {}

        class_like_pattern = re.compile(
            r"\b(class|interface|enum|struct)\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        python_def_pattern = re.compile(
            r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        )
        import_pattern = re.compile(
            r"(?m)^\s*(?:from\s+([\w\.]+)\s+import\s+([^\n#]+)|"
            r"import\s+([^\n#;]+)|"
            r"import\s+[^;\n]+\s+from\s+['\"]([^'\"]+)['\"]|"
            r"#include\s+[\"<]([^\">]+)[\">])"
        )
        extends_pattern = re.compile(
            r"\b(?:extends|implements)\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        python_inherit_pattern = re.compile(
            r"(?m)^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\(([^\)]*)\)\s*:"
        )

        for doc in code_docs:
            metadata = doc.get("metadata", {})
            source = metadata.get("source", "desconocido")
            text = doc.get("page_content", "") or ""

            found_entities: list[dict[str, Any]] = []
            for kind, name in class_like_pattern.findall(text):
                entity_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{source}|{kind}|{name}",
                ).hex
                found_entities.append(
                    {
                        "id": entity_id,
                        "name": name,
                        "kind": kind.lower(),
                        "source": source,
                    }
                )

            if source.lower().endswith(".py"):
                for fn_name in python_def_pattern.findall(text):
                    fn_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{source}|function|{fn_name}",
                    ).hex
                    found_entities.append(
                        {
                            "id": fn_id,
                            "name": fn_name,
                            "kind": "function",
                            "source": source,
                        }
                    )

            if not found_entities:
                module_name = Path(source).stem or self._normalize_id(source)
                module_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{source}|module|{module_name}",
                ).hex
                found_entities.append(
                    {
                        "id": module_id,
                        "name": module_name,
                        "kind": "module",
                        "source": source,
                    }
                )

            entities_by_file[source] = found_entities
            entity_rows.extend(found_entities)

        entities_by_name: dict[str, list[dict[str, Any]]] = {}
        for entity in entity_rows:
            entities_by_name.setdefault(entity["name"], []).append(entity)

        for doc in code_docs:
            metadata = doc.get("metadata", {})
            source = metadata.get("source", "desconocido")
            text = doc.get("page_content", "") or ""
            owner_entities = entities_by_file.get(source, [])
            owner_ids = [item["id"] for item in owner_entities]

            dependency_names: set[str] = set()
            for match in import_pattern.findall(text):
                from_mod, from_items, plain_import, es_import, c_include = match
                if from_items:
                    for item in from_items.split(","):
                        cleaned = item.strip().split(" as ")[0].strip()
                        if cleaned and cleaned != "*":
                            dependency_names.add(cleaned.split(".")[-1])
                if plain_import:
                    for item in plain_import.split(","):
                        cleaned = item.strip().split(" as ")[0].strip()
                        if cleaned:
                            dependency_names.add(cleaned.split(".")[-1])
                if from_mod:
                    dependency_names.add(from_mod.split(".")[-1])
                if es_import:
                    dependency_names.add(es_import.split("/")[-1])
                if c_include:
                    include_name = Path(c_include).stem
                    if include_name:
                        dependency_names.add(include_name)

            for parent in extends_pattern.findall(text):
                dependency_names.add(parent)

            for inherits in python_inherit_pattern.findall(text):
                for parent in inherits.split(","):
                    parent_name = parent.strip().split(".")[-1]
                    if parent_name:
                        dependency_names.add(parent_name)

            for dependency_name in dependency_names:
                targets = entities_by_name.get(dependency_name, [])
                for owner_id in owner_ids:
                    for target in targets:
                        if owner_id == target["id"]:
                            continue
                        dependency_rows.append(
                            {
                                "from_id": owner_id,
                                "to_id": target["id"],
                                "type": "DEPENDS_ON",
                            }
                        )

        unique_entities = {
            row["id"]: row for row in entity_rows
        }
        unique_dependencies = {
            (row["from_id"], row["to_id"], row["type"]): row
            for row in dependency_rows
        }

        return list(unique_entities.values()), list(unique_dependencies.values())

    def _index_code_graph(self, raw_docs: list[dict[str, Any]]) -> None:
        """Indexa entidades de código y dependencias para análisis de impacto."""
        if not self.neo4j_driver:
            return

        entities, dependencies = self._extract_code_entities(raw_docs)
        if not entities:
            LOGGER.info("ℹ️ No se detectaron entidades de código para Neo4j.")
            return

        file_rows = sorted(
            {
                row["source"]
                for row in entities
                if row.get("source")
            }
        )

        try:
            with self.neo4j_driver.session(
                database=self.neo4j_database,
            ) as session:
                session.run(
                    "CREATE CONSTRAINT code_file_path_unique "
                    "IF NOT EXISTS "
                    "FOR (f:CodeFile) REQUIRE f.path IS UNIQUE"
                )
                session.run(
                    "CREATE CONSTRAINT code_entity_id_unique "
                    "IF NOT EXISTS "
                    "FOR (e:CodeEntity) REQUIRE e.id IS UNIQUE"
                )

                session.run(
                    """
                    UNWIND $files AS file_path
                    MERGE (f:CodeFile {path: file_path})
                    """,
                    files=file_rows,
                )

                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (e:CodeEntity {id: row.id})
                    SET e.name = row.name,
                        e.kind = row.kind,
                        e.source = row.source
                    WITH e, row
                    MATCH (f:CodeFile {path: row.source})
                    MERGE (f)-[:DECLARES]->(e)
                    """,
                    rows=entities,
                )

                if dependencies:
                    session.run(
                        """
                        UNWIND $rels AS rel
                        MATCH (src:CodeEntity {id: rel.from_id})
                        MATCH (dst:CodeEntity {id: rel.to_id})
                        MERGE (src)-[r:DEPENDS_ON]->(dst)
                        SET r.type = rel.type
                        """,
                        rels=dependencies,
                    )

            LOGGER.info(
                "✅ Grafo de código actualizado en Neo4j: %s entidades, "
                "%s dependencias.",
                len(entities),
                len(dependencies),
            )
        except Neo4jError as exc:
            LOGGER.warning(
                "⚠️ Error indexando grafo de código en Neo4j: %s",
                exc,
            )

    def load_documents(self) -> list[dict[str, Any]]:
        """Carga y normaliza documentos fuente en formato interno."""
        documents: list[dict[str, Any]] = []
        file_entries: list[tuple[str, str, str]] = []

        for root, _, files in os.walk(self.data_path):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(file_path, self.data_path)
                ext = os.path.splitext(file_name)[1].lower()
                file_entries.append((file_path, rel_path, ext))

        total_files = len(file_entries)
        supported_files = sum(
            1
            for _, rel_path, _ in file_entries
            if self._is_supported_source_file(os.path.basename(rel_path))
            or os.path.splitext(rel_path)[1].lower() in (".pdf", ".xlsx", ".xls")
        )

        self._emit_progress(
            "scan_complete",
            total_files=total_files,
            supported_files=supported_files,
        )

        if total_files == 0:
            return documents

        for index, (file_path, rel_path, ext) in enumerate(file_entries, start=1):
            self._emit_progress(
                "file_processing",
                file=rel_path,
                index=index,
                total_files=total_files,
                progress=index / total_files,
            )
            LOGGER.info("📄 Procesando: %s", rel_path)
            try:
                if ext == '.pdf':
                    reader = PdfReader(file_path)
                    before_count = len(documents)
                    for page in reader.pages:
                        text = page.extract_text() or ""
                        documents.append({
                            "page_content": text,
                            "metadata": {
                                "source": rel_path,
                                "file_type": "pdf",
                            },
                        })
                    loaded_rows = len(documents) - before_count
                    self._emit_progress(
                        "file_loaded",
                        file=rel_path,
                        file_type="pdf",
                        records=loaded_rows,
                    )
                elif ext in ('.xlsx', '.xls'):
                    wb = openpyxl.load_workbook(file_path, data_only=True)
                    before_count = len(documents)
                    for sheet in wb.worksheets:
                        for row in sheet.iter_rows(values_only=True):
                            text = " ".join(
                                [
                                    str(cell)
                                    for cell in row
                                    if cell is not None
                                ]
                            )
                            if text:
                                documents.append({
                                    "page_content": text,
                                    "metadata": {
                                        "source": rel_path,
                                        "file_type": "excel",
                                    },
                                })
                    loaded_rows = len(documents) - before_count
                    self._emit_progress(
                        "file_loaded",
                        file=rel_path,
                        file_type="excel",
                        records=loaded_rows,
                    )
                elif self._is_supported_source_file(os.path.basename(rel_path)):
                    with open(
                        file_path,
                        "r",
                        encoding="utf-8",
                        errors="ignore",
                    ) as f:
                        text = f.read()
                    if text.strip():
                        detected_type = self._detect_file_type(
                            os.path.basename(rel_path)
                        )
                        documents.append({
                            "page_content": text,
                            "metadata": {
                                "source": rel_path,
                                "file_type": detected_type,
                            }
                        })
                        self._emit_progress(
                            "file_loaded",
                            file=rel_path,
                            file_type=detected_type,
                            records=1,
                        )
                    else:
                        self._emit_progress(
                            "file_empty",
                            file=rel_path,
                        )
                else:
                    LOGGER.warning("⚠️ Formato no soportado: %s", rel_path)
                    self._emit_progress(
                        "file_unsupported",
                        file=rel_path,
                    )
            except (
                OSError,
                ValueError,
                PdfReadError,
                InvalidFileException,
            ) as exc:
                LOGGER.warning("❌ Error cargando %s: %s", rel_path, exc)
                self._emit_progress(
                    "file_error",
                    file=rel_path,
                    error=str(exc),
                )

        self._emit_progress(
            "load_complete",
            loaded_documents=len(documents),
            total_files=total_files,
        )
        return documents

    def run(self, github_url: str | None = None) -> None:
        """Ejecuta la ingesta completa hacia ChromaDB y Neo4j."""
        self._emit_progress(
            "run_start",
            github_url=github_url or "",
        )
        # 0. Si hay GitHub, descargar primero
        if github_url:
            loader = GitHubLoader(self.data_path)
            try:
                loader.fetch_repo(github_url)
            except RuntimeError as exc:
                LOGGER.error("No se pudo cargar repositorio GitHub: %s", exc)
                return
        # 1. Cargar documentos
        raw_docs = self.load_documents()
        if not raw_docs:
            LOGGER.warning("⚠️ No se encontraron documentos para procesar.")
            self._emit_progress("run_no_documents")
            return

        # 2 y 3. Chunking + upsert en una o varias colecciones
        LOGGER.info("✂️ Dividiendo documentos en fragmentos...")
        graph_docs = []

        if self.enable_multi_collection:
            profiles = self.collection_profiles
        else:
            profiles = [{
                "name": "kdb_principal",
                "strategy": self.chunk_strategy,
                "for_graph": True
            }]

        for profile in profiles:
            profile_name = profile.get("name", "kdb_principal")
            saved_state = self._get_chunking_state()
            self._apply_profile_settings(profile)

            LOGGER.info(
                "🧠 Estrategia activa [%s]: %s",
                profile_name,
                self.chunk_strategy,
            )
            docs = self._chunk_documents(
                raw_docs, collection_name=profile_name)
            self._emit_progress(
                "profile_chunked",
                profile=profile_name,
                strategy=self.chunk_strategy,
                chunks=len(docs),
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )
            LOGGER.info(
                "💾 Guardando %s fragmentos en ChromaDB (%s)...",
                len(docs),
                profile_name,
            )

            collection = self.client.get_or_create_collection(
                name=profile_name,
                embedding_function=self.embedding_fn,
            )

            texts = [d.get("page_content", "") for d in docs]
            metadatas = [
                {
                    "doc_id": d.get("metadata", {}).get("doc_id", ""),
                    "repository": d.get("metadata", {}).get("repository", ""),
                    "file_path": d.get("metadata", {}).get("file_path", ""),
                    "language": d.get("metadata", {}).get("language", ""),
                    "symbol_name": d.get("metadata", {}).get("symbol_name", ""),
                    "symbol_type": d.get("metadata", {}).get("symbol_type", ""),
                    "chunk_type": d.get("metadata", {}).get("chunk_type", ""),
                    "line_start": d.get("metadata", {}).get("line_start", 0),
                    "line_end": d.get("metadata", {}).get("line_end", 0),
                    "source": d.get("metadata", {}).get("source", ""),
                    "file_type": d.get("metadata", {}).get("file_type", ""),
                    "chunk_strategy": d.get("metadata", {}).get(
                        "chunk_strategy", ""
                    ),
                    "collection": d.get("metadata", {}).get(
                        "collection", profile_name
                    ),
                    "parent_id": d.get("metadata", {}).get("parent_id", ""),
                    "position": d.get("metadata", {}).get("position", 0),
                }
                for d in docs
            ]
            ids = [
                self._build_vector_id(
                    d.get("metadata", {}), d.get("page_content", ""))
                for d in docs
            ]
            self._upsert_in_batches(collection, texts, metadatas, ids)

            if profile.get("for_graph", False):
                graph_docs = docs

            self._restore_chunking_state(saved_state)

        # 4. Indexar estructura documental en Neo4j (solo perfil principal para
        # continuidad)
        self._index_graph(graph_docs)
        self._index_code_graph(raw_docs)

        LOGGER.info("✅ Ingesta finalizada exitosamente.")
        self._emit_progress("run_complete")
        self.close()


if __name__ == "__main__":
    # Si ejecutas este archivo directamente, procesa la carpeta por defecto
    INGEST_DATA_PATH = "./documentos_fuente"
    INGEST_DB_PATH = "./db_chroma_kdb"

    # Crear carpeta si no existe
    os.makedirs(INGEST_DATA_PATH, exist_ok=True)

    ingestor = KDBIngestor(INGEST_DATA_PATH, INGEST_DB_PATH)
    ingestor.run()
