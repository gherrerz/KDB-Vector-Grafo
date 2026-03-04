Actúa como desarrollador senior Python y mantén este sistema de auditoría documental con RAG híbrido.

Objetivo:
Conservar y extender una app en Streamlit que indexa evidencia en ChromaDB y Neo4j, y responde consultas técnicas en español usando recuperación híbrida y trazabilidad de fuentes.

Requisitos técnicos vigentes:
- Python 3.10+
- pydantic>=2.0.0
- chromadb>=0.5.0
- openai>=1.0.0
- neo4j>=5.0.0
- streamlit>=1.18.1
- pypdf>=3.0.0
- openpyxl>=3.0.10
- unstructured[all-docs]>=0.6.1
- python-dotenv>=0.21.0

Arquitectura vigente:
1) ingestion.py
- Clase `KDBIngestor(data_path, db_path, chroma_client=None, progress_callback=None)`.
- Carga archivos de `./documentos_fuente` (PDF, Excel, texto, config y código).
- Chunking por perfiles/estrategia (small/large/code) con límites por tokens y caracteres.
- Upsert robusto con split recursivo de lotes/chunks cuando hay `BadRequestError`.
- Indexación en ChromaDB multi-colección (`kdb_principal`, `kdb_small`, `kdb_large`, `kdb_code`).
- Indexación en Neo4j:
  - Continuidad documental: `(:Document)-[:HAS_CHUNK]->(:Chunk)-[:NEXT]->(:Chunk)`
  - Grafo de código: `(:CodeFile)-[:DECLARES]->(:CodeEntity)` y `(:CodeEntity)-[:DEPENDS_ON]->(:CodeEntity)`
- Si Neo4j no está configurado, continuar en modo vectorial sin fallar.
- Emisión de eventos de progreso para UI (`scan_complete`, `file_processing`, `profile_chunked`, `upsert_*`, `run_complete`).

2) app.py
- Sidebar para carga de archivos, ZIP o carpeta local y para indexado desde URL GitHub.
- Botones de limpieza normal y limpieza profunda de índices.
- Recuperación híbrida:
  - Stage 1: recuperación de alta cobertura por intención y expansiones de consulta.
  - Stage 2: reranking MMR (relevancia + diversidad) con métricas de diagnóstico.
  - Evidencia estructural desde Neo4j con keywords y continuidad `prev/next`.
- Combinar evidencia vectorial + estructural para construir el prompt final.
- Llamar OpenAI Chat Completions (cliente v1) con `gpt-4o`, `temperature=0`.
- Responder siempre en español técnico, citando fuentes y declarando falta de evidencia cuando aplique.

Variables de entorno esperadas:
OPENAI_API_KEY
OPENAI_MODEL (opcional)
NEO4J_URI
NEO4J_USER
NEO4J_PASSWORD
NEO4J_DATABASE

Criterios de calidad:
- Manejo de errores robusto y trazable.
- Código simple, sin LangChain/LangGraph.
- Compatibilidad Windows.
- Evitar código muerto y mantener documentación sincronizada con cambios de arquitectura.