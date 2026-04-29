import os
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv
from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
    BlobSasPermissions,
)

# Carrega .env da mesma pasta do app.py
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

CU_ENDPOINT = os.getenv("AZURE_CU_ENDPOINT", "").rstrip("/")
CU_KEY = os.getenv("AZURE_CU_KEY", "")
API_VERSION = os.getenv("AZURE_CU_API_VERSION", "2025-11-01")

STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "content-understanding")

ANALYZERS = {
    "Imagem / Foto": os.getenv("ANALYZER_IMAGE", "analyzer_image"),
    "Fatura / Documento": os.getenv("ANALYZER_INVOICE", "faturateste"),
    "Vídeo de reunião": os.getenv("ANALYZER_MEETING", "meetinganalyzer"),
    "Áudio voicemail": os.getenv("ANALYZER_VOICEMAIL", "voicemailanalyzer"),
}

EXTENSIONS = {
    "Imagem / Foto": ["jpg", "jpeg", "png", "bmp", "tiff", "webp"],
    "Fatura / Documento": ["pdf", "jpg", "jpeg", "png", "tiff"],
    "Vídeo de reunião": ["mp4", "mov", "avi", "mkv", "webm"],
    "Áudio voicemail": ["mp3", "wav", "m4a", "ogg", "flac"],
}

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


st.set_page_config(page_title="Foundry Content Understanding", layout="wide")
st.title("Azure AI Foundry - Content Understanding")
st.caption("Capture foto pela câmera ou envie arquivos para os analyzers criados no AI-103 / Foundry.")


def validate_config():
    missing = []

    values = {
        "AZURE_CU_ENDPOINT": CU_ENDPOINT,
        "AZURE_CU_KEY": CU_KEY,
        "AZURE_STORAGE_CONNECTION_STRING": STORAGE_CONNECTION_STRING,
    }

    for name, value in values.items():
        if not value:
            missing.append(name)

    if missing:
        st.error(f"Configure no arquivo .env: {', '.join(missing)}")
        st.stop()

    if "/api/projects" in CU_ENDPOINT:
        st.error(
            "AZURE_CU_ENDPOINT está incorreto. Use somente o endpoint base, "
            "exemplo: https://projeto-undersanting-resource.services.ai.azure.com"
        )
        st.stop()


def upload_to_blob_and_get_sas(file_bytes: bytes, filename: str, content_type: str) -> str:
    service = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
    container = service.get_container_client(STORAGE_CONTAINER)

    try:
        container.create_container()
    except Exception:
        pass

    safe_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}/{uuid.uuid4()}-{Path(filename).name}"

    blob_client = container.get_blob_client(safe_name)
    blob_client.upload_blob(
        file_bytes,
        overwrite=True,
        content_settings=ContentSettings(
            content_type=content_type or "application/octet-stream"
        ),
    )

    account_name = service.account_name
    account_key = service.credential.account_key

    sas = generate_blob_sas(
        account_name=account_name,
        container_name=STORAGE_CONTAINER,
        blob_name=safe_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=2),
    )

    return f"https://{account_name}.blob.core.windows.net/{STORAGE_CONTAINER}/{safe_name}?{sas}"


def start_analysis(analyzer_id: str, file_url: str) -> str:
    url = (
        f"{CU_ENDPOINT}/contentunderstanding/analyzers/"
        f"{analyzer_id}:analyze?api-version={API_VERSION}"
    )

    headers = {
        "Ocp-Apim-Subscription-Key": CU_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "inputs": [
            {
                "url": file_url
            }
        ]
    }

    response = requests.post(url, headers=headers, json=payload, timeout=60)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Erro ao iniciar análise ({response.status_code}): {response.text}\n\nURL usada: {url}"
        )

    operation_location = (
        response.headers.get("Operation-Location")
        or response.headers.get("operation-location")
    )

    if operation_location:
        return operation_location

    data = response.json()
    return data.get("resultUrl") or data.get("operationLocation") or data.get("id") or ""


def poll_analysis(operation_location: str, max_wait_seconds: int = 180) -> dict:
    headers = {
        "Ocp-Apim-Subscription-Key": CU_KEY,
    }

    deadline = time.time() + max_wait_seconds
    last = {}

    while time.time() < deadline:
        response = requests.get(operation_location, headers=headers, timeout=60)

        if response.status_code >= 400:
            raise RuntimeError(
                f"Erro consultando operação ({response.status_code}): {response.text}"
            )

        last = response.json()
        status = str(last.get("status", "")).lower()

        if status in ("succeeded", "failed", "canceled", "cancelled"):
            return last

        time.sleep(3)

    raise TimeoutError(
        "Tempo excedido aguardando análise. Último retorno: "
        + json.dumps(last, ensure_ascii=False)[:1000]
    )


def analyze(analyzer_id: str, file_bytes: bytes, filename: str, content_type: str, max_wait: int):
    sas_url = upload_to_blob_and_get_sas(file_bytes, filename, content_type)
    operation = start_analysis(analyzer_id, sas_url)

    if not operation:
        raise RuntimeError(
            "A API não retornou Operation-Location. Verifique endpoint, API version e analyzer."
        )

    result = poll_analysis(operation, max_wait_seconds=max_wait)
    return result, sas_url


validate_config()

with st.sidebar:
    st.header("Configuração")

    st.write("Endpoint ativo:")
    st.code(CU_ENDPOINT)

    category = st.selectbox("Categoria", list(ANALYZERS.keys()))
    analyzer_id = st.text_input("Analyzer ID", value=ANALYZERS[category])

    max_wait = st.slider(
        "Timeout da análise",
        min_value=30,
        max_value=600,
        value=180,
        step=30,
    )

    st.info(f"Extensões sugeridas: {', '.join(EXTENSIONS[category])}")

tab_camera, tab_upload = st.tabs(["📷 Câmera", "📁 Upload"])

input_file = None
file_name = None
content_type = None

with tab_camera:
    st.subheader("Tirar foto")
    camera = st.camera_input("Use a câmera do navegador")

    if camera:
        input_file = camera.getvalue()
        file_name = "camera-photo.jpg"
        content_type = "image/jpeg"
        st.image(input_file, caption="Foto capturada", use_container_width=True)

with tab_upload:
    st.subheader("Enviar arquivo")

    uploaded = st.file_uploader(
        "Selecione arquivo",
        type=EXTENSIONS[category],
        accept_multiple_files=False,
    )

    if uploaded:
        input_file = uploaded.getvalue()
        file_name = uploaded.name
        suffix = Path(file_name).suffix.lower()
        content_type = uploaded.type or CONTENT_TYPES.get(
            suffix,
            "application/octet-stream",
        )

        st.write(
            f"Arquivo: `{file_name}` | Tipo: `{content_type}` | "
            f"Tamanho: {len(input_file):,} bytes"
        )

if st.button("Analisar no Azure AI Foundry", type="primary", disabled=input_file is None):
    with st.spinner("Enviando para Blob Storage e analisando no Content Understanding..."):
        try:
            result, sas_url = analyze(
                analyzer_id=analyzer_id,
                file_bytes=input_file,
                filename=file_name,
                content_type=content_type,
                max_wait=max_wait,
            )

            st.success("Análise concluída")

            col1, col2 = st.columns([1, 2])

            with col1:
                st.write("Analyzer usado")
                st.code(analyzer_id)

                st.write("URL SAS temporária")
                st.code(sas_url[:180] + "...")

            with col2:
                st.json(result)

                st.download_button(
                    "Baixar JSON",
                    data=json.dumps(result, ensure_ascii=False, indent=2),
                    file_name=f"resultado-{analyzer_id}.json",
                    mime="application/json",
                )

        except Exception as exc:
            st.error(str(exc))