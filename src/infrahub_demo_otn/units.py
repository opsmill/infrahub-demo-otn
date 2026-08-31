"""Fixed-point scale factors and conversions.

Infrahub has no Float attribute kind. Number is an integer, and JSON is not
filterable, sortable, or usable in computed attributes. Every physical
quantity is therefore stored as a scaled integer with the unit in the
attribute name.

This module is the only place a scale factor may appear. Writing `* 1000`
inline anywhere else is a spec violation.

Rounding is half-away-from-zero via Decimal, not Python's built-in round(),
which is banker's rounding and would make db_to_mdb(0.0005) == 0 while
db_to_mdb(0.0015) == 2.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Protocol

MDB_PER_DB = 1000
"""Millidecibels per decibel. 0.001 dB resolution."""

M_PER_KM = 1000
"""Metres per kilometre."""

MHZ_PER_THZ = 1_000_000
"""Megahertz per terahertz."""

MHZ_PER_GHZ = 1000
"""Megahertz per gigahertz.

Its own constant, not `MHZ_PER_THZ // 1000`. A frequency is read in terahertz and
a *width* is read in gigahertz: the grid is spaced at 50 GHz and a carrier occupies
79.6 GHz, so the map panel that prints an occupied width needs this scale and the
report that prints a centre frequency needs the other one.
"""

FS_PER_PS = 1000
"""Femtoseconds per picosecond, for chromatic dispersion."""

KBPS_PER_MBPS = 1000
"""Kilobits per megabit per second. E1 is 2048 kbps, which is 2.048 Mbps."""

NS_PER_US = 1000
"""Nanoseconds per microsecond. Latency is stored in ns and read in us.

Its own constant rather than a reuse of one of the other 1000s. Every scale
factor in this module happens to be 1000 or a power of it, so reusing `M_PER_KM`
to render a delay would pass any check that compares values and would be wrong
about what it was claiming. That is the trap `SUFFIX_DIVISORS` in
`tests/unit/test_schema_contract.py` exists to close, and it applies to Python
as much as to a Jinja2 template.
"""

MICRODEG_PER_DEG = 1_000_000
"""Microdegrees per degree. Site coordinates are stored at 1e-6 degree.

Its own constant rather than a reuse of `MHZ_PER_THZ`, for the reason
`NS_PER_US` gives: every factor in this module is a power of a thousand, so the
wrong one produces the right number and no comparison can catch it. A
microdegree of latitude is about eleven centimetres, which is finer than any
city centre is knowable and coarse enough to stay a whole number.
"""


KBPS_PER_GBPS = 1_000_000
"""Kilobits per gigabit per second. 100GBASE-LR4 is 103100000 kbps, 103.1 Gbps.

The client-signal catalog spans seven orders of magnitude, so its display
template switches between this constant and KBPS_PER_MBPS at one gigabit.
"""


GBPS_PER_TBPS = 1000
"""Gigabits per terabit per second. Aggregate capacity, not a stored attribute.

No schema attribute is in Tbps and none should be: `line_rate_gbps` and
`rate_gbps` are the stored quantities and they are right. This exists because
the answer to "what did that backhoe cost us" is twelve wavelengths at 400G, and
an operator says 4.8 terabits rather than 4800 gigabits. It is a rendering
factor, and it lives here for the same reason every other one does.
"""


MILLI_PER_UNIT = 1000
"""Milli-units per unit. The divisor that turns a `_MILLI` ratio back into a ratio.

`GROUP_INDEX_G652_MILLI` and `SPECTRAL_ROLLOFF_MILLI` are both a plain ratio held
at 0.001 resolution because there is no Float. Reading one back means dividing by
this, and this is its own constant for the reason `NS_PER_US` gives: every factor
in this module is a power of a thousand, so the wrong one yields a plausible
number and no comparison catches it.
"""


def _scale(value: float, factor: int) -> int:
    """Multiply and round half away from zero."""
    return int((Decimal(str(value)) * factor).to_integral_value(rounding=ROUND_HALF_UP))


def db_to_mdb(db: float) -> int:
    """Decibels to millidecibels. 0.21 -> 210."""
    return _scale(db, MDB_PER_DB)


def mdb_to_db(mdb: int) -> float:
    """Millidecibels to decibels. 210 -> 0.21."""
    return mdb / MDB_PER_DB


def km_to_m(km: float) -> int:
    """Kilometres to metres. 452.0 -> 452000."""
    return _scale(km, M_PER_KM)


def m_to_km(m: int) -> float:
    """Metres to kilometres. 452000 -> 452.0."""
    return m / M_PER_KM


def microdeg_to_deg(microdeg: int) -> float:
    """Microdegrees to degrees. 52370000 -> 52.37."""
    return microdeg / MICRODEG_PER_DEG


def ns_to_us(ns: int) -> float:
    """Nanoseconds to microseconds. 3819432 -> 3819.432."""
    return ns / NS_PER_US


def gbps_to_tbps(gbps: int) -> float:
    """Gigabits to terabits per second. 4800 -> 4.8."""
    return gbps / GBPS_PER_TBPS


def thz_to_mhz(thz: float) -> int:
    """Terahertz to megahertz. 193.7 -> 193700000."""
    return _scale(thz, MHZ_PER_THZ)


def mhz_to_thz(mhz: int) -> float:
    """Megahertz to terahertz. 193700000 -> 193.7."""
    return mhz / MHZ_PER_THZ


def mhz_to_ghz(mhz: int) -> float:
    """Megahertz to gigahertz. 79600 -> 79.6.

    The unit an occupied width is read in. A section holding 4,134,400 MHz is a
    section holding 4,134.4 GHz of the 4,800 GHz band, and the second reading is
    the one that fits in a map panel.
    """
    return mhz / MHZ_PER_GHZ


def ps_per_nm_to_fs_per_nm(ps_per_nm: float) -> int:
    """Picoseconds/nm to femtoseconds/nm. 17.0 -> 17000.

    Also used for the per-km coefficient, whose attribute suffix is
    _fs_per_nm_km. The scale factor is identical.
    """
    return _scale(ps_per_nm, FS_PER_PS)


def fs_per_nm_to_ps_per_nm(fs_per_nm: int) -> float:
    """Femtoseconds/nm to picoseconds/nm. 17000 -> 17.0."""
    return fs_per_nm / FS_PER_PS


GRID_FIRST_CHANNEL_MHZ = 191_350_000
"""Channel 1 centre frequency, 191.35 THz. ITU-T G.694.1 extended C-band."""

GRID_SPACING_MHZ = 50_000
"""50 GHz fixed grid. Flexgrid is not modelled."""

GRID_CHANNEL_COUNT = 96
"""Channels 1 to 96 inclusive. Channel 96 is 196.10 THz."""


def channel_to_frequency_mhz(channel: int) -> int:
    """ITU channel number to centre frequency in MHz. Channel 1 -> 191350000."""
    if not 1 <= channel <= GRID_CHANNEL_COUNT:
        raise ValueError(f"Channel {channel} is outside the 96-channel C-band grid (1 to {GRID_CHANNEL_COUNT})")
    return GRID_FIRST_CHANNEL_MHZ + (channel - 1) * GRID_SPACING_MHZ


def frequency_mhz_to_channel(frequency_mhz: int) -> int:
    """Centre frequency in MHz to ITU channel number. 191350000 -> 1."""
    offset = frequency_mhz - GRID_FIRST_CHANNEL_MHZ
    if offset % GRID_SPACING_MHZ != 0:
        raise ValueError(f"{frequency_mhz} MHz is not on the 50 GHz grid anchored at {GRID_FIRST_CHANNEL_MHZ} MHz")
    channel = offset // GRID_SPACING_MHZ + 1
    if not 1 <= channel <= GRID_CHANNEL_COUNT:
        raise ValueError(f"{frequency_mhz} MHz is outside the 96-channel C-band grid")
    return channel


SPECTRAL_ROLLOFF_MILLI = 100
"""Root-raised-cosine roll-off of 0.1, in milli-units.

0.1 is the shaping factor coherent DWDM transponders are specified at, and it is
the figure OpenROADM and current vendor line-card literature use. An integer
because the no-Float rule applies to constants as much as to attributes: divide by
`MILLI_PER_UNIT` to read it back.
"""

GUARD_BAND_MHZ = 9_200
"""Guard band added to the shaped signal, 9.2 GHz.

**This is a fitted modelling choice, not a measured constant, and nothing here
should be read as a citation.** It was fitted to **one** published flexgrid
media-channel width, 150 GHz at 128 GBd, and then checked against a second,
87.5 GHz at 64 GBd. Every other width this module produces is an extrapolation
from that single anchor.

It reproduces the anchor exactly and sits under the check. 128 GBd comes out at
exactly 150.000 GHz. 64 GBd comes out at 79.6 GHz, which fits inside an 87.5 GHz
media channel with 7.9 GHz to spare rather than filling it, so the model is
optimistic about 64 GBd by roughly one tenth of a channel and that is stated here
rather than glossed.

Landing 128 GBd on exactly 150.000 GHz means that mode sits precisely on a
boundary and moves in either direction under any change to the guard. A 10 GHz
guard would put it at 150.8 GHz, which changes what fits on a section.
"""


CBAND_LOWER_EDGE_MHZ = 191_325_000
"""Lower edge of the modelled C-band, 191.325 THz. Half a channel below channel 1."""

CBAND_UPPER_EDGE_MHZ = 196_125_000
"""Upper edge of the modelled C-band, 196.125 THz. Half a channel above channel 96."""

CBAND_EXTENT_MHZ = 4_800_000
"""Modelled C-band extent, 4800 GHz, edge to edge.

**Not 4750 GHz, which is the centre-to-centre span.** The 96 channel centres run
191.35 THz to 196.10 THz, 4750 GHz apart. Ninety-six channels of 50 GHz occupy
4800 GHz of spectrum, because each end channel carries 25 GHz outside its own
centre. Width semantics need the edge figure, and using 4750 where 4800 belongs
silently loses one channel of capacity.

`OtnOpticalPort.center_frequency_mhz` keeps its 191,350,000 to 196,100,000 bounds.
Those are centre-based, they are correct for a port centre, and they are a
different question from this one.
"""


def occupied_width_mhz(baud_mbaud: int) -> int:
    """Occupied spectral width of a carrier in MHz. 64000 -> 79600.

    width = round(baud x (1 + roll-off)) + guard, in integer arithmetic:
        width = (baud_mbaud * (MILLI_PER_UNIT + SPECTRAL_ROLLOFF_MILLI)) / 1000
                rounded half up, + GUARD_BAND_MHZ

    This is the only implementation of the width in the repository. The check, the
    generator, the reports and the dataset script all call it, so they cannot reach
    different answers about whether a plan fits.

    Rounded rather than truncated, though no seeded symbol rate needs it: all ten
    catalogue modes multiply out to a whole megahertz. The rounding is there so a
    rate that does not, such as an odd-numbered MBd figure, does not quietly lose
    a megahertz off the width and under-report what a carrier occupies.

    Nothing is stored. A carrier's width is its mode's symbol rate put through this
    function, one relationship hop away, every time it is needed.
    """
    if baud_mbaud <= 0:
        raise ValueError(f"baud_mbaud must be positive, got {baud_mbaud}")
    numerator = baud_mbaud * (MILLI_PER_UNIT + SPECTRAL_ROLLOFF_MILLI)
    shaped = (numerator + MILLI_PER_UNIT // 2) // MILLI_PER_UNIT
    return shaped + GUARD_BAND_MHZ


def carrier_interval_mhz(center_mhz: int, baud_mbaud: int) -> tuple[int, int]:
    """Half-open spectral interval of a carrier, [lower, upper), in MHz.

    Centred on `center_mhz`, which is the centre frequency of the `OtnFrequencyGrid`
    channel the carrier anchors on. The channel number stays the human-readable
    anchor; this is the spectrum it actually occupies.

    **Half-open by decision.** Two intervals that meet at exactly one frequency do
    not overlap. With closed intervals every densely packed plan would report false
    collisions on its own boundaries.

    **An odd width places the extra megahertz on the upper side.** `lower` is
    `center_mhz - width // 2` and `upper` is `lower + width`, so a 44401 MHz carrier
    reaches 22200 MHz below its centre and 22201 MHz above it. Integer division
    keeps both edges on whole megahertz, and stating the tie-break here is what
    stops two callers disagreeing about a 1 MHz edge.
    """
    width = occupied_width_mhz(baud_mbaud)
    lower = center_mhz - width // 2
    return lower, lower + width


def anchor_fits_band(center_mhz: int, baud_mbaud: int) -> bool:
    """Whether a carrier anchored here fits inside the modelled C-band.

    True when the whole interval lies within [CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ]. An interval that ends exactly on an edge fits: the band
    is the spectrum the line system passes, and touching its boundary uses none of
    what is beyond it.

    The usable anchor range is narrower for a wider mode, and this is where that is
    derived rather than found by trial. The 150,000 MHz carrier, the 128 GBd mode,
    cannot anchor on channel 1 or channel 96: it would need 25 GHz that is not
    there at either end. Channels 2 and 95 fit it exactly, flush against the edge
    with nothing to spare.
    """
    lower, upper = carrier_interval_mhz(center_mhz, baud_mbaud)
    return lower >= CBAND_LOWER_EDGE_MHZ and upper <= CBAND_UPPER_EDGE_MHZ


class SpectralInterval(Protocol):
    """Anything holding a half-open slice of spectrum, `[lower_mhz, upper_mhz)`.

    `plant.CarrierInterval` and `FreeBlock` both satisfy it. A carrier is compared
    against another carrier, and a carrier is compared against a free block, which
    are the same question about the same shape, so they get one protocol rather
    than one each.
    """

    @property
    def lower_mhz(self) -> int: ...

    @property
    def upper_mhz(self) -> int: ...


@dataclass(frozen=True)
class FreeBlock:
    """A contiguous run of unoccupied spectrum on one section, [lower, upper)."""

    lower_mhz: int
    upper_mhz: int

    @property
    def width_mhz(self) -> int:
        return self.upper_mhz - self.lower_mhz


def free_blocks(intervals: Iterable[SpectralInterval]) -> tuple[FreeBlock, ...]:
    """The unoccupied runs of spectrum left by a set of carriers, ascending.

    The C-band extent minus what the carriers hold. **The first and last blocks
    are bounded by the band edges, not by the first and last carrier**, so a
    section carrying one wavelength in the middle reports two free blocks rather
    than none, and an empty section reports one block of the whole band.

    Overlapping intervals merge rather than producing a negative block. A branch
    that has not passed the collision check can hold two carriers on the same
    spectrum, and a free-block report is one of the things an operator reads
    while working out why.

    An interval that runs past a band edge is clipped to the edge here. The
    carrier is unprovisionable and the collision check is what says so; clipping
    only keeps this function from claiming free spectrum outside the band.

    **It lives here rather than beside the GraphQL adapter that feeds it, and the
    reason is a dependency and not a preference.** `routing.py` allocates against
    free spectrum and `plant.py` imports `routing.py`, so the sweep cannot live in
    `plant.py` without a cycle. This module already fixes the band edges the sweep
    subtracts from and the half-open convention it honours, which makes it the one
    place both sides can reach. `plant.py` re-exports the name it used to own.

    `plant.occupancy_from_graphql` already sorts, so the sort below is a no-op on
    its output. It is here so a caller passing a filtered, concatenated or
    hand-built list gets the right answer instead of a plausible one. A route's
    free spectrum is exactly this function over every section's intervals
    concatenated, because a megahertz held on any one section of a route is held
    against the whole of it.
    """
    blocks: list[FreeBlock] = []
    cursor = CBAND_LOWER_EDGE_MHZ
    for interval in sorted(intervals, key=lambda item: (item.lower_mhz, item.upper_mhz)):
        lower = max(interval.lower_mhz, CBAND_LOWER_EDGE_MHZ)
        upper = min(interval.upper_mhz, CBAND_UPPER_EDGE_MHZ)
        if upper <= cursor:
            continue
        if lower > cursor:
            blocks.append(FreeBlock(lower_mhz=cursor, upper_mhz=lower))
        cursor = upper
    if cursor < CBAND_UPPER_EDGE_MHZ:
        blocks.append(FreeBlock(lower_mhz=cursor, upper_mhz=CBAND_UPPER_EDGE_MHZ))
    return tuple(blocks)


CWDM_FIRST_WAVELENGTH_NM = 1271
"""Channel 1 of the ITU-T G.694.2 coarse plan, 1271 nm."""

CWDM_SPACING_NM = 20
"""20 nm spacing. Wide enough that an uncooled laser stays in its slot over
temperature, which is the whole point of the coarse plan."""

CWDM_CHANNEL_COUNT = 18
"""Eighteen wavelengths, 1271 to 1611 nm inclusive."""

CWDM_BAND_EDGES_NM: tuple[tuple[str, int, int], ...] = (
    ("o", 1260, 1360),
    ("e", 1360, 1460),
    ("s", 1460, 1530),
    ("c", 1530, 1565),
    ("l", 1565, 1625),
)
"""The five ITU wavelength bands as half-open [lower, upper) ranges in nm.

The split over the eighteen coarse wavelengths is arithmetic, not a list: 5 in
the O band, 5 in E, 3 in S, 2 in C and 3 in L. The two C-band members are 1531
and 1551, which is the only place the coarse plan and the erbium window meet.

**The coarse plan carries no frequency and no channel number, by design.** Do
not add a wavelength-to-frequency conversion here for symmetry with the DWDM
grid above. c over 1271 nm is 235,871,328.09 MHz: not a whole megahertz, and on
no grid this repository models. `frequency_mhz_to_channel` raises for both
C-band wavelengths, so the overlap opens no writable wrong statement. Rounding
the number to make the function typecheck would produce a value nothing reads
and every reader would believe.
"""


def cwdm_index_to_wavelength_nm(index: int) -> int:
    """Coarse plan index to nominal central wavelength in nm. 1 -> 1271."""
    if not 1 <= index <= CWDM_CHANNEL_COUNT:
        raise ValueError(
            f"Index {index} is outside the {CWDM_CHANNEL_COUNT}-wavelength CWDM plan (1 to {CWDM_CHANNEL_COUNT})"
        )
    return CWDM_FIRST_WAVELENGTH_NM + (index - 1) * CWDM_SPACING_NM


def wavelength_nm_to_band(wavelength_nm: int) -> str:
    """Wavelength in nm to its ITU band letter. 1471 -> 's'."""
    for band, lower, upper in CWDM_BAND_EDGES_NM:
        if lower <= wavelength_nm < upper:
            return band
    lowest = CWDM_BAND_EDGES_NM[0][1]
    highest = CWDM_BAND_EDGES_NM[-1][2]
    raise ValueError(f"{wavelength_nm} nm is outside the ITU bands, {lowest} to {highest} nm")


C_M_PER_S = 299_792_458
"""Speed of light in vacuum, metres per second."""

GROUP_INDEX_G652_MILLI = 1468
"""G.652 group index at 1550 nm, 1.468, in milli-units.

Held here as the default. Each fiber type carries its own value, because
G.654 and G.655 differ and the difference is measurable over 1000 km.
"""


def propagation_delay_ns(length_m: int, group_index_milli: int) -> int:
    """One-way fiber propagation delay in nanoseconds.

    delay_ns = length_m * n / c * 1e9, with n = group_index_milli / 1000.
    Rearranged to stay in integer arithmetic:
        delay_ns = length_m * group_index_milli * 1_000_000 / c

    Rounded, not truncated. 1 km of G.652 is 4896.7 ns; floor division would
    give 4896 and the error compounds over a 15-span path.

    This is the floor under every latency SLA in the model, and at continental
    distances it dominates every other latency term by three orders of
    magnitude.
    """
    if length_m < 0:
        raise ValueError(f"length_m must not be negative, got {length_m}")
    if group_index_milli <= 0:
        raise ValueError(f"group_index_milli must be positive, got {group_index_milli}")
    numerator = length_m * group_index_milli * 1_000_000
    return (numerator + C_M_PER_S // 2) // C_M_PER_S
