from google.adk.agents.llm_agent import Agent
import os
import mysql.connector
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PASTA_MIDIAS = os.path.abspath(os.environ["PASTA_MIDIAS"])


def _conectar():
    """Abre a conexão com o MySQL e garante que a tabela existe."""
    conn = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "dataface"),
    )
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS historico_posts (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            filepath VARCHAR(500) UNIQUE NOT NULL,
            status VARCHAR(50) NOT NULL,
            post_id VARCHAR(255),
            data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    cur.close()
    return conn


def varrer_proxima_midia() -> str:
    """Varre a pasta local de mídias, consulta o banco MySQL e retorna o caminho
    absoluto da próxima foto ou vídeo que ainda não foi publicado.

    Returns:
        O caminho absoluto da próxima mídia, ou uma mensagem explicando que não há mídia disponível.
    """
    os.makedirs(PASTA_MIDIAS, exist_ok=True)

    extensoes_validas = (".png", ".jpg", ".jpeg", ".mp4", ".mov")
    arquivos = sorted(
        f for f in os.listdir(PASTA_MIDIAS) if f.lower().endswith(extensoes_validas)
    )

    if not arquivos:
        return f"Nenhuma mídia válida encontrada na pasta {PASTA_MIDIAS}. Adicione fotos ou vídeos nela."

    try:
        conn = _conectar()
    except mysql.connector.Error as e:
        return f"Erro ao conectar no MySQL: {e}"

    try:
        cur = conn.cursor()
        for nome in arquivos:
            caminho = os.path.join(PASTA_MIDIAS, nome)
            cur.execute(
                "SELECT status FROM historico_posts WHERE filepath = %s", (caminho,)
            )
            row = cur.fetchone()

            # Só pula o que já foi publicado; 'pendente' (tentativa que falhou) pode ser refeito.
            if row is not None and row[0] == "publicado":
                continue

            if row is None:
                cur.execute(
                    "INSERT INTO historico_posts (filepath, status) VALUES (%s, 'pendente')",
                    (caminho,),
                )
                conn.commit()
            return f"Próxima mídia encontrada: {caminho}"
    except mysql.connector.Error as e:
        return f"Erro no banco MySQL: {e}"
    finally:
        conn.close()

    return "Todas as mídias da pasta já foram publicadas."


def publicar_no_facebook(caption: str, media_path: str) -> str:
    """Publica uma foto ou vídeo na Página Comercial do Facebook via Graph API da Meta
    e atualiza o estado da publicação no banco MySQL.

    Args:
        caption: A legenda/copy de vendas para acompanhar a mídia.
        media_path: O caminho local completo do arquivo de foto (.jpg, .png) ou vídeo (.mp4).

    Returns:
        Uma mensagem informando sucesso ou o motivo da falha.
    """
    page_id = os.environ.get("FACEBOOK_PAGE_ID")
    token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")

    if not page_id or not token:
        return "Erro: Credenciais do Facebook (PAGE_ID ou ACCESS_TOKEN) não foram encontradas no ambiente."

    if not os.path.exists(media_path):
        return f"Erro: O arquivo de mídia não foi encontrado: {media_path}"

    ext = os.path.splitext(media_path)[1].lower()
    is_video = ext in [".mp4", ".mov", ".avi"]

    if is_video:
        url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
        payload = {"description": caption, "access_token": token}
    else:
        url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
        payload = {"caption": caption, "access_token": token}

    try:
        with open(media_path, "rb") as media_file:
            response = requests.post(
                url, data=payload, files={"source": media_file}, timeout=120
            )

        res_data = response.json()

        if response.status_code != 200:
            erro_msg = res_data.get("error", {}).get("message", response.text)
            return f"Falha na Graph API da Meta: {erro_msg}"

        post_id = res_data.get("id") or res_data.get("post_id")

        # A postagem já foi feita; se o banco falhar, avisamos sem dizer que a publicação falhou.
        try:
            conn = _conectar()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM historico_posts WHERE filepath = %s", (media_path,)
                )
                if cur.fetchone():
                    cur.execute(
                        "UPDATE historico_posts SET status = 'publicado', post_id = %s WHERE filepath = %s",
                        (post_id, media_path),
                    )
                else:
                    cur.execute(
                        "INSERT INTO historico_posts (filepath, status, post_id) VALUES (%s, 'publicado', %s)",
                        (media_path, post_id),
                    )
                conn.commit()

            finally:
                conn.close()
        except mysql.connector.Error as e:
            return (
                f"Mídia publicada no Facebook (ID do Post: {post_id}), "
                f"mas houve erro ao atualizar o MySQL: {e}"
            )

        return f"Sucesso! Mídia publicada na página do Facebook. ID do Post: {post_id}"

    except requests.exceptions.Timeout:
        return "Erro: A chamada para a Graph API do Facebook excedeu o tempo limite (Timeout)."
    except Exception as e:
        return f"Erro inesperado ao realizar upload para o Facebook: {str(e)}"


root_agent = Agent(
    model="gemini-3.8-flash",
    name="profissional_captacao_lead",
    description="Agente autônomo para automação de publicações em mídias sociais.",
    instruction=(
        "Você é um assistente de automação de mídias sociais. "
        "Quando o usuário digitar 'fazer' ou pedir uma postagem, siga este fluxo: "
        "1) chame varrer_proxima_midia para descobrir qual mídia postar; "
        "2) se ela retornar um caminho, crie uma legenda de vendas curta e atraente em português; "
        "3) chame publicar_no_facebook com a legenda e o caminho EXATO retornado no passo 1; "
        "4) informe ao usuário o resultado. "
        "Se não houver mídia disponível, apenas avise o usuário e não publique nada."
    ),
    tools=[varrer_proxima_midia, publicar_no_facebook],
)