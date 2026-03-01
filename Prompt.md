Actúa como desarrollador senior Python y crea un sistema de auditoría documental con RAG híbrido.

Objetivo:
Construir una app en Streamlit que permita subir PDFs/Excels, indexar semánticamente en ChromaDB y estructuralmente en Neo4j, y responder consultas con evidencia combinada.

Requisitos técnicos:
- Python 3.10+
- pydantic>=1.10.0,<2.0.0
- chromadb==0.4.22
- openai>=1.0.0
- neo4j>=5.0.0
- streamlit
- pypdf
- openpyxl
- python-dotenv

Arquitectura requerida:
1) ingestion.py
- Crear clase KDBIngestor(data_path, db_path)
- Cargar .pdf, .xlsx, .xls desde ./documentos_fuente
- Dividir texto en chunks con overlap
- Indexar en ChromaDB colección kdb_principal
- Crear grafo en Neo4j:
  - (:Document {name})
  - (:Chunk {id, text, position, source})
  - (Document)-[:HAS_CHUNK]->(Chunk)
  - (Chunk)-[:NEXT]->(Chunk)
- Si Neo4j no está configurado, continuar en modo vectorial sin fallar

2) app.py
- Streamlit con sidebar para subir archivos
- Botón “Indexar Nueva Evidencia” que ejecute KDBIngestor
- Consulta híbrida:
  - consultar_evidencia_kdb(query): top-k semántico
  - consultar_evidencia_grafo(query): búsqueda por keywords + continuidad prev/next
  - combinar resultados en prompt final
- Llamar API OpenAI (cliente v1) con model gpt-4o y temperature=0
- Responder en español técnico, citar fuentes, y declarar falta de evidencia cuando aplique

Variables de entorno esperadas:
OPENAI_API_KEY
OPENAI_MODEL (opcional)
NEO4J_URI
NEO4J_USER
NEO4J_PASSWORD
NEO4J_DATABASE

Criterios de calidad:
- Manejo de errores robusto
- Código simple, sin LangChain/LangGraph
- Compatibilidad Windows
- Documentación de ejecución en readme.md