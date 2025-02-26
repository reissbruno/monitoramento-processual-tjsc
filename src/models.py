from enum import Enum
from pydantic import BaseModel
from typing import Optional, List

class Movimentacao(BaseModel):
    evento: str = ""
    data_hora: str = ""
    descricao: str = ""
    documentos: str = ""

class ResponseSite(BaseModel):
    movimentacoes: List[Movimentacao] = []
    
class ResponseDefault(BaseModel):
    code: int
    message: str
    datetime: str
    results: List[ResponseSite]
    request_duration: float

class ResponseError(BaseModel):
    code: int
    message: str