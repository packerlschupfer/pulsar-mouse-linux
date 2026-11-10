"""
Pulsar X2 CrazyLight (X2 CL) Wireless — protocol driver.

Despite the VID it shares with the Sonix mice (0x3710), the X2 CL dongle
speaks the **Nordic 17-byte protocol**, not the 64-byte Sonix Feature Report
protocol — which is why the x2a / xlite_v4 / feinmann8k drivers return
garbage for it.  It therefore inherits everything from PulsarNordic and only
overrides the model-specific layout.

USB:
    VID 0x3710
    PID 0x5406 (2.4 GHz dongle)
    Interface 1, IN endpoint 0x82, 17-byte reports, report ID 0x08

Protocol decoded from a Pulsar Fusion USB capture contributed by
@iamtherobin in issue #7; see docs/protocol-x2-crazylight.md for the full
register map and the correlation method.

Differences from the X2A Wireless (PulsarNordic):

  * DPI is stored in 10 DPI steps, not 50 (0x27 -> 400 DPI).
  * Polling goes up to 8 kHz (0x10/0x20/0x40 for 2 K/4 K/8 K).
  * A third lift-off distance, 0.7 mm, stored as code 0x03.
  * A sixth button slot at 0x74, bound to DPI Loop by default.
  * Four onboard profiles rather than one.  The memory map is whichever
    profile is active, so reading or writing a different one switches first
    (command 0x0F) and reloads the map — the same thing Fusion does.

Status: reads and writes are confirmed on hardware.  @iamtherobin ran
        tools/x2cl-verify.py against a real X2 CrazyLight on 2026-09-15 and every
        setting in this driver round-tripped, over both the cable and the
        dongle, with the mouse restored byte for byte afterwards.

The DPI encoding is fully resolved: fourteen (bytes -> DPI) pairs across two
captures all decode exactly.  See docs/protocol-x2-crazylight.md.

Remaining unknown: factory reset.  Fusion never issued command 0x09 in any
of the three captures, so has_reset is False rather than guessing at a
destructive command.
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.nordic import PulsarNordic

# ── Identity ─────────────────────────────────────────────────────────────
#
# What the CrazyLight line answers the identity query (command 0x01,
# "CheckPsd" in Fusion's own Debug=2 log) with: cid 0x57, and a mid that
# says which CrazyLight it is.  The pair is read whole and looked up here,
# never through nordic.MODELS_BY_MID: that table came from Fusion's
# Cfg.ini, is keyed by the MID alone on cid 6, and collides with this line
# — an X2 CrazyLight's own capture answers `57 03 05` (see
# docs/protocol-x2-crazylight.md), and mid 0x03 there reads "Pulsar X2
# Mini Wireless" in it, which is how a genuine X2 CrazyLight used to be
# renamed.  nordic.py itself says Cfg.ini "does not list the CrazyLight at
# all", so on these PIDs that table is never consulted.
#
#   (0x57, 0x03)  X2 CrazyLight — the handshake tail in @iamtherobin's
#                 capture (#7).
#   (0x57, 0x68)  X3 LHD CrazyLight Medium — read on hardware through the
#                 dongle and over its USB-C cable alike, same pair both
#                 ways; its cabled HID string is "Pulsar X3 LHD CrazyLight
#                 Medium" (2026-11).
#
# The second field is the cabled PID: what find_sibling() matches a cabled
# driver on.  The 5406 dongle now serves more than one model, which is
# precisely what capabilities.model_key cannot describe — a key is exactly
# two drivers (see check-drivers' model_key check) — so the pair comes from
# asking instead, the way the Fusion2 dongles are handled.
CRAZYLIGHT_MODELS: dict[tuple[int, int], tuple[str, int]] = {
    (0x57, 0x03): ('Pulsar X2 CrazyLight', 0x3414),
    (0x57, 0x68): ('Pulsar X3 LHD CrazyLight Medium', 0x3527),
}

# The PIDs these identity rules speak for.  The Areson and Noctua drivers
# derive from the class below but are not CrazyLight mice, so for anything
# not keyed here the identity question falls back to what the Nordic family
# has always done - nordic.MODELS_BY_MID and its Cfg.ini names.
CRAZYLIGHT_PIDS = frozenset({
    (0x3710, 0x5406),   # the shared 2.4 GHz dongle
    (0x3710, 0x3414),   # X2 CrazyLight, cable
    (0x3710, 0x3527),   # X3 LHD CrazyLight Medium, cable
})

# Of those, the ones that are a receiver with a mouse on the other end, and
# so the only ones that can answer "which mouse do you carry".  Named rather
# than written out in __init__ so it cannot drift from the set above.
CRAZYLIGHT_DONGLE_PIDS = frozenset({
    (0x3710, 0x5406),
})
assert CRAZYLIGHT_DONGLE_PIDS <= CRAZYLIGHT_PIDS


class PulsarX2CrazyLight(PulsarNordic):
    """Driver for the Pulsar X2 CrazyLight through its 2.4 GHz dongle."""

    capabilities: DeviceCapabilities = DeviceCapabilities(
        name='Pulsar X2 CrazyLight Wireless',
        vid_pid_pairs=[(0x3710, 0x5406)],
        interface_num=1,
        report_size=17,
        num_profiles=4,
        # A live readout found profiles configured with six stages, with
        # distinct factory defaults through slot 6 (6400 orange, 12800
        # magenta).  The map reserves eight records (0x0C–0x2B) and eight
        # colours (0x2C–0x4B), but slots 7 and 8 just repeat slot 6, so six
        # is the real maximum.
        max_dpi_stages=6,
        # Four from Fusion's own profile list, and every one of them read
        # and written back byte for byte by @iamtherobin's verify run.  Not
        # from the 0x0A that follows a switch: its byte 6 is the event code
        # EVENT_PROFILE_CHANGED, which is also 0x04 (@f-o-o-g-s, #30).
        dpi_min=10,
        dpi_max=32000,
        dpi_step=10,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,   # side front (default: forward)
            'thumb2': 0x05,   # side back  (default: backward)
            'dpi':    0x0b,   # default: DPI Loop
        },
        polling_rates=[125, 250, 500, 1000, 2000, 4000, 8000],
        lod_values=[0.7, 1, 2],
        has_led=True,
        has_breathe_speed=True,
        brightness_range=(0x10, 0xFF),
        breathe_speed_range=(1, 5),
        has_angle_snap=True,
        has_ripple_control=True,
        has_motion_sync=True,
        has_debounce=True,
        debounce_range=(0, 20),
        has_stage_colors=True,
        has_reset=False,
        # Polling rate, debounce, angle snap, ripple control and motion
        # sync all live at 0x00-0xB1, inside the per-profile window: the
        # captures show profile 1 at 1 kHz, 3 at 4 kHz and 4 at 8 kHz.
        per_profile_globals=True,
        wireless=True,
        model_key='x2_crazylight',
        # No signal-quality channel: a tester walking an X2 CrazyLight out of
        # the dongle's range produced nothing at all on the config interface.
        reports_signal_quality=False,
        # Both seen in the Fusion captures: Turbo Mode at 0xB5, and Auto
        # Sleep at 0xAD/0xB7 in 10 s units, set anywhere from 10 s to 30 min.
        has_turbo=True,
        power_saving_range=(10, 1800, 10),
        button_labels={
            'left': 'Left Click',
            'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Front (forward)',
            'thumb2': 'Side Back (backward)',
            'dpi': 'DPI Button',
        },
    )

    # Slot order in the button table at 0x60, one 4-byte record each.
    # Defaults in the capture: 0x60 (01,01) left, 0x64 (01,02) right,
    # 0x68 (01,04) wheel, 0x6C (01,08) back, 0x70 (01,10) forward,
    # 0x74 (02,01) DPI loop.
    _BUTTON_ADDRS = {
        'left':   0x60,
        'right':  0x64,
        'wheel':  0x68,
        'thumb2': 0x6C,
        'thumb1': 0x70,
        'dpi':    0x74,
    }

    # DPI granularity changes with the range, and the stage record's mode
    # field says which is in use: 10 DPI steps to 10240, then 50 to 25600,
    # then 100.  Values that don't land on the step for their range are
    # snapped to it — the device has nothing finer to offer up there.
    _DPI_MODES = (
        (0, 1, 1, 10240),      # steps of  10 DPI
        (2, 5, 201, 25600),    # steps of  50 DPI
        (3, 10, 201, None),    # steps of 100 DPI, to Fusion's 32000 ceiling
    )


    # 0.7 mm is stored as 0x03 — it was added after 1 mm and 2 mm, so the
    # codes are not in physical order.
    _LOD_CODES = {1: 0x01, 2: 0x02, 0.7: 0x03}

    # Fusion writes the sleep timeout to 0xAD as well as 0xB7 on this mouse,
    # always with the same value, in all three captures (#7) - which is
    # where set_autosleep()'s mirroring came from, and the only mouse it has
    # ever been measured on.  See PulsarNordic._AUTOSLEEP_MIRROR for why the
    # base class no longer assumes it.  The CrazyLight Wired, the Noctua and
    # the Areson X2 all derive from this class and so keep the mirror: it is
    # what they have always done, their register maps were validated against
    # this one's, and nothing says it is wrong for them.
    _AUTOSLEEP_MIRROR = True

    # ── What this mouse turned out to be ──────────────────────────────────

    # (cid, mid) once answered, as an instance attribute: a mouse cannot
    # change which model it is, and a read that fails later must not undo
    # one that worked.  Class-level default so every subclass starts there
    # without an __init__ of its own; see nordic._model_mid for the same
    # rule on the Cfg.ini family.
    _cl_ids = None

    def __init__(self):
        super().__init__()
        # "Which mouse is here" is a question for the shared dongle alone.
        # cli.py, gui.py and find_sibling() all gate it on hasattr(), so as
        # a class attribute it would reach every subclass - the Areson and
        # Noctua mice, the cabled drivers - and make them answer a question
        # only a receiver with a mouse on the other end can.  Bound onto the
        # instance instead, and only for this dongle's own ID: an instance
        # attribute the class never defines, so the inherited tree is
        # exactly as it was.
        if self._is_shared_dongle():
            self.get_connected_mouse = self._connected_mouse

    def _is_shared_dongle(self) -> bool:
        """Whether this object is the receiver rather than a mouse."""
        return bool(self.capabilities.wireless
                    and set(self.capabilities.vid_pid_pairs)
                    & CRAZYLIGHT_DONGLE_PIDS)

    def open(self) -> None:
        super().open()
        if self._is_shared_dongle():
            # The dongle may have been re-paired, which is the same reason
            # xlite_v4_wireless.open() clears its _lod_tenths.  A mouse
            # cannot change which model it is, so the cabled drivers keep
            # their one ask per object; a receiver's answer is pairing
            # memory and names a different mouse after a re-pair, so
            # remembering it for the life of the object would leave the
            # GUI's long-lived dongle - and the _CARRIED_CACHE that
            # remember_connected_mouse keeps feeding - naming the old
            # mouse forever.
            self._cl_ids = None

    def _crazylight_ids(self) -> tuple[int, int] | None:
        """The (cid, mid) this mouse answered the identity query with.

        Asked once per driver object, remembered after that; None while it
        has not answered - which includes "asleep right now", so the next
        ask is free to try again rather than freezing a failure in.
        """
        if self._cl_ids is None:
            try:
                self._cl_ids = self.get_model_ids()
            except Exception:
                return None
        return self._cl_ids

    def _is_crazylight(self) -> bool:
        """Whether these capabilities are one of the CrazyLight IDs."""
        return bool(set(self.capabilities.vid_pid_pairs) & CRAZYLIGHT_PIDS)

    def refine_capabilities(self) -> bool:
        """Name the CrazyLight this is, from what it answers 0x01 with.

        Replaces the Cfg.ini refine for this family (see CRAZYLIGHT_MODELS
        for why the two tables must not meet): a pair the table does not
        know keeps the class description, because an answer nobody has
        decoded is not evidence for a different name, and after one change
        this returns False as the GUI's rebuild rule needs.

        The Areson and Noctua drivers inherit this method but not these
        PIDs, so theirs goes on being the Nordic family's business,
        unchanged.
        """
        if not self._is_crazylight():
            return super().refine_capabilities()
        ids = self._crazylight_ids()
        model = CRAZYLIGHT_MODELS.get(ids) if ids else None
        if model is None:
            return False
        link = 'Wireless' if self.capabilities.wireless else 'Wired'
        return self._narrow_capabilities(name=f'{model[0]} {link}')

    def model_name(self):
        """What the identity query says this CrazyLight is, or None."""
        if not self._is_crazylight():
            return super().model_name()
        model = CRAZYLIGHT_MODELS.get(self._crazylight_ids() or ())
        return model[0] if model else None

    def _connected_mouse(self) -> dict:
        """{'vid', 'pid', 'name'} of the mouse behind this dongle.

        The 5406 is one receiver for the whole CrazyLight line, so as on
        the Fusion2 dongles the only way to know which mouse is on it is to
        ask - and the mouse answers its identity through the dongle,
        which CRAZYLIGHT_MODELS then maps to the IDs it enumerates with on
        a cable, the pair find_sibling() matches a cabled driver on.

        Raises when the mouse will not say (asleep, out of range) or says
        something this repo has not decoded; find_sibling() and the CLI
        both read that as "cannot tell", never as a guess.
        """
        ids = self._crazylight_ids()
        model = CRAZYLIGHT_MODELS.get(ids) if ids else None
        if model is None:
            raise IOError('this dongle would not say which mouse it carries')
        name, pid = model
        return {'vid': 0x3710, 'pid': pid, 'name': f'{name} Wired'}
