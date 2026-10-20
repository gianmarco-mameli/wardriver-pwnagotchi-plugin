import ast
import logging
from pathlib import Path
import queue
import socket
from threading import Event, Lock, Thread
import time
import types
import unittest
from unittest.mock import Mock, patch


# Load GPS clients without requiring Pwnagotchi on the test host.
source = Path(__file__).resolve().parents[1] / 'wardriver.py'
tree = ast.parse(source.read_text())
clients = ast.Module(
    body=[node for node in tree.body
          if isinstance(node, ast.ClassDef)
          and node.name in ['Gps2IpClient', 'SerialGpsClient']],
    type_ignores=[],
)


class FakeSocket:
    def __init__(self):
        self.data = queue.Queue()
        self.closed = False
        self.connect = Mock()
        self.settimeout = Mock()

    def recv(self, size):
        try:
            return self.data.get(timeout=0.01)
        except queue.Empty:
            raise socket.timeout() from None

    def shutdown(self, how):
        self.data.put(b'')

    def close(self):
        self.closed = True


class Gps2IpTests(unittest.TestCase):
    def setUp(self):
        self.first = FakeSocket()
        self.second = FakeSocket()
        self.factory = Mock(side_effect=[self.first, self.second])
        network = types.SimpleNamespace(
            socket=self.factory, AF_INET=socket.AF_INET,
            SOCK_STREAM=socket.SOCK_STREAM, SHUT_RDWR=socket.SHUT_RDWR,
            timeout=socket.timeout,
        )
        self.namespace = dict(
            socket=network, Event=Event, Lock=Lock, Thread=Thread,
            time=time, logging=logging,
        )
        exec(compile(clients, str(source), 'exec'), self.namespace)  # noqa: S102
        self.client = self.namespace['Gps2IpClient']('127.0.0.1', 11123)
        self.client.RETRY_DELAY = 0.01
        self.addCleanup(self.client.disconnect)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail('Timed out waiting for GPS worker')

    def send_fix(self, connection):
        connection.data.put(
            b'$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M\n')
        self.wait_for(lambda: self.client.get_coordinates() is not None)

    def test_initial_failure_retries_in_background(self):
        self.first.connect.side_effect = ConnectionRefusedError('not ready')
        self.client.connect()
        self.wait_for(self.client.is_connected)
        self.assertTrue(self.first.closed)
        self.assertEqual(self.factory.call_count, 2)
        self.second.connect.assert_called_once_with(('127.0.0.1', 11123))
        self.second.settimeout.assert_any_call(self.client.CONNECT_TIMEOUT)
        self.send_fix(self.second)

    def test_retries_continue_beyond_five_failures(self):
        failed = [FakeSocket() for _ in range(6)]
        for connection in failed:
            connection.connect.side_effect = ConnectionRefusedError('not ready')
        self.factory.side_effect = failed + [self.second]
        self.client.connect()
        self.wait_for(self.client.is_connected)
        self.assertEqual(self.factory.call_count, 7)
        self.assertTrue(all(connection.closed for connection in failed))

    def test_eof_clears_fix_and_reconnects(self):
        self.client.connect()
        self.send_fix(self.first)
        self.first.data.put(b'')
        self.wait_for(
            lambda: self.factory.call_count == 2 and self.client.is_connected())
        self.assertIsNone(self.client.get_coordinates())
        self.second.data.put(b'$GPRMC,123519,A,4807.038,N,01131.000,E\n')
        self.wait_for(lambda: self.client.get_coordinates() is not None)
        self.assertIsNone(self.client.get_coordinates()['Altitude'])

    def test_partial_and_malformed_sentences_do_not_reconnect(self):
        self.client.connect()
        self.first.data.put(b'\xff\n$GPGGA,bad,,,,,1,08,,oops\n')
        self.first.data.put(b'$GPRMC,123519,A,4807.')
        self.first.data.put(b'038,N,01131.000,E\r\n')
        self.wait_for(lambda: self.client.get_coordinates() is not None)
        self.assertAlmostEqual(
            self.client.get_coordinates()['Latitude'], 48.1173)
        self.assertEqual(self.factory.call_count, 1)

    def test_no_fix_sentences_are_not_published(self):
        self.client.connect()
        self.first.data.put(b'$GPRMC,123519,V,4807.038,N,01131.000,E\n')
        self.first.data.put(
            b'$GPGGA,123519,4807.038,N,01131.000,E,0,00,,545.4,M\n')
        self.wait_for(self.first.data.empty)
        self.assertIsNone(self.client.get_coordinates())
        self.send_fix(self.first)

    def test_idle_socket_is_reopened(self):
        self.client.STALE_TIMEOUT = 0.03
        self.client.connect()
        self.wait_for(lambda: self.factory.call_count == 2)
        self.assertTrue(self.first.closed)

    def test_fix_expiry_returns_none(self):
        self.client.connect()
        self.send_fix(self.first)
        self.client.disconnect()
        # Feed a fix directly so the clock check is independent of the worker.
        self.client._Gps2IpClient__stop.clear()
        self.client._Gps2IpClient__process_sentence(
            b'$GPRMC,123519,A,4807.038,N,01131.000,E')
        updated = self.client._Gps2IpClient__last_update
        with patch.object(time, 'monotonic', return_value=updated + 60):
            self.assertIsNone(self.client.get_coordinates())
        self.assertIsNone(self.client.coordinates['Latitude'])

    def test_connect_is_idempotent_and_disconnect_stops_reader(self):
        self.client.connect()
        self.wait_for(self.client.is_connected)
        worker = self.client._Gps2IpClient__thread
        self.client.connect()
        self.assertIs(self.client._Gps2IpClient__thread, worker)
        self.client.disconnect()
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.client.is_connected())
        self.assertTrue(self.first.closed)
        self.client.connect()
        self.wait_for(self.client.is_connected)
        self.assertEqual(self.factory.call_count, 2)

    def test_disconnect_interrupts_retry_wait(self):
        self.client.RETRY_DELAY = 60
        self.first.connect.side_effect = ConnectionRefusedError('not ready')
        self.client.connect()
        self.wait_for(lambda: self.first.closed)
        worker = self.client._Gps2IpClient__thread
        self.client.disconnect()
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.factory.call_count, 1)

    def test_serial_parser_still_accepts_valid_fix(self):
        serial = self.namespace['SerialGpsClient']('/dev/ttyUSB0', 9600)
        parsed = serial._parse_nmea(
            '$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M')
        self.assertAlmostEqual(parsed['Latitude'], 48.1173)
        self.assertEqual(parsed['Altitude'], 545.4)

    def test_real_tcp_reconnect_and_shutdown(self):
        self.namespace['socket'] = socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(('127.0.0.1', 0))
            server.listen(2)
            server.settimeout(2)
            self.client.port = server.getsockname()[1]
            self.client.connect()
            first, _ = server.accept()
            with first:
                first.sendall(b'$GPRMC,123519,A,4807.')
                first.sendall(b'038,N,01131.000,E\r\n')
                self.wait_for(lambda: self.client.get_coordinates() is not None)
            second, _ = server.accept()
            with second:
                self.wait_for(lambda: self.client.get_coordinates() is None)
                second.sendall(b'$GPRMC,123520,A,4907.038,N,01131.000,E\n')
                self.wait_for(lambda: self.client.get_coordinates() is not None)
                self.assertAlmostEqual(
                    self.client.get_coordinates()['Latitude'], 49.1173)
                self.client.disconnect()
                self.assertFalse(self.client._Gps2IpClient__thread.is_alive())
                self.assertIsNone(self.client.get_coordinates())


if __name__ == '__main__':
    unittest.main()
