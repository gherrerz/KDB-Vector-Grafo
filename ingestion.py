import os
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, UnstructuredExcelLoader
from dotenv import load_dotenv

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
        self.embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200
        )

    def load_documents(self):
        """Carga y procesa documentos de diferentes formatos."""
        documents = []
        for file_name in os.listdir(self.data_path):
            file_path = os.path.join(self.data_path, file_name)
            
            print(f"📄 Procesando: {file_name}")
            
            try:
                if file_name.endswith('.pdf'):
                    loader = PyPDFLoader(file_path)
                    documents.extend(loader.load())
                elif file_name.endswith('.xlsx') or file_name.endswith('.xls'):
                    # UnstructuredExcelLoader requiere 'unstructured[all-docs]' instalado
                    loader = UnstructuredExcelLoader(file_path, mode="elements")
                    documents.extend(loader.load())
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
        docs = self.text_splitter.split_documents(raw_docs)

        # 3. Crear o actualizar la base de datos vectorial
        print(f"💾 Guardando {len(docs)} fragmentos en ChromaDB ({self.db_path})...")
        
        Chroma.from_documents(
            documents=docs,
            embedding=self.embeddings,
            persist_directory=self.db_path,
            collection_name="kdb_principal"
        )
        
        print("✅ Ingesta finalizada exitosamente.")

if __name__ == "__main__":
    # Si ejecutas este archivo directamente, procesa la carpeta por defecto
    INGEST_DATA_PATH = "./documentos_fuente"
    INGEST_DB_PATH = "./db_chroma_kdb"
    
    # Crear carpeta si no existe
    os.makedirs(INGEST_DATA_PATH, exist_ok=True)
    
    ingestor = KDBIngestor(INGEST_DATA_PATH, INGEST_DB_PATH)
    ingestor.run()