"""Helpers to clone/update and clean a GitHub repository for ingestion."""

import os
import shutil
import logging

import git  # pip install GitPython
from git.exc import GitCommandError, InvalidGitRepositoryError


LOGGER = logging.getLogger(__name__)

class GitHubLoader:
    """Clone/update repositories and keep only relevant source artifacts."""

    def __init__(self, base_data_path: str) -> None:
        """Initialize repository loader settings.

        Parameters:
            base_data_path: Root directory where repositories are stored.
        """
        self.base_data_path = base_data_path
        # Extensiones de KDB+ y docs que queremos conservar
        self.valid_extensions = {
            '.q', '.k', '.s', '.p',        # KDB+/q
            '.md', '.markdown', '.txt',    # Docs
            '.sql', '.sh', '.py', '.yaml'  # Config y scripts
        }

    def fetch_repo(
        self,
        repo_url: str,
        folder_name: str = "github_repo",
    ) -> str:
        """
        Clona o actualiza un repositorio y limpia archivos no deseados.

        Parameters:
            repo_url: URL del repositorio Git a descargar.
            folder_name: Nombre de la carpeta destino bajo base_data_path.

        Returns:
            Ruta absoluta de la carpeta del repositorio.

        Raises:
            RuntimeError: Si falla el clone o pull del repositorio.
        """
        target_path = os.path.join(self.base_data_path, folder_name)

        if os.path.exists(target_path):
            LOGGER.info("🔄 Actualizando repositorio en %s...", target_path)
            try:
                repo = git.Repo(target_path)
                repo.remotes.origin.pull()
            except (InvalidGitRepositoryError, GitCommandError) as exc:
                raise RuntimeError(
                    f"No se pudo actualizar el repositorio en {target_path}"
                ) from exc
        else:
            LOGGER.info("🚀 Clonando repositorio: %s...", repo_url)
            try:
                git.Repo.clone_from(repo_url, target_path, depth=1)
            except GitCommandError as exc:
                raise RuntimeError(
                    f"No se pudo clonar el repositorio: {repo_url}"
                ) from exc

        self._cleanup_repo(target_path)
        return target_path

    def _cleanup_repo(self, repo_path: str) -> None:
        """
        Elimina archivos binarios, imágenes y carpetas ocultas 
        para que el ingestor no pierda tiempo/tokens.
        """
        LOGGER.info(
            "🧹 Limpiando repositorio (filtrando archivos no relevantes)..."
        )
        for root, dirs, files in os.walk(repo_path, topdown=False):
            # 1. Eliminar carpeta .git y otras ocultas
            for d in dirs:
                if d.startswith('.') or d in [
                    'node_modules',
                    '__pycache__',
                    'bin',
                    'obj',
                ]:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)

            # 2. Eliminar archivos que no son código o texto
            for f in files:
                file_path = os.path.join(root, f)
                ext = os.path.splitext(f)[1].lower()
                if (
                    ext not in self.valid_extensions
                    and not f.startswith('README')
                ):
                    try:
                        os.remove(file_path)
                    except OSError as exc:
                        LOGGER.warning(
                            "No se pudo eliminar %s: %s", file_path, exc
                        )