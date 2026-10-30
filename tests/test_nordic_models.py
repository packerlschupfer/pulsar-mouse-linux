"""Regression tests for the phantom right-hand buttons (#30).

Four models share 3554:f507 and f508, and only one of them - the X2A
Wireless, KM=14 - has the right-hand thumb pair this driver's button table
was built from.  On the other three those slots hold something else, so a
mouse whose MID names one of them refuses both rather than reading a binding
that is not one and writing over whatever its firmware keeps there.

MID values from Fusion's Cfg.ini, contributed by @bluesceada; mid=7
corroborated on his own mouse by the CheckPsd read.
"""

import contextlib
import io
import unittest

from pulsar_mouse import cli
from pulsar_mouse.base import PulsarDevice
from pulsar_mouse.drivers.nordic import (
    ADDR_AUTOSLEEP,
    ADDR_AUTOSLEEP_ALT,
    CMD_MEM_SET,
    KM_RIGHT_FLANK,
    MODELS_BY_MID,
    PulsarNordic,
)

XLITE_V3, X2A_WIRELESS, X2_V2, X2_V2_MINI = 0x07, 0x08, 0x0A, 0x0B


def crazylight():
    """The one mouse 0xAD mirroring was measured on."""
    from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight

    class Scripted(PulsarX2CrazyLight):
        def __init__(self):
            super().__init__()
            self._fd = 17
            self._mem = {addr: 0 for addr in range(0x00, 0xD0)}
            self.sent = []

        def _send(self, packet):
            self.sent.append(bytes(packet))
            self.last = packet

        def _recv(self, timeout_ms=2000):
            body = bytearray(16)
            body[0], body[1] = 0x08, self.last[1]
            return bytes(body) + bytes([self._checksum(*body)])

        def _tunable_profile(self, profile):
            pass

    return Scripted()


def mouse(mid=None, deaf=False, status=0x00, cid=0x06):
    """A Nordic mouse whose handshake answers with `mid`, or not at all.

    `status` is byte 2 of the reply, which is 0 when the mouse answered.
    """

    class Scripted(PulsarNordic):
        def __init__(self):
            super().__init__()
            self._fd = 17
            self._mem = {addr: 0x01 for addr in range(0x60, 0x90)}
            self.handshakes = 0
            self.sent = []

        def _send(self, packet):
            self.sent.append(bytes(packet))
            self.last = packet

        def _recv(self, timeout_ms=2000):
            self.handshakes += 1
            if deaf:
                raise IOError('asleep')
            body = bytearray(16)
            body[0], body[1] = 0x08, self.last[1]
            body[2] = status
            body[10], body[11] = cid, mid or 0
            return bytes(body) + bytes([self._checksum(*body)])

        def _ensure_profile(self, profile):
            pass

        def _tunable_profile(self, profile):
            pass

    return Scripted()


class AbsentButtonTests(unittest.TestCase):
    def ids(self, device, *names):
        return [device.capabilities.buttons[n] for n in names]

    def test_a_six_button_model_refuses_the_right_flank(self):
        for mid in (XLITE_V3, X2_V2, X2_V2_MINI):
            device = mouse(mid)
            for btn_id in self.ids(device, *KM_RIGHT_FLANK):
                with self.assertRaises(NotImplementedError):
                    device.get_button(btn_id, 1)
                with self.assertRaises(NotImplementedError):
                    device.set_button(btn_id, 0x01, 0x01, 0x00, 1)

    def test_it_keeps_the_six_it_has(self):
        device = mouse(XLITE_V3)
        for btn_id in self.ids(device, 'left', 'right', 'wheel',
                               'thumb1', 'thumb2', 'dpi'):
            self.assertIsNotNone(device.get_button(btn_id, 1))

    def test_the_x2a_wireless_keeps_all_eight(self):
        device = mouse(X2A_WIRELESS)
        for btn_id in device.capabilities.buttons.values():
            self.assertIsNotNone(device.get_button(btn_id, 1))

    def test_a_mouse_that_will_not_say_keeps_all_eight(self):
        # Today's behaviour, and the safer way round: hiding two real
        # buttons from an X2A Wireless whose read failed would be worse than
        # leaving them, which is what the README has always warned about.
        device = mouse(deaf=True)
        for btn_id in device.capabilities.buttons.values():
            self.assertIsNotNone(device.get_button(btn_id, 1))

    def test_a_mid_newer_than_the_table_is_taken_as_six_button(self):
        # Four of the 21 are KM=14 and all four are known, so a model this
        # table has never heard of is likelier to be another KM=10 - and
        # offering two buttons that are not there is the worse mistake.
        device = mouse(0x7F)
        for btn_id in self.ids(device, *KM_RIGHT_FLANK):
            with self.assertRaises(NotImplementedError):
                device.get_button(btn_id, 1)
        self.assertIsNotNone(
            device.get_button(device.capabilities.buttons['dpi'], 1))

    def test_a_refused_answer_is_not_a_model(self):
        # Byte 2 is a status; without checking it, a refusal came back as
        # cid=0 mid=0 and was remembered as a model for the whole session.
        for refusal in (mouse(0x07, status=0x01), mouse(0x00), mouse(0x07, cid=0)):
            for btn_id in refusal.capabilities.buttons.values():
                self.assertIsNotNone(refusal.get_button(btn_id, 1))
            self.assertIsNone(refusal.model_name())

    def test_a_refusal_is_asked_again(self):
        device = mouse(0x07, status=0x01)
        for _ in range(3):
            device.get_button(device.capabilities.buttons['thumb3'], 1)
        self.assertEqual(device.handshakes, 3)

    def test_the_model_is_asked_once(self):
        device = mouse(X2A_WIRELESS)
        for _ in range(4):
            device.get_button(device.capabilities.buttons['thumb3'], 1)
        self.assertEqual(device.handshakes, 1)

    def test_a_deaf_mouse_is_asked_again(self):
        # Nothing is cached from a refusal, so a mouse that wakes up later
        # gets its buttons judged properly.
        device = mouse(deaf=True)
        for _ in range(3):
            device.get_button(device.capabilities.buttons['thumb3'], 1)
        self.assertEqual(device.handshakes, 3)

    def test_the_six_every_model_has_are_not_worth_asking_about(self):
        # No model is missing these, so reading them costs no handshake.
        device = mouse(XLITE_V3)
        for name in ('left', 'right', 'wheel', 'thumb1', 'thumb2', 'dpi'):
            device.get_button(device.capabilities.buttons[name], 1)
        self.assertEqual(device.handshakes, 0)

    def test_the_model_is_named(self):
        self.assertEqual(mouse(XLITE_V3).model_name(), 'Pulsar Xlite V3')
        self.assertEqual(mouse(X2A_WIRELESS).model_name(),
                         'Pulsar X2A Wireless')
        self.assertIsNone(mouse(deaf=True).model_name())

class RefineTests(unittest.TestCase):
    """What the window is built from, once the MID says which mouse it is."""

    def test_a_six_button_model_loses_the_right_flank(self):
        device = mouse(XLITE_V3)
        self.assertTrue(device.refine_capabilities())
        caps = device.capabilities
        self.assertEqual(caps.name, 'Pulsar Xlite V3')
        self.assertEqual(sorted(caps.buttons),
                         ['dpi', 'left', 'right', 'thumb1', 'thumb2', 'wheel'])
        self.assertNotIn('thumb3', caps.button_labels)

    def test_the_x2a_wireless_keeps_its_eight_and_gains_a_name(self):
        device = mouse(X2A_WIRELESS)
        self.assertTrue(device.refine_capabilities())
        caps = device.capabilities
        self.assertEqual(caps.name, 'Pulsar X2A Wireless')
        self.assertEqual(len(caps.buttons), 8)
        self.assertIn('thumb3', caps.buttons)

    def test_the_class_is_left_alone(self):
        # Only this instance is narrowed: scan_devices() reads the class.
        before = dict(PulsarNordic.capabilities.buttons)
        mouse(XLITE_V3).refine_capabilities()
        self.assertEqual(PulsarNordic.capabilities.buttons, before)
        self.assertEqual(len(before), 8)

    def test_the_second_call_changes_nothing(self):
        # The GUI rebuilds its pages on a True, so a driver that kept
        # saying True would rebuild them on every read.
        device = mouse(XLITE_V3)
        self.assertTrue(device.refine_capabilities())
        self.assertFalse(device.refine_capabilities())
        self.assertFalse(device.refine_capabilities())

    def test_a_mouse_that_will_not_say_is_left_as_the_family(self):
        device = mouse(deaf=True)
        self.assertFalse(device.refine_capabilities())
        self.assertEqual(device.capabilities, PulsarNordic.capabilities)

    def test_an_unknown_mid_loses_the_flank_but_keeps_the_name(self):
        device = mouse(0x7F)
        self.assertTrue(device.refine_capabilities())
        caps = device.capabilities
        self.assertEqual(caps.name, PulsarNordic.capabilities.name)
        self.assertNotIn('thumb3', caps.buttons)
        self.assertEqual(len(caps.buttons), 6)

    def test_a_refusal_is_left_as_the_family(self):
        device = mouse(0x07, status=0x01)
        self.assertFalse(device.refine_capabilities())
        self.assertEqual(device.capabilities, PulsarNordic.capabilities)

    def test_every_other_driver_refines_nothing(self):
        from pulsar_mouse.drivers import discover_all
        for name, cls in discover_all().items():
            if issubclass(cls, PulsarNordic):
                continue
            device = cls.__new__(cls)
            self.assertFalse(PulsarDevice.refine_capabilities(device), name)


class ButtonListTests(unittest.TestCase):
    """A button the mouse has not got must not take the rest with it."""

    def test_an_export_keeps_every_button_it_can_read(self):
        # The refusal used to drop the whole 'buttons' dict, the DPI
        # button's binding included (#30).
        device = mouse(XLITE_V3)
        data = device.export_profile(1)
        self.assertEqual(sorted(data['buttons']),
                         ['dpi', 'left', 'right', 'thumb1', 'thumb2', 'wheel'])

    def test_the_cli_prints_past_the_one_it_cannot_read(self):
        device = mouse(XLITE_V3)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_profile(device, 1)
        lines = [l.strip() for l in out.getvalue().splitlines()]
        named = [l.split()[0] for l in lines if l.startswith(('left', 'right',
                 'wheel', 'thumb', 'dpi'))]
        self.assertIn('dpi', named)
        self.assertNotIn('thumb3', named)
        self.assertNotIn('thumb4', named)
        self.assertNotIn('error', out.getvalue())

    def test_the_x2a_wireless_still_lists_eight(self):
        data = mouse(X2A_WIRELESS).export_profile(1)
        self.assertEqual(len(data['buttons']), 8)


class AutosleepMirrorTests(unittest.TestCase):
    """0xAD is only written where Fusion has been seen writing it (#30)."""

    def written(self, device, seconds=60):
        device.sent = []
        device.set_autosleep(seconds)
        return sorted({(p[3] << 8) | p[4] for p in device.sent
                       if p[1] == CMD_MEM_SET})

    def test_the_family_writes_only_the_byte_fusion_agreed_with(self):
        # @bluesceada's Xlite V3 held different values at 0xAD and 0xB7, and
        # Fusion's own UI showed 0xB7's.
        self.assertEqual(self.written(mouse(XLITE_V3)), [ADDR_AUTOSLEEP])

    def test_the_crazylight_still_writes_both(self):
        # Measured there: Fusion writes both, with the same value.
        light = crazylight()
        self.assertEqual(self.written(light),
                         [ADDR_AUTOSLEEP_ALT, ADDR_AUTOSLEEP])

    def test_the_value_is_tenths_of_a_minute_either_way(self):
        for device in (mouse(XLITE_V3), crazylight()):
            device.sent = []
            device.set_autosleep(180)
            wrote = [p for p in device.sent if p[1] == CMD_MEM_SET]
            self.assertEqual(wrote[-1][6], 18, type(device).__mro__[1].__name__)


class SleepControlTests(unittest.TestCase):
    """The family's sleep timer, measured off Fusion's own slider (#30)."""

    def test_the_family_offers_one_at_all(self):
        # It had none: the wrappers the CLI and GUI look for lived in the
        # X2 CrazyLight driver, two models away.
        device = mouse(XLITE_V3)
        self.assertEqual(device.capabilities.power_saving_range, (10, 600, 10))
        self.assertTrue(hasattr(device, 'set_power_saving_timeout'))

    def test_both_ends_are_the_ones_fusion_wrote(self):
        device = mouse(XLITE_V3)
        device._mem[ADDR_AUTOSLEEP] = 0x06      # a minute, as read
        for seconds, stored in ((10, 0x01), (600, 0x3c), (120, 0x0c)):
            device.sent = []
            device.set_power_saving_timeout(seconds)
            wrote = [p for p in device.sent if p[1] == CMD_MEM_SET]
            self.assertEqual((wrote[-1][3] << 8) | wrote[-1][4], ADDR_AUTOSLEEP)
            self.assertEqual(wrote[-1][6], stored)

    def test_a_value_outside_the_range_is_left_where_it_is(self):
        # A mouse holding something the slider cannot show comes back
        # clamped, and writing that clamp would quietly replace it.  Half an
        # hour, which this family's slider stops short of.
        device = mouse(XLITE_V3)
        device._mem[ADDR_AUTOSLEEP] = 0xb4      # 1800 s
        device.sent = []
        device.set_power_saving_timeout(600)    # the clamp, unchosen
        self.assertEqual([p for p in device.sent if p[1] == CMD_MEM_SET], [])
        device.set_power_saving_timeout(120)    # a real choice
        self.assertTrue([p for p in device.sent if p[1] == CMD_MEM_SET])

    def test_past_either_end_is_refused(self):
        device = mouse(XLITE_V3)
        device._mem[ADDR_AUTOSLEEP] = 0x06
        for seconds in (0, 5, 610, 1800):
            with self.assertRaises(ValueError):
                device.set_power_saving_timeout(seconds)

    def test_the_crazylight_keeps_its_own_half_hour(self):
        self.assertEqual(crazylight().capabilities.power_saving_range,
                         (10, 1800, 10))


class ProfileSwitchTests(unittest.TestCase):
    def test_a_switch_the_mouse_refused_raises(self):
        # Byte 2 is a status; a switch that did not take would leave every
        # read after it addressing the profile the mouse is still on.
        device = mouse(XLITE_V3, status=0x01)
        device._home_profile = 1
        with self.assertRaisesRegex(IOError, 'would not switch'):
            device._switch_to(2)

    def test_an_accepted_switch_goes_through(self):
        device = mouse(XLITE_V3)
        device._mem_read_all = lambda: None
        device._drain = lambda: None
        device._switch_to(2)
        self.assertEqual(device._profile, 2)
        switch = [p for p in device.sent if p[1] == 0x0F][-1]
        self.assertEqual(switch[6], 1)          # 0-based on the wire


class ProfileCountTests(unittest.TestCase):
    """Four profiles, and the globals that live in each of them (#30)."""

    def test_the_family_has_four(self):
        # 1 and 2 were measured in the capture, 3 and 4 by @bluesceada
        # switching to them in Fusion and with a button bound to it.
        self.assertEqual(PulsarNordic.capabilities.num_profiles, 4)

    def test_the_globals_are_per_profile(self):
        # The memory map is the active profile, so a "global" read with a
        # profile named has to load that profile first.
        self.assertTrue(PulsarNordic.capabilities.per_profile_globals)
        device = mouse(XLITE_V3)
        # The real _tunable_profile/_ensure_profile, which is what decides
        # whether a profile gets loaded before the read.
        del device.__class__._tunable_profile
        del device.__class__._ensure_profile
        device.__class__._mem_read_all = lambda self: None
        # _drain() reads until one times out, and this stub always answers.
        device.__class__._drain = lambda self: None
        device._profile = 1
        device._home_profile = 1
        device.sent = []
        device.get_debounce(profile=3)
        switch = [p for p in device.sent if p[1] == 0x0F]
        self.assertEqual([p[6] for p in switch], [2])    # 0-based on the wire

    def test_a_global_with_no_profile_stays_where_the_mouse_is(self):
        device = mouse(XLITE_V3)
        del device.__class__._tunable_profile
        del device.__class__._ensure_profile
        device.__class__._mem_read_all = lambda self: None
        device.__class__._drain = lambda self: None
        device._profile = 2
        device._home_profile = 2
        device.sent = []
        device.get_debounce()
        self.assertEqual([p for p in device.sent if p[1] == 0x0F], [])

    def test_the_sleep_step_is_finer_than_fusion_s_stops(self):
        # Fusion offers 10/20/30 s then whole minutes; 50 s was set here and
        # timed on hardware, so the step stays at 10 s.
        lo, hi, step = PulsarNordic.capabilities.power_saving_range
        self.assertEqual((lo, hi, step), (10, 600, 10))
        device = mouse(XLITE_V3)
        device._mem[ADDR_AUTOSLEEP] = 0x06
        device.sent = []
        device.set_power_saving_timeout(50)
        wrote = [p for p in device.sent if p[1] == CMD_MEM_SET]
        self.assertEqual(wrote[-1][6], 0x05)


class CfgIniTests(unittest.TestCase):
    def test_the_table_is_the_one_from_the_cfg_ini(self):
        # All 21 models in Fusion Wireless Mice 1.3.3's Cfg.ini, and the
        # four that have two flanks of thumb buttons.
        self.assertEqual(sorted(MODELS_BY_MID), list(range(0x01, 0x16)))
        self.assertEqual(
            sorted(mid for mid, (_n, km) in MODELS_BY_MID.items() if km == 14),
            [0x08, 0x09, 0x12, 0x13])
        self.assertTrue(all(km in (10, 14)
                            for _n, km in MODELS_BY_MID.values()))
        # The file really does name 0x12/0x14 and 0x13/0x15 the same.
        self.assertEqual(MODELS_BY_MID[0x12][0], MODELS_BY_MID[0x14][0])
        self.assertEqual(MODELS_BY_MID[0x13][0], MODELS_BY_MID[0x15][0])


if __name__ == '__main__':
    unittest.main()
