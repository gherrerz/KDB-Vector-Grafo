import os
import re
import uuid
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from neo4j import GraphDatabase

# simple loaders to avoid langchain_community
from pypdf import PdfReader
import openpyxl

# Cargar variables de entorno (API KEY)
load_dotenv()

class KDBIngestor:
    def __init__(self, data_path, db_path):
        """
        Inicializa el ingestor de la base de conocimientos.
        data_path: Carpeta donde están los documentos fuente.
        db_path: Carpeta donde se guardará la base vectorial.
        """
        self.data_path = data_path
        self.db_path = db_path
        # inicializar cliente ChromaDB con persistencia
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

        # Configuración de chunking (elige UNA estrategia descomentando una línea)
        self.chunk_strategy = "char_overlap"
        # self.chunk_strategy = "sentence_window"
        # self.chunk_strategy = "paragraph_window"
        # self.chunk_strategy = "heading_window"
        # self.chunk_strategy = "code_aware"

        # Parámetros generales de chunking
        self.chunk_size = 1000
        self.chunk_overlap = 200
        self.sentence_window_size = 6
        self.sentence_overlap = 2
        self.paragraph_window_size = 3
        self.paragraph_overlap = 1
        self.code_line_window = 80
        self.code_line_overlap = 20

        self.max_batch_tokens = 120000
        self.max_batch_items = 100

        self.text_extensions = {
            ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json", ".yaml", ".yml"
        }
        self.code_extensions = {
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go", ".rs", ".cpp", ".c",
            ".h", ".hpp", ".php", ".rb", ".kt", ".swift", ".scala", ".sql", ".sh", ".ps1", ".bat"
        }

        self.neo4j_uri = os.getenv("NEO4J_URI", "")
        self.neo4j_user = os.getenv("NEO4J_USER", "")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "")
        self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.neo4j_driver = self._init_neo4j_driver()

    def _init_neo4j_driver(self):
        if not (self.neo4j_uri and self.neo4j_user and self.neo4j_password):
            print("ℹ️ Neo4j no configurado. Se ejecutará solo indexación vectorial.")
            return None
        try:
            driver = GraphDatabase.driver(
                self.neo4j_uri,
                auth=(self.neo4j_user, self.neo4j_password)
            )
            with driver.session(database=self.neo4j_database) as session:
                session.run("RETURN 1")
            print("✅ Conexión con Neo4j establecida.")
            return driver
        except Exception as e:
            print(f"⚠️ No se pudo conectar a Neo4j: {e}")
            return None

    def _normalize_id(self, text: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", text)
        return normalized.strip("_")[:120] or "documento"

    def _index_graph(self, docs):
        if not self.neo4j_driver:
            return

        by_source = {}
        for item in docs:
            source = item.get("metadata", {}).get("source", "desconocido")
            by_source.setdefault(source, []).append(item)

        try:
            with self.neo4j_driver.session(database=self.neo4j_database) as session:
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
            print("✅ Indexación estructural en Neo4j completada.")
        except Exception as e:
            print(f"⚠️ Error indexando en Neo4j: {e}")

    def close(self):
        if self.neo4j_driver:
            self.neo4j_driver.close()

    def _estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        # Estimación conservadora (aprox. 1 token cada 4 caracteres)
        return max(1, len(text) // 4)

    def _upsert_in_batches(self, collection, texts, metadatas, ids):
        batch_texts = []
        batch_metadatas = []
        batch_ids = []
        batch_tokens = 0
        total = len(texts)
        inserted = 0

        def flush_batch():
            nonlocal batch_texts, batch_metadatas, batch_ids, batch_tokens, inserted
            if not batch_texts:
                return
            collection.upsert(
                documents=batch_texts,
                metadatas=batch_metadatas,
                ids=batch_ids
            )
            inserted += len(batch_texts)
            print(f"   ↳ Upsert batch OK: {inserted}/{total} chunks")
            batch_texts = []
            batch_metadatas = []
            batch_ids = []
            batch_tokens = 0

        for text, metadata, doc_id in zip(texts, metadatas, ids):
            item_tokens = self._estimate_tokens(text)

            if item_tokens > self.max_batch_tokens:
                print(f"⚠️ Chunk muy grande ({item_tokens} tokens est.). Se omite id={doc_id}")
                continue

            would_exceed_tokens = (batch_tokens + item_tokens) > self.max_batch_tokens
            would_exceed_items = len(batch_texts) >= self.max_batch_items

            if would_exceed_tokens or would_exceed_items:
                flush_batch()

            batch_texts.append(text)
            batch_metadatas.append(metadata)
            batch_ids.append(doc_id)
            batch_tokens += item_tokens

        flush_batch()

    def _get_chunking_state(self) -> dict:
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

    def _restore_chunking_state(self, state: dict):
        self.chunk_strategy = state.get("chunk_strategy", self.chunk_strategy)
        self.chunk_size = state.get("chunk_size", self.chunk_size)
        self.chunk_overlap = state.get("chunk_overlap", self.chunk_overlap)
        self.sentence_window_size = state.get("sentence_window_size", self.sentence_window_size)
        self.sentence_overlap = state.get("sentence_overlap", self.sentence_overlap)
        self.paragraph_window_size = state.get("paragraph_window_size", self.paragraph_window_size)
        self.paragraph_overlap = state.get("paragraph_overlap", self.paragraph_overlap)
        self.code_line_window = state.get("code_line_window", self.code_line_window)
        self.code_line_overlap = state.get("code_line_overlap", self.code_line_overlap)

    def _apply_profile_settings(self, profile: dict):
        self.chunk_strategy = profile.get("strategy", self.chunk_strategy)
        for key in [
            "chunk_size", "chunk_overlap", "sentence_window_size", "sentence_overlap",
            "paragraph_window_size", "paragraph_overlap", "code_line_window", "code_line_overlap"
        ]:
            if key in profile:
                setattr(self, key, profile[key])

    def _build_vector_id(self, metadata: dict, text: str) -> str:
        source = metadata.get("source", "desconocido")
        strategy = metadata.get("chunk_strategy", "unknown")
        position = metadata.get("position", 0)
        fingerprint = f"{source}|{strategy}|{position}|{text[:120]}"
        return uuid.uuid5(uuid.NAMESPACE_URL, fingerprint).hex

    def _split_char_overlap(self, text: str) -> list[str]:
        if not text:
            return []
        chunks = []
        start = 0
        step = max(1, self.chunk_size - self.chunk_overlap)
        while start < len(text):
            end = min(len(text), start + self.chunk_size)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start += step
        return chunks

    def _split_sentence_window(self, text: str) -> list[str]:
        if not text:
            return []
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return self._split_char_overlap(text)
        chunks = []
        i = 0
        step = max(1, self.sentence_window_size - self.sentence_overlap)
        while i < len(sentences):
            chunk = " ".join(sentences[i:i + self.sentence_window_size]).strip()
            if chunk:
                chunks.append(chunk)
            i += step
        return chunks

    def _split_paragraph_window(self, text: str) -> list[str]:
        if not text:
            return []
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if not paragraphs:
            return self._split_char_overlap(text)
        chunks = []
        i = 0
        step = max(1, self.paragraph_window_size - self.paragraph_overlap)
        while i < len(paragraphs):
            chunk = "\n\n".join(paragraphs[i:i + self.paragraph_window_size]).strip()
            if chunk:
                chunks.append(chunk)
            i += step
        return chunks

    def _split_heading_window(self, text: str) -> list[str]:
        if not text:
            return []
        heading_pattern = r"(?m)(?=^\s{0,3}#{1,6}\s+|^\s*\d+[\.)]\s+|^[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{8,}$)"
        sections = [s.strip() for s in re.split(heading_pattern, text) if s.strip()]
        if len(sections) <= 1:
            return self._split_paragraph_window(text)

        chunks = []
        current = ""
        for section in sections:
            candidate = (current + "\n\n" + section).strip() if current else section
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
        if not text:
            return []
        lines = text.splitlines()
        if not lines:
            return []

        code_boundary_pattern = re.compile(
            r"^\s*(def\s+|class\s+|async\s+def\s+|function\s+|async\s+function\s+|"
            r"public\s+|private\s+|protected\s+|interface\s+|enum\s+|struct\s+|"
            r"impl\s+|fn\s+|export\s+class\s+|export\s+function\s+|"
            r"if\s+__name__\s*==\s*['\"]__main__['\"])"
        )

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
                line_end = min(len(block_lines), line_start + self.code_line_window)
                piece = "\n".join(block_lines[line_start:line_end]).strip()
                if piece:
                    chunks.append(piece)
                line_start += line_step
        return chunks

    def _split_text(self, text: str) -> list[str]:
        strategy = self.chunk_strategy
        if strategy == "sentence_window":
            return self._split_sentence_window(text)
        if strategy == "paragraph_window":
            return self._split_paragraph_window(text)
        if strategy == "heading_window":
            return self._split_heading_window(text)
        if strategy == "code_aware":
            return self._split_code_aware(text)
        return self._split_char_overlap(text)

    def _chunk_documents(self, raw_docs: list[dict], collection_name: str = "kdb_principal") -> list[dict]:
        docs = []
        source_positions = {}
        for raw_idx, d in enumerate(raw_docs):
            text = d.get("page_content", "") or ""
            text = text.strip()
            if not text:
                continue

            metadata_base = d.get("metadata", {})
            source = metadata_base.get("source", "desconocido")
            source_positions.setdefault(source, 0)
            source_id = self._normalize_id(source)

            chunks = self._split_text(text)
            for chunk in chunks:
                position = source_positions[source]
                graph_chunk_id = f"{source_id}::{position}"
                parent_id = f"{source_id}::raw::{raw_idx}"
                metadata = {
                    **metadata_base,
                    "position": position,
                    "graph_chunk_id": graph_chunk_id,
                    "chunk_strategy": self.chunk_strategy,
                    "collection": collection_name,
                    "parent_id": parent_id
                }
                docs.append({"page_content": chunk, "metadata": metadata})
                source_positions[source] += 1
        return docs

    def load_documents(self):
        """Carga y procesa documentos de diferentes formatos."""
        documents = []
        for root, _, files in os.walk(self.data_path):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(file_path, self.data_path)
                ext = os.path.splitext(file_name)[1].lower()

                print(f"📄 Procesando: {rel_path}")
                try:
                    if ext == '.pdf':
                        reader = PdfReader(file_path)
                        for page in reader.pages:
                            text = page.extract_text() or ""
                            documents.append({
                                "page_content": text,
                                "metadata": {"source": rel_path, "file_type": "pdf"}
                            })
                    elif ext in ('.xlsx', '.xls'):
                        wb = openpyxl.load_workbook(file_path, data_only=True)
                        for sheet in wb.worksheets:
                            for row in sheet.iter_rows(values_only=True):
                                text = " ".join([str(cell) for cell in row if cell is not None])
                                if text:
                                    documents.append({
                                        "page_content": text,
                                        "metadata": {"source": rel_path, "file_type": "excel"}
                                    })
                    elif ext in self.text_extensions or ext in self.code_extensions:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            text = f.read()
                        if text.strip():
                            documents.append({
                                "page_content": text,
                                "metadata": {
                                    "source": rel_path,
                                    "file_type": "code" if ext in self.code_extensions else "text"
                                }
                            })
                    else:
                        print(f"⚠️ Formato no soportado: {rel_path}")
                except Exception as e:
                    print(f"❌ Error cargando {rel_path}: {e}")
        return documents

    def run(self):
        """Proceso principal de ingesta, particionado e indexación."""
        print("🚀 Iniciando proceso de ingesta...")
        
        # 1. Cargar documentos
        raw_docs = self.load_documents()
        if not raw_docs:
            print("⚠️ No se encontraron documentos para procesar.")
            return

        # 2 y 3. Chunking + upsert en una o varias colecciones
        print("✂️ Dividiendo documentos en fragmentos...")
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

            print(f"🧠 Estrategia activa [{profile_name}]: {self.chunk_strategy}")
            docs = self._chunk_documents(raw_docs, collection_name=profile_name)
            print(f"💾 Guardando {len(docs)} fragmentos en ChromaDB ({profile_name})...")

            try:
                collection = self.client.get_collection(
                    profile_name,
                    embedding_function=self.embedding_fn
                )
            except Exception:
                collection = self.client.create_collection(
                    name=profile_name,
                    embedding_function=self.embedding_fn
                )

            texts = [d.get("page_content", "") for d in docs]
            metadatas = [
                {
                    "source": d.get("metadata", {}).get("source", ""),
                    "file_type": d.get("metadata", {}).get("file_type", ""),
                    "chunk_strategy": d.get("metadata", {}).get("chunk_strategy", ""),
                    "collection": d.get("metadata", {}).get("collection", profile_name),
                    "parent_id": d.get("metadata", {}).get("parent_id", ""),
                    "position": d.get("metadata", {}).get("position", 0)
                }
                for d in docs
            ]
            ids = [
                self._build_vector_id(d.get("metadata", {}), d.get("page_content", ""))
                for d in docs
            ]
            self._upsert_in_batches(collection, texts, metadatas, ids)

            if profile.get("for_graph", False):
                graph_docs = docs

            self._restore_chunking_state(saved_state)

        # 4. Indexar estructura documental en Neo4j (solo perfil principal para continuidad)
        self._index_graph(graph_docs)
        
        print("✅ Ingesta finalizada exitosamente.")
        self.close()

if __name__ == "__main__":
    # Si ejecutas este archivo directamente, procesa la carpeta por defecto
    INGEST_DATA_PATH = "./documentos_fuente"
    INGEST_DB_PATH = "./db_chroma_kdb"
    
    # Crear carpeta si no existe
    os.makedirs(INGEST_DATA_PATH, exist_ok=True)
    
    ingestor = KDBIngestor(INGEST_DATA_PATH, INGEST_DB_PATH)
    ingestor.run()