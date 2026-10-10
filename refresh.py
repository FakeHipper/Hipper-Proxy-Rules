"""核验公开配置与远程规则资源，仅在内容变化时生成发布记录。"""

import concurrent.futures
import hashlib
import ipaddress
import json
import re
import sys
import urllib.request
import urllib.parse
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
FILES = ('Hipper.conf', 'Hipper.yaml')
PUBLIC_URL = 'https://raw.githubusercontent.com/FakeHipper/Hipper-Proxy-Rules/main/Hipper.conf'
CLAUDE_DOMAINS = ('claude.ai', 'claude.com', 'anthropic.com', 'claude.app', 'claudeusercontent.com', 'claudemcpcontent.com')
SR_NODES = ('DMIT-LAX-CMIN2-VLESS', 'DMIT-LAX-CMIN2-HY2')


def check_credentials(value):
    forbidden = {'authentication', 'authorization', 'password', 'uuid', 'token', 'private-key', 'short-id', 'secret', 'username'}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower().replace('_', '-') in forbidden:
                raise ValueError('公开配置出现认证字段：' + str(key))
            check_credentials(child)
    elif isinstance(value, list):
        for child in value:
            check_credentials(child)
    elif isinstance(value, str):
        for url in re.findall(r'(?=(https?://[^\s]+))', value):
            parsed = urllib.parse.urlsplit(url)
            if parsed.username or parsed.password or parsed.query:
                raise ValueError('公开URL不得包含用户信息或查询参数')


def sections(text):
    result = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            if line in result:
                raise ValueError('配置段重复：' + line)
            section = line
            result[section] = []
        elif section:
            result[section].append(line)
    return result


def check_routing(sr, clash):
    parsed = sections(sr)
    if parsed.get('[Proxy]'):
        raise ValueError('公开Shadowrocket不得定义节点')
    rules = parsed['[Rule]']
    expected_sr = ['DOMAIN-SUFFIX,' + host + ',AI' for host in CLAUDE_DOMAINS]
    expected_clash = ['DOMAIN-SUFFIX,' + host + ',🤖 AI 服务' for host in CLAUDE_DOMAINS]
    if rules[:6] != expected_sr or clash['rules'][:6] != expected_clash:
        raise ValueError('Claude本地规则须完整且先于远程、广告和IP规则')
    general = dict(line.split(' = ', 1) for line in parsed['[General]'] if ' = ' in line)
    if general.get('dns-fallback-system') != 'false':
        raise ValueError('不得使用系统DNS回退')
    for key in ('dns-server', 'fallback-dns-server'):
        if not general.get(key) or any(
            not url.startswith('https://') or url.rsplit('#proxy=', 1)[-1] not in SR_NODES
            for url in general[key].split(',')
        ):
            raise ValueError('Shadowrocket DNS须明确绑定本地DMIT节点')
    ai = next(line for line in parsed['[Proxy Group]'] if line.startswith('AI ='))
    candidates = [value for value in ai.split('=', 1)[1].strip().split(',')[1:] if '=' not in value]
    if set(candidates) != set(SR_NODES) | {'REJECT'}:
        raise ValueError('Shadowrocket AI组须只引用DMIT节点和拒绝策略')
    if rules[-1] != 'FINAL,AI':
        raise ValueError('未分类流量须保留固定出口兜底')
    groups = {g['name']: g for g in clash['proxy-groups']}
    if clash['rules'][-1] != 'MATCH,🐟 漏网之鱼' or groups['🐟 漏网之鱼'].get('proxies') != ['DMIT固定出口']:
        raise ValueError('Clash未分类流量不得直连回退或更换出口')
    if any(g.get('use') and g.get('empty-fallback') != 'REJECT' for g in groups.values()):
        raise ValueError('空节点组须拒绝连接，不得使用COMPATIBLE直连')
    for host in CLAUDE_DOMAINS:
        resolvers = clash['dns']['nameserver-policy'].get('+.' + host, [])
        if not resolvers or any(not url.startswith('https://') or not url.endswith('#DMIT固定出口') for url in resolvers):
            raise ValueError('Claude DNS须绑定固定出口：' + host)


def check_public():
    sr = (ROOT / FILES[0]).read_text()
    clash = yaml.safe_load((ROOT / FILES[1]).read_text())
    check_credentials(sr)
    check_credentials(clash)
    check_routing(sr, clash)
    if '[MITM]' in sr or '[Script]' in sr:
        raise ValueError('公开配置不得增加解密或脚本段')
    if 'update-url = ' + PUBLIC_URL not in sr:
        raise ValueError('整份更新地址不属于本仓库')
    if clash.get('proxies') or clash.get('secret'):
        raise ValueError('公开Clash不得包含节点凭据或控制密钥')
    if clash.get('external-controller') != '127.0.0.1:9990':
        raise ValueError('控制接口须限制到本机')
    if any(p != {'type': 'inline', 'payload': []} for p in clash['proxy-providers'].values()):
        raise ValueError('公开节点提供器只能包含空占位，实际节点须保留本地覆写')
    groups = {g['name']: g for g in clash['proxy-groups']}
    fixed = {'name': 'DMIT固定出口', 'type': 'select', 'proxies': ['REJECT'], 'use': ['DMIT私有节点'], 'empty-fallback': 'REJECT'}
    if groups['DMIT固定出口'] != fixed:
        raise ValueError('固定出口不得增加直连、机场或动态引入节点')
    for name in ('🤖 AI 服务', '📱 TikTok', '🐟 漏网之鱼'):
        if groups[name] != {'name': name, 'type': 'select', 'proxies': ['DMIT固定出口']}:
            raise ValueError('固定出口策略被改变：' + name)
    for line in sr.splitlines():
        if line.startswith(('AI =', 'TikTok =')) and any(x in line for x in [',DIRECT,', ',PROXY,', ',美国节点,']):
            raise ValueError('固定出口组不得自动使用其他出口')
    for name in FILES:
        text = (ROOT / name).read_text()
        if re.search(r'(?im)^\s*(password|uuid|private-key|short-id|authorization|token)\s*[:=]', text):
            raise ValueError('公开文件出现认证字段：' + name)
        if re.search(r'[?&]token=', text, re.I):
            raise ValueError('公开文件出现订阅令牌')
        if re.search(r'BEGIN .*PRIVATE KEY', text):
            raise ValueError('公开文件出现私钥')
        for address in re.findall(r'IP-CIDR,([^,\s]+),DIRECT', text):
            network = ipaddress.ip_network(address, strict=False)
            if network.is_global and network.prefixlen == network.max_prefixlen:
                raise ValueError('公开文件不得暴露单个公网端点直连规则')
    urls = {line.split(',')[1] for line in sr.splitlines() if line.startswith(('RULE-SET,https:', 'DOMAIN-SET,https:'))}
    providers = {k: v for k, v in clash['rule-providers'].items() if v.get('type') == 'http'}
    urls.update(p['url'] for p in providers.values())
    if any(not u.startswith('https://') or '?' in u for u in urls):
        raise ValueError('远程规则地址须为无令牌HTTPS地址')
    return urls, providers


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'clash.meta'})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('规则资源超过允许大小')
    text = data.decode('utf-8-sig')
    is_yaml = re.search(r'^payload\s*:', text, re.M) is not None
    if is_yaml:
        parsed = yaml.safe_load(text)
        if set(parsed) != {'payload'} or not isinstance(parsed['payload'], list):
            raise ValueError('规则YAML结构不符合要求')
        entries = parsed['payload']
    else:
        entries = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(('#', '//'))]
    if not entries or any(not isinstance(x, str) for x in entries):
        raise ValueError('规则内容为空或非文本')
    if any(re.match(r'^(FINAL|MATCH|SCRIPT|INCLUDE)\s*,', x, re.I) for x in entries):
        raise ValueError('远程资源不得改变兜底或加入脚本')
    return url, {'sha256': hashlib.sha256(data).hexdigest(), 'format': 'yaml' if is_yaml else 'text', 'entries': len(entries)}


def main():
    urls, providers = check_public()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        resources = dict(pool.map(fetch, sorted(urls)))
    for name, provider in providers.items():
        if provider.get('format', 'yaml') != resources[provider['url']]['format']:
            raise ValueError('提供器格式不一致：' + name)
    digest = hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()
    for name in FILES:
        path = ROOT / name
        body = re.sub(r'^# 上游规则核验摘要: .*\n', '', path.read_text())
        new = '# 上游规则核验摘要: ' + digest + '\n' + body
        if path.read_text() != new:
            path.write_text(new)
    report = {'resource_digest': digest, 'resources': resources, 'scope': '资源内容和公开配置检查；不代表设备或服务器验收'}
    (ROOT / 'rule-health.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('公开配置检查通过；远程资源数：' + str(len(resources)))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('发布终止：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
