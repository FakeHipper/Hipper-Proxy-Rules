"""验证公开框架拒绝凭据、直连回退及固定出口策略漂移。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('refresh', ROOT / 'refresh.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class PublicConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.original_root = refresh.ROOT
        refresh.ROOT = self.root
        self.sr = (ROOT / 'Hipper.conf').read_text()
        self.clash = yaml.safe_load((ROOT / 'Hipper.yaml').read_text())
        self.write()

    def tearDown(self):
        refresh.ROOT = self.original_root
        self.directory.cleanup()

    def write(self):
        (self.root / 'Hipper.conf').write_text(self.sr)
        (self.root / 'Hipper.yaml').write_text(yaml.safe_dump(self.clash, allow_unicode=True, sort_keys=False))

    def rejected(self, reason=None):
        self.write()
        with self.assertRaisesRegex(ValueError, reason or '.'):
            refresh.check_public()

    def test_reviewed_public_configuration_is_accepted(self):
        urls, providers = refresh.check_public()
        self.assertGreater(len(urls), 60)
        self.assertGreater(len(providers), 25)

    def test_missing_claude_content_rule_is_rejected(self):
        for host in ('claude.app', 'claudeusercontent.com', 'claudemcpcontent.com'):
            with self.subTest(host=host):
                source = self.sr
                self.sr = '\n'.join(line for line in source.splitlines() if not line.startswith('DOMAIN-SUFFIX,' + host + ',')) + '\n'
                self.rejected()
                self.sr = source

    def test_direct_claude_rule_is_rejected(self):
        self.sr = self.sr.replace('DOMAIN-SUFFIX,claude.ai,AI', 'DOMAIN-SUFFIX,claude.ai,DIRECT')
        self.rejected()

    def test_advertising_cannot_precede_local_claude_rules(self):
        self.clash['rules'].insert(0, 'DOMAIN-SUFFIX,claude.ai,REJECT')
        self.rejected()

    def test_unbound_claude_dns_is_rejected(self):
        self.clash['dns']['nameserver-policy']['+.claude.ai'] = ['https://1.1.1.1/dns-query']
        self.rejected()

    def test_system_dns_fallback_is_rejected(self):
        self.sr = self.sr.replace('dns-fallback-system = false', 'dns-fallback-system = true')
        self.rejected('系统DNS')

    def test_unclassified_clash_traffic_cannot_fall_back_to_direct(self):
        self.clash['rules'][-1] = 'MATCH,DIRECT'
        self.rejected()

    def test_empty_airport_group_cannot_become_compatible(self):
        group = next(g for g in self.clash['proxy-groups'] if g.get('use') == ['PKCloud节点'])
        group['empty-fallback'] = 'COMPATIBLE'
        self.rejected()

    def test_inline_private_node_is_rejected(self):
        self.clash['proxy-providers']['DMIT私有节点'] = {
            'type': 'inline',
            'payload': [{'name': '测试节点', 'type': 'socks5', 'server': 'example.net', 'port': 1080}],
        }
        self.rejected()

    def test_nested_subscription_token_is_rejected(self):
        self.clash['proxy-providers']['PKCloud节点'] = {
            'type': 'http', 'url': 'https://example.net/subscription/private-value',
        }
        self.rejected()

    def test_missing_clash_content_rule_is_rejected(self):
        self.clash['rules'] = [r for r in self.clash['rules'] if not r.startswith('DOMAIN-SUFFIX,claudeusercontent.com,')]
        self.rejected()

    def test_unclassified_shadowrocket_traffic_cannot_fall_back_to_default(self):
        self.sr = self.sr.replace('FINAL,AI', 'FINAL,PROXY')
        self.rejected()

    def test_ambiguous_shadowrocket_dns_policy_is_rejected(self):
        self.sr = self.sr.replace('#proxy=DMIT-LAX-CMIN2-VLESS', '#proxy=AI')
        self.rejected()

    def test_fixed_outlet_cannot_add_direct(self):
        group = next(g for g in self.clash['proxy-groups'] if g['name'] == 'DMIT固定出口')
        group['proxies'].append('DIRECT')
        self.rejected()

    def test_ai_group_cannot_import_airport(self):
        group = next(g for g in self.clash['proxy-groups'] if g['name'] == '🤖 AI 服务')
        group['use'] = ['PKCloud节点']
        group['empty-fallback'] = 'REJECT'
        self.rejected()

    def test_fixed_outlet_cannot_import_all_providers(self):
        group = next(g for g in self.clash['proxy-groups'] if g['name'] == 'DMIT固定出口')
        group['include-all-providers'] = True
        self.rejected()

    def test_shadowrocket_inline_uuid_is_rejected(self):
        self.sr = self.sr.replace('[Proxy]\n', '[Proxy]\n测试节点 = vless,example.net,443,uuid=00000000-0000-0000-0000-000000000000\n')
        self.rejected()

    def test_quoted_authentication_is_rejected(self):
        self.clash['authentication'] = ['test:example-only']
        self.rejected()

    def test_rule_url_userinfo_is_rejected(self):
        self.clash['rule-providers']['Claude']['url'] = 'https://test:example-only@raw.githubusercontent.com/example/rules.txt'
        self.rejected()

    def test_rule_provider_authorization_header_is_rejected(self):
        self.clash['rule-providers']['Claude']['header'] = {'Authorization': ['Bearer example-only']}
        self.rejected()

    def test_rule_url_with_comma_in_userinfo_is_rejected(self):
        self.clash['rule-providers']['Claude']['url'] = 'https://demo,part:example-only@example.invalid/rules.yaml'
        self.rejected()


if __name__ == '__main__':
    unittest.main()
