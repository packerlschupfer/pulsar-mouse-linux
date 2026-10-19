"""Regression tests for Nordic-family hidraw response framing."""

import unittest
from unittest.mock import Mock, patch

import usb.core

from pulsar_mouse.drivers.nordic import (
    CMD_MEM_GET,
    PulsarNordic,
    REPORT_ID,
)
from pulsar_mouse.drivers.x2_areson_wired import PulsarX2AresonWired
from pulsar_mouse.drivers.x2_areson_wireless import PulsarX2AresonWireless
from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight


def make_report(report_id, command, payload=b''):
    report = bytearray(17)
    report[0] = report_id
    report[1] = command
    report[6:6 + len(payload)] = payload
    report[16] = PulsarNordic._checksum(*report[:16])
    return bytes(report)


class NordicHidrawReceiveTests(unittest.TestCase):
    def setUp(self):
        self.driver = PulsarX2AresonWireless()
        self.driver._fd = 17

    def read_reports(self, reports, driver=None, timeout_ms=10):
        device = driver or self.driver
        with (
            patch('pulsar_mouse.drivers.nordic.select.select',
                  return_value=([device._fd], [], [])),
            patch('pulsar_mouse.drivers.nordic.os.read',
                  side_effect=reports),
        ):
            return device._recv(timeout_ms=timeout_ms)

    def test_accepts_areson_input_report_09_and_processes_payload(self):
        response = make_report(0x09, CMD_MEM_GET, b'\x11\x22\x33')
        received = self.read_reports([response])
        self.assertEqual(received, response)
        self.assertEqual(received[6:9], b'\x11\x22\x33')

    def test_mem_read_matches_09_response_to_request(self):
        response = bytearray(make_report(0x09, CMD_MEM_GET, b'\x11\x22\x33'))
        response[3:6] = b'\x01\x02\x03'
        response[16] = PulsarNordic._checksum(*response[:16])
        response = bytes(response)
        with (
            patch('pulsar_mouse.drivers.nordic.os.write') as write,
            patch('pulsar_mouse.drivers.nordic.select.select',
                  return_value=([self.driver._fd], [], [])),
            patch('pulsar_mouse.drivers.nordic.os.read',
                  return_value=response),
        ):
            self.assertEqual(
                self.driver._mem_read_range(0x0102, 3),
                b'\x11\x22\x33',
            )
        sent = write.call_args.args[1]
        self.assertEqual(sent[0], REPORT_ID)
        self.assertEqual(sent[1], CMD_MEM_GET)

    def test_mem_read_skips_another_clients_reply_for_other_address(self):
        foreign = bytearray(make_report(0x09, CMD_MEM_GET, b'\xaa'))
        foreign[3:6] = b'\x00\x20\x0a'      # someone else's address
        foreign[16] = PulsarNordic._checksum(*foreign[:16])
        wanted = bytearray(make_report(0x09, CMD_MEM_GET, b'\x11\x22'))
        wanted[3:6] = b'\x01\x02\x02'
        wanted[16] = PulsarNordic._checksum(*wanted[:16])
        with (
            patch('pulsar_mouse.drivers.nordic.os.write'),
            patch('pulsar_mouse.drivers.nordic.select.select',
                  return_value=([self.driver._fd], [], [])),
            patch('pulsar_mouse.drivers.nordic.os.read',
                  side_effect=[bytes(foreign), bytes(wanted)]),
        ):
            self.assertEqual(self.driver._mem_read_range(0x0102, 2),
                             b'\x11\x22')

    def test_skips_bad_length_and_bad_checksum_reports(self):
        short = make_report(0x09, CMD_MEM_GET)[:-1]
        bad_checksum = bytearray(make_report(0x09, CMD_MEM_GET))
        bad_checksum[16] ^= 0x01
        valid = make_report(0x09, CMD_MEM_GET, b'\x44')
        received = self.read_reports([short, bytes(bad_checksum), valid])
        self.assertEqual(received, valid)

    def test_other_nordic_device_accepts_report_08_only(self):
        device = PulsarX2CrazyLight()
        device._fd = 18
        response = make_report(REPORT_ID, CMD_MEM_GET, b'\x55')
        self.assertEqual(
            self.read_reports([response], driver=device),
            response,
        )

    def test_rejects_report_09_for_other_nordic_devices(self):
        device = PulsarX2CrazyLight()
        device._fd = 18
        report = make_report(0x09, CMD_MEM_GET)
        with (
            patch('pulsar_mouse.drivers.nordic.time.monotonic',
                  side_effect=[0, 0, 1]),
            patch('pulsar_mouse.drivers.nordic.select.select',
                  return_value=([device._fd], [], [])),
            patch('pulsar_mouse.drivers.nordic.os.read',
                  return_value=report),
        ):
            with self.assertRaises(usb.core.USBTimeoutError):
                device._recv(timeout_ms=10)

    def test_missing_response_times_out(self):
        with self.assertRaises(usb.core.USBTimeoutError):
            self.driver._recv(timeout_ms=0)

    def test_wired_areson_accepts_report_09(self):
        # Captured from the cable (25a7:fa7b): same reply as the dongle.
        captured = bytes.fromhex('09 08 00 00 00 0a 01 54 04 51 00 55 00 55 00 55 91')
        device = PulsarX2AresonWired()
        device._fd = 19
        self.assertEqual(self.read_reports([captured], driver=device), captured)

    def test_libusb_receive_path_remains_unchanged(self):
        response = make_report(REPORT_ID, CMD_MEM_GET, b'\x55')
        device = PulsarX2CrazyLight()
        device._dev = Mock()
        device._dev.read.return_value = response
        self.assertEqual(device._recv(), response)

    def test_command_does_not_return_unrelated_reports_as_a_reply(self):
        self.driver._send = Mock()
        self.driver._recv = Mock(
            return_value=make_report(0x09, CMD_MEM_GET + 1))
        with self.assertRaisesRegex(RuntimeError, 'no response for command'):
            self.driver._command(CMD_MEM_GET)
        self.assertEqual(
            self.driver._recv.call_count,
            self.driver._MAX_STALE_REPLIES + 1,
        )


if __name__ == '__main__':
    unittest.main()
