"""Regression tests for the X3's lift-off distance (#24): byte 7 is a mode and
byte 8 the distance in tenths of a millimetre, which is what Fusion2 itself
writes - unlike the X2A, whose register x2a.py's table came from.  Also the 1K
dongle, which uses that encoding only while it is carrying such a mouse."""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from pulsar_mouse import cli

from pulsar_mouse.base import PulsarDevice
from pulsar_mouse.drivers.x2a import LOD_TENTHS_PIDS, PulsarX2A
from pulsar_mouse.drivers.x2h import PulsarX2H
from pulsar_mouse.drivers.xlite_wired import PulsarXliteWired
from pulsar_mouse.drivers.x3 import PulsarX3
from pulsar_mouse.drivers.xlite_v4 import PulsarXliteV4
from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless

X3_PID = 0x3409
X3_LHD_PID = 0x3410
XLITE_V4_PID = 0x3401
# A Fusion2 mouse the dongle can carry whose lift-off shape nobody has read:
# the X2 v3, X2H v3 and X2A v3 are in the Dongle Kit and not in this repo.
UNREAD_PID = 0x34FF


def scripted(cls, byte7=0x02, byte8=0x0a, paired=X3_PID, pairing=True):
    """`cls` with a transport that records writes and answers a read of
    07/82/03 with `byte7`/`byte8`, as a mouse holding that pair would.

    A read of 01/84/04 is answered with `paired` as the cabled PID the
    dongle is carrying, or refused when `pairing` is False, as a dongle that
    will not say would.
    """

    class Scripted(cls):
        def __init__(self):
            super().__init__()
            self.sent = []
            self.pairing_reads = 0

        def _set_report(self, data):
            self.sent.append(bytes(data))

        def _get_report(self):
            rsp = bytearray(self.sent[-1])
            rsp[0] = 0x01
            if rsp[1:4] == bytes([0x07, 0x82, 0x03]):
                rsp[7], rsp[8] = byte7, byte8
            elif rsp[1:4] == bytes([0x01, 0x84, 0x04]):
                self.pairing_reads += 1
                if not pairing:
                    raise IOError('the dongle will not say')
                rsp[6:10] = bytes([0x10, 0x37,
                                   paired & 0xFF, paired >> 8])
            return bytes(rsp)

    return Scripted()


class X3LodTests(unittest.TestCase):
    def test_the_three_fusion2_settings_round_trip(self):
        for mm, pair in ((0.7, (0x02, 0x07)),
                         (1, (0x02, 0x0a)),
                         (2, (0x02, 0x14))):
            mouse = scripted(PulsarX3, *pair)
            mouse.set_lod(mm, 1)
            self.assertEqual(mouse.sent[-1][1:4], bytes([0x07, 0x02, 0x03]))
            self.assertEqual(mouse.sent[-1][6], 1)
            self.assertEqual(tuple(mouse.sent[-1][7:9]), pair)
            self.assertEqual(mouse.get_lod(1), mm)

    def test_the_stock_pair_is_one_millimetre_not_two(self):
        # 02 0a is what an X3 ships on, and what x2a.py's get_lod() called
        # "2 mm" because it read the mode byte.
        self.assertEqual(scripted(PulsarX3, 0x02, 0x0a).get_lod(1), 1.0)
        self.assertEqual(scripted(PulsarX2A, 0x02, 0x0a).get_lod(1), 2)

    def test_a_pair_this_app_wrote_reads_as_one_millimetre(self):
        # Neither mode 0 (the old "1 mm") nor mode 1 (the old "2 mm") says a
        # distance; both landed at or near the mouse's 1 mm.
        self.assertEqual(scripted(PulsarX3, 0x00, 0x00).get_lod(1), 1.0)
        self.assertEqual(scripted(PulsarX3, 0x01, 0x01).get_lod(1), 1.0)

    def test_a_value_the_mouse_cannot_be_set_to_is_refused(self):
        mouse = scripted(PulsarX3)
        for mm in (0, 0.5, 1.5, 3):
            with self.assertRaisesRegex(ValueError, 'LOD must be one of'):
                mouse.set_lod(mm, 1)
        self.assertEqual(mouse.sent, [])

    def test_lod_values_are_what_fusion2_offers(self):
        self.assertEqual(PulsarX3.capabilities.lod_values, [0.7, 1, 2])


class OldExportTests(unittest.TestCase):
    def test_an_old_export_restores_the_mouse_s_one_millimetre(self):
        for version in (1, 2):
            for saved in (1, 2):
                mouse = scripted(PulsarX3)
                mouse.import_profile(1, {'version': version, 'lod_mm': saved})
                self.assertEqual(tuple(mouse.sent[-1][7:9]), (0x02, 0x0a))

    def test_a_current_export_is_taken_as_written(self):
        mouse = scripted(PulsarX3)
        mouse.import_profile(1, {'version': 3, 'lod_mm': 2})
        self.assertEqual(tuple(mouse.sent[-1][7:9]), (0x02, 0x14))

    def test_exports_say_version_3(self):
        mouse = scripted(PulsarX3)
        self.assertEqual(mouse.export_profile(1)['version'], 3)

    def test_no_other_driver_translates_an_old_lod(self):
        self.assertIs(PulsarDevice._lod_export_is_mode_byte(
            scripted(PulsarX2A)), False)
        mouse = scripted(PulsarX2A, 0x01, 0x00)
        mouse.import_profile(1, {'version': 1, 'lod_mm': 2})
        self.assertEqual(tuple(mouse.sent[-1][7:9]), (0x01, 0x01))


class DongleTests(unittest.TestCase):
    """The dongle follows the mouse it is carrying."""

    def test_an_x3_on_the_dongle_gets_the_tenths_encoding(self):
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x0a, paired=X3_PID)
        self.assertEqual(dongle.get_lod(1), 1.0)
        dongle.set_lod(2, 1)
        self.assertEqual(tuple(dongle.sent[-1][7:9]), (0x02, 0x14))

    def test_an_xlite_v4_on_the_dongle_gets_it_too(self):
        # @variasjon's, the second of the five kit mice to be read: the five
        # profiles he had never touched all hold 02 0a (#34).
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x0a,
                          paired=XLITE_V4_PID)
        self.assertEqual(dongle.get_lod(1), 1.0)
        dongle.set_lod(2, 1)
        self.assertEqual(tuple(dongle.sent[-1][7:9]), (0x02, 0x14))

    def test_a_mouse_nobody_has_read_keeps_the_x2a_encoding(self):
        dongle = scripted(PulsarXliteV4Wireless, 0x01, 0x00,
                          paired=UNREAD_PID)
        self.assertEqual(dongle.get_lod(1), 2)      # byte 7, x2a.py's table
        dongle.set_lod(2, 1)
        self.assertEqual(tuple(dongle.sent[-1][7:9]), (0x01, 0x01))

    def test_a_dongle_that_will_not_say_reads_nothing(self):
        # Not the X2A encoding as a fallback: see _lod_is_tenths().
        dongle = scripted(PulsarXliteV4Wireless, 0x01, 0x00, pairing=False)
        with self.assertRaises(IOError):
            dongle.get_lod(1)

    def test_the_pairing_is_asked_once_per_open(self):
        dongle = scripted(PulsarXliteV4Wireless, paired=X3_PID)
        for _ in range(3):
            dongle.get_lod(1)
        self.assertEqual(dongle.pairing_reads, 1)

    def test_a_refused_ask_writes_nothing(self):
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x0a, pairing=False)
        with self.assertRaises(IOError):
            dongle.set_lod(2, 1)
        self.assertEqual([p[1:4] for p in dongle.sent],
                         [bytes([0x01, 0x84, 0x04])])

    def test_only_an_answer_is_remembered(self):
        # Nothing is cached from a refusal, so the next call asks again
        # rather than carrying a guess through the rest of the open.
        dongle = scripted(PulsarXliteV4Wireless, 0x01, 0x00, pairing=False)
        for _ in range(2):
            with self.assertRaises(IOError):
                dongle.get_lod(1)
        self.assertEqual(dongle.pairing_reads, 2)

    def test_a_read_and_a_later_open_s_write_cannot_disagree(self):
        # @f-o-o-g-s's case: the GUI reads the page in one open and applies
        # in a later one, so a refusal in either of them used to be a guess
        # that the other one did not share.  An X3 holding 02 0a (1 mm) with
        # the page read's ask lost: the row showed 2 mm and the Apply after
        # it wrote 02 14, moving a mouse nobody had touched to 2 mm (#24).
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x0a, pairing=False)
        with self.assertRaises(IOError):
            dongle.get_lod(1)       # the page has no value to apply
        self.assertEqual(dongle.sent[-1][1:4], bytes([0x01, 0x84, 0x04]))

    def test_a_new_open_asks_again(self):
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x0a, paired=X3_PID)
        dongle.get_lod(1)
        dongle._lod_tenths = None       # as open() does
        dongle.get_lod(1)
        self.assertEqual(dongle.pairing_reads, 2)

    def test_the_dongle_offers_the_two_every_fusion2_mouse_has(self):
        # Not the X3's 0.7 mm: capabilities are a class variable.
        self.assertEqual(PulsarXliteV4Wireless.capabilities.lod_values, [1, 2])
        dongle = scripted(PulsarXliteV4Wireless, paired=X3_PID)
        with self.assertRaisesRegex(ValueError, 'LOD must be one of'):
            dongle.set_lod(0.7, 1)

    def test_an_old_import_warns_when_the_dongle_will_not_say(self):
        # The version check asks the same question, and it used to sit
        # outside import_profile()'s try, so a refusal stopped the import
        # where it stood - after the DPI write, with nothing said (#24).
        dongle = scripted(PulsarXliteV4Wireless, pairing=False)
        warnings = dongle.import_profile(1, {'version': 2, 'lod_mm': 2,
                                             'brightness': 0x80})
        self.assertTrue(any('LOD' in w for w in warnings), warnings)
        # ...and the brightness after it still went out.
        self.assertEqual(dongle.sent[-1][1:4], bytes([0x03, 0x03, 0x03]))

    def test_an_old_export_translates_only_for_a_tenths_mouse(self):
        dongle = scripted(PulsarXliteV4Wireless, paired=X3_PID)
        dongle.import_profile(1, {'version': 2, 'lod_mm': 2})
        self.assertEqual(tuple(dongle.sent[-1][7:9]), (0x02, 0x0a))

        other = scripted(PulsarXliteV4Wireless, paired=UNREAD_PID)
        other.import_profile(1, {'version': 2, 'lod_mm': 2})
        self.assertEqual(tuple(other.sent[-1][7:9]), (0x01, 0x01))

    def test_the_dongle_describes_the_mouse_it_carries(self):
        # Five profiles and two lift-off values fit the whole line and no
        # mouse measured on it: an X3 has six and three (#24, #34).
        dongle = scripted(PulsarXliteV4Wireless, paired=X3_PID)
        self.assertEqual(dongle.capabilities.num_profiles, 5)
        self.assertTrue(dongle.refine_capabilities())
        caps = dongle.capabilities
        self.assertEqual(caps.name, 'Pulsar X3 (1K dongle)')
        self.assertEqual(caps.num_profiles, 6)
        self.assertEqual(caps.lod_values, [0.7, 1, 2])
        # Its own ceiling, not the mouse's: the link decides this one.
        self.assertEqual(caps.polling_rates,
                         PulsarXliteV4Wireless.capabilities.polling_rates)
        self.assertIs(caps.wireless, True)
        self.assertEqual(caps.vid_pid_pairs,
                         PulsarXliteV4Wireless.capabilities.vid_pid_pairs)

    def test_it_then_offers_the_x3_s_shortest_lift_off(self):
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x07, paired=X3_PID)
        dongle.refine_capabilities()
        dongle.set_lod(0.7, 1)
        self.assertEqual(tuple(dongle.sent[-1][7:9]), (0x02, 0x07))
        self.assertEqual(dongle.get_lod(1), 0.7)

    def test_an_xlite_v4_brings_its_six_profiles_and_two_values(self):
        dongle = scripted(PulsarXliteV4Wireless, paired=XLITE_V4_PID)
        self.assertTrue(dongle.refine_capabilities())
        caps = dongle.capabilities
        self.assertEqual(caps.name, 'Pulsar Xlite v4 (1K dongle)')
        self.assertEqual(caps.num_profiles, 6)
        self.assertEqual(caps.lod_values, [1, 2])

    def test_a_mouse_with_no_cabled_driver_is_left_alone(self):
        dongle = scripted(PulsarXliteV4Wireless, paired=UNREAD_PID)
        self.assertFalse(dongle.refine_capabilities())
        self.assertEqual(dongle.capabilities,
                         PulsarXliteV4Wireless.capabilities)

    def test_a_dongle_that_will_not_say_is_left_alone(self):
        dongle = scripted(PulsarXliteV4Wireless, pairing=False)
        self.assertFalse(dongle.refine_capabilities())
        self.assertEqual(dongle.capabilities,
                         PulsarXliteV4Wireless.capabilities)

    def test_it_settles_after_one_pass(self):
        # The GUI rebuilds its pages on a True.
        dongle = scripted(PulsarXliteV4Wireless, paired=X3_PID)
        self.assertTrue(dongle.refine_capabilities())
        self.assertFalse(dongle.refine_capabilities())

    def test_the_class_still_describes_the_line(self):
        scripted(PulsarXliteV4Wireless, paired=X3_PID).refine_capabilities()
        self.assertEqual(PulsarXliteV4Wireless.capabilities.num_profiles, 5)
        self.assertEqual(PulsarXliteV4Wireless.capabilities.lod_values, [1, 2])

    def test_the_pids_are_the_ones_that_were_read(self):
        self.assertEqual(LOD_TENTHS_PIDS, {X3_PID, X3_LHD_PID, XLITE_V4_PID})


class CabledXliteV4Tests(unittest.TestCase):
    """The same register on the same mouse, reached by its own cable."""

    def test_it_reads_the_distance_rather_than_the_mode(self):
        # x2a.py's table called 02 0a "2 mm" while the mouse was on 1.0.
        self.assertEqual(scripted(PulsarXliteV4, 0x02, 0x0a).get_lod(1), 1.0)

    def test_it_writes_the_mode_and_the_tenths(self):
        for mm, pair in ((1, (0x02, 0x0a)), (2, (0x02, 0x14))):
            mouse = scripted(PulsarXliteV4, *pair)
            mouse.set_lod(mm, 1)
            self.assertEqual(tuple(mouse.sent[-1][7:9]), pair)
            self.assertEqual(mouse.get_lod(1), mm)

    def test_a_pair_this_app_wrote_reads_as_one_millimetre(self):
        # What --lod 1 and --lod 2 put there before this: 00 00 and 01 01.
        self.assertEqual(scripted(PulsarXliteV4, 0x00, 0x00).get_lod(1), 1.0)
        self.assertEqual(scripted(PulsarXliteV4, 0x01, 0x01).get_lod(1), 1.0)

    def test_the_cable_reads_what_the_dongle_read(self):
        # @variasjon ran the same probe over the cable: p1 still held the
        # 01 01 this app wrote, p2-p6 the mouse's own 02 0a (#34).
        self.assertEqual(scripted(PulsarXliteV4, 0x01, 0x01).get_lod(1), 1.0)
        self.assertEqual(scripted(PulsarXliteV4, 0x02, 0x0a).get_lod(2), 1.0)

    def test_its_limits_are_the_mouse_s_own(self):
        caps = PulsarXliteV4.capabilities
        # 02/82/10 answers with a count of 6 on both connections, and the
        # mouse carries the XS-1 like the rest of the Dongle Kit (#34, #35).
        self.assertEqual(caps.num_profiles, 6)
        self.assertEqual((caps.dpi_max, caps.dpi_step), (32000, 10))
        # The same values the dongle that carries it offers, so one mouse
        # cannot have two ceilings depending on which end it is reached from.
        dongle = PulsarXliteV4Wireless.capabilities
        self.assertEqual((caps.dpi_max, caps.dpi_step),
                         (dongle.dpi_max, dongle.dpi_step))

    def test_its_two_choices_are_unchanged(self):
        # 0.7 would be 02 07, and the Dongle Kit page claims it, but no
        # capture of Fusion2 driving this mouse has been seen.
        self.assertEqual(PulsarXliteV4.capabilities.lod_values, [1, 2])

    def test_the_older_sonix_mice_are_untouched(self):
        for cls in (PulsarX2A, PulsarX2H, PulsarXliteWired):
            self.assertIs(cls.get_lod, PulsarX2A.get_lod, cls.__name__)
            self.assertIs(cls.set_lod, PulsarX2A.set_lod, cls.__name__)


class CliRefineTests(unittest.TestCase):
    """The CLI validates --profile against the mouse, not the family (#34).

    The dongle allows five profiles until it has asked which mouse it has;
    both mice ever measured behind it have six, so main() has to open and
    refine before it checks the argument.
    """

    def run_cli(self, device, *argv):
        out = io.StringIO()
        with patch.object(cli, 'find_device', lambda name=None: device), \
                patch.object(sys, 'argv', ['pulsar-mouse', *argv]), \
                contextlib.redirect_stdout(out):
            cli.main()
        return out.getvalue()

    def dongle(self):
        dongle = scripted(PulsarXliteV4Wireless, 0x02, 0x0a, paired=X3_PID)
        dongle.open = lambda: None
        dongle.close = lambda: None
        return dongle

    def test_the_sixth_profile_of_a_six_profile_mouse_is_allowed(self):
        dongle = self.dongle()
        self.run_cli(dongle, '--profile', '6', '--lod', '1')
        write = [f for f in dongle.sent if f[1:4] == bytes([0x07, 0x02, 0x03])]
        self.assertEqual(write[-1][6], 6)           # the profile byte
        self.assertEqual(tuple(write[-1][7:9]), (0x02, 0x0a))

    def test_a_seventh_is_still_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(self.dongle(), '--profile', '7', '--lod', '1')
        self.assertIn('1–6', str(caught.exception))

    def test_a_mouse_it_cannot_identify_keeps_the_family_s_five(self):
        dongle = scripted(PulsarXliteV4Wireless, paired=UNREAD_PID)
        dongle.open = lambda: None
        dongle.close = lambda: None
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(dongle, '--profile', '6', '--lod', '1')
        self.assertIn('1–5', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
