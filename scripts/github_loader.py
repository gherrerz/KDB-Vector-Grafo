import os
import shutil
import git  # pip install GitPython
from pathlib import Path

class GitHubLoader:
    def __init__(self, base_data_path):
        self.base_data_path = base_data_path
        # Extensiones de KDB+ y docs que queremos conservar
        self.valid_extensions = {
            '.q', '.k', '.s', '.p',        # KDB+/q
            '.md', '.markdown', '.txt',    # Docs
            '.sql', '.sh', '.py', '.yaml'  # Config y scripts
        }

    def fetch_repo(self, repo_url, folder_name="github_repo"):
        """
        Clona o actualiza un repositorio y limpia archivos no deseados.
        """
        target_path = os.path.join(self.base_data_path, folder_name)
        
        if os.path.exists(target_path):
            print(f"🔄 Actualizando repositorio en {target_path}...")
            repo = git.Repo(target_path)
            repo.remotes.origin.pull()
        else:
            print(f"🚀 Clonando repositorio: {repo_url}...")
            git.Repo.clone_from(repo_url, target_path, depth=1) # depth=1 para ahorrar espacio

        self._cleanup_repo(target_path)
        return target_path

    def _cleanup_repo(self, repo_path):
        """
        Elimina archivos binarios, imágenes y carpetas ocultas 
        para que el ingestor no pierda tiempo/tokens.
        """
        print("🧹 Limpiando repositorio (filtrando archivos no relevantes)...")
        for root, dirs, files in os.walk(repo_path, topdown=False):
            # 1. Eliminar carpeta .git y otras ocultas
            for d in dirs:
                if d.startswith('.') or d in ['node_modules', '__pycache__', 'bin', 'obj']:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)

            # 2. Eliminar archivos que no son código o texto
            for f in files:
                file_path = os.path.join(root, f)
                ext = os.path.splitext(f)[1].lower()
                if ext not in self.valid_extensions and not f.startswith('README'):
                    try:
                        os.remove(file_path)
                    except:
                        pass