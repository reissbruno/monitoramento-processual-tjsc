from os import environ as env
from datetime import datetime
import time
import os
import certifi
from urllib.parse import urljoin

from fastapi.logger import logger
from fastapi.responses import JSONResponse
import base64
from fastapi import status
from bs4 import BeautifulSoup
import httpx
from capmonstercloudclient import CapMonsterClient, ClientOptions
from capmonstercloudclient.requests import TurnstileProxylessRequest

# Local imports
from src.models import Movimentacao, Telemetria


# Captura variáveis de ambiente e cria constantes
TEMPO_LIMITE = int(env.get('TEMPO_LIMITE', 180))
TENTATIVAS_MAXIMAS_CAPTCHA = int(env.get('TENTATIVAS_MAXIMAS_CAPTCHA', 5))  
TENTATIVAS_MAXIMAS_RECURSIVAS = int(env.get('TENTATIVAS_MAXIMAS_RECURSIVAS', 5))  
CAPMONSTER_API_KEY = env.get('CAPMONSTER_API_KEY')


# Função para formatar o número do processo
def formatar_numero_processo(numero_processo):
    return ''.join(filter(str.isdigit, numero_processo))


# Função para capturar todas as movimentações da página HTML
async def capturar_todas_movimentacoes(pagina_html):
    sopa = BeautifulSoup(pagina_html, "html.parser")
    tabelas = sopa.find_all('table', class_='infraTable')

    tabela_eventos = None
    for tabela in tabelas:
        cabecalhos = [th.get_text(strip=True) for th in tabela.find_all('th')]
        if 'Evento' in cabecalhos and 'Data/Hora' in cabecalhos and 'Descrição' in cabecalhos and 'Documentos' in cabecalhos:
            tabela_eventos = tabela
            break

    if not tabela_eventos:
        logger.warning("Tabela de eventos não encontrada na página HTML.")
        return ["Nenhuma movimentação encontrada na tabela"]

    movimentacoes = []
    linhas = tabela_eventos.find_all('tr', class_=['infraTrClara', 'infraTrEscura'])
    
    if not linhas:
        logger.warning("Nenhuma linha de eventos encontrada na tabela.")
        return ["Nenhuma movimentação encontrada na tabela"]

    for linha in linhas:
        celulas = linha.find_all('td')
        if len(celulas) >= 4:  
            evento = celulas[0].get_text(strip=True)
            data_hora = celulas[1].get_text(strip=True)
            descricao = celulas[2].get_text(strip=True)
            
            link_documento_completo = ""
            link_documento_elemento = celulas[3].find('a', class_='infraLinkDocumento')
            if link_documento_elemento and 'href' in link_documento_elemento.attrs:
                url_geral = "https://eprocwebcon.tjsc.jus.br/consulta1g/"
                link_documento = link_documento_elemento['href']
                link_documento_completo = urljoin(url_geral, link_documento)

            movimentacao = Movimentacao(
                evento=evento,
                data_hora=data_hora,
                descricao=descricao,
                documentos=link_documento_completo
            )
            movimentacoes.append(movimentacao)
            logger.debug(f"Movimentação capturada: {movimentacao.dict()}")

    return movimentacoes if movimentacoes else ["Nenhuma movimentação encontrada na tabela"]


# Resolve o CAPTCHA Turnstile usando o CapMonster
async def resolver_captcha_turnstile(website_url: str, site_key: str, page_data: str) -> dict:
    os.environ['SSL_CERT_FILE'] = certifi.where()

    client_options = ClientOptions(api_key=CAPMONSTER_API_KEY)
    cap_monster_client = CapMonsterClient(options=client_options)

    turnstile_request = TurnstileProxylessRequest(
        websiteURL=website_url,
        websiteKey=site_key,
        pageData=page_data
    )

    solution = await cap_monster_client.solve_captcha(turnstile_request)
    return solution


# Função principal para acessar o site do TJSC e capturar as movimentações com httpx
async def fetch(numero_processo: str, telemetria: Telemetria) -> dict:
    """
    Função que acessa a página inicial do TJSC com httpx, resolve o CAPTCHA Turnstile com retry,
    envia o formulário via POST e captura as movimentações do processo. Retorna os
    resultados como objetos Movimentacao, incluindo links de documentos e a duração
    da requisição.
    """

    # Validação inicial
    if not numero_processo or not isinstance(numero_processo, str):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                'code': 2,
                'message': 'ERRO_ENTIDADE_NAO_PROCESSAVEL'
            }
        )

    if telemetria.tentativas >= TENTATIVAS_MAXIMAS_RECURSIVAS:
        logger.error("Número máximo de tentativas recursivas atingido.")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                'code': 3,
                'message': 'ERRO_SERVIDOR_INTERNO'
            }
        )

    logger.info(f'Função fetch() iniciou. Processo: {numero_processo} - Tentativa {telemetria.tentativas}')

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Host": "eprocwebcon.tjsc.jus.br",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
    }

    # Criar cliente HTTP para manter cookies e sessão
    client = httpx.Client(timeout=TEMPO_LIMITE, verify=False, headers=headers)
    results = None

    try:
        # Acessar a página inicial
        url_inicial = "https://eprocwebcon.tjsc.jus.br/consulta1g/externo_controlador.php?acao=processo_consulta_publica"
        response = client.get(url_inicial, follow_redirects=False)
        content_length = response.headers.get('Content-Length')
        if content_length:
            telemetria.bytes_enviados += int(content_length)
        else:
            telemetria.bytes_enviados += len(response.content)

        if response.status_code != 200:
            raise Exception(f"Falha ao acessar a página inicial: {response.status_code}")
        
        page_data = base64.b64encode(response.text.encode('utf-8')).decode('utf-8')

        soup = BeautifulSoup(response.text, "html.parser")

        # Verificar se o CAPTCHA Turnstile está presente
        turnstile_div = soup.find("div", class_="cf-turnstile")
        if not turnstile_div:
            raise Exception("Elemento do CAPTCHA Turnstile não encontrado na página inicial.")

        site_key = turnstile_div.get("data-sitekey")
        if not site_key:
            raise Exception("Chave do site (data-sitekey) não encontrada no elemento Turnstile.")

        # Resolver o CAPTCHA Turnstile
        telemetria.captchas_resolvidos += 1
        solution = await resolver_captcha_turnstile(url_inicial, site_key, page_data)

        token = solution.get("token")

        form_data = {
            "hdnInfraTipPagina": "1",
            "sbmNovo": "Consultar",
            "txtNumProcesso": numero_processo,
            "txtNumChave": "",
            "txtNumChaveDocumentos": "",
            "txtParte": "",
            "chkFonetica": "N",
            "chkFoneticaS": "",
            "txtStrOAB": "",
            "rdTipo": "CPF",
            "cf-turnstile-response": token, 
            "hdnInfraCaptcha": "0"
            "hdnInfraSelecoes" "Infra"
        }
        url_post = "https://eprocwebcon.tjsc.jus.br/consulta1g/externo_controlador.php?acao=processo_consulta_publica"

        response_post = client.post(url_post, data=form_data, follow_redirects=False)
        content_length = response_post.headers.get('Content-Length')
        if content_length:
            telemetria.bytes_enviados += int(content_length)
        else:
            telemetria.bytes_enviados += len(response_post.content)

        pagina_html = response_post.text
        if "Processo não encontrado" in pagina_html:
            results = {
                'code': 200,
                'message': 'Processo não encontrado',
                'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                'telemetria': telemetria
            }
            return results
        else:
            # Verificar redirecionamento
            if response_post.status_code == 302:
                redirect_url = response_post.headers["Location"]
                logger.info(f"URL de redirecionamento: {redirect_url}")
            else:
                raise Exception(f"Requisição POST não resultou em redirecionamento: {response_post.status_code}")

            # Acessar a página de detalhes
            redirect_url = urljoin(url_inicial, redirect_url)
            response_get = client.get(redirect_url, follow_redirects=True)
            content_length = response_get.headers.get('Content-Length')
            if content_length:
                telemetria.bytes_enviados += int(content_length)
            else:
                telemetria.bytes_enviados += len(response_get.content)

            if response_get.status_code != 200:
                raise Exception(f"Falha ao acessar a página de detalhes: {response_get.status_code}")

            # Capturar as movimentações
            pagina_html = response_get.text
            if "Processo não encontrado" in pagina_html:
                results = {
                    'code': 200,
                    'message': 'Processo não encontrado',
                    'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                    'telemetria': telemetria
                }
                return results
            else:
                # Verificar redirecionamento
                if response_post.status_code == 302:
                    redirect_url = response_post.headers["Location"]
                    logger.info(f"URL de redirecionamento: {redirect_url}")
                else:
                    raise Exception(f"Requisição POST não resultou em redirecionamento: {response_post.status_code}")

                # Acessar a página de detalhes
                redirect_url = urljoin(url_inicial, redirect_url)
                response_get = client.get(redirect_url, follow_redirects=True)
                content_length = response_get.headers.get('Content-Length')
                if content_length:
                    telemetria.bytes_enviados += int(content_length)
                else:
                    telemetria.bytes_enviados += len(response_get.content)

                if response_get.status_code != 200:
                    raise Exception(f"Falha ao acessar a página de detalhes: {response_get.status_code}")

                # Capturar as movimentações
                pagina_html = response_get.text
                soup_novo = BeautifulSoup(pagina_html, "html.parser")
                link = soup_novo.find(
                    'a', 
                    href=lambda href: href and "externo_controlador.php?acao=processo_seleciona_publica" in href,
                    text=lambda t: t and "listar todos os eventos" in t.lower()
                )
                if link:
                    
                    link_href = link.get("href")
                    url_base = "https://eprocwebcon.tjsc.jus.br/consulta1g/"
                    url_eventos = urljoin(url_base, link_href) 
                    response_eventos = client.get(url_eventos, follow_redirects=True, timeout=TEMPO_LIMITE)
                    content_length = response_eventos.headers.get('Content-Length')
                    if content_length:
                        telemetria.bytes_enviados += int(content_length)
                    else:
                        telemetria.bytes_enviados += len(response_eventos.content)
                    
                    if response_eventos.status_code != 200:
                        raise Exception(f"Falha ao acessar a página de eventos: {response_eventos.status_code}")
                    
                    pagina_html = response_eventos.text
                    
                    todas_movimentacoes = await capturar_todas_movimentacoes(pagina_html)
                    
                else:
                    logger.warning("Link para eventos não encontrado na página de detalhes.")
                    todas_movimentacoes = await capturar_todas_movimentacoes(pagina_html)

                # Processar os resultados
                if not todas_movimentacoes or todas_movimentacoes == ["Nenhuma movimentação encontrada na tabela"]:
                    logger.error("Nenhuma movimentação encontrada para o processo.")
                    results = {
                        'code': 200,
                        'message': 'Nenhuma movimentação encontrada para o processo.',
                        'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                    }
                else:
                    
                    logger.info("Processo consultado com sucesso.")
                    results = {
                        'code': 200,
                        'message': 'Consulta realizada com sucesso',
                        'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                        'results': todas_movimentacoes,
                    }

    except httpx.RequestError as e:
        logger.error(f"Erro de requisição: {e}")
        telemetria.tempo_total = round(time.time() - telemetria.tempo_total, 2)
        results = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                'code': 4,
                'message': 'ERRO_SERVIDOR_INTERNO',
                'telemetria': telemetria
            }
        )
    except Exception as e:
        logger.error(f"Erro durante a consulta: {e}")
        if telemetria.tentativas < TENTATIVAS_MAXIMAS_CAPTCHA:
            logger.info("Tentando resolver o CAPTCHA novamente...")
            telemetria.tentativas += 1
            return await fetch(numero_processo, telemetria)
        else:
            telemetria.tempo_total = round(time.time() - telemetria.tempo_total, 2)
            results = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    'code': 4,
                    'message': 'ERRO_SERVIDOR_INTERNO',
                    'telemetria': telemetria
                }
            )
    finally:
        client.close()
        telemetria.tempo_total = round(time.time() - telemetria.tempo_total, 2)
        if results is not None and isinstance(results, dict) and "telemetria" not in results:
            results["telemetria"] = telemetria

    return results