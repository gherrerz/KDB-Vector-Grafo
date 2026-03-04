import os
from atlassian import Confluence
from bs4 import BeautifulSoup # Para limpiar el HTML de las páginas

class ConfluenceLoader:
    def __init__(self, url, username, api_token):
        self.confluence = Confluence(
            url=url,
            username=username,
            password=api_token
        )

    def fetch_space_content(self, space_key, limit=50):
        """Extrae todas las páginas de un espacio y las devuelve como documentos."""
        pages = self.confluence.get_all_pages_from_space(space_key, start=0, limit=limit)
        docs = []
        
        for page in pages:
            page_id = page['id']
            # Obtener contenido expandido (HTML)
            content_data = self.confluence.get_page_by_id(page_id, expand='body.storage')
            title = content_data['title']
            html_body = content_data['body']['storage']['value']
            
            # Limpiar HTML para obtener texto plano
            soup = BeautifulSoup(html_body, "html.parser")
            text = soup.get_text(separator=' ')
            
            docs.append({
                "page_content": text,
                "metadata": {
                    "source": f"confluence/{space_key}/{title}",
                    "title": title,
                    "url": f"{self.confluence.url}/spaces/{space_key}/pages/{page_id}",
                    "file_type": "confluence"
                }
            })
        return docs