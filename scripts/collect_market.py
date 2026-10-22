#!/usr/bin/env python3
"""축산 수급현황(KAPE 축산유통정보 다봄) + 축산·식품·육가공 뉴스 수집기.

GitHub Actions가 주기적으로 실행해서 data/market.json, data/news.json 을 갱신합니다.
대시보드(index.html)는 브라우저에서 외부 사이트를 직접 읽을 수 없으므로(CORS) 이 JSON을 읽습니다.
표준 라이브러리만 사용합니다.

환경변수 (선택)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET : 있으면 네이버 뉴스 검색 API 사용(요약문 포함), 없으면 구글 뉴스 RSS 사용
"""
import json, os, re, ssl, sys, time, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'data')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

KAPE_DASH = 'https://www.ekapepia.com/v3/supplyStatus/supplyStatusDashboard.do?menuSn=14&boardInfoNo='
KAPE_MAIN = 'https://www.ekapepia.com/v3/web/main.do?userGroup=common'

# 뉴스 분류별 검색어 — 필요하면 여기만 고치면 됩니다
NEWS_QUERIES = {
    '축산': ['돼지고기 가격', '돼지 도매가격', '한우 가격', '축산물 수급', '아프리카돼지열병', '수입 돼지고기'],
    '육가공': ['육가공', '햄 소시지', '베이컨 육가공', '육가공품 수출', '대체육'],
    '식품업계': ['식품업계 가격 인상', '식품기업 실적', '가공식품 물가', '식품 원재료 가격'],
    '경쟁사': ['CJ제일제당 육가공', '롯데웰푸드 햄', '동원F&B 햄', '대상 청정원 육가공', '사조대림', '하림 육가공', '목우촌',
              '선진 육가공', '진주햄', '에쓰푸드', '도드람'],
}
# 제목에 아래 단어가 하나도 없으면 해당 분류에서 제외 (검색 결과의 엉뚱한 기사 거르기)
RELEVANT = {
    '축산': r'돼지|돈육|한돈|양돈|돈가|삼겹|목살|한우|소고기|쇠고기|육우|축산|가축|도축|도매가|ASF|돼지열병|구제역|럼피|조류인플루엔자|AI 발생|닭고기|육계|계란|산란계|수입육|원료육',
    '육가공': r'육가공|가공육|햄|소시지|소세지|베이컨|너겟|닭고기가공|대체육|배양육|식육|육류|미트|돈까스|돈가스',
    '식품업계': r'식품|식료품|먹거리|외식|가공품|라면|원재료|원가|물가|가격 ?인상|가격 ?인하|장바구니|유통업계|대형마트|편의점',
    '경쟁사': r'CJ|씨제이|제일제당|롯데웰푸드|롯데햄|동원|대상|청정원|사조|대림|하림|목우촌|선진|진주햄|에쓰푸드|도드람|팜스코|이지바이오|농협',
}


def fetch(url, headers=None, data=None, timeout=30):
    h = {'User-Agent': UA, 'Accept-Language': 'ko-KR,ko;q=0.9'}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                raw = r.read()
                cs = r.headers.get_content_charset() or 'utf-8'
                return raw.decode(cs, errors='replace')
        except Exception as e:  # 일시 오류는 재시도
            last = e
            time.sleep(2 + attempt * 3)
    raise last


# ---------------------------------------------------------------- 작은 DOM
class Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs or []), [], parent

    def cls(self):
        return (self.attrs.get('class') or '').split()

    def text(self):
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else (' ' if c.tag == 'br' else '') + c.text())
        return ''.join(out)

    def clean(self):
        return re.sub(r'\s+', ' ', html.unescape(self.text())).strip()

    def find_all(self, pred):
        res = []
        for c in self.children:
            if isinstance(c, Node):
                if pred(c):
                    res.append(c)
                res.extend(c.find_all(pred))
        return res

    def find(self, pred):
        r = self.find_all(pred)
        return r[0] if r else None

    def kids(self, tag=None):
        return [c for c in self.children if isinstance(c, Node) and (tag is None or c.tag == tag)]


VOID = {'br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'col', 'area', 'base', 'wbr'}


class DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def dom(text):
    b = DomBuilder()
    b.feed(text)
    return b.root


def has_cls(name):
    return lambda n: name in n.cls()


def num(s):
    s = (s or '').replace(',', '').replace('+', '').replace(' ', '').strip()
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


# ---------------------------------------------------------------- KAPE 수급현황판
def parse_change(td):
    """비교 칸: 값 / 화살표 % / 증감"""
    box = td.find(lambda n: n.tag == 'div' and any(c in ('data-up', 'data-down', 'data-same') for c in n.cls())) or td
    spans = box.kids('span')
    val = spans[0].clean() if spans else ''
    pct = next((s.clean() for s in spans if 'arrow' in s.cls()), '')
    diff = next((s.clean() for s in spans if 'sign' in s.cls()), '')
    c = box.cls()
    d = 'up' if 'data-up' in c else ('down' if 'data-down' in c else 'same')
    if d == 'same' and pct:
        d = 'same'
    return {'v': val, 'pct': pct, 'diff': diff.replace(' ', ''), 'dir': d}


def parse_box(box, species):
    h6 = box.find(lambda n: n.tag == 'h6')
    if not h6:
        return None
    tdiv = h6.find(lambda n: n.tag == 'div')
    title = re.sub(r'차트\s*확인', '', tdiv.clean() if tdiv else '').strip()
    btn = h6.find(lambda n: n.tag == 'button')
    m = re.search(r"openChart\('(\w+)'\)", btn.attrs.get('onclick', '')) if btn else None
    chart_key = m.group(1) if m else ''
    src_el = h6.find(lambda n: n.tag == 'i')
    source = re.sub(r'^출처\s*[:：]\s*', '', src_el.clean()) if src_el else ''
    table = box.find(lambda n: n.tag == 'table')
    if not table:
        return None
    rows = table.find_all(lambda n: n.tag == 'tr')
    if not rows:
        return None
    first, rest = rows[0], rows[1:]
    tds = first.kids('td')
    date_txt = tds[0].clean() if tds else ''
    mains = []
    for td in tds[1:]:
        b = td.find(lambda n: n.tag == 'b')
        em = td.find(lambda n: n.tag == 'em')
        unit = next((s.clean() for s in td.kids('span')), '')
        mains.append({'label': b.clean() if b else '', 'value': em.clean() if em else '', 'unit': unit,
                      'hl': 'red' if em and 'data-red' in em.cls() else ''})
    comps = []
    for tr in rest:
        tds = tr.kids('td')
        if not tds:
            continue
        lab_td = tds[0]
        unit_el = lab_td.find(lambda n: n.tag == 'i')
        unit = unit_el.clean().strip('()') if unit_el else ''
        full = lab_td.clean()
        label = full.split('(')[0].strip()
        if unit_el:
            label = full.replace(unit_el.clean(), '').strip()
        m = re.search(r'\(([^()]*\d[^()]*)\)\s*$', full)
        period = m.group(1) if m else ''
        label = re.sub(r'\s*\([^()]*\)\s*$', '', label).strip()
        comps.append({'label': label, 'unit': unit, 'period': period,
                      'cells': [parse_change(td) for td in tds[1:]]})
    return {'species': species, 'title': title, 'source': source, 'chart': chart_key,
            'date': re.sub(r'\s*기준\s*$', '', date_txt).strip(), 'mains': mains, 'comps': comps}


SPECIES = {'cow': '한우', 'pig': '돼지', 'chicken': '육계', 'egg': '계란'}


def chart_series(raw):
    """'차트 확인' 팝업의 추이 데이터: 팝업(id) 안 차트 div → 해당 차트의 JSON.parse('[...]') 스크립트"""
    layer_at = [(m.start(), m.group(1)) for m in re.finditer(r'class="dashboard-layer"\s+id="(\w+)"', raw)]
    owner = {}
    for m in re.finditer(r'id="(chart_[\w-]+)"', raw):
        prev = [lid for pos, lid in layer_at if pos < m.start()]
        if prev:
            owner[m.group(1)] = prev[-1]
    out = {}
    pat = re.compile(r"\$\('#(chart_[\w-]+)'\).*?JSON\.parse\('(\[.*?\])'\).*?render\w*\('chart_[\w-]+'\s*,\s*c\s*,\s*'(\w*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'", re.S)
    for m in pat.finditer(raw):
        cid, js, period, n1, n2 = m.groups()
        lid = owner.get(cid)
        if not lid:
            continue
        try:
            rows = json.loads(js)
        except Exception:
            continue
        pts = sorted(({'p': r.get('period'), 'v1': r.get('value1'), 'v2': r.get('value2')} for r in rows), key=lambda x: str(x['p']))
        out[lid] = {'period': period, 'names': [n1, n2] if n2 else [n1], 'points': pts}
    return out


def collect_kape_dashboard():
    raw = fetch(KAPE_DASH)
    root = dom(raw)
    series = chart_series(raw)
    out = []
    for key, species in SPECIES.items():
        wrap = root.find(lambda n: n.attrs.get('id') == key and 'main-dashboard-wrap' in n.cls())
        if not wrap:
            continue
        for b in wrap.find_all(has_cls('dashboard-data-box')):
            p = parse_box(b, species)
            if not p or not p['mains']:
                continue
            p['series'] = series.get(p['chart'])
            out.append(p)
    return out


def collect_kape_main():
    root = dom(fetch(KAPE_MAIN))
    res = {'signals': [], 'fx': [], 'retail': []}
    # 수급상황 신호등
    for tbl in root.find_all(lambda n: n.tag == 'table'):
        tds = tbl.find_all(lambda n: n.tag == 'td' and any(c.endswith('-light') for c in n.cls()))
        if not tds:
            continue
        head = [th.clean() for th in tbl.find_all(lambda n: n.tag == 'th')]
        for tr in tbl.find_all(lambda n: n.tag == 'tr'):
            cells = tr.kids('td')
            lights = [c for c in cells if any(x.endswith('-light') for x in c.cls())]
            if not lights:
                continue
            name = ''
            th = tr.find(lambda n: n.tag == 'th')
            lab = th or (cells[0] if cells and cells[0] not in lights else None)
            if lab:
                name = lab.clean()
                img = lab.find(lambda n: n.tag == 'img')
                if not name and img:
                    name = img.attrs.get('alt', '')
            items = []
            for i, c in enumerate(lights):
                color = next(x for x in c.cls() if x.endswith('-light')).replace('-light', '')
                sp = c.find(lambda n: n.tag == 'span')
                dv = c.find(lambda n: n.tag == 'div')
                items.append({'term': ['단기', '중기', '장기'][i] if i < 3 else '', 'year': sp.clean() if sp else '',
                              'status': dv.clean() if dv else '', 'color': color})
            res['signals'].append({'name': name, 'items': items})
        if res['signals']:
            break
    # 환율/유가, 식육포장 가격
    for card in root.find_all(has_cls('bottom-card')):
        for side in card.find_all(lambda n: any(c in ('left-card', 'right-card') for c in n.cls())):
            db = side.find(has_cls('data-box'))
            if not db:
                continue
            ttl = db.find(lambda n: n.tag in ('h5', 'h6'))
            title = re.sub(r'\+$', '', ttl.clean()).strip() if ttl else ''
            em = db.find(lambda n: n.tag == 'em')
            unit = em.find(lambda n: n.tag == 'i').clean() if em and em.find(lambda n: n.tag == 'i') else ''
            value = em.clean().replace(unit, '').strip() if em else ''
            pdiv = db.find(lambda n: n.tag == 'div' and n.find(lambda m: m.tag == 'p'))
            sub = pdiv.find(lambda n: n.tag == 'p').clean() if pdiv else ''
            li = db.find(lambda n: n.tag == 'li')
            chg = None
            if li:
                p = li.find(lambda n: n.tag == 'p')
                arrow = li.find(has_cls('arrow'))
                sign = li.find(has_cls('sign'))
                chg = {'label': p.clean() if p else '', 'pct': arrow.clean() if arrow else '',
                       'diff': sign.clean() if sign else '',
                       'dir': 'up' if 'data-up' in li.cls() else ('down' if 'data-down' in li.cls() else 'same')}
            df = side.find(has_cls('data-from'))
            dft = df.clean() if df else ''
            m = re.match(r'(.*?기준)\s*출처\s*[:：]\s*(.*)$', dft)
            item = {'title': title, 'sub': sub, 'value': value, 'unit': unit, 'change': chg,
                    'asof': m.group(1) if m else dft, 'source': m.group(2) if m else ''}
            (res['fx'] if '/' in unit and ('$' in unit or '배럴' in unit) else res['retail']).append(item)
    return res


# ---------------------------------------------------------------- 뉴스
FOREIGN = re.compile(r'vietnam\.vn|roughnecks|\.vn$|yahoo|msn|indexbox', re.I)


def clean_title(t, source):
    t = html.unescape(re.sub(r'<[^>]+>', '', t or '')).strip()
    if source and t.endswith(' - ' + source):
        t = t[: -len(source) - 3].strip()
    return t


def google_news(query):
    url = ('https://news.google.com/rss/search?q=' + urllib.parse.quote(query + ' when:7d')
           + '&hl=ko&gl=KR&ceid=KR:ko')
    root = ET.fromstring(fetch(url))
    out = []
    for it in root.findall('./channel/item'):
        src_el = it.find('source')
        source = (src_el.text or '').strip() if src_el is not None else ''
        try:
            dt = parsedate_to_datetime(it.findtext('pubDate')).astimezone(KST)
        except Exception:
            continue
        out.append({'title': clean_title(it.findtext('title'), source), 'link': it.findtext('link'),
                    'source': source, 'desc': '', 'ts': dt.isoformat(timespec='minutes')})
    return out


def naver_news(query, cid, secret):
    url = 'https://openapi.naver.com/v1/search/news.json?display=50&sort=date&query=' + urllib.parse.quote(query)
    data = json.loads(fetch(url, headers={'X-Naver-Client-Id': cid, 'X-Naver-Client-Secret': secret}))
    out = []
    for it in data.get('items', []):
        try:
            dt = parsedate_to_datetime(it['pubDate']).astimezone(KST)
        except Exception:
            continue
        link = it.get('originallink') or it.get('link')
        host = urllib.parse.urlparse(link).netloc.replace('www.', '')
        out.append({'title': clean_title(it.get('title'), ''), 'link': link, 'source': host,
                    'desc': clean_title(it.get('description'), '')[:220], 'ts': dt.isoformat(timespec='minutes')})
    return out


def norm_key(t):
    return re.sub(r'[^0-9A-Za-z가-힣]', '', t)[:40]


def collect_news():
    cid, sec = os.environ.get('NAVER_CLIENT_ID'), os.environ.get('NAVER_CLIENT_SECRET')
    provider = 'naver' if cid and sec else 'google'
    cutoff = datetime.now(KST) - timedelta(days=7)
    items, seen, errors = [], {}, []
    for cat, qs in NEWS_QUERIES.items():
        for q in qs:
            try:
                got = naver_news(q, cid, sec) if provider == 'naver' else google_news(q)
            except Exception as e:
                errors.append(f'{q}: {e}')
                continue
            for n in got:
                if not re.search(r'[가-힣]', n['title']) or FOREIGN.search(n['source'] or ''):
                    continue
                if datetime.fromisoformat(n['ts']) < cutoff:
                    continue
                if not re.search(RELEVANT[cat], n['title'] + ' ' + n.get('desc', ''), re.I):
                    continue
                k = norm_key(n['title'])
                if k in seen:
                    it = seen[k]
                    if cat not in it['cats']:
                        it['cats'].append(cat)
                    if q not in it['kw']:
                        it['kw'].append(q)
                    continue
                n.update({'cats': [cat], 'kw': [q]})
                seen[k] = n
                items.append(n)
            time.sleep(0.4)
    items.sort(key=lambda x: x['ts'], reverse=True)
    return {'provider': provider, 'items': items[:240], 'errors': errors}


# ---------------------------------------------------------------- 저장
def save(name, payload):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    old = None
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding='utf-8'))
        except Exception:
            old = None
    body = {k: v for k, v in payload.items() if k != 'updated'}
    if old and {k: v for k, v in old.items() if k != 'updated'} == body:
        print(name, '변경 없음')
        return
    payload['updated'] = datetime.now(KST).isoformat(timespec='minutes')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    print(name, '저장')


def main():
    ok = True
    try:
        boxes = collect_kape_dashboard()
        main_info = collect_kape_main()
        if not boxes:
            raise RuntimeError('수급현황판 표를 찾지 못했습니다')
        save('market.json', {'source': 'KAPE 축산유통정보 다봄 (ekapepia.com)', 'boxes': boxes, **main_info})
        print('KAPE', len(boxes), 'boxes /', len(main_info['signals']), 'signals /', len(main_info['fx']), 'fx /',
              len(main_info['retail']), 'retail')
    except Exception as e:
        ok = False
        print('KAPE 수집 실패:', e, file=sys.stderr)
    try:
        news = collect_news()
        if not news['items']:
            raise RuntimeError('기사 0건 · ' + '; '.join(news['errors'][:3]))
        save('news.json', news)
        print('NEWS', news['provider'], len(news['items']), 'items', len(news['errors']), 'errors')
    except Exception as e:
        ok = False
        print('뉴스 수집 실패:', e, file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
