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
        # simple splitting helper: chunks of specified size with overlap
        self.chunk_size = 1000
        self.chunk_overlap = 200
        self.max_batch_tokens = 120000
        self.max_batch_items = 100

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

    def load_documents(self):
        """Carga y procesa documentos de diferentes formatos."""
        documents = []
        for file_name in os.listdir(self.data_path):
            file_path = os.path.join(self.data_path, file_name)
            
            print(f"📄 Procesando: {file_name}")
            try:
                if file_name.lower().endswith('.pdf'):
                    reader = PdfReader(file_path)
                    for i, page in enumerate(reader.pages):
                        text = page.extract_text() or ""
                        documents.append({"page_content": text, "metadata": {"source": file_name}})
                elif file_name.lower().endswith(('.xlsx', '.xls')):
                    wb = openpyxl.load_workbook(file_path, data_only=True)
                    for sheet in wb.worksheets:
                        for row in sheet.iter_rows(values_only=True):
                            text = " ".join([str(cell) for cell in row if cell is not None])
                            if text:
                                documents.append({"page_content": text, "metadata": {"source": file_name}})
                else:
                    print(f"⚠️ Formato no soportado: {file_name}")
            except Exception as e:
                print(f"❌ Error cargando {file_name}: {e}")
        return documents

    def run(self):
        """Proceso principal de ingesta, particionado e indexación."""
        print("🚀 Iniciando proceso de ingesta...")
        
        # 1. Cargar documentos
        raw_docs = self.load_documents()
        if not raw_docs:
            print("⚠️ No se encontraron documentos para procesar.")
            return

        # 2. Dividir el texto en fragmentos (chunks)
        print("✂️ Dividiendo documentos en fragmentos...")
        # manual splitting
        docs = []
        source_positions = {}
        for d in raw_docs:
            text = d.get("page_content", "")
            source = d.get("metadata", {}).get("source", "desconocido")
            source_positions.setdefault(source, 0)
            source_id = self._normalize_id(source)
            start = 0
            while start < len(text):
                end = min(len(text), start + self.chunk_size)
                chunk = text[start:end]
                position = source_positions[source]
                graph_chunk_id = f"{source_id}::{position}"
                metadata = {
                    **d.get("metadata", {}),
                    "position": position,
                    "graph_chunk_id": graph_chunk_id
                }
                docs.append({"page_content": chunk, "metadata": metadata})
                source_positions[source] += 1
                start += self.chunk_size - self.chunk_overlap

        # 3. Crear o actualizar la base de datos vectorial usando ChromaDB puro
        print(f"💾 Guardando {len(docs)} fragmentos en ChromaDB ({self.db_path})...")
        collection = None
        try:
            collection = self.client.get_collection(
                "kdb_principal",
                embedding_function=self.embedding_fn
            )
        except Exception:
            collection = self.client.create_collection(
                name="kdb_principal",
                embedding_function=self.embedding_fn
            )

        texts = [d.get("page_content", "") for d in docs]
        metadatas = [{"source": d.get("metadata", {}).get("source", "")} for d in docs]
        ids = [f"{d.get('metadata', {}).get('graph_chunk_id', uuid.uuid4().hex)}::{uuid.uuid4().hex[:8]}" for d in docs]
        self._upsert_in_batches(collection, texts, metadatas, ids)

        # 4. Indexar estructura documental en Neo4j
        self._index_graph(docs)
        
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