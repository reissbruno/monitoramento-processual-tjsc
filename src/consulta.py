from os import environ as env
from datetime import datetime
import time
from urllib.parse import urljoin
import base64

from fastapi.logger import logger
from fastapi.responses import JSONResponse
from fastapi import status
from bs4 import BeautifulSoup
from gradio_client import Client, handle_file
import httpx

# Local imports
from src.models import Movimentacao, Telemetria

# Captura variáveis de ambiente e cria constantes
TEMPO_LIMITE = int(env.get('TEMPO_LIMITE', 180))
TENTATIVAS_MAXIMAS_CAPTCHA = int(env.get('TENTATIVAS_MAXIMAS_CAPCAPTCHA', 30))  
TENTATIVAS_MAXIMAS_RECURSIVAS = int(env.get('TENTATIVAS_MAXIMAS_RECURSIVAS', 30))  

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

# Função principal para acessar o site do TJSC e capturar as movimentações com httpx
async def fetch(numero_processo: str, telemetria: Telemetria) -> dict:
    """
    Função que acessa a página inicial do TJSC com httpx, resolve o CAPTCHA com retry,
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
        # 1. Acessar a página inicial
        url_inicial = "https://eprocwebcon.tjsc.jus.br/consulta1g/externo_controlador.php?acao=processo_consulta_publica"
        response = client.get(url_inicial, follow_redirects=False)
        content_length = response.headers.get('Content-Length')
        if content_length:
            telemetria.bytes_enviados += int(content_length)
        else:
            telemetria.bytes_enviados += len(response.content)

        if response.status_code != 200:
            raise Exception(f"Falha ao acessar a página inicial: {response.status_code}")

        soup = BeautifulSoup(response, "html.parser")
        if "Nossos sistemas detectaram" in soup.text:
            # Pegar url captcha
            url_captcha = soup.find("img", {"id": "imgInfraCaptcha"}).get("src")
        else:
            imagem_captcha = soup.find("img", {"id": "imgInfraCaptcha"})
            label_captcha = soup.find("label", {"id": "lblInfraCaptcha"})
            if not label_captcha:
                raise Exception("Label do CAPTCHA não encontrada!")

            imagem_captcha = label_captcha.find("img")
            if not imagem_captcha:
                raise Exception("Imagem do CAPTCHA não encontrada dentro do label!")

            url_captcha = imagem_captcha.get("src")
            if not url_captcha:
                raise Exception("Atributo 'src' da imagem do CAPTCHA não encontrado!")

            # 3. Resolver o CAPTCHA
            if url_captcha.startswith("data:"):
                _, codificado = url_captcha.split(",", 1)
                bytes_imagem = base64.b64decode(codificado)
            else:
                response_captcha = client.get(url_captcha)
                content_length = response_captcha.headers.get('Content-Length')
                if content_length:
                    telemetria.bytes_enviados += int(content_length)
                else:
                    telemetria.bytes_enviados += len(response.content)
                if response_captcha.status_code != 200:
                    raise Exception(f"Falha ao baixar a imagem do CAPTCHA: {response_captcha.status_code}")
                bytes_imagem = response_captcha.content

            # Tentar resolver o CAPTCHA com API
            cliente_api = Client("Nischay103/captcha_recognition")
            telemetria.captchas_resolvidos += 1
            try:
                with open("captcha_temporario.png", "wb") as arquivo:
                    arquivo.write(bytes_imagem)
                resultado_ocr = cliente_api.predict(
                    input=handle_file("captcha_temporario.png"),
                    api_name="/predict"
                ).strip()
                logger.info(f"CAPTCHA reconhecido pela API: {resultado_ocr}")
            except Exception as e:
                logger.error(f"Erro ao usar a API captcha_recognition: {e}")
                # Fallback para ddddocr
                try:
                    import ddddocr
                    motor_ocr = ddddocr.DdddOcr()
                    resultado_ocr = motor_ocr.classification(bytes_imagem)
                    logger.info(f"CAPTCHA reconhecido pelo ddddocr (fallback): {resultado_ocr}")
                except Exception as fallback_e:
                    logger.error(f"Erro no fallback ddddocr: {fallback_e}")
                    raise

            # Validar o resultado do OCR
            if not resultado_ocr or len(resultado_ocr) != 4 or not resultado_ocr.isalnum():
                logger.warning(f"Resultado do OCR inválido: {resultado_ocr}. Reiniciando sessão...")
                client.close()
                if telemetria.tentativas < TENTATIVAS_MAXIMAS_CAPTCHA:
                    logger.info("Tentando resolver o CAPTCHA novamente...")
                    telemetria.tentativas += 1
                    return await fetch(numero_processo, telemetria)
                else:
                    raise Exception("Máximo de tentativas para resolver o CAPTCHA atingido.")

            # 4. Enviar o formulário via POST
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
                "txtInfraCaptcha": resultado_ocr,
                "hdnInfraCaptcha": "1",
                "hdnInfraSelecoes": "Infra"
            }
            url_post = "https://eprocwebcon.tjsc.jus.br/consulta1g/externo_controlador.php?acao=processo_consulta_publica"

            response_post = client.post(url_post, data=form_data, follow_redirects=False)
            content_length = response_post.headers.get('Content-Length')
            if content_length:
                telemetria.bytes_enviados += int(content_length)
            else:
                telemetria.bytes_enviados += len(response_post.content)

            # 5. Verificar redirecionamento
            if response_post.status_code == 302:
                redirect_url = response_post.headers["Location"]
                logger.info(f"URL de redirecionamento: {redirect_url}")
            else:
                raise Exception(f"Requisição POST não resultou em redirecionamento: {response_post.status_code}")

            # 6. Acessar a página de detalhes
            redirect_url = urljoin(url_inicial, redirect_url)
            response_get = client.get(redirect_url, follow_redirects=True)
            content_length = response_get.headers.get('Content-Length')
            if content_length:
                telemetria.bytes_enviados += int(content_length)
            else:
                telemetria.bytes_enviados += len(response_get.content)

            if response_get.status_code != 200:
                raise Exception(f"Falha ao acessar a página de detalhes: {response_get.status_code}")

            # 7. Capturar as movimentações
            pagina_html = response_get.text
            todas_movimentacoes = await capturar_todas_movimentacoes(pagina_html)

            # 8. Processar os resultados
            if not todas_movimentacoes or todas_movimentacoes == ["Nenhuma movimentação encontrada na tabela"]:
                logger.error("Nenhuma movimentação encontrada para o processo.")
                results = {
                    'code': 200,
                    'message': 'Nenhuma movimentação encontrada para o processo.',
                    'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                }
            else:
                ultima_movimentacao_atual = todas_movimentacoes[0] if isinstance(todas_movimentacoes[0], Movimentacao) else None
                logger.info(f"Última movimentação atual: {ultima_movimentacao_atual}")

                if not ultima_movimentacao_atual:
                    logger.error("Nenhuma movimentação válida encontrada para o processo.")
                    results = {
                        'code': 200,
                        'message': 'Nenhuma movimentação válida encontrada para o processo.',
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
