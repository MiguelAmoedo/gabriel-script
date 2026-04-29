"""
EPDP UFAM - Radar Regulatório Automático
=========================================
Monitor diário de publicações oficiais (DOU/STF/STJ via LexML, ANPD, CGU, MGI, TCU)
focado em LGPD, IA, Governança de Dados e Decisões Governamentais.

Uso: python monitor_dpo.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from time import mktime
from typing import Optional

import feedparser
import requests
import urllib3
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

# Avisos InsecureRequestWarning são esperados em portais .gov.br com certificados
# expirados/auto-assinados. Desligamos para manter o terminal limpo.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

if getattr(sys, "frozen", False):
    BASE_PATH = os.path.dirname(sys.executable)
else:
    BASE_PATH = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.path.join(BASE_PATH, "dpo_base_dados.json")
HTML_REPORT = os.path.join(BASE_PATH, "Dashboard_EPDP_UFAM.html")

DIAS_RETROSPECTIVA = 90
TIMEOUT_HTTP = 30
MAX_RETRIES = 2
MAX_WORKERS = 5
RESUMO_MAX_CHARS = 320

console = Console()

# ----------------------------------------------------------------------------
# Query LexML — operadores em minúsculo funcionam melhor no backend
# ----------------------------------------------------------------------------
QUERY_LEXML = (
    '("proteção de dados" ou "LGPD" ou "ANPD" ou "inteligência artificial" '
    'ou "governança de dados") e ("portaria" ou "resolução" ou "acórdão" '
    'ou "MEC" ou "Universidade" ou "MGI")'
)
ENCODED_LEXML = urllib.parse.quote_plus(QUERY_LEXML)

FEEDS: dict[str, str] = {
    "ANPD (Oficial)": "https://www.gov.br/anpd/pt-br/assuntos/noticias/RSS",
    "CGU (Transparência)": "https://www.gov.br/cgu/pt-br/assuntos/noticias/RSS",
    "MGI (Governo Digital)": "https://www.gov.br/gestao/pt-br/assuntos/noticias/RSS",
    "TCU (Contas e Gov)": (
        "https://portal.tcu.gov.br/lumis/portal/feed/rss.jsp"
        "?idServiceInstance=8A8182604C749B0E014C794A6E823616"
    ),
    "Diário Oficial / LexML": f"https://www.lexml.gov.br/busca/srss?q={ENCODED_LEXML}",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}

# ----------------------------------------------------------------------------
# Inteligência de Classificação — palavras-chave por domínio
# ----------------------------------------------------------------------------
TERMOS_REGULAMENTACAO = [
    "portaria", "resolução", "decreto", "normativa", "instrução normativa",
    "guia", "diretriz", "projeto de lei", "medida provisória",
]
TERMOS_FISCALIZACAO = [
    "multa", "sanção", "vazamento", "incidente", "auditoria", "condenação",
    "fiscalização", "violação", "infração", "notificação",
]
TERMOS_JURISPRUDENCIA = [
    "acórdão", "stf", "stj", "mpf", "tcu", "tribunal", "decisão",
    "súmula", "agravo", "habeas data",
]
TERMOS_UFAM = [
    "ufam", "mec", "universidade federal", "ifes", "andifes",
    "ensino superior", "instituição federal", "capes",
]
TERMOS_IA_DADOS = [
    "inteligência artificial", "governança de dados", "algoritmo",
    "machine learning", "lgpd", "proteção de dados", "anpd",
    "dados pessoais", " ia ",
]


# ============================================================================
# MODELO
# ============================================================================

@dataclass
class Achado:
    orgao: str
    titulo: str
    link: str
    data: str
    timestamp: float
    prioridade: str
    tags: list[str]
    resumo: str


# ============================================================================
# PERSISTÊNCIA
# ============================================================================

def carregar_banco() -> list[dict]:
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"[yellow]Banco corrompido ou ilegível, recriando: {e}[/yellow]")
        return []


def salvar_banco(achados: list[dict]) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(achados, f, ensure_ascii=False, indent=2)


# ============================================================================
# PARSING / NORMALIZAÇÃO
# ============================================================================

def limpar_html(texto: str) -> str:
    if not texto:
        return ""
    soup = BeautifulSoup(texto, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text()).strip()


def parse_data(entry) -> tuple[datetime, bool]:
    """Tenta múltiplos campos de data e retorna (datetime, foi_real)."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        valor = entry.get(key)
        if valor:
            try:
                return datetime.fromtimestamp(mktime(valor)), True
            except (TypeError, ValueError, OverflowError):
                continue
    return datetime.now(), False


def classificar(titulo: str, resumo: str) -> Optional[tuple[str, list[str]]]:
    """
    Aplica regras de classificação retornando (prioridade, tags) ou None
    quando o item deve ser descartado.

    Hierarquia de prioridade: Ação Exigida > Alto Impacto > Informativo
    """
    texto = f"{titulo} {resumo}".lower()

    # Filtro anti-ruído: ouvidoria sem caráter normativo
    if "plataforma integrada de ouvidoria" in titulo.lower() and "notícia" not in texto:
        return None

    tags: list[str] = []
    prioridade = "Informativo"

    if any(k in texto for k in TERMOS_FISCALIZACAO):
        prioridade = "Ação Exigida"
        tags.append("Fiscalização/Incidente")

    if any(k in texto for k in TERMOS_REGULAMENTACAO):
        if prioridade != "Ação Exigida":
            prioridade = "Ação Exigida"
        tags.append("Regulamentação")

    if any(k in texto for k in TERMOS_JURISPRUDENCIA):
        tags.append("Jurisprudência")

    if any(k in texto for k in TERMOS_IA_DADOS):
        if prioridade == "Informativo":
            prioridade = "Ação Exigida"
        tags.append("IA/Governança de Dados")

    if any(k in texto for k in TERMOS_UFAM):
        if prioridade == "Informativo":
            prioridade = "Alto Impacto"
        tags.append("Contexto MEC/UFAM")

    return prioridade, tags


# ============================================================================
# COLETOR DE FEED (executado em paralelo)
# ============================================================================

def buscar_feed(
    orgao: str, url: str, links_vistos: set[str], limite_ts: float
) -> tuple[str, list[dict], Optional[str]]:
    """Baixa e processa um feed. Retorna (orgao, achados, erro)."""
    sessao = requests.Session()
    sessao.headers.update(HEADERS)

    resposta = None
    last_err: Optional[str] = None

    for tentativa in range(MAX_RETRIES + 1):
        try:
            resposta = sessao.get(url, timeout=TIMEOUT_HTTP, verify=False)
            resposta.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if tentativa == MAX_RETRIES:
                return orgao, [], f"Conexão falhou após {MAX_RETRIES + 1} tentativas: {last_err}"

    if resposta is None:
        return orgao, [], last_err or "Resposta vazia"

    feed = feedparser.parse(resposta.content)
    if feed.bozo and not feed.entries:
        return orgao, [], f"XML malformado: {feed.bozo_exception}"

    achados: list[dict] = []
    for entry in feed.entries:
        link = entry.get("link", "").strip()
        if not link or link in links_vistos:
            continue

        dt_pub, dt_real = parse_data(entry)
        if dt_pub.timestamp() < limite_ts:
            continue

        titulo = entry.get("title", "Sem Título").strip()
        resumo = limpar_html(entry.get("summary", ""))

        clf = classificar(titulo, resumo)
        if clf is None:
            continue
        prioridade, tags = clf

        achado = Achado(
            orgao=orgao,
            titulo=titulo,
            link=link,
            data=dt_pub.strftime("%d/%m/%Y") + ("" if dt_real else "*"),
            timestamp=dt_pub.timestamp(),
            prioridade=prioridade,
            tags=tags,
            resumo=(resumo[:RESUMO_MAX_CHARS] + "...") if len(resumo) > RESUMO_MAX_CHARS else resumo,
        )
        achados.append(asdict(achado))

    return orgao, achados, None


# ============================================================================
# ORQUESTRAÇÃO
# ============================================================================

def monitorar() -> None:
    banco = carregar_banco()
    links_vistos = {item["link"] for item in banco if item.get("link")}

    limite_tempo = datetime.now() - timedelta(days=DIAS_RETROSPECTIVA)
    limite_ts = limite_tempo.timestamp()

    console.print(
        Panel(
            f"[bold green]EPDP UFAM • Radar Regulatório Automático[/bold green]\n"
            f"[white]LexML (DOU/STF/STJ) + ANPD + CGU + MGI + TCU\n"
            f"Janela: últimos [cyan]{DIAS_RETROSPECTIVA}[/cyan] dias  •  "
            f"Base atual: [cyan]{len(banco)}[/cyan] registros[/white]",
            border_style="green",
            title="DPO Sentinel",
        )
    )

    novos: list[dict] = []
    erros: dict[str, str] = {}
    relatorio: dict[str, int] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[cyan]{task.fields[fonte]}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Coletando publicações", total=len(FEEDS), fonte="iniciando..."
        )

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(buscar_feed, orgao, url, links_vistos, limite_ts): orgao
                for orgao, url in FEEDS.items()
            }
            for future in as_completed(futures):
                orgao = futures[future]
                try:
                    _, achados, erro = future.result()
                    if erro:
                        erros[orgao] = erro
                    novos.extend(achados)
                    relatorio[orgao] = len(achados)
                except Exception as e:  # noqa: BLE001
                    erros[orgao] = f"Falha inesperada: {e}"
                    relatorio[orgao] = 0
                progress.update(task, advance=1, fonte=orgao)

    # Tabela de resumo
    tabela = Table(title="Coleta por Fonte", show_header=True, header_style="bold cyan")
    tabela.add_column("Fonte", style="white")
    tabela.add_column("Novos", justify="right", style="green")
    tabela.add_column("Status")
    for orgao in FEEDS:
        n = relatorio.get(orgao, 0)
        status = "[red]falhou[/red]" if orgao in erros else "[green]ok[/green]"
        tabela.add_row(orgao, str(n), status)
    console.print(tabela)

    for o, e in erros.items():
        console.print(f"[red]✗ {o}[/red]: {e}")

    # Mescla, deduplica, filtra por janela e ordena
    todas = banco + novos
    seen: set[str] = set()
    unicas: list[dict] = []
    for n in todas:
        link = n.get("link")
        if link and link not in seen:
            seen.add(link)
            unicas.append(n)

    validas = [n for n in unicas if n.get("timestamp", 0) >= limite_ts]
    validas.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

    salvar_e_abrir(validas, len(novos), DIAS_RETROSPECTIVA)


# ============================================================================
# MÉTRICAS PARA O DASHBOARD
# ============================================================================

def calcular_metricas(achados: list[dict]) -> dict:
    metrics = {
        "total": len(achados),
        "acao": sum(1 for a in achados if a.get("prioridade") == "Ação Exigida"),
        "impacto": sum(1 for a in achados if a.get("prioridade") == "Alto Impacto"),
        "info": sum(1 for a in achados if a.get("prioridade") == "Informativo"),
    }
    por_fonte: dict[str, int] = {}
    for a in achados:
        por_fonte[a.get("orgao", "?")] = por_fonte.get(a.get("orgao", "?"), 0) + 1
    metrics["por_fonte"] = por_fonte

    # Documentos da última semana (hot count)
    sete_dias_ts = (datetime.now() - timedelta(days=7)).timestamp()
    metrics["ultima_semana"] = sum(
        1 for a in achados if a.get("timestamp", 0) >= sete_dias_ts
    )
    return metrics


# ============================================================================
# DASHBOARD HTML
# ============================================================================

def gerar_dashboard(achados: list[dict], dias: int) -> None:
    json_data = json.dumps(achados, ensure_ascii=False)
    metrics = calcular_metricas(achados)
    atualizado = datetime.now().strftime("%d/%m/%Y %H:%M")

    html = HTML_TEMPLATE.format(
        dias=dias,
        atualizado=atualizado,
        total=metrics["total"],
        acao=metrics["acao"],
        impacto=metrics["impacto"],
        info=metrics["info"],
        ultima_semana=metrics["ultima_semana"],
        json_data=json_data,
    )

    with open(HTML_REPORT, "w", encoding="utf-8") as f:
        f.write(html)


def salvar_e_abrir(achados: list[dict], qtd_novos: int, dias: int) -> None:
    if not achados:
        console.print(
            f"\n[yellow]Nenhuma publicação relevante nos últimos {dias} dias.[/yellow]"
        )
        console.print("[white]Dica: ajuste palavras-chave ou tente novamente mais tarde.[/white]")
        return

    salvar_banco(achados)
    gerar_dashboard(achados, dias)

    console.print(
        f"\n[bold green]✔[/bold green] Concluído! "
        f"[cyan]{len(achados)}[/cyan] documentos na base "
        f"([green]+{qtd_novos}[/green] novos nesta execução)."
    )
    console.print(f"[dim]Banco: {DB_FILE}[/dim]")
    console.print(f"[dim]Dashboard: {HTML_REPORT}[/dim]")
    webbrowser.open(f"file://{os.path.realpath(HTML_REPORT)}")


# ============================================================================
# TEMPLATE HTML — usa {{ }} para escapar chaves do .format()
# ============================================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Inteligência Regulatória — EPDP UFAM</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body {{ background-color: #f8fafc; font-family: 'Inter', sans-serif; }}
        ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
        ::-webkit-scrollbar-track {{ background: transparent; }}
        ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 10px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: #94a3b8; }}
        .scrollbar-hide::-webkit-scrollbar {{ display: none; }}
        .card-Ação {{ border-left: 4px solid #ef4444; }}
        .card-Alto {{ border-left: 4px solid #f59e0b; }}
        .card-Informativo {{ border-left: 4px solid #3b82f6; }}
        .tag {{ font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; padding: 4px 10px; border-radius: 9999px; display: inline-flex; align-items: center; }}
        .data-asterisco {{ color: #ef4444; font-weight: bold; cursor: help; }}
        .kbd {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.7rem; padding: 1px 6px; border-radius: 4px; border: 1px solid #cbd5e1; background: #f1f5f9; color: #475569; }}
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: none; }} }}
        .fade-in {{ animation: fadeIn 0.25s ease-out; }}
    </style>
</head>
<body class="flex h-screen overflow-hidden text-slate-800">

    <!-- ====== SIDEBAR ====== -->
    <aside class="w-72 bg-slate-900 text-white h-full flex flex-col shadow-2xl z-20 relative">
        <div class="p-6 border-b border-slate-800">
            <h1 class="text-xl font-bold text-emerald-400 flex items-center tracking-tight">
                <i class="fa-solid fa-scale-balanced mr-3 text-2xl"></i> EPDP Radar
            </h1>
            <p class="text-slate-400 text-xs mt-2 font-medium tracking-wide uppercase">DPO Universitário · UFAM</p>
        </div>

        <div class="p-6 space-y-7 flex-1 overflow-y-auto">
            <div>
                <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3 flex justify-between items-center">
                    <span>Busca Rápida</span>
                    <span class="kbd">/</span>
                </h3>
                <div class="relative">
                    <div class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                        <i class="fa-solid fa-magnifying-glass text-slate-500 text-sm"></i>
                    </div>
                    <input type="text" id="searchInput" placeholder="Portaria, Multa, IA..."
                           class="w-full bg-slate-950 border border-slate-700 rounded-lg py-2.5 pl-10 pr-3 text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent transition-all shadow-inner">
                </div>
            </div>

            <div>
                <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Filtrar por Fonte</h3>
                <div class="relative">
                    <select id="sourceFilter" class="w-full appearance-none bg-slate-950 border border-slate-700 rounded-lg py-2.5 pl-3 pr-10 text-sm text-white focus:outline-none focus:ring-2 focus:ring-emerald-500 transition-all shadow-inner cursor-pointer">
                        <option value="Todas">Todas as Fontes</option>
                        <option value="ANPD (Oficial)">ANPD (Oficial)</option>
                        <option value="CGU (Transparência)">CGU</option>
                        <option value="MGI (Governo Digital)">MGI (Governo Digital)</option>
                        <option value="TCU (Contas e Gov)">TCU</option>
                        <option value="Diário Oficial / LexML">LexML (DOU/STF/STJ)</option>
                    </select>
                    <div class="pointer-events-none absolute inset-y-0 right-0 flex items-center px-3 text-slate-400">
                        <i class="fa-solid fa-chevron-down text-xs"></i>
                    </div>
                </div>
            </div>

            <div>
                <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Ações</h3>
                <button id="exportBtn" class="w-full bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg py-2.5 px-3 text-sm text-white transition-all flex items-center justify-center">
                    <i class="fa-solid fa-file-export mr-2 text-emerald-400"></i> Exportar JSON
                </button>
            </div>

            <div>
                <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Distribuição por Fonte</h3>
                <div id="sourceStats" class="space-y-2 text-xs"></div>
            </div>
        </div>

        <div class="p-5 border-t border-slate-800 bg-slate-950 text-xs text-slate-500 text-center">
            <p class="mb-1"><i class="fa-regular fa-clock mr-1"></i> Retrospectiva: {dias} dias</p>
            <p>Atualizado: <span class="font-semibold text-slate-400">{atualizado}</span></p>
        </div>
    </aside>

    <!-- ====== MAIN ====== -->
    <main class="flex-1 h-full overflow-y-auto bg-slate-50 relative flex flex-col">

        <!-- HEADER STICKY -->
        <div class="sticky top-0 z-10 bg-slate-50/95 backdrop-blur-sm border-b border-slate-200 px-8 lg:px-12 pt-8 pb-4">
            <div class="flex flex-col md:flex-row md:justify-between md:items-end gap-4 mb-6">
                <div>
                    <h2 class="text-3xl font-extrabold text-slate-900 tracking-tight">Atos &amp; Jurisprudência</h2>
                    <p class="text-sm text-slate-500 mt-1">Monitoramento de publicações relevantes para Governo, IA e Proteção de Dados.</p>
                </div>
                <div class="inline-flex items-center text-sm font-semibold text-slate-600 bg-white px-5 py-2.5 rounded-full shadow-sm border border-slate-200">
                    <span id="countDisplay" class="text-emerald-600 font-bold text-lg mr-2">0</span> documentos visíveis
                </div>
            </div>

            <!-- KPI CARDS -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
                <div class="bg-white rounded-xl border border-slate-200 p-4 shadow-sm">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Total</span>
                        <i class="fa-solid fa-database text-slate-300"></i>
                    </div>
                    <div class="text-2xl font-extrabold text-slate-800 mt-1">{total}</div>
                    <div class="text-xs text-emerald-600 font-semibold mt-1"><i class="fa-solid fa-fire mr-1"></i>{ultima_semana} nos últimos 7 dias</div>
                </div>
                <div class="bg-white rounded-xl border border-slate-200 p-4 shadow-sm">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-semibold text-red-600 uppercase tracking-wider">Ação Exigida</span>
                        <i class="fa-solid fa-triangle-exclamation text-red-300"></i>
                    </div>
                    <div class="text-2xl font-extrabold text-red-600 mt-1">{acao}</div>
                    <div class="text-xs text-slate-400 mt-1">Normas, multas e incidentes</div>
                </div>
                <div class="bg-white rounded-xl border border-slate-200 p-4 shadow-sm">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-semibold text-amber-600 uppercase tracking-wider">Alto Impacto</span>
                        <i class="fa-solid fa-building-columns text-amber-300"></i>
                    </div>
                    <div class="text-2xl font-extrabold text-amber-600 mt-1">{impacto}</div>
                    <div class="text-xs text-slate-400 mt-1">Contexto MEC / UFAM / IFES</div>
                </div>
                <div class="bg-white rounded-xl border border-slate-200 p-4 shadow-sm">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-semibold text-blue-600 uppercase tracking-wider">Informativo</span>
                        <i class="fa-solid fa-circle-info text-blue-300"></i>
                    </div>
                    <div class="text-2xl font-extrabold text-blue-600 mt-1">{info}</div>
                    <div class="text-xs text-slate-400 mt-1">Notícias gerais e eventos</div>
                </div>
            </div>

            <!-- TABS -->
            <div class="flex space-x-2 overflow-x-auto pb-2 scrollbar-hide">
                <button onclick="filterPriority('Todos', this)" data-priority="Todos" class="filter-tab bg-slate-800 text-white px-4 py-2 rounded-lg text-sm font-semibold transition-all shadow-sm border border-transparent whitespace-nowrap">
                    <i class="fa-solid fa-layer-group mr-2"></i>Todos <span class="ml-1.5 text-xs opacity-70" data-count="Todos"></span>
                </button>
                <button onclick="filterPriority('Ação Exigida', this)" data-priority="Ação Exigida" class="filter-tab bg-white text-slate-600 hover:bg-slate-100 px-4 py-2 rounded-lg text-sm font-medium transition-all border border-slate-200 whitespace-nowrap">
                    <i class="fa-solid fa-triangle-exclamation mr-2 text-red-500"></i>Normas / Riscos <span class="ml-1.5 text-xs text-slate-400" data-count="Ação Exigida"></span>
                </button>
                <button onclick="filterPriority('Alto Impacto', this)" data-priority="Alto Impacto" class="filter-tab bg-white text-slate-600 hover:bg-slate-100 px-4 py-2 rounded-lg text-sm font-medium transition-all border border-slate-200 whitespace-nowrap">
                    <i class="fa-solid fa-building-columns mr-2 text-amber-500"></i>Contexto Institucional <span class="ml-1.5 text-xs text-slate-400" data-count="Alto Impacto"></span>
                </button>
                <button onclick="filterPriority('Informativo', this)" data-priority="Informativo" class="filter-tab bg-white text-slate-600 hover:bg-slate-100 px-4 py-2 rounded-lg text-sm font-medium transition-all border border-slate-200 whitespace-nowrap">
                    <i class="fa-solid fa-circle-info mr-2 text-blue-500"></i>Apenas Informativos <span class="ml-1.5 text-xs text-slate-400" data-count="Informativo"></span>
                </button>
            </div>
        </div>

        <!-- GRID DE CARDS -->
        <div class="p-8 lg:p-12">
            <div id="newsGrid" class="grid grid-cols-1 xl:grid-cols-2 gap-6 pb-12 items-stretch"></div>
        </div>
    </main>

    <script>
        const allNews = {json_data};
        let currentFilter = 'Todos';

        function escapeHtml(s) {{
            return String(s ?? '').replace(/[&<>"']/g, c => ({{
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }})[c]);
        }}

        function updateTabCounts() {{
            const counts = {{ Todos: allNews.length, 'Ação Exigida': 0, 'Alto Impacto': 0, 'Informativo': 0 }};
            allNews.forEach(n => {{ if (counts[n.prioridade] !== undefined) counts[n.prioridade]++; }});
            document.querySelectorAll('[data-count]').forEach(el => {{
                const k = el.getAttribute('data-count');
                el.textContent = counts[k] != null ? `(${{counts[k]}})` : '';
            }});
        }}

        function renderSourceStats() {{
            const stats = {{}};
            allNews.forEach(n => {{ stats[n.orgao] = (stats[n.orgao] || 0) + 1; }});
            const max = Math.max(1, ...Object.values(stats));
            const container = document.getElementById('sourceStats');
            container.innerHTML = Object.entries(stats)
                .sort((a, b) => b[1] - a[1])
                .map(([fonte, n]) => {{
                    const pct = Math.round((n / max) * 100);
                    return `
                    <div>
                        <div class="flex justify-between mb-1">
                            <span class="text-slate-300 truncate">${{escapeHtml(fonte)}}</span>
                            <span class="text-slate-400 font-semibold">${{n}}</span>
                        </div>
                        <div class="w-full bg-slate-800 rounded h-1.5 overflow-hidden">
                            <div class="bg-emerald-500 h-full rounded" style="width: ${{pct}}%"></div>
                        </div>
                    </div>`;
                }}).join('') || '<div class="text-slate-500">Sem dados</div>';
        }}

        function renderCards(data) {{
            const grid = document.getElementById('newsGrid');
            document.getElementById('countDisplay').innerText = data.length;

            if (data.length === 0) {{
                grid.innerHTML = `
                <div class="col-span-full flex flex-col items-center justify-center py-24 text-slate-400 bg-white rounded-2xl border border-dashed border-slate-300">
                    <div class="bg-slate-50 p-6 rounded-full mb-4">
                        <i class="fa-solid fa-file-shield text-5xl text-slate-300"></i>
                    </div>
                    <h3 class="text-lg font-medium text-slate-600 mb-1">Nenhum documento encontrado</h3>
                    <p class="text-sm">Ajuste os filtros ou o termo de busca.</p>
                </div>`;
                return;
            }}

            grid.innerHTML = data.map(item => {{
                const cardClass = item.prioridade.split(' ')[0];
                const tagsHtml = (item.tags || []).map(tag => {{
                    let icon = 'fa-tag';
                    let colorClass = 'bg-slate-100 text-slate-600 border-slate-200';
                    if (tag === 'Jurisprudência') {{ icon = 'fa-scale-balanced'; colorClass = 'bg-indigo-50 text-indigo-600 border-indigo-100'; }}
                    if (tag === 'Regulamentação') {{ icon = 'fa-book-bookmark'; colorClass = 'bg-blue-50 text-blue-600 border-blue-100'; }}
                    if (tag === 'Fiscalização/Incidente') {{ icon = 'fa-shield-virus'; colorClass = 'bg-rose-50 text-rose-600 border-rose-100'; }}
                    if (tag === 'IA/Governança de Dados') {{ icon = 'fa-brain'; colorClass = 'bg-purple-50 text-purple-600 border-purple-100'; }}
                    if (tag === 'Contexto MEC/UFAM') {{ icon = 'fa-graduation-cap'; colorClass = 'bg-amber-50 text-amber-600 border-amber-100'; }}
                    return `<span class="tag border ${{colorClass}}"><i class="fa-solid ${{icon}} mr-1.5 opacity-70"></i>${{escapeHtml(tag)}}</span>`;
                }}).join('');

                let badgeHtml = '';
                if (item.prioridade === 'Ação Exigida')
                    badgeHtml = '<span class="bg-red-100 text-red-700 text-[10px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider border border-red-200 shadow-sm"><i class="fa-solid fa-triangle-exclamation mr-1"></i> Atenção DPO</span>';
                else if (item.prioridade === 'Alto Impacto')
                    badgeHtml = '<span class="bg-amber-100 text-amber-700 text-[10px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider border border-amber-200 shadow-sm"><i class="fa-solid fa-building-columns mr-1"></i> Impacto Institucional</span>';

                let dataDisplay = escapeHtml(item.data || '');
                if (dataDisplay.includes('*')) {{
                    dataDisplay = dataDisplay.replace('*', '<span class="data-asterisco" title="Data assumida pela coleta (não fornecida pelo feed)">*</span>');
                }}

                const resumoHtml = item.resumo
                    ? `<p class="text-sm text-slate-500 mt-3 mb-4 line-clamp-3 leading-relaxed">${{escapeHtml(item.resumo)}}</p>`
                    : '';

                return `
                <div class="card-${{cardClass}} fade-in bg-white rounded-xl shadow-sm hover:shadow-lg transition-shadow duration-300 flex flex-col h-full border border-slate-200/60 overflow-hidden group">
                    <div class="p-6 flex-1 flex flex-col">
                        <div class="flex justify-between items-start mb-4">
                            <span class="text-[10px] font-bold text-slate-500 uppercase tracking-widest bg-slate-100 px-2.5 py-1 rounded-md border border-slate-200">${{escapeHtml(item.orgao)}}</span>
                            <span class="text-xs text-slate-400 font-medium whitespace-nowrap ml-2"><i class="fa-regular fa-calendar mr-1.5"></i>${{dataDisplay}}</span>
                        </div>
                        <h3 class="text-lg font-bold text-slate-800 leading-snug mb-1">
                            <a href="${{escapeHtml(item.link)}}" target="_blank" rel="noopener" class="hover:text-emerald-600 transition-colors decoration-emerald-300 decoration-2 underline-offset-4 group-hover:underline">${{escapeHtml(item.titulo)}}</a>
                        </h3>
                        ${{resumoHtml}}
                    </div>
                    <div class="px-6 pb-6 mt-auto">
                        <div class="flex flex-wrap gap-2 mb-5">
                            ${{badgeHtml}}
                            ${{tagsHtml}}
                        </div>
                        <div class="pt-4 border-t border-slate-100 flex justify-end items-center">
                            <a href="${{escapeHtml(item.link)}}" target="_blank" rel="noopener" class="text-sm font-semibold text-emerald-600 hover:text-emerald-700 transition-colors flex items-center">
                                Ler documento original <i class="fa-solid fa-arrow-right ml-2 text-xs transition-transform group-hover:translate-x-1"></i>
                            </a>
                        </div>
                    </div>
                </div>`;
            }}).join('');
        }}

        function applyFilters() {{
            const searchTerm = document.getElementById('searchInput').value.toLowerCase().trim();
            const sourceTerm = document.getElementById('sourceFilter').value;
            const filtered = allNews.filter(n => {{
                const searchString = `${{n.titulo}} ${{n.orgao}} ${{n.resumo}} ${{(n.tags || []).join(' ')}}`.toLowerCase();
                const matchesSearch = !searchTerm || searchString.includes(searchTerm);
                const matchesSource = sourceTerm === 'Todas' || n.orgao === sourceTerm;
                const matchesPriority = currentFilter === 'Todos' || n.prioridade === currentFilter;
                return matchesSearch && matchesSource && matchesPriority;
            }});
            renderCards(filtered);
        }}

        function filterPriority(priority, element) {{
            currentFilter = priority;
            document.querySelectorAll('.filter-tab').forEach(tab => {{
                tab.classList.remove('bg-slate-800', 'text-white', 'shadow-sm', 'border-transparent');
                tab.classList.add('bg-white', 'text-slate-600', 'border-slate-200');
            }});
            if (element) {{
                element.classList.remove('bg-white', 'text-slate-600', 'border-slate-200');
                element.classList.add('bg-slate-800', 'text-white', 'shadow-sm', 'border-transparent');
            }}
            applyFilters();
        }}

        function exportJson() {{
            const blob = new Blob([JSON.stringify(allNews, null, 2)], {{ type: 'application/json' }});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `epdp_ufam_${{new Date().toISOString().slice(0,10)}}.json`;
            a.click();
            URL.revokeObjectURL(a.href);
        }}

        // Listeners
        document.getElementById('searchInput').addEventListener('input', applyFilters);
        document.getElementById('sourceFilter').addEventListener('change', applyFilters);
        document.getElementById('exportBtn').addEventListener('click', exportJson);

        // Atalhos: "/" foca busca, "Esc" limpa
        document.addEventListener('keydown', (e) => {{
            const input = document.getElementById('searchInput');
            if (e.key === '/' && document.activeElement !== input) {{
                e.preventDefault();
                input.focus();
            }} else if (e.key === 'Escape' && document.activeElement === input) {{
                input.value = '';
                applyFilters();
                input.blur();
            }}
        }});

        // Init
        updateTabCounts();
        renderSourceStats();
        renderCards(allNews);
    </script>
</body>
</html>
"""


# ============================================================================
# ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    try:
        monitorar()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido pelo usuário.[/yellow]")
        sys.exit(130)
