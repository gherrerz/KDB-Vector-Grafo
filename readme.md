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

## Estado actual del proyecto (4-mar-2026)

Breve resumen:

- La aplicación Streamlit opera de forma estable en entorno local.
- La búsqueda híbrida usa un pipeline vectorial en 2 etapas (recall + reranking) y validación estructural con Neo4j.
- La ingesta reporta progreso en vivo (escaneo, chunking, batches, errores BadRequest y cierre).

Cambios y mejoras relevantes:

- `app.py`:
  - Retrieval en 2 etapas: Stage 1 de alta cobertura por intención y Stage 2 con reranking MMR (relevancia + diversidad).
  - Clasificación de intención (`listing`, `counting`, `impact_analysis`, `bug_rootcause`, `architecture`, `security`, `performance`, `refactor_plan`).
  - Diagnóstico de consulta en UI: `intent`, `stage1_k`, `stage1_raw`, `stage1_deduped`, `stage2_scored`, `stage2_final`, `stage2_mmr_lambda`.
  - Respuestas forzadas en español en el prompt de sistema.
- `ingestion.py`:
  - Ingesta robusta con guardas por tokens/caracteres y split recursivo en lotes para evitar overflow de embeddings.
  - Emisión de eventos de progreso para trazabilidad operativa de la indexación.
  - Indexación combinada en Chroma + Neo4j (continuidad documental y entidades/calls de código para análisis de dependencia).
- `scripts/github_loader.py`:
  - Carga de repositorios GitHub con limpieza de artefactos no relevantes previo a indexación.

Notas importantes sobre compatibilidad y seguridad:

- Las modificaciones aplicadas directamente dentro del `venv` y el parche de `pydantic` son soluciones temporales para que el proyecto funcione en tu entorno actual (Python 3.14). Recomendado: usar Python 3.13 o esperar una actualización de `chromadb`/`pydantic` upstream y revertir los parches locales.
- Una clave OpenAI fue probada y añadida en `.env` para desarrollo; si alguna clave fue expuesta, revócala inmediatamente en https://platform.openai.com/account/api-keys y crea una nueva.

---

## ✅ Funcionalidades

- Ingesta de documentos `.pdf`, `.xlsx`, `.xls` y archivos de texto/código/config.
- Indexación semántica multi-colección en ChromaDB (`kdb_principal`, `kdb_small`, `kdb_large`, `kdb_code`).
- Indexación estructural en Neo4j (`Document -> Chunk -> NEXT`) y grafo de entidades de código (`CodeFile`, `CodeEntity`, `DEPENDS_ON`).
- Ingesta recursiva desde carpeta local o repositorio GitHub.
- Consulta híbrida: Stage 1 (recall alto) + Stage 2 (MMR), combinada con evidencia estructural.
- Telemetría de ingesta en UI (progreso, archivo actual, chunks insertados/fallidos, batches divididos).

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

La app mostrará la URL local en la consola de Streamlit (normalmente `http://localhost:8501`, salvo que el puerto esté ocupado).

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
- `unstructured[all-docs]>=0.6.1`
- `pypdf>=3.0.0`
- `openpyxl>=3.0.10`

---

## Problemas comunes y recomendaciones

- Si ves errores de `pydantic`/`chromadb` con Python 3.14, crea un entorno con Python 3.13 y reinstala las dependencias para una solución más estable.
- Si aparece el error "An instance of Chroma already exists for ./db_chroma_kdb with different settings", reinicia la app o elimina la carpeta `db_chroma_kdb` (asegúrate de respaldar datos si son importantes).  Ahora `app.py` reusa el cliente Chroma en caché para evitar ese conflicto.

---

## Seguridad

- Nunca comprometas claves en commits. Revoca y regenera claves si se exponen.
- Mantén `.env` fuera del repositorio y usa un gestor de secretos en producción.

---

## 🧪 Calidad técnica (Checklist antes/después)

Fecha de corte: **4-mar-2026**

### Antes

- [ ] Docstrings faltantes en funciones/clases clave de `app.py` e `ingestion.py`.
- [ ] Uso extendido de `except Exception` y bloques silenciosos (`pass`).
- [ ] Varias líneas por encima de 79 caracteres (PEP 8).
- [ ] Ausencia de tests unitarios para helpers críticos de ingesta/chunking.
- [ ] Trazabilidad inconsistente por uso de `print` en utilidades/scripts.

### Después

- [x] Docstrings: **0 faltantes** en archivos Python principales auditados.
- [x] Excepciones genéricas: eliminadas de los módulos principales (`except Exception` -> 0 en el barrido).
- [x] PEP 8 (79 columnas): `app.py` = 0 y `ingestion.py` = 0 líneas fuera de límite.
- [x] Tests unitarios: suite activa con **9 tests OK** (`python -m unittest discover -s tests -p "test_*.py"`).
- [x] Mejor trazabilidad: reemplazo de `print` por `logging` en scripts y flujo de ingesta.

### Evidencia de validación

- [x] `get_errors`: sin errores en `app.py`, `ingestion.py`, `pydantic_patch.py`, `scripts/check_neo4j.py`, `scripts/github_loader.py`.
- [x] Recuento automático de docstrings y líneas >79 ejecutado en terminal.
- [x] Ejecución de pruebas automatizadas completada en verde.

---

Si quieres, actualizo este README con un `README_RUN.md` con comandos y recomendaciones para migrar a Python 3.13 y cómo limpiar los parches locales en el `venv`.
```