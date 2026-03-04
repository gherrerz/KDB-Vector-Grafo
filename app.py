"""Aplicación Streamlit para auditoría con RAG híbrido (ChromaDB + Neo4j)."""

from neo4j.exceptions import Neo4jError, ServiceUnavailable
from neo4j import Driver
from neo4j import GraphDatabase
from chromadb.utils import embedding_functions
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
import chromadb
import openai
import streamlit as st
import os
import re
import io
import shutil
import zipfile
import gc
import logging
from datetime import datetime

from typing import Any

from ingestion import KDBIngestor
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# --- IMPORTS ---
# no langchain dependencies, use simple retrieval and OpenAI API

# --- CONFIGURACIÓN DE RUTAS ---
DATA_PATH = "./documentos_fuente"
CHROMA_PATH = "./db_chroma_kdb"
os.makedirs(DATA_PATH, exist_ok=True)

DEFAULT_COLLECTIONS = ["kdb_principal", "kdb_small", "kdb_large", "kdb_code"]
STRATEGY_OPTIONS = ["all", "char_overlap", "sentence_window",
                    "paragraph_window", "heading_window", "code_aware"]

NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USER", "")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# Configuración de página
st.set_page_config(page_title="Auditor KDB Pro",
                   layout="wide", page_icon="🕵️‍♂️")


LOGGER = logging.getLogger(__name__)


def _copiar_carpeta_recursiva(origen: str, destino_base: str) -> int:
    """Copia una carpeta completa dentro de `destino_base` recursivamente."""
    origen_abs = os.path.abspath(origen)
    destino_abs = os.path.abspath(destino_base)

    if not os.path.isdir(origen_abs):
        raise ValueError(f"La ruta no es una carpeta válida: {origen}")

    if origen_abs == destino_abs or origen_abs.startswith(
            destino_abs + os.sep):
        raise ValueError(
            "La carpeta origen no puede estar dentro de documentos_fuente.")

    base_name = os.path.basename(origen_abs.rstrip("\\/"))
    copied = 0
    ignored_dirs = {
        ".git",
        ".svn",
        ".hg",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".idea",
        ".vscode"}

    for root, dirs, files in os.walk(origen_abs, topdown=True):
        dirs[:] = [
            d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        rel = os.path.relpath(root, origen_abs)
        for file_name in files:
            src = os.path.join(root, file_name)
            if rel == ".":
                rel_target = os.path.join(base_name, file_name)
            else:
                rel_target = os.path.join(base_name, rel, file_name)
            dst = os.path.join(destino_base, rel_target)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
                copied += 1
            except PermissionError:
                LOGGER.warning("⚠️ Permiso denegado al copiar: %s", src)
            except OSError as exc:
                LOGGER.warning("⚠️ No se pudo copiar %s: %s", src, exc)

    return copied


def _extraer_zip_recursivo(uploaded_file: Any, destino_base: str) -> int:
    """Extrae un archivo ZIP cargado por Streamlit en `destino_base`."""
    data = uploaded_file.getvalue()
    extracted = 0

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue

            normalized = os.path.normpath(member.filename).replace("\\", "/")
            parts = [p for p in normalized.split("/") if p not in ("", ".")]
            if not parts or ".." in parts:
                continue

            dst = os.path.join(destino_base, *parts)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            with zf.open(member, "r") as src_stream, open(
                dst, "wb"
            ) as dst_stream:
                shutil.copyfileobj(src_stream, dst_stream)
            extracted += 1

    return extracted


def _limpiar_directorio(path: str) -> None:
    """Elimina el contenido de un directorio preservando la carpeta raíz."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
        return

    for name in os.listdir(path):
        target = os.path.join(path, name)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            try:
                os.remove(target)
            except FileNotFoundError:
                pass


def _reset_neo4j() -> bool:
    """Borra el grafo completo de Neo4j cuando la conexión está disponible."""
    if neo4j_driver is None:
        return False
    try:
        with neo4j_driver.session(database=NEO4J_DATABASE) as session:
            session.run("MATCH (n) DETACH DELETE n")
        return True
    except (Neo4jError, ServiceUnavailable):
        return False


def _reset_chroma_collections() -> int:
    """Elimina las colecciones `kdb_*` del cliente Chroma actual."""
    global vector_stores
    if chroma_client is None:
        return 0

    deleted = 0
    try:
        existing = chroma_client.list_collections()
    except (AttributeError, ValueError, RuntimeError):
        return 0

    for item in existing:
        name = getattr(item, "name", None)
        if not name:
            continue
        if not name.startswith("kdb_"):
            continue
        try:
            chroma_client.delete_collection(name)
            deleted += 1
        except (AttributeError, ValueError, RuntimeError):
            continue

    vector_stores = {}
    return deleted


def _limpieza_profunda_chroma() -> tuple[bool, str]:
    """Reinicia la carpeta de ChromaDB y limpia estado en memoria."""
    global chroma_client, vector_stores
    mensaje = ""

    try:
        _reset_chroma_collections()
    except (AttributeError, ValueError, RuntimeError):
        pass

    vector_stores = {}
    chroma_client = None
    gc.collect()

    try:
        if os.path.exists(CHROMA_PATH):
            shutil.rmtree(CHROMA_PATH)
        os.makedirs(CHROMA_PATH, exist_ok=True)
        return True, "db_chroma_kdb borrado y recreado"
    except OSError as exc:
        mensaje = (
            "Windows mantiene lock sobre chroma.sqlite3. "
            "Cierra la app y ejecuta: "
            "Remove-Item .\\db_chroma_kdb -Recurse -Force; "
            "New-Item .\\db_chroma_kdb -ItemType Directory"
        )
        return False, f"{mensaje}. Detalle: {exc}"

# --- INICIALIZACIÓN DE COMPONENTES ---


@st.cache_resource
def init_vector_stores() -> tuple[Any, dict[str, Any]]:
    """Inicializa cliente Chroma y colecciones de trabajo disponibles."""
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
    except (AttributeError, ValueError, RuntimeError) as exc:
        LOGGER.warning("No se pudo listar colecciones de Chroma: %s", exc)

    collections = {}
    for name in sorted(collection_names):
        collections[name] = client.get_or_create_collection(
            name=name,
            embedding_function=embedding_fn,
        )

    if not collections:
        collections["kdb_principal"] = client.create_collection(
            name="kdb_principal",
            embedding_function=embedding_fn
        )

    return client, collections


@st.cache_resource
def init_neo4j_driver() -> Driver | None:
    """Inicializa el driver de Neo4j y valida conectividad."""
    if not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD):
        return None
    try:
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run("RETURN 1")
        return driver
    except (Neo4jError, ServiceUnavailable):
        return None


chroma_client, vector_stores = init_vector_stores()
neo4j_driver = init_neo4j_driver()

# --- DEFINICIÓN DE AYUDA SIMPLE ---
# No usamos agentes: hacemos búsqueda y consulta directa con OpenAI

# initialize OpenAI client (new 1.0+ interface)
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

system_prompt = """Eres un agente de investigación profunda especializado en análisis exhaustivos de bases de código y temas.

**Idioma obligatorio:**
- Responde siempre en español.
- No uses inglés para encabezados ni secciones.
- Usa "Resumen" en lugar de "Summary".
- Si la pregunta viene en inglés, traduce tu respuesta al español.

**Directorio de trabajo:** {{cwd}}

Su función es:
- Analizar a fondo la estructura, los patrones y las convenciones de la base de código proporcionada.
- Investigar el tema en profundidad, relacionándolo con la base de código existente.
- Identificar tecnologías, dependencias y decisiones arquitectónicas relevantes.
- Identificar restricciones técnicas, riesgos y oportunidades.
- Presentar hallazgos estructurados con secciones claras.

**Formato de salida:**
Estructure su respuesta con estas secciones cuando sea relevante:
- **Resumen**: Resumen general de los hallazgos (2-3 oraciones).
- **Análisis de la base de código**: Patrones clave, pila tecnológica y convenciones encontradas.
- **Hallazgos de la investigación**: Hallazgos detallados sobre el tema solicitado.
- **Restricciones y riesgos**: Limitaciones o riesgos técnicos a considerar.
- **Recomendaciones**: Próximos pasos prácticos basados ​​en los hallazgos.

**Mejores prácticas:**
- Usar grep y glob para explorar la base de código sistemáticamente.
- Leer los archivos reales para comprender los detalles reales de la implementación.
- Ser específico: citar las rutas de los archivos y los patrones encontrados.
- Conectar los hallazgos de la investigación con las realidades del código fuente.
- Priorizar la profundidad y la precisión sobre la amplitud.
"""


def consultar_evidencia_kdb(
    query: str,
    strategy_filter: str = "all",
    collection_filter: str = "all",
    per_collection_k: int = 4,
    max_final_results: int = 8,
) -> list[dict[str, Any]]:
    """Wrapper compatible para recuperar resultados de Stage 2."""
    return _retrieve_vector_stage2(
        query=query,
        strategy_filter=strategy_filter,
        collection_filter=collection_filter,
        per_collection_k=per_collection_k,
        max_final_results=max_final_results,
    )["results"]


def _classify_query_intent(query: str) -> str:
    """Clasifica intención de consulta para ajustar retrieval."""
    query_lower = query.lower()
    if any(token in query_lower for token in ["impacto", "afecta", "depende", "blast radius"]):
        return "impact_analysis"
    if any(token in query_lower for token in ["error", "bug", "falla", "fallo", "exception", "traceback"]):
        return "bug_rootcause"
    if any(token in query_lower for token in ["seguridad", "vulnerabilidad", "inyeccion", "xss", "auth"]):
        return "security"
    if any(token in query_lower for token in ["performance", "rendimiento", "latencia", "optimizar"]):
        return "performance"
    if any(token in query_lower for token in ["refactor", "refactorizar", "restructurar", "modularizar"]):
        return "refactor_plan"
    if any(
        token in query_lower
        for token in ["todos", "todas", "listar", "listado", "cuales", "cuáles"]
    ):
        return "listing"
    if any(token in query_lower for token in ["cuantos", "cuántos", "total", "cantidad"]):
        return "counting"
    if any(token in query_lower for token in ["arquitectura", "componentes", "módulos", "modulos"]):
        return "architecture"
    return "how_it_works"


def _build_query_expansions(query: str, intent: str) -> list[str]:
    """Genera expansiones simples para mejorar recall en Stage 1."""
    expansions = [query]
    keywords = _extraer_keywords(query)
    if keywords:
        expansions.append(" ".join(keywords[:6]))
    if intent in {"listing", "counting"} and len(keywords) >= 2:
        expansions.append(" ".join(keywords[:2]))

    unique: list[str] = []
    seen = set()
    for item in expansions:
        normalized = item.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(item)
    return unique


def _resolve_stage1_k(intent: str, per_collection_k: int) -> int:
    """Define tamaño de recuperación Stage 1 según intención."""
    base = max(2, per_collection_k)
    if intent in {"listing", "counting"}:
        return max(20, base * 4)
    if intent in {"impact_analysis", "architecture"}:
        return max(12, base * 3)
    if intent in {"security", "performance", "refactor_plan"}:
        return max(14, base * 3)
    return max(10, base * 2)


def _retrieve_stage1_candidates(
    query: str,
    strategy_filter: str,
    collection_filter: str,
    per_collection_k: int,
    intent: str,
) -> dict[str, Any]:
    """Stage 1: recuperación de alta cobertura por colección."""
    if not vector_stores:
        return {"results": [], "stats": {"intent": intent, "stage1_k": 0}}

    if collection_filter != "all" and collection_filter in vector_stores:
        target_collections = {
            collection_filter: vector_stores[collection_filter]}
    elif collection_filter == "all":
        target_collections = vector_stores
    else:
        return {"results": [], "stats": {"intent": intent, "stage1_k": 0}}

    where = None
    if strategy_filter != "all":
        where = {"chunk_strategy": strategy_filter}

    stage1_k = _resolve_stage1_k(intent, per_collection_k)
    expanded_queries = _build_query_expansions(query, intent)
    all_docs: list[dict[str, Any]] = []

    for collection_name, collection in target_collections.items():
        for expanded_query in expanded_queries:
            try:
                query_args = {
                    "query_texts": [expanded_query],
                    "n_results": stage1_k,
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
                        "distance": distance,
                        "retrieved_with": expanded_query,
                    })
            except (AttributeError, TypeError, ValueError):
                continue

    deduped_stage1 = []
    seen_stage1 = set()
    for item in all_docs:
        key = item.get("parent_id") or (item.get("source", ""), item.get("text", "")[:220])
        if key in seen_stage1:
            continue
        seen_stage1.add(key)
        deduped_stage1.append(item)

    return {
        "results": deduped_stage1,
        "stats": {
            "intent": intent,
            "stage1_k": stage1_k,
            "expanded_queries": expanded_queries,
            "stage1_raw": len(all_docs),
            "stage1_deduped": len(deduped_stage1),
        },
    }


def _score_stage2_candidate(
    item: dict[str, Any],
    query_keywords: list[str],
    intent: str,
) -> float:
    """Calcula score de Stage 2 combinando similitud y señales léxicas."""
    text = (item.get("text", "") or "").lower()
    source = (item.get("source", "") or "").lower()
    distance = item.get("distance")

    if isinstance(distance, (int, float)):
        clipped = max(0.0, min(float(distance), 2.0))
        distance_score = 1.0 - (clipped / 2.0)
    else:
        distance_score = 0.20

    if query_keywords:
        text_hits = sum(1 for keyword in query_keywords if keyword in text)
        source_hits = sum(1 for keyword in query_keywords if keyword in source)
        keyword_score = text_hits / len(query_keywords)
        source_score = source_hits / len(query_keywords)
    else:
        keyword_score = 0.0
        source_score = 0.0

    if intent in {"listing", "counting"}:
        return (0.30 * distance_score) + (0.55 * keyword_score) + (0.15 * source_score)
    return (0.55 * distance_score) + (0.35 * keyword_score) + (0.10 * source_score)


def _tokenize_for_mmr(text: str) -> set[str]:
    """Tokeniza texto para similitud léxica en MMR."""
    return set(re.findall(r"[a-zA-ZáéíóúÁÉÍÓÚñÑ0-9_]{3,}", (text or "").lower()))


def _jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Calcula similitud de Jaccard entre dos conjuntos de tokens."""
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a.intersection(tokens_b))
    union = len(tokens_a.union(tokens_b))
    if union == 0:
        return 0.0
    return intersection / union


def _resolve_mmr_lambda(intent: str) -> float:
    """Define balance relevancia/diversidad para MMR según intención."""
    if intent in {"listing", "counting"}:
        return 0.35
    if intent in {"impact_analysis", "architecture", "refactor_plan"}:
        return 0.50
    if intent in {"security", "performance"}:
        return 0.58
    return 0.62


def _rerank_stage2(
    candidates: list[dict[str, Any]],
    query: str,
    intent: str,
    max_final_results: int,
) -> dict[str, Any]:
    """Stage 2: reranking MMR (relevancia + diversidad)."""
    if not candidates:
        return {"results": [], "stats": {"stage2_scored": 0, "stage2_final": 0}}

    query_keywords = _extraer_keywords(query)
    scored = []
    for item in candidates:
        score = _score_stage2_candidate(item, query_keywords, intent)
        scored.append({**item, "stage2_score": score})

    scored.sort(
        key=lambda row: (
            row.get("stage2_score", 0.0),
            -len((row.get("text", "") or "")),
        ),
        reverse=True,
    )

    safe_max = max(1, max_final_results)
    if len(scored) <= safe_max:
        return {
            "results": scored,
            "stats": {
                "stage2_scored": len(scored),
                "stage2_final": len(scored),
                "stage2_mmr_lambda": _resolve_mmr_lambda(intent),
            },
        }

    candidate_pool = []
    for item in scored:
        content = " ".join([
            item.get("source", "") or "",
            item.get("text", "") or "",
        ])
        candidate_pool.append({
            **item,
            "_mmr_tokens": _tokenize_for_mmr(content),
        })

    selected: list[dict[str, Any]] = []
    lambda_mmr = _resolve_mmr_lambda(intent)

    while candidate_pool and len(selected) < safe_max:
        best_idx = 0
        best_mmr_score = float("-inf")

        for idx, candidate in enumerate(candidate_pool):
            relevance = float(candidate.get("stage2_score", 0.0))
            if not selected:
                diversity_penalty = 0.0
            else:
                max_similarity = max(
                    _jaccard_similarity(
                        candidate.get("_mmr_tokens", set()),
                        selected_item.get("_mmr_tokens", set()),
                    )
                    for selected_item in selected
                )
                diversity_penalty = max_similarity

            mmr_score = (lambda_mmr * relevance) - (
                (1.0 - lambda_mmr) * diversity_penalty
            )
            if mmr_score > best_mmr_score:
                best_mmr_score = mmr_score
                best_idx = idx

        chosen = candidate_pool.pop(best_idx)
        chosen["stage2_mmr_score"] = best_mmr_score
        selected.append(chosen)

    for item in selected:
        item.pop("_mmr_tokens", None)

    return {
        "results": selected,
        "stats": {
            "stage2_scored": len(scored),
            "stage2_final": len(selected),
            "stage2_mmr_lambda": lambda_mmr,
        },
    }


def _retrieve_vector_stage2(
    query: str,
    strategy_filter: str,
    collection_filter: str,
    per_collection_k: int,
    max_final_results: int,
) -> dict[str, Any]:
    """Pipeline vectorial completo en 2 etapas."""
    intent = _classify_query_intent(query)
    stage1 = _retrieve_stage1_candidates(
        query=query,
        strategy_filter=strategy_filter,
        collection_filter=collection_filter,
        per_collection_k=per_collection_k,
        intent=intent,
    )
    stage2 = _rerank_stage2(
        candidates=stage1.get("results", []),
        query=query,
        intent=intent,
        max_final_results=max_final_results,
    )
    return {
        "results": stage2.get("results", []),
        "stats": {
            **stage1.get("stats", {}),
            **stage2.get("stats", {}),
        },
    }


def _extraer_keywords(query: str) -> list[str]:
    """Extrae términos relevantes del texto de consulta."""
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


def consultar_evidencia_grafo(
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recupera evidencia estructural desde Neo4j usando keywords."""
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
            docs: list[dict[str, Any]] = []
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
    except (Neo4jError, ServiceUnavailable):
        return []


def recuperar_evidencia_hibrida(
    query: str,
    strategy_filter: str = "all",
    collection_filter: str = "all",
    per_collection_k: int = 4,
    max_final_results: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    """Combina evidencia semántica de Chroma con evidencia de Neo4j."""
    vector_pipeline = _retrieve_vector_stage2(
        query=query,
        strategy_filter=strategy_filter,
        collection_filter=collection_filter,
        per_collection_k=per_collection_k,
        max_final_results=max_final_results,
    )
    evidencia_vector = vector_pipeline.get("results", [])
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
            combined.append({"source": item.get("source", ""),
                            "text": item.get("text", "")})
            seen.add(key)

    return {
        "vector": evidencia_vector,
        "graph": evidencia_grafo,
        "combined": combined,
        "retrieval_stats": vector_pipeline.get("stats", {}),
    }


def generar_respuesta(
    user_question: str,
    strategy_filter: str = "all",
    collection_filter: str = "all",
    per_collection_k: int = 4,
    max_final_results: int = 8,
    evidencia_precomputada: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Genera una respuesta final usando contexto híbrido y OpenAI."""
    evidencia = evidencia_precomputada
    if evidencia is None:
        evidencia = recuperar_evidencia_hibrida(
            user_question,
            strategy_filter=strategy_filter,
            collection_filter=collection_filter,
            per_collection_k=per_collection_k,
            max_final_results=max_final_results,
        )

    evidencia_semantica = "\n\n".join(
        [
            (
                f"Fuente: {d['source']} | "
                f"Colección: {d.get('collection', '')} | "
                f"Estrategia: {d.get('chunk_strategy', '')}\n{d['text']}"
            )
            for d in evidencia["vector"]
        ]
    )

    evidencia_estructural = "\n\n".join([
        (
            f"Fuente: {d['source']} | "
            f"Posición: {d.get('position', 0)} | "
            f"Score estructural: {d.get('score', 0)}\n"
            f"Previo: {d.get('prev_text', '')}\n"
            f"Actual: {d.get('text', '')}\n"
            f"Siguiente: {d.get('next_text', '')}"
        )
        for d in evidencia["graph"]
    ])

    if not evidencia_semantica:
        evidencia_semantica = "No se recuperó evidencia semántica en ChromaDB."
    if not evidencia_estructural:
        evidencia_estructural = (
            "No se recuperó evidencia estructural en Neo4j."
        )

    prompt = (
        f"{system_prompt}\n\n"
        "IMPORTANTE: RESPONDE SOLO EN ESPAÑOL. "
        "NO USES INGLÉS EN TÍTULOS O CONTENIDO.\n\n"
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
    st.subheader("📦 Gestión de Conocimiento")

    # Input para URL de GitHub
    github_url = st.text_input(
        "URL de Repositorio GitHub",
        placeholder="https://github.com/user/repo")

    if st.button("🚀 Ingestar Proyecto"):
        if github_url:
            with st.status(
                "Procesando repositorio...", expanded=True
            ) as status:
                try:
                    st.write("📥 Clonando y limpiando código...")
                    # Instanciamos el ingestor con las rutas correctas
                    ingestor = KDBIngestor(
                        DATA_PATH, CHROMA_PATH, chroma_client=chroma_client)

                    # Llamamos al nuevo método run que acepta la URL
                    ingestor.run(github_url=github_url)

                    status.update(label="✅ Ingesta completada!",
                                  state="complete", expanded=False)
                    st.success(
                        "El repositorio ha sido indexado en ChromaDB y Neo4j.")

                    # Opcional: Reiniciar estado para limpiar caché de búsqueda
                    # si fuera necesario
                    st.rerun()
                except (RuntimeError, ValueError, OSError) as e:
                    status.update(label="❌ Error en la ingesta", state="error")
                    st.error(f"Detalles: {e}")
        else:
            st.warning("Por favor, introduce una URL válida.")
    st.divider()
    st.header("📥 Ingesta de Evidencia")
    if neo4j_driver is None:
        st.warning(
            "Neo4j no configurado. El sistema funcionará en modo vectorial.")
    else:
        st.success("Neo4j conectado. Validación estructural habilitada.")
    files = st.file_uploader(
        "Browse files (archivos o .zip de carpeta)",
        accept_multiple_files=True,
        type=["pdf", "xlsx", "xls", "zip", "txt", "md",
              "py", "js", "ts", "java", "json", "yml", "yaml"]
    )
    carpeta_local = st.text_input(
        "Ruta de carpeta local (opcional, carga recursiva)",
        placeholder=r"C:\ruta\a\mi\repositorio"
    )

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
    retrieval_k = st.slider(
        "Resultados por colección (top-k)",
        min_value=2,
        max_value=30,
        value=12,
        step=1,
        help=(
            "Sube este valor si la consulta necesita cobertura completa "
            "(por ejemplo: listar todos los DAO)."
        ),
    )
    retrieval_max = st.slider(
        "Máximo evidencia combinada",
        min_value=4,
        max_value=60,
        value=24,
        step=1,
        help="Cantidad máxima de evidencias únicas que pasan al prompt.",
    )
    debug_query_mode = st.checkbox(
        "Modo diagnóstico de consulta",
        value=True,
        help="Muestra cuántos chunks/fuentes se recuperan antes de responder.",
    )

    if st.button("🧹 Limpiar fuente e índices"):
        with st.status("Limpiando datos previos...", expanded=True) as status:
            try:
                _limpiar_directorio(DATA_PATH)
                st.write("🧼 documentos_fuente limpiado")

                deleted_collections = _reset_chroma_collections()
                st.write(
                    f"🧼 Chroma limpiado ({deleted_collections} colecciones)")

                neo4j_cleaned = _reset_neo4j()
                if neo4j_cleaned:
                    st.write("🕸️ Neo4j limpiado")
                else:
                    st.write(
                        "ℹ️ Neo4j no estaba disponible "
                        "o no se pudo limpiar"
                    )

                status.update(label="✅ Limpieza completa", state="complete")
                st.success(
                    "Base limpia. Ya puedes cargar nueva evidencia "
                    "sin duplicados."
                )
                st.rerun()
            except (OSError, ValueError, RuntimeError) as e:
                status.update(label="⚠️ Limpieza incompleta", state="error")
                st.error(f"No se pudo completar la limpieza: {e}")

    if st.button("🧨 Limpieza profunda (reinicio de DB)"):
        with st.status(
            "Ejecutando limpieza profunda...", expanded=True
        ) as status:
            try:
                _limpiar_directorio(DATA_PATH)
                st.write("🧼 documentos_fuente limpiado")

                deep_ok, deep_msg = _limpieza_profunda_chroma()
                if deep_ok:
                    st.write("🧼 Chroma físico reiniciado")
                else:
                    st.write(
                        "⚠️ No se pudo borrar físicamente "
                        "la DB en caliente"
                    )

                neo4j_cleaned = _reset_neo4j()
                if neo4j_cleaned:
                    st.write("🕸️ Neo4j limpiado")
                else:
                    st.write(
                        "ℹ️ Neo4j no estaba disponible "
                        "o no se pudo limpiar"
                    )

                if deep_ok:
                    status.update(
                        label="✅ Limpieza profunda completa", state="complete")
                    st.success("Limpieza profunda completada.")
                    st.rerun()
                else:
                    status.update(
                        label="⚠️ Limpieza profunda parcial", state="error")
                    st.warning(deep_msg)
            except (OSError, ValueError, RuntimeError) as e:
                status.update(
                    label="⚠️ Limpieza profunda incompleta", state="error")
                st.error(f"No se pudo completar la limpieza profunda: {e}")

    if st.button("🚀 Indexar Nueva Evidencia"):
        if files or carpeta_local.strip():
            with st.status("Procesando...", expanded=True) as status:
                total_copiados = 0
                log_lines: list[str] = []
                progress_box = st.empty()
                detail_box = st.empty()
                log_box = st.empty()

                def add_log(message: str) -> None:
                    """Acumula y muestra logs recientes del proceso de ingesta."""
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    log_lines.append(f"[{timestamp}] {message}")
                    log_box.code(
                        "\n".join(log_lines[-80:]),
                        language="text",
                    )

                progress_box.progress(0, text="Preparando ingesta...")
                detail_box.info(
                    "Esperando archivos para iniciar análisis de ingesta..."
                )

                for f in files or []:
                    if f.name.lower().endswith(".zip"):
                        count = _extraer_zip_recursivo(f, DATA_PATH)
                        total_copiados += count
                        st.write(
                            f"📦 ZIP extraído: {f.name} ({count} archivos)")
                    else:
                        file_path = os.path.join(DATA_PATH, f.name)
                        os.makedirs(os.path.dirname(file_path), exist_ok=True)
                        with open(file_path, "wb") as buffer:
                            buffer.write(f.getbuffer())
                        total_copiados += 1

                if carpeta_local.strip():
                    count = _copiar_carpeta_recursiva(
                        carpeta_local.strip(), DATA_PATH)
                    total_copiados += count
                    st.write(
                        f"📁 Carpeta copiada recursivamente ({count} archivos)")

                st.write(f"📚 Archivos listos para indexar: {total_copiados}")
                add_log(f"Archivos listos para indexar: {total_copiados}")

                st.write(
                    "Analizando contenido y generando embeddings + "
                    "grafo (ChromaDB + Neo4j)..."
                )

                ingest_state = {
                    "processed_files": 0,
                    "total_files": 0,
                    "inserted": 0,
                    "failed": 0,
                    "total_chunks": 0,
                }

                def on_ingest_event(event: dict[str, Any]) -> None:
                    """Actualiza barra de progreso y sysout durante ingesta."""
                    event_type = event.get("event", "")

                    if event_type == "scan_complete":
                        ingest_state["total_files"] = int(
                            event.get("total_files", 0)
                        )
                        supported = int(event.get("supported_files", 0))
                        add_log(
                            "Escaneo completo: "
                            f"{supported}/{ingest_state['total_files']} "
                            "archivos soportados"
                        )
                        return

                    if event_type == "file_processing":
                        ingest_state["processed_files"] = int(
                            event.get("index", 0)
                        )
                        total_files = max(1, int(event.get("total_files", 1)))
                        file_progress = (
                            ingest_state["processed_files"] / total_files
                        )
                        global_progress = min(0.4, file_progress * 0.4)
                        current_file = event.get("file", "")
                        progress_box.progress(
                            int(global_progress * 100),
                            text=(
                                "Cargando archivos "
                                f"({ingest_state['processed_files']}/"
                                f"{total_files})"
                            ),
                        )
                        detail_box.info(
                            "Archivo actual: "
                            f"{current_file} | "
                            f"Avance carga: {file_progress * 100:.1f}%"
                        )
                        add_log(f"Procesando archivo: {current_file}")
                        return

                    if event_type == "file_unsupported":
                        add_log(
                            "Formato no soportado (omitido): "
                            f"{event.get('file', '')}"
                        )
                        return

                    if event_type == "file_error":
                        add_log(
                            "Error leyendo archivo: "
                            f"{event.get('file', '')} | "
                            f"{event.get('error', '')}"
                        )
                        return

                    if event_type == "profile_chunked":
                        add_log(
                            "Chunking perfil="
                            f"{event.get('profile', '')} "
                            f"estrategia={event.get('strategy', '')} "
                            f"chunks={event.get('chunks', 0)} "
                            f"chunk_size={event.get('chunk_size', 0)} "
                            f"overlap={event.get('chunk_overlap', 0)}"
                        )
                        detail_box.info(
                            "Perfil activo: "
                            f"{event.get('profile', '')} | "
                            f"Estrategia: {event.get('strategy', '')} | "
                            f"Chunk size: {event.get('chunk_size', 0)} | "
                            f"Overlap: {event.get('chunk_overlap', 0)}"
                        )
                        return

                    if event_type == "upsert_start":
                        ingest_state["total_chunks"] = int(
                            event.get("total_chunks", 0)
                        )
                        add_log(
                            "Upsert iniciado: "
                            f"chunks={ingest_state['total_chunks']} "
                            f"max_batch_tokens={event.get('max_batch_tokens', 0)} "
                            f"max_batch_chars={event.get('max_batch_chars', 0)}"
                        )
                        return

                    if event_type == "upsert_batch_result":
                        ingest_state["inserted"] = int(event.get("inserted", 0))
                        ingest_state["failed"] = int(event.get("failed", 0))
                        ingest_state["total_chunks"] = int(
                            event.get("total_chunks", 0)
                        )

                        upsert_progress = float(event.get("progress", 0.0))
                        global_progress = min(0.95, 0.4 + upsert_progress * 0.55)
                        progress_box.progress(
                            int(global_progress * 100),
                            text=(
                                "Insertando embeddings "
                                f"({upsert_progress * 100:.1f}%)"
                            ),
                        )
                        detail_box.info(
                            "Chunks OK: "
                            f"{ingest_state['inserted']} | "
                            "Chunks fallo: "
                            f"{ingest_state['failed']} | "
                            "Total chunks: "
                            f"{ingest_state['total_chunks']}"
                        )
                        add_log(
                            "Batch resultado: "
                            f"size={event.get('batch_size', 0)} "
                            f"tokens={event.get('batch_tokens', 0)} "
                            f"chars={event.get('batch_chars', 0)} "
                            f"ok={ingest_state['inserted']} "
                            f"fail={ingest_state['failed']}"
                        )
                        return

                    if event_type == "upsert_bad_request":
                        add_log(
                            "BadRequestError: "
                            f"batch_size={event.get('batch_size', 0)} "
                            f"sample_id={event.get('sample_id', '')}"
                        )
                        return

                    if event_type == "upsert_split_batch":
                        add_log(
                            "Split batch por BadRequestError: "
                            f"batch_size={event.get('batch_size', 0)} "
                            f"depth={event.get('depth', 0)}"
                        )
                        return

                    if event_type == "upsert_end":
                        add_log(
                            "Upsert finalizado: "
                            f"inserted={event.get('inserted', 0)} "
                            f"failed={event.get('failed', 0)}"
                        )
                        return

                    if event_type == "run_no_documents":
                        add_log("No se encontraron documentos para procesar")
                        return

                    if event_type == "run_complete":
                        progress_box.progress(100, text="Ingesta completada")
                        add_log("Ingesta finalizada exitosamente")

                # Pasar cliente ChromaDB en caché para evitar múltiples
                # instancias
                ingestor = KDBIngestor(
                    DATA_PATH,
                    CHROMA_PATH,
                    chroma_client=chroma_client,
                    progress_callback=on_ingest_event,
                )
                ingestor.run()

                status.update(label="✅ KDB Actualizada", state="complete")
            st.success("Archivos listos para auditoría")
            st.rerun()
        else:
            st.warning(
                "Sube archivos/.zip o indica una carpeta local para indexar.")

    st.divider()
    if st.button("🗑️ Limpiar Historial de Chat"):
        st.session_state.messages = []
        st.rerun()

# --- PANEL DE CHAT ---
st.title("🕵️‍♂️ Panel de Auditoría Inteligente")
st.caption(
    "Motor: OpenAI | RAG Híbrido: ChromaDB (semántico) + Neo4j (estructural)")

# Inicializar historial
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "Listo para auditar. Sube tus archivos en la barra lateral "
                "para comenzar."
            ),
        }
    ]

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
                evidencia_debug = recuperar_evidencia_hibrida(
                    user_input,
                    strategy_filter=selected_strategy,
                    collection_filter=selected_collection,
                    per_collection_k=retrieval_k,
                    max_final_results=retrieval_max,
                )

                response_text = generar_respuesta(
                    user_input,
                    strategy_filter=selected_strategy,
                    collection_filter=selected_collection,
                    per_collection_k=retrieval_k,
                    max_final_results=retrieval_max,
                    evidencia_precomputada=evidencia_debug,
                )
            except (
                APIError,
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                AuthenticationError,
                BadRequestError,
                ValueError,
                RuntimeError,
            ) as e:
                response_text = f"❌ Error al generar respuesta: {str(e)}"

            if debug_query_mode and "evidencia_debug" in locals():
                vector_docs = evidencia_debug.get("vector", [])
                graph_docs = evidencia_debug.get("graph", [])
                combined_docs = evidencia_debug.get("combined", [])
                retrieval_stats = evidencia_debug.get("retrieval_stats", {})
                unique_sources = sorted(
                    {
                        item.get("source", "")
                        for item in combined_docs
                        if item.get("source", "")
                    }
                )
                with st.expander("🧪 Diagnóstico de consulta", expanded=False):
                    st.write(
                        f"Vector: {len(vector_docs)} | "
                        f"Grafo: {len(graph_docs)} | "
                        f"Combinado: {len(combined_docs)}"
                    )
                    st.write(
                        "Intent: "
                        f"{retrieval_stats.get('intent', 'n/a')} | "
                        "Stage1_k: "
                        f"{retrieval_stats.get('stage1_k', 0)} | "
                        "S1 raw/dedup: "
                        f"{retrieval_stats.get('stage1_raw', 0)}/"
                        f"{retrieval_stats.get('stage1_deduped', 0)} | "
                        "S2 final: "
                        f"{retrieval_stats.get('stage2_final', 0)}"
                    )
                    st.write(
                        f"top-k por colección: {retrieval_k} | "
                        f"máx evidencia: {retrieval_max}"
                    )
                    if unique_sources:
                        st.write("Fuentes recuperadas:")
                        st.code("\n".join(unique_sources[:120]), language="text")
                    else:
                        st.write("Sin fuentes recuperadas para esta consulta.")

            st.markdown(response_text)
            st.session_state.messages.append(
                {"role": "assistant", "content": response_text})
