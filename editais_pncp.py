"""Coleta editais de engenharia abertos no PNCP (MG, SP, BA, PR) e consulta por raio a partir da cidade de um lead.

Sem dependências externas (só biblioteca padrão; Python 3.9+).

Uso:
    python3 editais_pncp.py coletar            # baixa do PNCP e atualiza 'Buscar Editais.html'
    python3 editais_pncp.py app                # só regera 'Buscar Editais.html'
    python3 editais_pncp.py atualizar_coords   # reaplica municipios_coords.csv sem recoletar
    python3 editais_pncp.py entorno BA "Porto Seguro" 100
"""
import csv
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

API = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
BASE = Path(__file__).resolve().parent
ARQUIVO_EDITAIS = BASE / "editais_engenharia.csv"
ARQUIVO_PENDENCIAS = BASE / "municipios_sem_coordenada.csv"
# Aceita: uf;municipio;lat;lon   e/ou   codigo_ibge;lat;lon (separador ; ou ,)
ARQUIVO_COORDS = BASE / "municipios_coords.csv"

UFS = {"MG": "Minas Gerais", "SP": "São Paulo", "BA": "Bahia", "PR": "Paraná"}
MODALIDADES = {4: "Concorrência eletrônica", 6: "Pregão eletrônico"}

COORDS_AMOSTRA = {
    ("SP", "campinas"): (-22.9099, -47.0626),
    ("SP", "piracicaba"): (-22.7253, -47.6492),
    ("MG", "pouso alegre"): (-22.2298, -45.9367),
    ("MG", "manhuacu"): (-20.2587, -42.0336),
    ("BA", "porto seguro"): (-16.4436, -39.0644),
    ("PR", "curitiba"): (-25.4284, -49.2733),
}

# Casamento por palavra (evita "manobra", "sobra", "aponte"...). Sufixo \w* cobre plurais/flexões.
TERMOS_ENGENHARIA = [
    r"obras?", r"engenharia", r"reforma\w*", r"construc\w*", r"pavimenta\w*", r"drenage\w*",
    r"edifica\w*", r"pontes?", r"saneamento", r"esgot\w*", r"asfalt\w*", r"infraestrutura",
    r"projeto executivo", r"manutencao predial", r"instalac\w* eletric\w*", r"hidraulic\w*",
    r"terraplenage\w*", r"recapeamento", r"galerias?", r"contenc\w*", r"urbaniza\w*",
    r"passage\w* molhada\w*", r"quadra\w* coberta\w*", r"calcament\w*",
]
# Falsos positivos frequentes (serviço/compra que só menciona o termo)
EXCLUSOES = [
    r"mao de obra", r"locacao de (veiculos|maquinas|equipamentos)", r"fornecimento de energia",
    r"aquisicao de (material|materiais)", r"transporte (coletivo|escolar)",
]
RE_INCLUI = re.compile(r"\b(" + "|".join(TERMOS_ENGENHARIA) + r")\b")
RE_EXCLUI = re.compile(r"\b(" + "|".join(EXCLUSOES) + r")\b")


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", texto).strip()


def eh_engenharia(objeto: str) -> bool:
    obj = normalizar(objeto)
    if not RE_INCLUI.search(obj):
        return False
    # Exclui só se o termo de engenharia aparecer apenas dentro da expressão excluída
    if RE_EXCLUI.search(obj) and not RE_INCLUI.search(RE_EXCLUI.sub(" ", obj)):
        return False
    return True


def carregar_coords() -> tuple:
    """Retorna (por_nome, por_ibge)."""
    por_nome, por_ibge = dict(COORDS_AMOSTRA), {}
    if ARQUIVO_COORDS.exists():
        with open(ARQUIVO_COORDS, encoding="utf-8-sig") as f:
            amostra = f.read(2048)
            f.seek(0)
            delim = ";" if amostra.count(";") >= amostra.count(",") else ","
            for row in csv.DictReader(f, delimiter=delim):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                lat = row.get("lat") or row.get("latitude")
                lon = row.get("lon") or row.get("longitude")
                if not lat or not lon:
                    continue
                ponto = (float(lat), float(lon))
                if row.get("codigo_ibge"):
                    por_ibge[row["codigo_ibge"]] = ponto
                if row.get("uf") and (row.get("municipio") or row.get("nome")):
                    por_nome[(row["uf"].upper(), normalizar(row.get("municipio") or row["nome"]))] = ponto
    return por_nome, por_ibge


def distancia_km(a: tuple, b: tuple) -> float:
    """Distância em linha reta (haversine). Por estrada costuma ser 20–40% maior."""
    r = 6371.0
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


PAUSA_ENTRE_REQUISICOES = 1.0  # segundos; a API do PNCP devolve 429 se for mais rápido


def _get(params: dict, tentativas: int = 8) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    for i in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "coleta-editais/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 204:
                    return {"data": [], "totalPaginas": 0}
                corpo = r.read()
                return json.loads(corpo) if corpo else {"data": [], "totalPaginas": 0}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429:
                raise
            if i == tentativas - 1:
                raise
            espera = min(60, 5 * 2 ** i)
            if isinstance(e, urllib.error.HTTPError) and (e.headers.get("Retry-After") or "").isdigit():
                espera = int(e.headers["Retry-After"])
            print(f"\n  aguardando {espera}s ({e})")
            time.sleep(espera)
    return {"data": [], "totalPaginas": 0}


def buscar_uf(uf: str, modalidade: int, dias_frente: int = 60) -> list:
    """Editais com recebimento de propostas aberto até hoje + dias_frente.
    (O endpoint /proposta só usa dataFinal; dataInicial é ignorado.)"""
    params = {
        "dataFinal": (date.today() + timedelta(days=dias_frente)).strftime("%Y%m%d"),
        "codigoModalidadeContratacao": modalidade,
        "uf": uf,
        "tamanhoPagina": 50,
        "pagina": 1,
    }
    resultados = []
    while True:
        corpo = _get(params)
        resultados.extend(corpo.get("data") or [])
        total = corpo.get("totalPaginas") or 0
        print(f"  {uf} mod {modalidade}: página {params['pagina']}/{total}", end="\r")
        if params["pagina"] >= total:
            break
        params["pagina"] += 1
        time.sleep(PAUSA_ENTRE_REQUISICOES)
    print()
    return resultados


def link_pncp(item: dict) -> str:
    cnpj = (item.get("orgaoEntidade") or {}).get("cnpj")
    if cnpj and item.get("anoCompra") and item.get("sequencialCompra"):
        return f"https://pncp.gov.br/app/editais/{cnpj}/{item['anoCompra']}/{item['sequencialCompra']}"
    return ""


def coletar() -> None:
    por_nome, por_ibge = carregar_coords()
    agora = datetime.now()
    linhas, vistos, pendencias = [], set(), {}
    for uf in UFS:
        for cod_mod, nome_mod in MODALIDADES.items():
            for item in buscar_uf(uf, cod_mod):
                ctrl = item.get("numeroControlePNCP", "")
                if ctrl in vistos:
                    continue
                vistos.add(ctrl)
                objeto = re.sub(r"\s+", " ", item.get("objetoCompra") or "").strip()
                # Guarda todos os editais; a coluna "engenharia" marca os de obras/serviços de engenharia
                engenharia = 1 if eh_engenharia(objeto) else 0
                enc = item.get("dataEncerramentoProposta") or ""
                try:
                    if enc and datetime.fromisoformat(enc) < agora:
                        continue  # prazo já encerrado
                except ValueError:
                    pass
                unidade = item.get("unidadeOrgao") or {}
                municipio = unidade.get("municipioNome", "")
                ibge = str(unidade.get("codigoIbge") or "")
                ponto = por_ibge.get(ibge) or por_nome.get((uf, normalizar(municipio)))
                if not ponto:
                    pendencias[(uf, municipio, ibge)] = pendencias.get((uf, municipio, ibge), 0) + 1
                linhas.append({
                    "uf": uf,
                    "municipio": municipio,
                    "codigo_ibge": ibge,
                    "lat": ponto[0] if ponto else "",
                    "lon": ponto[1] if ponto else "",
                    "orgao": (item.get("orgaoEntidade") or {}).get("razaoSocial", ""),
                    "objeto": objeto,
                    "valor_estimado": item.get("valorTotalEstimado", ""),
                    "abertura_proposta": item.get("dataAberturaProposta", ""),
                    "encerramento_proposta": enc,
                    "modalidade": nome_mod,
                    "engenharia": engenharia,
                    "numero_controle_pncp": ctrl,
                    "link_pncp": link_pncp(item),
                    "link_origem": item.get("linkSistemaOrigem") or "",
                })
    linhas.sort(key=lambda r: (r["uf"], r["encerramento_proposta"]))
    campos = list(linhas[0].keys()) if linhas else ["vazio"]
    with open(ARQUIVO_EDITAIS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    with open(ARQUIVO_PENDENCIAS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["uf", "municipio", "codigo_ibge", "qtd_editais"])
        for (uf, mun, ibge), n in sorted(pendencias.items(), key=lambda x: -x[1]):
            w.writerow([uf, mun, ibge, n])
    sem = sum(pendencias.values())
    n_eng = sum(r["engenharia"] for r in linhas)
    print(f"{len(linhas)} editais abertos ({n_eng} de engenharia) salvos em {ARQUIVO_EDITAIS.name} "
          f"({sem} sem coordenada, {len(pendencias)} municípios em {ARQUIVO_PENDENCIAS.name})")


def atualizar_coords() -> None:
    """Preenche lat/lon no CSV já coletado, sem consultar a API de novo."""
    por_nome, por_ibge = carregar_coords()
    with open(ARQUIVO_EDITAIS, encoding="utf-8-sig") as f:
        leitor = csv.DictReader(f, delimiter=";")
        campos, linhas = leitor.fieldnames, list(leitor)
    pendencias = {}
    for row in linhas:
        ponto = por_ibge.get(row.get("codigo_ibge", "")) or por_nome.get((row["uf"], normalizar(row["municipio"])))
        row["lat"], row["lon"] = (ponto if ponto else ("", ""))
        if not ponto:
            chave = (row["uf"], row["municipio"], row.get("codigo_ibge", ""))
            pendencias[chave] = pendencias.get(chave, 0) + 1
    with open(ARQUIVO_EDITAIS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    with open(ARQUIVO_PENDENCIAS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["uf", "municipio", "codigo_ibge", "qtd_editais"])
        for (uf, mun, ibge), n in sorted(pendencias.items(), key=lambda x: -x[1]):
            w.writerow([uf, mun, ibge, n])
    print(f"{len(linhas)} editais; {sum(pendencias.values())} ainda sem coordenada")


ARQUIVO_TEMPLATE = BASE / "app_template.html"
# No GitHub Actions, SAIDA_APP=site/index.html; no Mac, o app fica na pasta principal
ARQUIVO_APP = Path(os.environ["SAIDA_APP"]) if os.environ.get("SAIDA_APP") else BASE.parent / "Buscar Editais.html"


def gerar_app() -> None:
    """Gera 'Buscar Editais.html' (abre com dois cliques, funciona sem internet) com os dados embutidos."""
    editais = []
    with open(ARQUIVO_EDITAIS, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            editais.append({
                "uf": r["uf"], "municipio": r["municipio"],
                "lat": float(r["lat"]) if r.get("lat") else None,
                "lon": float(r["lon"]) if r.get("lon") else None,
                "orgao": r["orgao"], "objeto": r["objeto"],
                "valor": float(r["valor_estimado"]) if r.get("valor_estimado") else None,
                "encerramento": r.get("encerramento_proposta", ""),
                "modalidade": r["modalidade"],
                "eng": r.get("engenharia", "1") != "0",
                "link_pncp": r.get("link_pncp", ""), "link_origem": r.get("link_origem", ""),
            })
    municipios = []
    if ARQUIVO_COORDS.exists():
        with open(ARQUIVO_COORDS, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                municipios.append([r["municipio"], r["uf"], float(r["lat"]), float(r["lon"])])
    municipios.sort(key=lambda m: (normalizar(m[0]), m[1]))
    atualizado = datetime.fromtimestamp(ARQUIVO_EDITAIS.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    dados = {"editais": editais, "municipios": municipios, "ufs": list(UFS), "atualizado": atualizado}
    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = ARQUIVO_TEMPLATE.read_text(encoding="utf-8").replace("/*__DADOS__*/null", js)
    import base64
    for marcador, arquivo in (("__LOGO_CLARO__", "logo_claro.png"), ("__LOGO_ESCURO__", "logo_escuro.png"),
                              ("__FAVICON__", "favicon.png")):
        caminho = BASE / arquivo
        uri = "data:image/png;base64," + base64.b64encode(caminho.read_bytes()).decode() if caminho.exists() else ""
        html = html.replace(marcador, uri)
    ARQUIVO_APP.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO_APP.write_text(html, encoding="utf-8")
    print(f"App atualizado: {ARQUIVO_APP}")


def consultar_entorno(uf_lead: str, cidade_lead: str, raio_km: float = 100) -> list:
    por_nome, _ = carregar_coords()
    origem = por_nome.get((uf_lead.upper(), normalizar(cidade_lead)))
    if origem is None:
        raise ValueError(f"Coordenadas não encontradas para {cidade_lead}/{uf_lead}. "
                         f"Inclua o município em {ARQUIVO_COORDS.name}.")
    encontrados = []
    with open(ARQUIVO_EDITAIS, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("engenharia", "1") == "0":
                continue
            if not row.get("lat") or not row.get("lon"):
                continue
            d = distancia_km(origem, (float(row["lat"]), float(row["lon"])))
            if d <= raio_km:
                row["distancia_km"] = round(d, 1)
                encontrados.append(row)
    return sorted(encontrados, key=lambda r: r["distancia_km"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "coletar"
    if cmd == "coletar":
        coletar()
        gerar_app()
    elif cmd == "atualizar_coords":
        atualizar_coords()
        gerar_app()
    elif cmd == "app":
        gerar_app()
    elif cmd == "entorno":
        uf, cidade = sys.argv[2], sys.argv[3]
        raio = float(sys.argv[4]) if len(sys.argv) > 4 else 100
        res = consultar_entorno(uf, cidade, raio)
        print(f"{len(res)} editais até {raio:g} km de {cidade}/{uf}")
        for e in res[:30]:
            print(f"{e['distancia_km']:>6} km | {e['municipio']:<22} | até {e['encerramento_proposta'][:10]} | {e['objeto'][:80]}")
    else:
        print(__doc__)
