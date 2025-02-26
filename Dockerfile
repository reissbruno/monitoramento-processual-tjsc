FROM python:3.11-slim-buster

# Atualiza o sistema e instala dependências essenciais (exemplo: gcc para compilações nativas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
 && rm -rf /var/lib/apt/lists/*

# Define o diretório de trabalho
WORKDIR /app

# Copia o arquivo de requisitos e instala as dependências Python
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copia o restante do código da aplicação para a imagem
COPY . .

# Exponha a porta desejada (neste exemplo, 80)
EXPOSE 80

# Comando para iniciar a aplicação usando uvicorn
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "80"]
