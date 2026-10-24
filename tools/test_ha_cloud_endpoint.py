import unittest

from ha_cloud_endpoint import owned_connections


class OwnedCloudConnectionTest(unittest.TestCase):
    def row(self, remote='010200C0:1633', state='01', inode='1234'):
        return f' 0: 0100007F:1234 {remote} {state} 00000000:00000000 00:00000000 00000000 0 0 {inode} 1'

    def test_established_owned_resolved_peer_is_required(self):
        raw = 'socket:[1234]\n' + self.row()
        self.assertEqual(owned_connections(raw, {'192.0.2.1'}), [{'peer': '192.0.2.1', 'port': 5683, 'inode': '1234'}])
        self.assertEqual(owned_connections(raw, {'198.51.100.1'}), [])
        self.assertEqual(owned_connections(self.row(), {'192.0.2.1'}), [])
        self.assertEqual(owned_connections('/config/socket:[1234]\n' + self.row(), {'192.0.2.1'}), [])

    def test_listen_closed_and_other_process_connections_are_excluded(self):
        for raw in (self.row(state='0A'), self.row(state='06'), self.row(inode='9999')):
            self.assertEqual(owned_connections('socket:[1234]\n' + raw, {'192.0.2.1'}), [])

    def test_linux_ipv6_word_order_is_parsed(self):
        row = self.row(remote='B80D0120000000000000000001000000:1633')
        self.assertEqual(owned_connections('socket:[1234]\n' + row, {'2001:db8::1'})[0]['peer'], '2001:db8::1')
