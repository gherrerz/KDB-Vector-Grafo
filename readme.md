# 🕵️‍♂️ Auditor KDB Pro (RAG Híbrido)

Auditor KDB Pro es una aplicación de auditoría técnica con Streamlit que implementa un **RAG híbrido**:

- **Validación semántica:** ChromaDB (similitud vectorial)
````markdown
# 🕵️‍♂️ Auditor KDB Pro (RAG Híbrido)

Auditor KDB Pro es una aplicación de auditoría técnica con Streamlit que implementa un **RAG híbrido**:

- **Validación semántica:** ChromaDB (similitud vectorial)
- **Validación estructural:** Neo4j (grafo de continuidad documental)

El sistema responde en español, cita fuentes y mantiene trazabilidad basada en evidencia.

---

## Estado actual del proyecto (3-mar-2026)

Breve resumen:

- La aplicación Streamlit arranca localmente y la UI está disponible (ej. http://localhost:8503).
- Se actualizaron dependencias y se añadieron utilidades para compatibilidad con Python 3.14, además de parches temporales aplicados en el `venv` para `chromadb`/`pydantic`.
- Se implementó la capacidad de clonar repositorios GitHub y limpiarlos antes de indexar (`scripts/github_loader.py`).

Cambios y mejoras relevantes:

- `requirements.txt` actualizado (pydantic >=2, chromadb >=0.5.0, GitPython instalado).
- `app.py`:
  - `@st.cache_resource` en inicializadores de ChromaDB y Neo4j para evitar reinicios y conflictos de instancias.
  - Correcciones en rutas y variables (`DATA_PATH`, `CHROMA_PATH`).
  - Integración con el ingestor para reusar el cliente Chroma en caché.
- `ingestion.py`:
  - `KDBIngestor.__init__` ahora acepta un `chroma_client` opcional.
  - `run(github_url=...)` soporta descarga y limpieza de repositorios antes de indexar.
- `scripts/github_loader.py`: nuevo loader para clonar y filtrar repositorios.
- `pydantic_patch.py`: parche temporal para mitigar errores de inferencia de `pydantic` en Python 3.14 (se aplicó localmente para permitir ejecución).

Notas importantes sobre compatibilidad y seguridad:

- Las modificaciones aplicadas directamente dentro del `venv` y el parche de `pydantic` son soluciones temporales para que el proyecto funcione en tu entorno actual (Python 3.14). Recomendado: usar Python 3.13 o esperar una actualización de `chromadb`/`pydantic` upstream y revertir los parches locales.
- Una clave OpenAI fue probada y añadida en `.env` para desarrollo; si alguna clave fue expuesta, revócala inmediatamente en https://platform.openai.com/account/api-keys y crea una nueva.

---

## ✅ Funcionalidades

- Ingesta de documentos `.pdf`, `.xlsx`, `.xls` y ficheros de texto/código.
- Indexación semántica con ChromaDB (múltiples colecciones: `kdb_principal`, `kdb_small`, `kdb_large`, `kdb_code`).
- Indexación estructural en Neo4j (Document -> Chunk -> NEXT).
- Ingesta recursiva de carpetas y clonación de repositorios GitHub para indexado.
- Consulta híbrida (semántica + estructural) y generación de respuestas con OpenAI.

---

## ⚙️ Rápido: instalación y ejecución (recomendado)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
# Opcional: levantar Neo4j con el script si deseas capacidad estructural
powershell -ExecutionPolicy Bypass -File .\scripts\setup_neo4j.ps1 -Password "TuPasswordSeguro"
.\.venv\Scripts\python.exe .\scripts\check_neo4j.py
python -m streamlit run app.py
```

La app mostrará la URL local en la consola de Streamlit (ej. `http://localhost:8503`).

---

## ¿Cómo indexar?

1. Abre la UI en el navegador.
2. En el sidebar sube archivos o indica una carpeta local para indexar.
3. O usa la sección "Ingestar Proyecto" para pegar la URL de un repo GitHub y pulsar "Ingestar".
4. Espera a que la barra de estado indique finalizado; luego formula preguntas en el chat.

---

## Dependencias clave (actualizadas)

- `pydantic>=2.0.0`
- `chromadb>=0.5.0`
- `openai>=1.0.0`
- `neo4j>=5.0.0`
- `streamlit>=1.18.1`

---

## Problemas comunes y recomendaciones

- Si ves errores de `pydantic`/`chromadb` con Python 3.14, crea un entorno con Python 3.13 y reinstala las dependencias para una solución más estable.
- Si aparece el error "An instance of Chroma already exists for ./db_chroma_kdb with different settings", reinicia la app o elimina la carpeta `db_chroma_kdb` (asegúrate de respaldar datos si son importantes).  Ahora `app.py` reusa el cliente Chroma en caché para evitar ese conflicto.

---

## Seguridad

- Nunca comprometas claves en commits. Revoca y regenera claves si se exponen.
- Mantén `.env` fuera del repositorio y usa un gestor de secretos en producción.

---

Si quieres, actualizo este README con un `README_RUN.md` con comandos y recomendaciones para migrar a Python 3.13 y cómo limpiar los parches locales en el `venv`.
```