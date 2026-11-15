from google.adk.agents.llm_agent import Agent
from google import genai
from google.genai import types
import asyncio
import os
import random
import time
import mysql.connector
import requests

try:
    from google.adk.models.google_llm import Gemini
except Exception:  # versão do ADK sem esse caminho de import
    Gemini = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Configuração (variáveis de ambiente / .env carregado pelo `adk run`) -----
PASTA_MIDIAS = os.path.abspath(os.getenv("PASTA_MIDIAS", os.path.join(BASE_DIR, "midias")))
META_API_VERSION = os.getenv("META_API_VERSION", "v26.0")  # v19.0 expirou em 21/05/2026
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Usado quando o modelo principal responde "sobrecarregado" (503) várias vezes seguidas.
GEMINI_MODEL_RESERVA = os.getenv("GEMINI_MODEL_RESERVA", "gemini-3.5-flash-lite")
BRIEFING_NEGOCIO = os.getenv(
    "BRIEFING_NEGOCIO",
    "Negócio local. Tom próximo e direto. Termine com uma chamada para falar no WhatsApp.",
)

EXTENSOES_FOTO = (".png", ".jpg", ".jpeg")
EXTENSOES_VIDEO = (".mp4", ".mov")
EXTENSOES_VALIDAS = EXTENSOES_FOTO + EXTENSOES_VIDEO
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}
LIMITE_INLINE_BYTES = 20 * 1024 * 1024  # acima disso o Gemini recebe via Files API
# Legendas geradas por gerar_legenda, por nome de arquivo. A publicação usa esta cópia exata,
# porque o texto repassado pelo modelo do agente pode corromper emojis (aparecem como "�").
_LEGENDAS: dict[str, str] = {}

# Tempo máximo (segundos) de cada chamada do Gemini ao gerar a legenda.
TIMEOUT_LEGENDA_S = int(os.getenv("GEMINI_TIMEOUT_LEGENDA_S", "30"))

# Status no banco:
#   pendente   -> reservada, ainda não enviada à Meta (pode ser escolhida de novo)
#   publicando -> envio à Meta iniciado; NUNCA é escolhida de novo automaticamente
#   publicado  -> confirmada, com post_id


def _conectar():
    """Abre a conexão com o MySQL e garante que a tabela existe."""
    conn = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "dataface"),
    )
    try:
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
    except mysql.connector.Error:
        conn.close()
        raise
    return conn


def _caminho_da_midia(nome_arquivo: str) -> str:
    """Monta o caminho absoluto a partir do NOME do arquivo.

    O modelo só trafega o nome (sem barras), então o caminho gravado no banco
    é sempre o mesmo, em qualquer sistema operacional. Aceita também um
    caminho completo e aproveita só o nome final.
    """
    nome = os.path.basename(nome_arquivo.strip().strip('"').replace("\\", "/"))
    return os.path.join(PASTA_MIDIAS, nome)


def _definir_status(caminho: str, status: str, post_id: str | None = None) -> None:
    """Grava o status da mídia (cria o registro se ainda não existir)."""
    conn = _conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO historico_posts (filepath, status, post_id)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE status = VALUES(status), post_id = VALUES(post_id)
            """,
            (caminho, status, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def varrer_proxima_midia() -> str:
    """Varre a pasta local de mídias, consulta o banco MySQL e retorna o NOME do
    próximo arquivo de foto ou vídeo que ainda não foi publicado.

    Returns:
        O nome do próximo arquivo, ou uma mensagem explicando que não há mídia disponível.
    """
    os.makedirs(PASTA_MIDIAS, exist_ok=True)

    arquivos = sorted(
        f for f in os.listdir(PASTA_MIDIAS) if f.lower().endswith(EXTENSOES_VALIDAS)
    )
    if not arquivos:
        return f"Nenhuma mídia válida encontrada na pasta {PASTA_MIDIAS}. Adicione fotos ou vídeos nela."

    try:
        conn = _conectar()
    except mysql.connector.Error as e:
        return f"Erro ao conectar no MySQL: {e}"

    travadas = []
    disponiveis = []  # (nome, caminho, já_registrada_no_banco)
    try:
        cur = conn.cursor()
        for nome in arquivos:
            caminho = os.path.join(PASTA_MIDIAS, nome)
            cur.execute(
                "SELECT status FROM historico_posts WHERE filepath = %s", (caminho,)
            )
            row = cur.fetchone()

            if row is not None and row[0] == "publicado":
                continue
            # Envio iniciado e não confirmado: pode já estar no ar, então não repete.
            if row is not None and row[0] == "publicando":
                travadas.append(nome)
                continue

            disponiveis.append((nome, caminho, row is not None))

        if disponiveis:
            # Sorteia entre todas as mídias que ainda não foram publicadas.
            nome, caminho, registrada = random.choice(disponiveis)
            if not registrada:
                cur.execute(
                    "INSERT INTO historico_posts (filepath, status) VALUES (%s, 'pendente')",
                    (caminho,),
                )
                conn.commit()
            return f"Próxima mídia encontrada. nome_arquivo: {nome}"
    except mysql.connector.Error as e:
        return f"Erro no banco MySQL: {e}"
    finally:
        conn.close()

    if travadas:
        return (
            "Não há mídia nova. Estas ficaram com envio não confirmado (status 'publicando'): "
            f"{', '.join(travadas)}. Confira na Página se o post existe e corrija o status no banco "
            "para 'publicado' ou 'pendente'."
        )
    return "Todas as mídias da pasta já foram publicadas."


def _erro_temporario(e: Exception) -> bool:
    """True para sobrecarga/limite de taxa do Gemini (vale a pena tentar de novo)."""
    texto = str(e).lower()
    return any(
        t in texto
        for t in (
            "503", "unavailable", "overloaded", "429", "resource_exhausted", "500", "internal",
            "504", "deadline", "timeout", "timed out",
        )
    )


def _gerar_com_retentativa(client, contents) -> str:
    """Chama o Gemini com espera progressiva; se a sobrecarga persistir, usa o modelo reserva."""
    ultimo_erro = None
    for modelo in dict.fromkeys([GEMINI_MODEL, GEMINI_MODEL_RESERVA]):
        for tentativa, espera in enumerate((0, 3), start=1):
            time.sleep(espera)
            print(f"[legenda] {modelo}, tentativa {tentativa}...", flush=True)
            try:
                resposta = client.models.generate_content(model=modelo, contents=contents)
                return (resposta.text or "").strip()
            except Exception as e:
                ultimo_erro = e
                print(f"[legenda] {modelo} falhou: {str(e)[:150]}", flush=True)
                if not _erro_temporario(e):
                    raise
    raise ultimo_erro


def gerar_legenda(nome_arquivo: str, instrucoes_extras: str = "") -> str:
    """Envia a foto ou vídeo ao Gemini para que ele VEJA a mídia e escreva a
    legenda de vendas com base no briefing do negócio.

    Args:
        nome_arquivo: O nome do arquivo retornado por varrer_proxima_midia.
        instrucoes_extras: Ajustes pedidos pelo usuário (ex.: "mais curta"). Opcional.

    Returns:
        O texto da legenda, ou uma mensagem de erro começando com "Erro".
    """
    caminho = _caminho_da_midia(nome_arquivo)
    if not os.path.exists(caminho):
        return f"Erro: O arquivo de mídia não foi encontrado: {caminho}"

    mime = MIME_TYPES.get(os.path.splitext(caminho)[1].lower())
    if not mime:
        return f"Erro: Extensão não suportada: {caminho}"

    prompt = (
        "Você é um copywriter de redes sociais. Analise a mídia anexada e escreva UMA "
        "legenda de vendas *curta* e atraente em português do Brasil para o Facebook.\n"
        f"Briefing do negócio: {BRIEFING_NEGOCIO}\n"
        "Regras: fale do que realmente aparece na mídia; no máximo 600 caracteres; até 3 "
        "emojis; sem markdown; não invente preços nem promoções. Responda só com a legenda."
    )
    if instrucoes_extras:
        prompt += f"\nAjustes pedidos (prioridade): {instrucoes_extras}"

    try:
        
        client = genai.Client(http_options=types.HttpOptions(timeout=TIMEOUT_LEGENDA_S * 1000))
        if os.path.getsize(caminho) <= LIMITE_INLINE_BYTES:
            with open(caminho, "rb") as f:
                midia = types.Part.from_bytes(data=f.read(), mime_type=mime)
        else:
            midia = client.files.upload(file=caminho, config={"mime_type": mime})
            limite = time.monotonic() + 300
            while getattr(midia.state, "name", str(midia.state)) != "ACTIVE":
                if getattr(midia.state, "name", "") == "FAILED" or time.monotonic() > limite:
                    return "Erro: O Gemini não conseguiu processar o vídeo."
                time.sleep(3)
                midia = client.files.get(name=midia.name)

        legenda = _gerar_com_retentativa(client, [midia, prompt]).strip('"').strip()
        if not legenda:
            return "Erro: O Gemini retornou uma legenda vazia."
        _LEGENDAS[os.path.basename(caminho)] = legenda
        return legenda
    except Exception as e:
        return f"Erro ao gerar a legenda com o Gemini: {e}"


def publicar_no_facebook(caption: str, nome_arquivo: str) -> str:
    """Publica uma foto ou vídeo na Página do Facebook via Graph API da Meta
    e atualiza o estado da publicação no banco MySQL.

    Args:
        caption: A legenda/copy de vendas para acompanhar a mídia.
        nome_arquivo: O nome do arquivo retornado por varrer_proxima_midia.

    Returns:
        Uma mensagem informando sucesso ou o motivo da falha.
    """
    page_id = os.environ.get("FACEBOOK_PAGE_ID")
    token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")
    if not page_id or not token:
        return "Erro: Credenciais do Facebook (PAGE_ID ou ACCESS_TOKEN) não foram encontradas no ambiente."

    media_path = _caminho_da_midia(nome_arquivo)
    if not os.path.exists(media_path):
        return f"Erro: O arquivo de mídia não foi encontrado: {media_path}"

    
    caption = _LEGENDAS.get(os.path.basename(media_path)) or caption

    ext = os.path.splitext(media_path)[1].lower()
    if ext not in EXTENSOES_VALIDAS:
        return f"Erro: Extensão não suportada: {ext}"
    is_video = ext in EXTENSOES_VIDEO

    try:
        conn = _conectar()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, post_id FROM historico_posts WHERE filepath = %s", (media_path,)
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0] == "publicado":
            return f"Esta mídia já foi publicada (ID do Post: {row[1]}). Nada foi enviado."
        if row and row[0] == "publicando":
            return (
                "Esta mídia tem um envio anterior não confirmado. Confira na Página se o post existe "
                "antes de tentar de novo. Nada foi enviado."
            )
        _definir_status(media_path, "publicando")
    except mysql.connector.Error as e:
        return f"Erro no MySQL antes de publicar (nada foi enviado ao Facebook): {e}"

    if is_video:
        url = f"https://graph-video.facebook.com/{META_API_VERSION}/{page_id}/videos"
        payload = {"description": caption, "access_token": token}
        timeout = 900
    else:
        url = f"https://graph.facebook.com/{META_API_VERSION}/{page_id}/photos"
        payload = {"caption": caption, "access_token": token}
        timeout = 180

    try:
        with open(media_path, "rb") as media_file:
            response = requests.post(
                url, data=payload, files={"source": media_file}, timeout=timeout
            )
    except requests.exceptions.ConnectionError as e:
        
        _liberar_para_nova_tentativa(media_path)
        return f"Erro: Sem conexão com a Graph API do Facebook: {e}"
    except requests.exceptions.Timeout:
        
        return (
            "Erro: A chamada para a Graph API excedeu o tempo limite. O post PODE ter sido criado; "
            "confira na Página. A mídia ficou com status 'publicando' e não será reenviada sozinha."
        )
    except Exception as e:
        return (
            f"Erro inesperado no upload para o Facebook: {e}. Confira na Página se o post foi criado; "
            "a mídia ficou com status 'publicando'."
        )

    try:
        res_data = response.json()
    except ValueError:
        res_data = {}

    if response.status_code != 200 or "error" in res_data:
        # A Meta recusou: nada foi publicado, pode tentar de novo.
        _liberar_para_nova_tentativa(media_path)
        erro_msg = res_data.get("error", {}).get("message", response.text[:300])
        return f"Falha na Graph API da Meta: {erro_msg}"


    post_id = res_data.get("post_id") or res_data.get("id")

    try:
        _definir_status(media_path, "publicado", post_id)
    except mysql.connector.Error as e:
        return (
            f"Mídia publicada no Facebook (ID do Post: {post_id}), mas houve erro ao atualizar o "
            f"MySQL: {e}. Ela ficou como 'publicando' e NÃO será postada de novo; ajuste o status "
            "para 'publicado' no banco."
        )

    return f"Sucesso! Mídia publicada na página do Facebook. ID do Post: {post_id}"


def _liberar_para_nova_tentativa(media_path: str) -> None:
    """Volta a mídia para 'pendente' quando é certo que nada foi publicado."""
    try:
        _definir_status(media_path, "pendente")
    except mysql.connector.Error:
        pass  





TIMEOUT_PRIMEIRA_RESPOSTA = float(os.getenv("GEMINI_TIMEOUT_S", "25"))


def _opcoes_retry(tentativas: int):
    return types.HttpRetryOptions(attempts=tentativas, initial_delay=2, exp_base=2)


if Gemini is not None:

    class GeminiComReserva(Gemini):
        """Gemini que cai para um modelo reserva quando o principal está sobrecarregado."""

        reserva: str = GEMINI_MODEL_RESERVA

        async def generate_content_async(self, llm_request, stream=False):
            ja_emitiu = False
            gen = super().generate_content_async(llm_request, stream)
            try:
                while True:
                    try:
                        if ja_emitiu:
                            resp = await gen.__anext__()
                        else:
                            # Limita a espera pela 1ª resposta (inclui os retries internos).
                            resp = await asyncio.wait_for(
                                gen.__anext__(), timeout=TIMEOUT_PRIMEIRA_RESPOSTA
                            )
                    except StopAsyncIteration:
                        return
                    ja_emitiu = True
                    yield resp
            except Exception as e:
                # Se já enviou parte da resposta (streaming), trocar de modelo duplicaria conteúdo.
                estourou = isinstance(e, asyncio.TimeoutError)
                if ja_emitiu or not (estourou or _erro_temporario(e)):
                    raise
                motivo = "demorou demais" if estourou else "indisponível"
                print(f"[aviso] {self.model} {motivo}; usando {self.reserva}", flush=True)
                try:
                    await gen.aclose()
                except Exception:
                    pass

            llm_request.model = self.reserva
            try:
                modelo_reserva = Gemini(model=self.reserva, retry_options=_opcoes_retry(5))
            except Exception:
                modelo_reserva = Gemini(model=self.reserva)
            async for resp in modelo_reserva.generate_content_async(llm_request, stream):
                yield resp


def _modelo_do_agente():
    """Principal com poucas tentativas (troca rápido) e reserva automática em caso de 503/429."""
    if Gemini is None:
        return GEMINI_MODEL
    try:
        return GeminiComReserva(model=GEMINI_MODEL, retry_options=_opcoes_retry(2))
    except Exception:
        try:
            return GeminiComReserva(model=GEMINI_MODEL)
        except Exception:
            return GEMINI_MODEL


root_agent = Agent(
    model=_modelo_do_agente(),
    name="profissional_captacao_lead",
    description="Agente autônomo para automação de publicações em mídias sociais.",
    instruction=(
        "Você é um assistente de automação de mídias sociais. "
        "Leia o arquivo txt 'briefinf_negocio'para Quando o usuário digitar 'fazer' ou pedir uma postagem, você siga este fluxo e fazer da melhor forma: "
        "1) chame varrer_proxima_midia para descobrir qual mídia postar; "
        "2) se ela retornar um nome_arquivo, chame gerar_legenda com esse nome EXATO; "
        "3) se a legenda não começar com 'Erro', chame publicar_no_facebook com a legenda "
        "retornada, sem alterá-la, e o mesmo nome_arquivo; "
        "4) informe ao usuário o resultado, repetindo a mensagem da ferramenta. "
        "Nunca escreva a legenda por conta própria e nunca invente nomes de arquivo ou IDs. "
        "Se não houver mídia disponível ou alguma ferramenta retornar erro, apenas avise o "
        "usuário e não publique nada."
    ),
    tools=[varrer_proxima_midia, gerar_legenda, publicar_no_facebook],
)