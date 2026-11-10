"""Which CrazyLight is on the shared 5406 dongle (#44).

Pulsar ships one 2.4 GHz receiver for the whole CrazyLight line, so the
dongle's class description can only name the line until it has asked.  It
asks with the Nordic identity query (command 0x01): cid 0x57 and a mid that
says which CrazyLight answered, looked up in CRAZYLIGHT_MODELS.

That table is deliberately **not** nordic.MODELS_BY_MID.  Cfg.ini does not
list the CrazyLight at all, is keyed by the mid alone on cid 6, and collides
with this line: an X2 CrazyLight answers mid 0x03, which Cfg.ini reads as
"Pulsar X2 Mini Wireless" - so before @pogojonson's fix, refining a genuine
X2 CrazyLight renamed it to a different mouse.

Written after the merge, pinning the behaviour verified by hand on the PR.
"""

import contextlib
import unittest

import pulsar_mouse as P
from pulsar_mouse.drivers.feinmann_noctua import PulsarFeinmannNoctua
from pulsar_mouse.drivers.nordic import MODELS_BY_MID, PulsarNordic
from pulsar_mouse.drivers.x2_areson_wireless import PulsarX2AresonWireless
from pulsar_mouse.drivers.x2_crazylight import (
    CRAZYLIGHT_DONGLE_PIDS, CRAZYLIGHT_MODELS, CRAZYLIGHT_PIDS,
    PulsarX2CrazyLight)
from pulsar_mouse.drivers.x2_crazylight_wired import PulsarX2CrazyLightWired
from pulsar_mouse.drivers.x2a import PulsarX2A
from pulsar_mouse.drivers.x3_lhd_crazylight import PulsarX3LHDCrazyLight

X2_CL = (0x57, 0x03)
X3_LHD_CL = (0x57, 0x68)


def asked(cls, ids=X2_CL):
    """`cls` with a mouse that answers the identity query with `ids`.

    `open()` is the driver's own - with PulsarNordic's USB half stubbed out -
    so what it does to the remembered identity is under test rather than
    replaced.  Set `.carries` to re-pair the dongle mid-test.
    """

    class Scripted(cls):
        carries = ids

        def get_model_ids(self):
            if self.carries is None:
                raise IOError('asleep, or out of range')
            self.asks = getattr(self, 'asks', 0) + 1
            return self.carries

        def close(self):
            pass

    return Scripted()


@contextlib.contextmanager
def no_usb():
    """PulsarNordic.open() without the USB, so the overrides above it run."""
    real = PulsarNordic.open
    PulsarNordic.open = lambda self: None
    try:
        yield
    finally:
        PulsarNordic.open = real


class TheTwoTablesMustNotMeetTests(unittest.TestCase):
    def test_cfg_ini_would_rename_a_genuine_x2_crazylight(self):
        # The bug the fix is for: this is what MODELS_BY_MID says about the
        # mid an X2 CrazyLight actually answers with.
        self.assertEqual(MODELS_BY_MID[X2_CL[1]][0], 'Pulsar X2 Mini Wireless')
        self.assertEqual(CRAZYLIGHT_MODELS[X2_CL][0], 'Pulsar X2 CrazyLight')

    def test_the_dongle_keeps_its_own_name_for_an_x2_crazylight(self):
        dongle = asked(PulsarX2CrazyLight, X2_CL)
        self.assertFalse(dongle.refine_capabilities())
        self.assertEqual(dongle.capabilities.name,
                         'Pulsar X2 CrazyLight Wireless')
        self.assertEqual(dongle.model_name(), 'Pulsar X2 CrazyLight')

    def test_a_cfg_ini_mouse_is_still_cfg_ini_business(self):
        # The Areson and Noctua drivers inherit these methods but none of
        # these PIDs, so their naming is unchanged.
        for cls in (PulsarX2AresonWireless, PulsarFeinmannNoctua):
            pids = set(cls.capabilities.vid_pid_pairs)
            self.assertFalse(pids & CRAZYLIGHT_PIDS, cls.__name__)


class RefineTests(unittest.TestCase):
    def test_it_names_the_crazylight_the_dongle_carries(self):
        dongle = asked(PulsarX2CrazyLight, X3_LHD_CL)
        self.assertTrue(dongle.refine_capabilities())
        self.assertEqual(dongle.capabilities.name,
                         'Pulsar X3 LHD CrazyLight Medium Wireless')

    def test_it_settles_after_one_pass(self):
        # Every open calls this, and the GUI rebuilds its pages whenever it
        # returns True - so a second call must not.
        dongle = asked(PulsarX2CrazyLight, X3_LHD_CL)
        self.assertTrue(dongle.refine_capabilities())
        for _ in range(3):
            self.assertFalse(dongle.refine_capabilities())

    def test_the_identity_is_asked_once(self):
        dongle = asked(PulsarX2CrazyLight, X3_LHD_CL)
        dongle.refine_capabilities()
        dongle.refine_capabilities()
        dongle.model_name()
        dongle.get_connected_mouse()
        self.assertEqual(dongle.asks, 1)

    def test_a_mouse_that_will_not_say_is_left_alone(self):
        dongle = asked(PulsarX2CrazyLight, None)
        self.assertFalse(dongle.refine_capabilities())
        self.assertEqual(dongle.capabilities.name,
                         'Pulsar X2 CrazyLight Wireless')
        self.assertIsNone(dongle.model_name())

    def test_a_refused_ask_is_free_to_try_again(self):
        # None is not cached: "asleep right now" must not freeze in.
        dongle = asked(PulsarX2CrazyLight, None)
        self.assertIsNone(dongle._crazylight_ids())
        self.assertIsNone(dongle._cl_ids)

    def test_a_pair_nobody_has_decoded_keeps_the_class_description(self):
        dongle = asked(PulsarX2CrazyLight, (0x57, 0xAA))
        self.assertFalse(dongle.refine_capabilities())
        self.assertEqual(dongle.capabilities.name,
                         'Pulsar X2 CrazyLight Wireless')
        self.assertIsNone(dongle.model_name())
        with self.assertRaises(IOError):
            dongle.get_connected_mouse()

    def test_the_cabled_driver_already_names_itself(self):
        wired = asked(PulsarX3LHDCrazyLight, X3_LHD_CL)
        self.assertEqual(wired.capabilities.name,
                         'Pulsar X3 LHD CrazyLight Medium Wired')
        self.assertFalse(wired.refine_capabilities())

    def test_the_class_still_describes_the_line(self):
        asked(PulsarX2CrazyLight, X3_LHD_CL).refine_capabilities()
        self.assertEqual(PulsarX2CrazyLight.capabilities.name,
                         'Pulsar X2 CrazyLight Wireless')


class OnlyTheDongleIsAskableTests(unittest.TestCase):
    def test_only_the_shared_receiver_answers_the_question(self):
        # cli.py, gui.py and find_sibling() all gate on hasattr(), so this
        # must not reach a cabled driver or a mouse that is not a CrazyLight.
        self.assertTrue(hasattr(PulsarX2CrazyLight(), 'get_connected_mouse'))
        for cls in (PulsarX2CrazyLightWired, PulsarX3LHDCrazyLight,
                    PulsarX2AresonWireless, PulsarFeinmannNoctua):
            self.assertFalse(hasattr(cls(), 'get_connected_mouse'),
                             cls.__name__)

    def test_the_dongle_pids_are_a_subset_of_the_identity_pids(self):
        self.assertTrue(CRAZYLIGHT_DONGLE_PIDS <= CRAZYLIGHT_PIDS)

    def test_it_answers_the_cabled_ids_find_sibling_matches_on(self):
        for ids, (name, pid) in CRAZYLIGHT_MODELS.items():
            carried = asked(PulsarX2CrazyLight, ids).get_connected_mouse()
            self.assertEqual(carried, {'vid': 0x3710, 'pid': pid,
                                       'name': f'{name} Wired'})

    def test_every_model_in_the_table_has_a_driver_on_that_pid(self):
        from pulsar_mouse.drivers import discover_all
        cabled = {p for cls in discover_all().values()
                  if cls.capabilities.wireless is not True
                  for p in cls.capabilities.vid_pid_pairs}
        for _ids, (name, pid) in CRAZYLIGHT_MODELS.items():
            self.assertIn((0x3710, pid), cabled, name)


class CabledPollingTests(unittest.TestCase):
    """The cable's 1 kHz ceiling, which the new driver inherits and the
    README now states for it.  Untested until this mouse made it
    load-bearing in two places rather than one.
    """

    def cabled(self, cls, stored):
        """`cls` with the transport stubbed, not set_polling_rate itself.

        Overriding the method under test is how the first version of this
        got a false pass: the wired class's decline never ran.  These stub
        _write_value and _tunable_profile, which is what the base class's
        set_polling_rate reaches for, so the real override chain runs.
        """

        class Scripted(cls):
            written = None

            def get_stored_polling_rate(self, profile=None):
                return stored

            def _tunable_profile(self, profile):
                pass

            def _write_value(self, addr, value):
                Scripted.written = (addr, value)

        return Scripted()

    def rate_written(self, mouse):
        from pulsar_mouse.drivers.nordic import (ADDR_POLLING_RATE,
                                                 POLL_VAL_TO_HZ)
        written = type(mouse).written
        if written is None:
            return None
        addr, value = written
        self.assertEqual(addr, ADDR_POLLING_RATE)
        return POLL_VAL_TO_HZ[value]

    def test_it_reports_what_the_cable_actually_runs(self):
        # The rate is per profile and shared with the dongle, so a profile
        # set to 8 kHz over the dongle still holds 8 kHz while cabled.
        for cls in (PulsarX2CrazyLightWired, PulsarX3LHDCrazyLight):
            mouse = self.cabled(cls, 8000)
            self.assertEqual(mouse.get_polling_rate(), 1000, cls.__name__)
            self.assertEqual(mouse.get_stored_polling_rate(), 8000)
            self.assertEqual(mouse.capabilities.polling_rates,
                             [125, 250, 500, 1000])

    def test_writing_the_cap_over_a_higher_stored_rate_is_declined(self):
        # That write changes nothing here and would cut the profile for the
        # next time the mouse is on the dongle.
        for cls in (PulsarX2CrazyLightWired, PulsarX3LHDCrazyLight):
            mouse = self.cabled(cls, 2000)
            mouse.set_polling_rate(1000)
            self.assertIsNone(self.rate_written(mouse), cls.__name__)

    def test_a_real_change_is_written_as_asked(self):
        for cls in (PulsarX2CrazyLightWired, PulsarX3LHDCrazyLight):
            mouse = self.cabled(cls, 2000)
            mouse.set_polling_rate(500)
            self.assertEqual(self.rate_written(mouse), 500, cls.__name__)

    def test_the_cap_is_written_when_nothing_higher_is_stored(self):
        for cls in (PulsarX2CrazyLightWired, PulsarX3LHDCrazyLight):
            mouse = self.cabled(cls, 500)
            mouse.set_polling_rate(1000)
            self.assertEqual(self.rate_written(mouse), 1000, cls.__name__)

    def test_the_dongle_has_no_ceiling(self):
        self.assertEqual(PulsarX2CrazyLight.capabilities.polling_rates,
                         [125, 250, 500, 1000, 2000, 4000, 8000])


class SiblingTests(unittest.TestCase):
    def setUp(self):
        self._real_scan = P.scan_devices
        self.addCleanup(setattr, P, 'scan_devices', self._real_scan)
        P._CARRIED_CACHE.clear()
        self.addCleanup(P._CARRIED_CACHE.clear)

    def pair(self, me, bus):
        # no_usb() rather than a stubbed open(): _carried_mouse() opens the
        # dongle for its one read, so the driver's own open() - and the
        # identity it forgets there - is part of what this exercises.
        P.scan_devices = lambda: bus
        with no_usb():
            return P.find_sibling(me)

    def test_the_key_still_pairs_the_x2_crazylight(self):
        dongle = asked(PulsarX2CrazyLight, X2_CL)
        wired = asked(PulsarX2CrazyLightWired, X2_CL)
        self.assertIs(self.pair(dongle, [dongle, wired]), wired)
        self.assertIs(self.pair(wired, [wired, dongle]), dongle)

    def test_the_dongle_falls_through_its_key_to_ask(self):
        # Its key names the X2 CrazyLight pair; this mouse has none, because
        # a key is exactly two drivers.
        dongle = asked(PulsarX2CrazyLight, X3_LHD_CL)
        wired = asked(PulsarX3LHDCrazyLight, X3_LHD_CL)
        self.assertIsNone(wired.capabilities.model_key)
        self.assertIs(self.pair(dongle, [dongle, wired]), wired)
        self.assertIs(self.pair(wired, [wired, dongle]), dongle)

    def test_it_never_pairs_with_a_different_mouse(self):
        other = asked(PulsarX2A, X2_CL)
        dongle = asked(PulsarX2CrazyLight, X3_LHD_CL)
        self.assertIsNone(self.pair(dongle, [dongle, other]))
        # The dongle carries the X2 CL, but the cabled mouse is the other one.
        dongle = asked(PulsarX2CrazyLight, X2_CL)
        wired = asked(PulsarX3LHDCrazyLight, X3_LHD_CL)
        self.assertIsNone(self.pair(dongle, [dongle, wired]))
        self.assertIsNone(self.pair(wired, [wired, dongle]))

    def test_the_key_does_not_pre_empt_the_question(self):
        # The half this file missed first time round, and the one a reviewer
        # found: the dongle's key names the X2 CrazyLight pair, so with an
        # X2 cable plugged in to charge it was returned whatever the dongle
        # was actually carrying - both directions.
        dongle = asked(PulsarX2CrazyLight, X3_LHD_CL)
        x2 = asked(PulsarX2CrazyLightWired, X2_CL)
        x3 = asked(PulsarX3LHDCrazyLight, X3_LHD_CL)
        self.assertIsNone(self.pair(dongle, [dongle, x2]))
        self.assertIsNone(self.pair(x2, [x2, dongle]))
        # With both cables there, it is the one the dongle names.
        self.assertIs(self.pair(dongle, [dongle, x2, x3]), x3)

    def test_an_askable_candidate_is_asked_not_taken_on_its_key(self):
        # Same bug from the cabled side: the X2 cable and the 5406 dongle
        # share a key, and the dongle can be asked, so it is.
        x2 = asked(PulsarX2CrazyLightWired, X2_CL)
        self.assertEqual(x2.capabilities.model_key, 'x2_crazylight')
        for ids, expected in ((X2_CL, True), (X3_LHD_CL, False)):
            dongle = asked(PulsarX2CrazyLight, ids)
            self.assertEqual(dongle.capabilities.model_key, 'x2_crazylight')
            got = self.pair(x2, [x2, dongle])
            self.assertEqual(got is dongle, expected, ids)

    def test_nothing_cabled_is_not_asked_at_all(self):
        dongle = asked(PulsarX2CrazyLight, X2_CL)
        self.assertIsNone(self.pair(dongle, [dongle]))
        self.assertFalse(hasattr(dongle, 'asks'))

    def test_a_keyed_pair_that_cannot_be_asked_is_unchanged(self):
        # The fall-through must not reach a driver with no way to ask: only
        # x2_crazylight has both a model_key and get_connected_mouse.
        noctua = asked(PulsarFeinmannNoctua, X2_CL)
        other = asked(PulsarX2A, X2_CL)
        self.assertIsNone(self.pair(noctua, [noctua, other]))

    def test_a_re_paired_dongle_is_asked_again(self):
        # A receiver's answer is pairing memory, not what it is: pair a
        # different CrazyLight to the same dongle and it has to say so.
        # The Fusion2 dongle clears its own per-open memory for exactly
        # this reason - see xlite_v4_wireless.open().
        dongle = asked(PulsarX2CrazyLight, X2_CL)
        with no_usb():
            dongle.open()
            self.assertEqual(dongle.get_connected_mouse()['pid'], 0x3414)
            dongle.carries = X3_LHD_CL
            self.assertEqual(dongle.get_connected_mouse()['pid'], 0x3414,
                             'within one open it may be remembered')
            dongle.open()
            self.assertEqual(dongle.get_connected_mouse()['pid'], 0x3527)
            self.assertTrue(dongle.refine_capabilities())
            self.assertEqual(dongle.capabilities.name,
                             'Pulsar X3 LHD CrazyLight Medium Wireless')

    def test_a_cabled_mouse_keeps_its_one_ask(self):
        # It cannot change which model it is, so re-opening must not cost
        # another handshake.
        for cls in (PulsarX2CrazyLightWired, PulsarX3LHDCrazyLight):
            mouse = asked(cls, X3_LHD_CL)
            with no_usb():
                mouse.open()
                mouse._crazylight_ids()
                mouse.open()
                mouse._crazylight_ids()
            self.assertEqual(mouse.asks, 1, cls.__name__)

    def test_exactly_one_driver_has_both_a_key_and_the_ask(self):
        from pulsar_mouse.drivers import discover_all
        both = {name for name, cls in discover_all().items()
                if cls.capabilities.model_key
                and hasattr(cls(), 'get_connected_mouse')}
        self.assertEqual(both, {'x2_crazylight'})


if __name__ == '__main__':
    unittest.main()
