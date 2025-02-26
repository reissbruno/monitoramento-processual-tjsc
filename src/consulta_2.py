from os import environ as env
from datetime import datetime
import time
import base64

from fastapi.logger import logger
from fastapi.responses import JSONResponse
from fastapi import status
from bs4 import BeautifulSoup
from gradio_client import Client, handle_file

# 3rd party imports do Selenium
from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.alert import Alert
from selenium.webdriver.chrome.service import Service
import logging
import chromedriver_autoinstaller

# Local imports
from src.models import Movimentacao  

# Captura variáveis de ambiente e cria constantes
TEMPO_LIMITE = int(env.get('TEMPO_LIMITE', 180))
TENTATIVAS_MAXIMAS_CAPCAPTCHA = int(env.get('TENTATIVAS_MAXIMAS_CAPCAPTCHA', 30))  
TENTATIVAS_MAXIMAS_RECURSIVAS = int(env.get('TENTATIVAS_MAXIMAS_RECURSIVAS', 30))  
logging.getLogger("urllib3").setLevel(logging.ERROR)

# Funções auxiliares
def criar_navegador() -> webdriver.Chrome:
    caminho_chromedriver = chromedriver_autoinstaller.install()
    servico = Service(executable_path=caminho_chromedriver)
    opcoes = ChromeOptions()
    opcoes.add_argument('--headless=new')
    opcoes.add_argument('--no-sandbox')
    opcoes.add_argument('--disable-background-networking')
    opcoes.add_argument('--disable-sync')
    opcoes.add_argument('--metrics-recording-only')
    opcoes.add_argument('--no-first-run')
    opcoes.add_argument('--disable-dev-shm-usage')
    opcoes.add_argument('--window-size=1920,1080')
    opcoes.add_argument('--disable-gpu')
    opcoes.add_argument('--disable-extensions')
    opcoes.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                         'AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/133.0.6943.126 Safari/537.36')
    opcoes_seleniumwire = {
        'disable_encoding': True,
        'ssl_intercept': False,
        'connection_retry_count': 0
    }
    try:
        navegador = webdriver.Chrome(service=servico, options=opcoes, seleniumwire_options=opcoes_seleniumwire)
        return navegador
    
    except Exception as e:
        logger.error(f"Erro ao criar o navegador: {e}")
        raise


# Função para formatar o número do processo
def formatar_numero_processo(numero_processo):
    return ''.join(filter(str.isdigit, numero_processo))


# Função para capturar todas as movimentações da página HTML
def capturar_todas_movimentacoes(pagina_html):
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
            
            link_documento = ""
            link_documento_elemento = celulas[3].find('a', class_='infraLinkDocumento')

            if link_documento_elemento and 'href' in link_documento_elemento.attrs:
                link_documento = link_documento_elemento['href']
            movimentacao = Movimentacao(
                evento=evento,
                data_hora=data_hora,
                descricao=descricao,
                documentos=link_documento
            )
            movimentacoes.append(movimentacao)
            logger.debug(f"Movimentação capturada: {movimentacao.dict()}")

    return movimentacoes if movimentacoes else ["Nenhuma movimentação encontrada na tabela"]


# Função principal para acessar o site do TJSC e capturar as movimentações
async def fetch(numero_processo: str, tentativas=0) -> dict:
    """
    Função que acessa a página inicial com Selenium, resolve o CAPTCHA com retry,
    preenche o formulário e verifica o processo no TJSC. Retorna todas as movimentações
    para o usuário como objetos Movimentacao, com o link dos documentos, e inclui a duração
    da requisição.
    """
    start_time = time.time()

    if not numero_processo or not isinstance(numero_processo, str):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                'code': 2,
                'message': 'ERRO_ENTIDADE_NAO_PROCESSAVEL'
            }
        )

    if tentativas >= TENTATIVAS_MAXIMAS_RECURSIVAS:
        logger.error("Número máximo de tentativas recursivas atingido.")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                'code': 3,
                'message': 'ERRO_SERVIDOR_INTERNO'
            }
        )

    logger.info(f'Função fetch() iniciou. Processo: {numero_processo} - Tentativa {tentativas + 1}')

    # Inicializar o navegador
    try:
        navegador = criar_navegador()
    except Exception as e:
        logger.error(f"Erro ao inicializar o navegador: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                'code': 3,
                'message': 'ERRO_SERVIDOR_INTERNO'
            }
        )

    try:
        # Navegar para a página inicial do TJSC
        navegador.get('https://eprocwebcon.tjsc.jus.br/consulta1g/externo_controlador.php?acao=processo_consulta_publica')
        
        espera = WebDriverWait(navegador, TEMPO_LIMITE)
        campo_processo = espera.until(EC.presence_of_element_located((By.ID, "txtNumProcesso")))

        campo_processo.clear()
        campo_processo.send_keys(numero_processo)

        rotulo_captcha = espera.until(EC.presence_of_element_located((By.ID, "lblInfraCaptcha")))
        imagem_captcha = rotulo_captcha.find_element(By.TAG_NAME, "img")
        url_captcha = imagem_captcha.get_attribute("src")
        logger.info(f"URL do CAPTCHA: {url_captcha}")

        if not url_captcha:
            logger.error("URL do CAPTCHA não encontrada!")
            navegador.quit()
            return await fetch(numero_processo, tentativas + 1)

        if url_captcha.startswith("data:"):
            _, codificado = url_captcha.split(",", 1)
            bytes_imagem = base64.b64decode(codificado)

        else:
            # Verificar se a janela principal ainda existe antes de abrir uma nova
            if len(navegador.window_handles) == 0:
                logger.error("A janela principal do navegador foi fechada inesperadamente.")
                navegador.quit()
                return await fetch(numero_processo, tentativas + 1)

            navegador.execute_script("window.open('');")
            janelas = navegador.window_handles
            
            if len(janelas) < 2:
                logger.error("Não foi possível abrir uma nova janela para capturar o CAPTCHA.")
                navegador.quit()
                return await fetch(numero_processo, tentativas + 1)

            navegador.switch_to.window(janelas[1])  # Alternar para a nova janela

            try:
                navegador.get(url_captcha)
                time.sleep(5)  
                bytes_imagem = navegador.get_screenshot_as_png()

            except Exception as e:
                logger.error(f"Erro ao capturar o CAPTCHA: {e}")
                if len(navegador.window_handles) > 1:
                    navegador.close()  

                if len(navegador.window_handles) > 0:
                    navegador.switch_to.window(janelas[0])  
                navegador.quit()
                return await fetch(numero_processo, tentativas + 1)
            
            finally:
                if len(navegador.window_handles) > 1:
                    navegador.close()  # Fechar a nova janela

                if len(navegador.window_handles) > 0:
                    navegador.switch_to.window(janelas[0])  

        cliente = Client("Nischay103/captcha_recognition")
        try:
            with open("captcha_temporario.png", "wb") as arquivo:
                arquivo.write(bytes_imagem)
            resultado_ocr = cliente.predict(
                input=handle_file("captcha_temporario.png"),
                api_name="/predict"
            ).strip()
            logger.info(f"CAPTCHA reconhecido pela API: {resultado_ocr}")

        except Exception as e:
            logger.error(f"Erro ao usar a API captcha_recognition: {e}")
            try:
                import ddddocr
                motor_ocr = ddddocr.DdddOcr()
                resultado_ocr = motor_ocr.classification(bytes_imagem)
                logger.info(f"CAPTCHA reconhecido pelo ddddocr (fallback): {resultado_ocr}")

            except Exception as fallback_e:
                logger.error(f"Erro no fallback ddddocr: {fallback_e}")
                navegador.quit()
                return await fetch(numero_processo, tentativas + 1)

        if not resultado_ocr or len(resultado_ocr) != 4 or not resultado_ocr.isalnum():
            logger.warning(f"Resultado do OCR inválido: {resultado_ocr}. Reiniciando sessão...")
            navegador.quit()
            return await fetch(numero_processo, tentativas + 1)

        campo_captcha = espera.until(EC.presence_of_element_located((By.ID, "txtInfraCaptcha")))
        campo_captcha.clear()
        campo_captcha.send_keys(resultado_ocr)

        botao_consultar = espera.until(EC.element_to_be_clickable((By.ID, "sbmNovo")))
        botao_consultar.click()

        try:
            espera.until(EC.presence_of_element_located(
                (By.XPATH, "//h1[contains(text(),'Consulta Processual - Detalhes do Processo')]")
            ))
            pagina_html = navegador.page_source
            navegador.quit()
            todas_movimentacoes = capturar_todas_movimentacoes(pagina_html)

            if not todas_movimentacoes or todas_movimentacoes == ["Nenhuma movimentação encontrada na tabela"]:
                logger.error("Nenhuma movimentação encontrada para o processo.")
                duration = time.time() - start_time

                return {
                    'code': 400,
                    'message': 'Nenhuma movimentação encontrada para o processo.',
                    'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                    'request_duration': duration
                }

            ultima_movimentacao_atual = todas_movimentacoes[0] if isinstance(todas_movimentacoes[0], Movimentacao) else None
            logger.info(f"Última movimentação atual: {ultima_movimentacao_atual}")

            if not ultima_movimentacao_atual:
                logger.error("Nenhuma movimentação válida encontrada para o processo.")
                duration = time.time() - start_time

                return {
                    'code': 400,
                    'message': 'Nenhuma movimentação válida encontrada para o processo.',
                    'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                    'request_duration': duration
                }

            logger.info("Processo consultado com sucesso.")
            duration = time.time() - start_time

            # 
            return {
                'code': 0,
                'message': 'Consulta realizada com sucesso',
                'results': todas_movimentacoes,
                'datetime': datetime.now().strftime('%d-%m-%Y %H:%M:%S'),
                'request_duration': duration
            }

        except Exception as e:
            logger.error(f"Erro durante a submissão ou carregamento da página: {e}")
            navegador.quit()
            return await fetch(numero_processo, tentativas + 1)

    except Exception as e:
        logger.error(f"Erro geral durante a consulta: {e}")
        if 'navegador' in locals() and navegador:
            navegador.quit()
        return await fetch(numero_processo, tentativas + 1)

    finally:
        if 'navegador' in locals() and navegador:
            navegador.quit()
        logger.info(f'Função fetch() terminou. Processo: {numero_processo}')
