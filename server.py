# stdlib imports
import logging
import time
from os import environ as env

# 3rd party imports
from fastapi.logger import logger as fastapi_logger
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# Local imports
from src import models, consulta

# Define configuracoes basicas para o logger
msg_frt = "[%(asctime)s] %(levelname)s [%(name)s] - %(message)s"
time_frt = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(msg_frt, time_frt)
handler = logging.StreamHandler()
handler.setFormatter(formatter)
LOG_LEVEL = env.get('LOG_LEVEL', default='INFO')
fastapi_logger.addHandler(handler)
fastapi_logger.setLevel(LOG_LEVEL)

logger_name = env.get('BOT_NAME', default='monitoramento-processual-tjsc')
fastapi_logger.name = logger_name

desc = '<a href="https://eprocwebcon.tjsc.jus.br/consulta1g/externo_controlador.php?acao=processo_consulta_publica" target="_blank">FONTE</a>'

tags_metadata = [
    {
        'name': 'monitoramento-processual-tjsc',
        'description': 'Consulta processual no tribunal de justiça de Santa Catarina',
    }
]

responses = {
    407: {'model': models.ResponseError, 'description': 'Proxy Authentication Required'},
    422: {'model': models.ResponseError, 'description': 'Unprocessable Entity'},
    500: {'model': models.ResponseError, 'description': 'Erro interno no servidor'},
    502: {'model': models.ResponseError, 'description': 'Bad Gateway'},
    504: {'model': models.ResponseError, 'description': 'Conexao com o site excedeu tempo limite'},
    509: {'model': models.ResponseError, 'description': 'Nao foi possivel resolver o captcha'},
    512: {'model': models.ResponseError, 'description': 'Erro ao executar parse da pagina'},
    513: {'model': models.ResponseError, 'description': 'Argumentos invalidos'}

}

#---------------------------- Application -------------------------------
app = FastAPI(
    title='REST API - Monitoramento processual no tribunal de justiça de Santa Catarina', 
    description=desc, 
    debug=False, 
    openapi_tags=tags_metadata,
    openapi_url="/api/monitoramento-processual-tjsc/consulta/docs/openapi.json",
    docs_url='/api/monitoramento-processual-tjsc/consulta/docs'
)

app.add_middleware(
    CORSMiddleware, 
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'])

#---------------------------- Query params -------------------------------
processo = Query(
    ..., 
    description='Processo a ser consultado',
)
#---------------------------- Endpoints -------------------------------
@app.get(
    path="/api/monitoramento-processual-tjsc/consulta", 
    tags=['monitoramento-processual-tjsc'])
async def get_consulta(processo: str):
    return await consulta.fetch(processo)


#--------------------------- Static Files ------------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")


