# Actualizar pip primero
python -m pip install --upgrade pip

# Instalar todas las dependencias del proyecto
pip install -r requirements.txt

# Configurar Neo4j local con Docker (Windows PowerShell)
# Ejecuta desde la raíz del proyecto:
# powershell -ExecutionPolicy Bypass -File .\scripts\setup_neo4j.ps1 -Password "TuPasswordSeguro"
# powershell -ExecutionPolicy Bypass -File .\scripts\setup_neo4j.ps1 -Password "D7RADJFQ7jEkeGDzaB_VMsYDwX23iIUxXDs5VPs3G6k"

# Verificar conectividad con Neo4j
# .\.venv\Scripts\python.exe .\scripts\check_neo4j.py

# Arrancar la app
# .\.venv\Scripts\python.exe -m streamlit run app.py