"""Testa URLs dos feeds."""
import requests
import urllib3
urllib3.disable_warnings()

urls = [
    ('ANPD', 'https://www.gov.br/anpd/pt-br/rss'),
    ('CGU', 'https://www.gov.br/cgu/pt-br/rss'),
    ('MGI', 'https://www.gov.br/gestao/pt-br/rss'),
    ('TCU', 'https://portal.tcu.gov.br/rss/noticias/rss.xml'),
    ('LexML', 'https://www.lexml.gov.br/busca/srss?q=LGPD'),
    ('Gov Educacao', 'https://www.gov.br/pt-br/rss/categorias/educacao-e-pesquisa'),
]

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

for name, url in urls:
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        print(f'{name}: HTTP {r.status_code} - {len(r.content)} bytes')
        if r.status_code == 200:
            ct = r.headers.get('Content-Type', 'unknown')
            print(f'  Content-Type: {ct}')
            preview = r.text[:150].replace('\n', ' ')
            print(f'  Preview: {preview}...')
    except Exception as e:
        print(f'{name}: ERROR - {e}')
    print()
