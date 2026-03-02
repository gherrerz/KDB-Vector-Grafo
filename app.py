import streamlit as st
import os
import re
from ingestion import KDBIngestor
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# --- IMPORTS ---
import openai
import chromadb
from chromadb.utils import embedding_functions
from neo4j import GraphDatabase
# no langchain dependencies, use simple retrieval and OpenAI API

# --- CONFIGURACIÓN DE RUTAS ---
DATA_PATH = "./documentos_fuente"
CHROMA_PATH = "./db_chroma_kdb"
os.makedirs(DATA_PATH, exist_ok=True)

DEFAULT_COLLECTIONS = ["kdb_principal", "kdb_small", "kdb_large", "kdb_code"]
STRATEGY_OPTIONS = ["all", "char_overlap", "sentence_window", "paragraph_window", "heading_window", "code_aware"]

NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USER", "")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# Configuración de página
st.set_page_config(page_title="Auditor KDB Pro", layout="wide", page_icon="🕵️‍♂️")

# --- INICIALIZACIÓN DE COMPONENTES ---
def init_vector_stores():
    if not os.getenv("OPENAI_API_KEY"):
        st.error("❌ API Key no encontrada. Configura el archivo .env")
        st.stop()
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    embedding_fn = embedding_functions.OpenAIEmbeddingFunction(
        api_key=os.getenv("OPENAI_API_KEY"),
        model_name="text-embedding-3-small"
    )
    collection_names = set(DEFAULT_COLLECTIONS)
    try:
        existing = client.list_collections()
        for item in existing:
            name = getattr(item, "name", None)
            if name and name.startswith("kdb_"):
                collection_names.add(name)
    except Exception:
        pass

    collections = {}
    for name in sorted(collection_names):
        try:
            collections[name] = client.get_collection(name, embedding_function=embedding_fn)
        except Exception:
            continue

    if not collections:
        collections["kdb_principal"] = client.create_collection(
            name="kdb_principal",
            embedding_function=embedding_fn
        )

    return collections


def init_neo4j_driver():
    if not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD):
        return None
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run("RETURN 1")
        return driver
    except Exception:
        return None

vector_stores = init_vector_stores()
neo4j_driver = init_neo4j_driver()

# --- DEFINICIÓN DE AYUDA SIMPLE ---
# No usamos agentes: hacemos búsqueda y consulta directa con OpenAI

# initialize OpenAI client (new 1.0+ interface)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

system_prompt = """Eres un Auditor Técnico experto y Analista de Datos. Tu objetivo es analizar la base de conocimientos proporcionada.

REGLAS DE ORO:
1. IDIOMA: Responde siempre en ESPAÑOL profesional.
2. RIGOR: Si los datos provienen de una tabla, interprétalos con precisión quirúrgica.
3. CITACIÓN: Menciona siempre el nombre del archivo fuente (p.ej., 'Según reporte_final.pdf, página 4...').
4. HONESTIDAD: Si no encuentras evidencia explícita en los documentos, di: 'No se encontraron registros suficientes para validar esta información'.
"""

def consultar_evidencia_kdb(
    query: str,
    strategy_filter: str = "all",
    collection_filter: str = "all",
    per_collection_k: int = 4
) -> list[dict]:
    """Busca en una o varias colecciones ChromaDB y permite filtrar por metadatos."""
    if not vector_stores:
        return []

    if collection_filter != "all" and collection_filter in vector_stores:
        target_collections = {collection_filter: vector_stores[collection_filter]}
    elif collection_filter == "all":
        target_collections = vector_stores
    else:
        return []

    where = None
    if strategy_filter != "all":
        where = {"chunk_strategy": strategy_filter}

    all_docs = []
    for collection_name, collection in target_collections.items():
        try:
            query_args = {
                "query_texts": [query],
                "n_results": per_collection_k
            }
            if where:
                query_args["where"] = where

            results = collection.query(**query_args)
            documents = results.get("documents", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0] if results.get("distances") else []

            for i, text in enumerate(documents):
                metadata = metadatas[i] if i < len(metadatas) else {}
                distance = distances[i] if i < len(distances) else None
                all_docs.append({
                    "text": text,
                    "source": metadata.get("source", ""),
                    "collection": metadata.get("collection", collection_name),
                    "chunk_strategy": metadata.get("chunk_strategy", ""),
                    "parent_id": metadata.get("parent_id", ""),
                    "file_type": metadata.get("file_type", ""),
                    "distance": distance
                })
        except Exception:
            continue

    all_docs = sorted(all_docs, key=lambda x: x.get("distance") if x.get("distance") is not None else 999)

    deduped = []
    seen = set()
    for d in all_docs:
        key = d.get("parent_id") or (d.get("source", ""), d.get("text", "")[:200])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)

    return deduped[:8]


def _extraer_keywords(query: str) -> list[str]:
    tokens = re.findall(r"[a-zA-ZáéíóúÁÉÍÓÚñÑ0-9]{4,}", query.lower())
    stopwords = {
        "para", "como", "esta", "este", "estos", "estas", "sobre", "entre",
        "donde", "desde", "hasta", "tambien", "segun", "datos", "evidencia",
        "validacion", "estructura", "semantica", "documento", "documentos"
    }
    filtered = [t for t in tokens if t not in stopwords]
    unique = []
    for t in filtered:
        if t not in unique:
            unique.append(t)
    return unique[:8]


def consultar_evidencia_grafo(query: str, limit: int = 5) -> list[dict]:
    if neo4j_driver is None:
        return []

    keywords = _extraer_keywords(query)
    if not keywords:
        return []

    cypher = """
    MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
    WHERE any(k IN $keywords WHERE toLower(c.text) CONTAINS k)
    WITH d, c,
         reduce(score = 0, k IN $keywords |
            score + CASE WHEN toLower(c.text) CONTAINS k THEN 1 ELSE 0 END
         ) AS score
    OPTIONAL MATCH (prev:Chunk)-[:NEXT]->(c)
    OPTIONAL MATCH (c)-[:NEXT]->(nxt:Chunk)
    RETURN d.name AS source,
           c.text AS text,
           c.position AS position,
           score,
           prev.text AS prev_text,
           nxt.text AS next_text
    ORDER BY score DESC, position ASC
    LIMIT $limit
    """

    try:
        with neo4j_driver.session(database=NEO4J_DATABASE) as session:
            rows = session.run(cypher, keywords=keywords, limit=limit)
            docs = []
            for row in rows:
                docs.append({
                    "text": row.get("text", ""),
                    "source": row.get("source", ""),
                    "position": row.get("position", 0),
                    "score": row.get("score", 0),
                    "prev_text": row.get("prev_text", ""),
                    "next_text": row.get("next_text", "")
                })
            return docs
    except Exception:
        return []


def recuperar_evidencia_hibrida(
    query: str,
    strategy_filter: str = "all",
    collection_filter: str = "all"
) -> dict:
    evidencia_vector = consultar_evidencia_kdb(
        query,
        strategy_filter=strategy_filter,
        collection_filter=collection_filter
    )
    evidencia_grafo = consultar_evidencia_grafo(query)

    combined = []
    seen = set()
    for item in evidencia_vector:
        key = (item.get("source", ""), item.get("text", "")[:200])
        if key not in seen:
            combined.append(item)
            seen.add(key)

    for item in evidencia_grafo:
        key = (item.get("source", ""), item.get("text", "")[:200])
        if key not in seen:
            combined.append({"source": item.get("source", ""), "text": item.get("text", "")})
            seen.add(key)

    return {
        "vector": evidencia_vector,
        "graph": evidencia_grafo,
        "combined": combined
    }


def generar_respuesta(
    user_question: str,
    strategy_filter: str = "all",
    collection_filter: str = "all"
) -> str:
    evidencia = recuperar_evidencia_hibrida(
        user_question,
        strategy_filter=strategy_filter,
        collection_filter=collection_filter
    )

    evidencia_semantica = "\n\n".join(
        [
            (
                f"Fuente: {d['source']} | Colección: {d.get('collection', '')} | "
                f"Estrategia: {d.get('chunk_strategy', '')}\n{d['text']}"
            )
            for d in evidencia["vector"]
        ]
    )

    evidencia_estructural = "\n\n".join([
        (
            f"Fuente: {d['source']} | Posición: {d.get('position', 0)} | Score estructural: {d.get('score', 0)}\n"
            f"Previo: {d.get('prev_text', '')}\n"
            f"Actual: {d.get('text', '')}\n"
            f"Siguiente: {d.get('next_text', '')}"
        )
        for d in evidencia["graph"]
    ])

    if not evidencia_semantica:
        evidencia_semantica = "No se recuperó evidencia semántica en ChromaDB."
    if not evidencia_estructural:
        evidencia_estructural = "No se recuperó evidencia estructural en Neo4j."

    prompt = (
        f"{system_prompt}\n\n"
        "VALIDA LA RESPUESTA CON DOS CAPAS:\n"
        "1) Semántica (similitud vectorial)\n"
        "2) Estructural (posición y continuidad del chunk en grafo)\n\n"
        f"EVIDENCIA SEMÁNTICA:\n{evidencia_semantica}\n\n"
        f"EVIDENCIA ESTRUCTURAL:\n{evidencia_estructural}\n\n"
        f"PREGUNTA:\n{user_question}"
    )
    # use new OpenAI chat API (v1.0+)
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        temperature=0
    )
    return resp.choices[0].message.content.strip()

# --- INTERFAZ DE USUARIO (SIDEBAR) ---
with st.sidebar:
    st.header("📥 Ingesta de Evidencia")
    if neo4j_driver is None:
        st.warning("Neo4j no configurado. El sistema funcionará en modo vectorial.")
    else:
        st.success("Neo4j conectado. Validación estructural habilitada.")
    files = st.file_uploader("Subir archivos (PDF, Excel)", accept_multiple_files=True)

    st.subheader("🔎 Filtros de búsqueda")
    selected_strategy = st.selectbox(
        "Estrategia de chunk",
        STRATEGY_OPTIONS,
        index=0
    )
    collection_options = ["all"] + sorted(vector_stores.keys())
    selected_collection = st.selectbox(
        "Colección",
        collection_options,
        index=0
    )
    
    if st.button("🚀 Indexar Nueva Evidencia"):
        if files:
            with st.status("Procesando...", expanded=True) as status:
                for f in files:
                    file_path = os.path.join(DATA_PATH, f.name)
                    with open(file_path, "wb") as buffer:
                        buffer.write(f.getbuffer())
                
                st.write("Analizando contenido y generando embeddings + grafo (ChromaDB + Neo4j)...")
                
                ingestor = KDBIngestor(DATA_PATH, CHROMA_PATH)
                ingestor.run()
                
                status.update(label="✅ KDB Actualizada", state="complete")
            st.success("Archivos listos para auditoría")
            st.rerun()

    st.divider()
    if st.button("🗑️ Limpiar Historial de Chat"):
        st.session_state.messages = []
        st.rerun()

# --- PANEL DE CHAT ---
st.title("🕵️‍♂️ Panel de Auditoría Inteligente")
st.caption("Motor: OpenAI | RAG Híbrido: ChromaDB (semántico) + Neo4j (estructural)")

# Inicializar historial
if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": "Listo para auditar. Sube tus archivos en la barra lateral para comenzar."}]

# Mostrar mensajes anteriores
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Capturar nueva consulta
if user_input := st.chat_input("Consulta la base de conocimientos..."):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Procesar respuesta del Auditor usando el generador simple
    with st.chat_message("assistant"):
        with st.spinner("El Auditor está analizando la evidencia..."):
            try:
                response_text = generar_respuesta(
                    user_input,
                    strategy_filter=selected_strategy,
                    collection_filter=selected_collection
                )
            except Exception as e:
                response_text = f"❌ Error al generar respuesta: {str(e)}"
            st.markdown(response_text)
            st.session_state.messages.append({"role": "assistant", "content": response_text})