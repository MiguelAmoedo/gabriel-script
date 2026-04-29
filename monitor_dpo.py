import feedparser
import json
import os
import sys
import webbrowser
import urllib.parse
from datetime import datetime, timedelta
from time import mktime
from rich.console import Console
from rich.panel import Panel
from rich.progress import track
import requests
from bs4 import BeautifulSoup
import urllib3

# Desativa avisos chatos de SSL no console (necessário para sites do governo)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuração de Caminhos ---
if getattr(sys, 'frozen', False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

console = Console()

# --- O Motor Definitivo: LexML (Diário Oficial, STF, STJ, Congresso) ---
# Operadores em minúsculo costumam funcionar melhor no backend do LexML
query_lexml = '("proteção de dados" ou "LGPD" ou "ANPD" ou "inteligência artificial" ou "governança de dados") e ("portaria" ou "resolução" ou "acórdão" ou "MEC" ou "Universidade" ou "MGI")'
# Usando quote_plus para transformar espaços em '+' em vez de '%20'
encoded_lexml = urllib.parse.quote_plus(query_lexml)

FEEDS = {
    "ANPD (Oficial)": "https://www.gov.br/anpd/pt-br/assuntos/noticias/RSS",
    "CGU (Transparência)": "https://www.gov.br/cgu/pt-br/assuntos/noticias/RSS",
    "MGI (Governo Digital)": "https://www.gov.br/gestao/pt-br/assuntos/noticias/RSS",
    "TCU (Contas e Gov)": "https://portal.tcu.gov.br/lumis/portal/feed/rss.jsp?idServiceInstance=8A8182604C749B0E014C794A6E823616",
    "Diário Oficial / LexML": f"https://www.lexml.gov.br/busca/srss?q={encoded_lexml}"
}

# --- Inteligência de Classificação Regulatória ---
TERMOS_REGULAMENTACAO = ["portaria", "resolução", "decreto", "normativa", "guia", "diretriz", "projeto de lei"]
TERMOS_FISCALIZACAO = ["multa", "sanção", "vazamento", "incidente", "auditoria", "condenação", "fiscalização"]
TERMOS_JURISPRUDENCIA = ["acórdão", "stf", "stj", "mpf", "tcu", "cgu", "tribunal", "decisão"]
TERMOS_UFAM = ["ufam", "mec", "universidade federal", "ifes", "andifes", "educação"]
TERMOS_IA_DADOS = ["inteligência artificial", "governança de dados", "algoritmo", "ia"] 

DB_FILE = os.path.join(base_path, "dpo_base_dados.json")
HTML_REPORT = os.path.join(base_path, "Dashboard_EPDP_UFAM.html")

def carregar_banco():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: return []
    return []

def monitorar():
    banco_existente = carregar_banco()
    links_vistos = {item['link'] for item in banco_existente}
    novos_achados = []
    
    # Aumentado para 90 dias para garantir captura de dados (pode voltar para 30 depois)
    DIAS_RETROSPECTIVA = 90
    limite_tempo = datetime.now() - timedelta(days=DIAS_RETROSPECTIVA)
    limite_timestamp = limite_tempo.timestamp()

    console.print(Panel(f"[bold green]EPDP UFAM - Scanner de Diários Oficiais[/bold green]\n[white]Acessando LexML (DOU) e bases (Últimos {DIAS_RETROSPECTIVA} dias)...", border_style="green"))

    # Headers mais robustos para evitar bloqueio (403 Forbidden)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7'
    }

    for orgao, url in track(FEEDS.items(), description="Processando fontes..."):
        try:
            # verify=False contorna problemas de certificado SSL do governo
            # timeout=30 dá mais tempo para o LexML processar a busca complexa
            resposta = requests.get(url, headers=headers, timeout=30, verify=False)
            resposta.raise_for_status() # Força cair no 'except' se der erro 404, 403, 500
            
            feed = feedparser.parse(resposta.content)
            
            # Se o feedparser encontrar um erro no XML, ele registra em feed.bozo_exception
            if feed.bozo and hasattr(feed, 'bozo_exception'):
                console.print(f"[yellow]Aviso no parse da fonte {orgao}: {feed.bozo_exception}[/yellow]")

            for entry in feed.entries:
                if entry.link in links_vistos: continue

                dt_raw = entry.get('published_parsed') or entry.get('updated_parsed')
                dt_pub = datetime.fromtimestamp(mktime(dt_raw)) if dt_raw else datetime.now()
                
                if dt_pub.timestamp() < limite_timestamp: 
                    continue

                titulo = entry.get('title', 'Sem Título')
                resumo = entry.get('summary', '')
                
                if resumo:
                    resumo = BeautifulSoup(resumo, "html.parser").get_text()

                texto_total = f"{titulo} {resumo}".lower()
                
                if "plataforma integrada de ouvidoria" in titulo.lower() and "notícia" not in texto_total:
                    continue

                prioridade = "Informativo"
                tags = []
                
                if any(k in texto_total for k in TERMOS_FISCALIZACAO):
                    prioridade = "Ação Exigida"
                    tags.append("Fiscalização/Incidente")
                    
                if any(k in texto_total for k in TERMOS_REGULAMENTACAO):
                    prioridade = "Ação Exigida" if prioridade == "Informativo" else prioridade
                    tags.append("Regulamentação")
                    
                if any(k in texto_total for k in TERMOS_JURISPRUDENCIA):
                    tags.append("Jurisprudência")
                
                if any(k in texto_total for k in TERMOS_IA_DADOS):
                    prioridade = "Ação Exigida" if prioridade == "Informativo" else prioridade
                    tags.append("IA/Governança de Dados")
                
                if any(k in texto_total for k in TERMOS_UFAM):
                    prioridade = "Alto Impacto" if prioridade != "Ação Exigida" else "Ação Exigida"
                    tags.append("Contexto MEC/UFAM")

                novos_achados.append({
                    "orgao": orgao,
                    "titulo": titulo,
                    "link": entry.link,
                    "data": dt_pub.strftime('%d/%m/%Y') if dt_raw else f"{dt_pub.strftime('%d/%m/%Y')}*",
                    "timestamp": dt_pub.timestamp(),
                    "prioridade": prioridade,
                    "tags": tags,
                    "resumo": resumo[:300] + "..." if len(resumo) > 300 else resumo
                })
        except requests.exceptions.RequestException as e:
            console.print(f"[red]Erro de Conexão na fonte {orgao}: {e}[/red]")
        except Exception as e:
            console.print(f"[red]Erro inesperado na fonte {orgao}: {e}[/red]")

    todas_noticias = banco_existente + novos_achados
    noticias_validas = [n for n in todas_noticias if n['timestamp'] >= limite_timestamp]
    noticias_validas.sort(key=lambda x: x['timestamp'], reverse=True)
    
    salvar_e_abrir(noticias_validas, DIAS_RETROSPECTIVA)

def gerar_dashboard(achados, dias):
    json_data = json.dumps(achados)
    
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Inteligência Regulatória - EPDP UFAM</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            body {{ background-color: #f8fafc; font-family: 'Inter', sans-serif; }}
            ::-webkit-scrollbar {{ width: 6px; }}
            ::-webkit-scrollbar-track {{ background: transparent; }}
            ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 10px; }}
            ::-webkit-scrollbar-thumb:hover {{ background: #94a3b8; }}
            .card-Ação {{ border-left: 4px solid #ef4444; }}
            .card-Alto {{ border-left: 4px solid #f59e0b; }}
            .card-Informativo {{ border-left: 4px solid #3b82f6; }}
            .tag {{ font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; padding: 4px 10px; border-radius: 9999px; display: inline-flex; align-items: center; }}
            .data-asterisco {{ color: #ef4444; font-weight: bold; cursor: help; }}
        </style>
    </head>
    <body class="flex h-screen overflow-hidden text-slate-800">

        <aside class="w-72 bg-slate-900 text-white h-full flex flex-col shadow-2xl z-20 relative">
            <div class="p-6 border-b border-slate-800">
                <h1 class="text-xl font-bold text-emerald-400 flex items-center tracking-tight">
                    <i class="fa-solid fa-scale-balanced mr-3 text-2xl"></i> EPDP Radar
                </h1>
                <p class="text-slate-400 text-xs mt-2 font-medium tracking-wide uppercase">DPO Universitário - UFAM</p>
            </div>

            <div class="p-6 space-y-8 flex-1 overflow-y-auto">
                <div>
                    <h3 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Busca Rápida</h3>
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
            </div>
            
            <div class="p-6 border-t border-slate-800 bg-slate-950 text-xs text-slate-500 text-center rounded-br-lg">
                <p class="mb-1"><i class="fa-regular fa-clock mr-1"></i> Retrospectiva: {dias} dias</p>
                <p>Atualizado: <span class="font-semibold text-slate-400">{datetime.now().strftime('%d/%m/%Y %H:%M')}</span></p>
            </div>
        </aside>

        <main class="flex-1 h-full overflow-y-auto bg-slate-50 relative flex flex-col">
            
            <div class="sticky top-0 z-10 bg-slate-50/95 backdrop-blur-sm border-b border-slate-200 px-8 lg:px-12 pt-8 pb-4">
                <div class="flex flex-col md:flex-row md:justify-between md:items-end gap-4 mb-6">
                    <div>
                        <h2 class="text-3xl font-bold text-slate-900 tracking-tight">Atos e Jurisprudência</h2>
                        <p class="text-sm text-slate-500 mt-1">Monitoramento de publicações relevantes para Governo e Proteção de Dados.</p>
                    </div>
                    <div class="inline-flex items-center text-sm font-semibold text-slate-600 bg-white px-5 py-2.5 rounded-full shadow-sm border border-slate-200">
                        <span id="countDisplay" class="text-emerald-600 font-bold text-lg mr-2">0</span> documentos
                    </div>
                </div>

                <div class="flex space-x-2 overflow-x-auto pb-2 scrollbar-hide">
                    <button onclick="filterPriority('Todos', this)" class="filter-tab bg-slate-800 text-white px-4 py-2 rounded-lg text-sm font-semibold transition-all shadow-sm border border-transparent whitespace-nowrap">
                        <i class="fa-solid fa-layer-group mr-2"></i>Todos os Registros
                    </button>
                    <button onclick="filterPriority('Ação Exigida', this)" class="filter-tab bg-white text-slate-600 hover:bg-slate-100 px-4 py-2 rounded-lg text-sm font-medium transition-all border border-slate-200 whitespace-nowrap">
                        <i class="fa-solid fa-triangle-exclamation mr-2 text-red-500"></i>Normas / Riscos
                    </button>
                    <button onclick="filterPriority('Alto Impacto', this)" class="filter-tab bg-white text-slate-600 hover:bg-slate-100 px-4 py-2 rounded-lg text-sm font-medium transition-all border border-slate-200 whitespace-nowrap">
                        <i class="fa-solid fa-building-columns mr-2 text-amber-500"></i>Contexto Institucional
                    </button>
                    <button onclick="filterPriority('Informativo', this)" class="filter-tab bg-white text-slate-600 hover:bg-slate-100 px-4 py-2 rounded-lg text-sm font-medium transition-all border border-slate-200 whitespace-nowrap">
                        <i class="fa-solid fa-circle-info mr-2 text-blue-500"></i>Apenas Informativos
                    </button>
                </div>
            </div>

            <div class="p-8 lg:p-12">
                <div id="newsGrid" class="grid grid-cols-1 xl:grid-cols-2 gap-6 pb-12 items-stretch">
                </div>
            </div>
        </main>

        <script>
            const allNews = {json_data};
            let currentFilter = 'Todos';

            function renderCards(data) {{
                const grid = document.getElementById('newsGrid');
                document.getElementById('countDisplay').innerText = data.length;
                
                if(data.length === 0) {{
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
                    const tagsHtml = item.tags.map(tag => {{
                        let icon = 'fa-tag';
                        let colorClass = 'bg-slate-100 text-slate-600 border-slate-200';
                        
                        if(tag === 'Jurisprudência') {{ icon = 'fa-scale-balanced'; colorClass = 'bg-indigo-50 text-indigo-600 border-indigo-100'; }}
                        if(tag === 'Regulamentação') {{ icon = 'fa-book-bookmark'; colorClass = 'bg-blue-50 text-blue-600 border-blue-100'; }}
                        if(tag === 'Fiscalização/Incidente') {{ icon = 'fa-shield-virus'; colorClass = 'bg-rose-50 text-rose-600 border-rose-100'; }}
                        if(tag === 'IA/Governança de Dados') {{ icon = 'fa-brain'; colorClass = 'bg-purple-50 text-purple-600 border-purple-100'; }}
                        
                        return `<span class="tag border ${{colorClass}}"><i class="fa-solid ${{icon}} mr-1.5 opacity-70"></i>${{tag}}</span>`;
                    }}).join('');
                    
                    let badgeHtml = '';
                    if(item.prioridade === 'Ação Exigida') badgeHtml = '<span class="bg-red-100 text-red-700 text-[10px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider border border-red-200 shadow-sm"><i class="fa-solid fa-triangle-exclamation mr-1"></i> Atenção DPO</span>';
                    else if(item.prioridade === 'Alto Impacto') badgeHtml = '<span class="bg-amber-100 text-amber-700 text-[10px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider border border-amber-200 shadow-sm"><i class="fa-solid fa-building-columns mr-1"></i> Impacto Institucional</span>';

                    let dataDisplay = item.data;
                    if (dataDisplay.includes('*')) {{
                        dataDisplay = dataDisplay.replace('*', '<span class="data-asterisco" title="Data assumida pela coleta">*</span>');
                    }}

                    let resumoHtml = item.resumo ? `<p class="text-sm text-slate-500 mt-3 mb-4 line-clamp-3 leading-relaxed">${{item.resumo}}</p>` : '';

                    return `
                    <div class="card-${{cardClass}} bg-white rounded-xl shadow-sm hover:shadow-lg transition-shadow duration-300 flex flex-col h-full border border-slate-200/60 overflow-hidden group">
                        <div class="p-6 flex-1 flex flex-col">
                            <div class="flex justify-between items-start mb-4">
                                <span class="text-[10px] font-bold text-slate-500 uppercase tracking-widest bg-slate-100 px-2.5 py-1 rounded-md border border-slate-200">${{item.orgao}}</span>
                                <span class="text-xs text-slate-400 font-medium whitespace-nowrap ml-2"><i class="fa-regular fa-calendar mr-1.5"></i>${{dataDisplay}}</span>
                            </div>
                            <h3 class="text-lg font-bold text-slate-800 leading-snug mb-1">
                                <a href="${{item.link}}" target="_blank" class="hover:text-emerald-600 transition-colors decoration-emerald-300 decoration-2 underline-offset-4 group-hover:underline">${{item.titulo}}</a>
                            </h3>
                            ${{resumoHtml}}
                        </div>
                        <div class="px-6 pb-6 mt-auto">
                            <div class="flex flex-wrap gap-2 mb-5">
                                ${{badgeHtml}}
                                ${{tagsHtml}}
                            </div>
                            <div class="pt-4 border-t border-slate-100 flex justify-end items-center">
                                <a href="${{item.link}}" target="_blank" class="text-sm font-semibold text-emerald-600 hover:text-emerald-700 transition-colors flex items-center">
                                    Ler documento original <i class="fa-solid fa-arrow-right ml-2 text-xs transition-transform group-hover:translate-x-1"></i>
                                </a>
                            </div>
                        </div>
                    </div>`;
                }}).join('');
            }}

            function applyFilters() {{
                const searchTerm = document.getElementById('searchInput').value.toLowerCase();
                const sourceTerm = document.getElementById('sourceFilter').value;
                
                const filtered = allNews.filter(n => {{
                    const searchString = `${{n.titulo}} ${{n.orgao}} ${{n.resumo}} ${{n.tags.join(' ')}}`.toLowerCase();
                    const matchesSearch = searchString.includes(searchTerm);
                    const matchesSource = sourceTerm === 'Todas' || n.orgao === sourceTerm;
                    const matchesPriority = currentFilter === 'Todos' || n.prioridade === currentFilter;
                    
                    return matchesSearch && matchesSource && matchesPriority;
                }});
                
                renderCards(filtered);
            }}

            function filterPriority(priority, element) {{
                currentFilter = priority;
                const tabs = document.querySelectorAll('.filter-tab');
                tabs.forEach(tab => {{
                    tab.classList.remove('bg-slate-800', 'text-white', 'shadow-sm', 'border-transparent');
                    tab.classList.add('bg-white', 'text-slate-600', 'border-slate-200');
                }});
                if(element) {{
                    element.classList.remove('bg-white', 'text-slate-600', 'border-slate-200');
                    element.classList.add('bg-slate-800', 'text-white', 'shadow-sm', 'border-transparent');
                }}
                applyFilters();
            }}

            document.getElementById('searchInput').addEventListener('input', applyFilters);
            document.getElementById('sourceFilter').addEventListener('change', applyFilters);

            renderCards(allNews);
        </script>
    </body>
    </html>
    """
    with open(HTML_REPORT, "w", encoding="utf-8") as f: f.write(html)

def salvar_e_abrir(achados_completos, dias):
    if achados_completos:
        gerar_dashboard(achados_completos, dias)
        with open(DB_FILE, 'w', encoding='utf-8') as f: json.dump(achados_completos, f)
        console.print(f"\n[bold green]✔[/bold green] Concluído! {len(achados_completos)} documentos normativos carregados.")
        webbrowser.open(f"file://{os.path.realpath(HTML_REPORT)}")
    else:
        # Mensagem atualizada para o caso de continuar zerado
        console.print(f"\n[yellow]Ainda não há novas portarias ou acórdãos nos últimos {dias} dias.[/yellow]")
        console.print("[white]Dica: Tente rodar amanhã ou alterar a lista de palavras-chave.[/white]")

if __name__ == "__main__":
    monitorar()