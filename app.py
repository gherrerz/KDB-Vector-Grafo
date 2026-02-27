import streamlit as st
import os
from ingestion import KDBIngestor  # Asegúrate de tener ingestion.py en la misma carpeta
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# --- IMPORTS COMPATIBLES CON PYDANTIC V1 (Estabilidad garantizada) ---
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langgraph.prebuilt import create_react_agent
# Tool tradicional de LangChain (compatible con Pydantic v1)
from langchain.tools import Tool 
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

# --- CONFIGURACIÓN DE RUTAS ---
DATA_PATH = "./documentos_fuente"
CHROMA_PATH = "./db_chroma_kdb"
os.makedirs(DATA_PATH, exist_ok=True)

# Configuración de página
st.set_page_config(page_title="Auditor KDB Pro", layout="wide", page_icon="🕵️‍♂️")

# --- INICIALIZACIÓN DE COMPONENTES (Cache con v1) ---
@st.cache_resource
def init_vector_store():
    # Asegúrate de tener OPENAI_API_KEY en tu archivo .env
    if not os.getenv("OPENAI_API_KEY"):
        st.error("❌ API Key no encontrada. Configura el archivo .env")
        st.stop()
        
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    return Chroma(
        persist_directory=CHROMA_PATH,
        embedding_function=embeddings,
        collection_name="kdb_principal"
    )

vector_store = init_vector_store()

# --- DEFINICIÓN DEL AGENTE (LangGraph + Pydantic v1) ---
llm = ChatOpenAI(model_name="gpt-4o", temperature=0)

# Función de búsqueda de la herramienta (compatible v1)
def consultar_evidencia_kdb(query: str) -> str:
    """Busca información técnica en los documentos, tablas de Excel y reportes auditados."""
    docs = vector_store.similarity_search(query, k=5)
    return "\n\n".join([d.page_content for d in docs])

# Tool tradicional
retriever_tool = Tool(
    name="consultar_evidencia_kdb",
    func=consultar_evidencia_kdb,
    description="Busca información técnica en los documentos para responder preguntas de auditoría."
)

system_message = """Eres un Auditor Técnico experto y Analista de Datos. Tu objetivo es analizar la base de conocimientos proporcionada.
REGLAS DE ORO:
1. IDIOMA: Responde siempre en ESPAÑOL profesional.
2. RIGOR: Si los datos provienen de una tabla, interprétalos con precisión quirúrgica.
3. CITACIÓN: Menciona siempre el nombre del archivo fuente (p.ej., 'Según reporte_final.pdf, página 4...').
4. HONESTIDAD: Si no encuentras evidencia explícita en los documentos, di: 'No se encontraron registros suficientes para validar esta información'."""

# Crear el Agente con compatibilidad Pydantic v1
# MemorySaver permite que recuerde la conversación anterior en esta sesión
agent_executor = create_react_agent(
    llm, 
    tools=[retriever_tool],
    state_modifier=system_message,
    checkpointer=MemorySaver()
)

# --- INTERFAZ DE USUARIO (SIDEBAR) ---
with st.sidebar:
    st.header("📥 Ingesta de Evidencia")
    files = st.file_uploader("Subir archivos (PDF, Excel)", accept_multiple_files=True)
    
    if st.button("🚀 Indexar Nueva Evidencia"):
        if files:
            with st.status("Procesando...", expanded=True) as status:
                for f in files:
                    file_path = os.path.join(DATA_PATH, f.name)
                    with open(file_path, "wb") as buffer:
                        buffer.write(f.getbuffer())
                
                st.write("Analizando contenido y generando embeddings (ChromaDB)...")
                
                # Llama a la clase KDBIngestor importada de ingestion.py
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
st.caption("Motor: LangGraph | Vector DB: ChromaDB | Pydantic: v1")

# Inicializar historial visual si no existe
if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": "Listo para auditar. Sube tus archivos en la barra lateral para comenzar."}]

# Mostrar mensajes anteriores
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Capturar nueva consulta
if user_input := st.chat_input("Consulta la base de conocimientos..."):
    # Guardar y mostrar pregunta del usuario
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Procesar respuesta del Auditor
    with st.chat_message("assistant"):
        with st.spinner("El Auditor está analizando la evidencia..."):
            
            # Configuración para que LangGraph mantenga el hilo de conversación
            config = {"configurable": {"thread_id": "auditoria_sesion_001"}}
            
            # Ejecución del agente con la pregunta
            result = agent_executor.invoke(
                {"messages": [HumanMessage(content=user_input)]}, 
                config
            )
            
            # Obtener el último mensaje generado por el agente
            response_text = result["messages"][-1].content
            
            st.markdown(response_text)
            st.session_state.messages.append({"role": "assistant", "content": response_text})