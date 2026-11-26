"""Gera o Buscador de Editais: estudo de oportunidades para o lead, com dados ao vivo do PNCP.

A página consulta o PNCP direto do navegador; aqui só embutimos a lista de municípios
(com coordenadas) e as imagens da marca. Sem dependências externas (Python 3.9+).

Uso:
    python3 editais_pncp.py            # gera 'Buscador de Editais.html' na pasta principal
No GitHub Actions, SAIDA_APP=site/index.html publica a página no site.
"""
import base64
import csv
import json
import os
import re
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARQUIVO_COORDS = BASE / "municipios_coords.csv"  # uf;municipio;codigo_ibge;lat;lon
ARQUIVO_TEMPLATE = BASE / "estudo_template.html"
ARQUIVO_SAIDA = (Path(os.environ["SAIDA_APP"]) if os.environ.get("SAIDA_APP")
                 else BASE.parent / "Buscador de Editais.html")


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", texto).strip()


def gerar() -> None:
    municipios = []
    with open(ARQUIVO_COORDS, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if r.get("codigo_ibge") and r.get("lat") and r.get("lon"):
                municipios.append([r["municipio"], r["uf"], r["codigo_ibge"],
                                   round(float(r["lat"]), 4), round(float(r["lon"]), 4)])
    municipios.sort(key=lambda m: (normalizar(m[0]), m[1]))
    js = json.dumps(municipios, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = ARQUIVO_TEMPLATE.read_text(encoding="utf-8").replace("/*__MUNICIPIOS__*/null", js)
    for marcador, arquivo in (("__LOGO_CLARO__", "logo_claro.png"), ("__LOGO_ESCURO__", "logo_escuro.png"),
                              ("__FAVICON__", "favicon.png")):
        caminho = BASE / arquivo
        uri = "data:image/png;base64," + base64.b64encode(caminho.read_bytes()).decode() if caminho.exists() else ""
        html = html.replace(marcador, uri)
    ARQUIVO_SAIDA.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO_SAIDA.write_text(html, encoding="utf-8")
    print(f"Buscador de Editais gerado: {ARQUIVO_SAIDA}")
    if os.environ.get("SAIDA_APP"):
        # Endereço antigo do gerador (estudo.html) continua funcionando
        (ARQUIVO_SAIDA.parent / "estudo.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>Buscador de Editais</title>'
            '<meta http-equiv="refresh" content="0; url=./">'
            '<script>location.replace("./" + location.search)</script>'
            '<a href="./">Abrir o Buscador de Editais</a>', encoding="utf-8")


if __name__ == "__main__":
    gerar()
