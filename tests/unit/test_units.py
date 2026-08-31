"""Reference values are hand-computed. Do not regenerate them from the code.

Grouped into one class per quantity. The three round-trip tests were `for`
loops over a tuple of values; a failure named the test and not the value that
broke it, which is the one thing a round-trip test has to tell you. They are
parametrised now, so the case appears in the test id.
"""

from collections import Counter

import pytest

from infrahub_demo_otn import units
from infrahub_demo_otn.units import (
    C_M_PER_S,
    CBAND_EXTENT_MHZ,
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    CWDM_CHANNEL_COUNT,
    CWDM_FIRST_WAVELENGTH_NM,
    CWDM_SPACING_NM,
    FS_PER_PS,
    GRID_CHANNEL_COUNT,
    GRID_FIRST_CHANNEL_MHZ,
    GRID_SPACING_MHZ,
    GROUP_INDEX_G652_MILLI,
    GUARD_BAND_MHZ,
    M_PER_KM,
    MDB_PER_DB,
    MHZ_PER_THZ,
    SPECTRAL_ROLLOFF_MILLI,
    anchor_fits_band,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    cwdm_index_to_wavelength_nm,
    db_to_mdb,
    frequency_mhz_to_channel,
    fs_per_nm_to_ps_per_nm,
    km_to_m,
    m_to_km,
    mdb_to_db,
    mhz_to_ghz,
    mhz_to_thz,
    occupied_width_mhz,
    propagation_delay_ns,
    ps_per_nm_to_fs_per_nm,
    thz_to_mhz,
    wavelength_nm_to_band,
)


def test_scale_constants() -> None:
    assert MDB_PER_DB == 1000
    assert M_PER_KM == 1000
    assert MHZ_PER_THZ == 1_000_000
    assert FS_PER_PS == 1000


class TestDecibels:
    @pytest.mark.parametrize(
        ("db", "mdb"),
        [
            (0.21, 210),  # G.652 attenuation coefficient
            (0.0, 0),
            (24.5, 24500),  # 400G DP-16QAM required OSNR
            (-3.5, -3500),  # a loss expressed as negative gain
            (5.0, 5000),  # EDFA noise figure
        ],
    )
    def test_db_to_mdb(self, db: float, mdb: int) -> None:
        assert db_to_mdb(db) == mdb

    @pytest.mark.parametrize(
        ("db", "mdb"),
        [
            # Python's round() gives 0 for the first and 2 for the second.
            (0.0005, 1),
            (0.0015, 2),
            (-0.0005, -1),  # away from zero, not towards it
        ],
    )
    def test_rounding_is_half_away_from_zero_not_bankers(self, db: float, mdb: int) -> None:
        assert db_to_mdb(db) == mdb

    @pytest.mark.parametrize("mdb", [0, 1, 210, 24500, -3500])
    def test_round_trip(self, mdb: int) -> None:
        assert db_to_mdb(mdb_to_db(mdb)) == mdb


class TestLength:
    @pytest.mark.parametrize(
        ("km", "metres"),
        [(452.0, 452_000), (0.5, 500), (1250.0, 1_250_000)],
    )
    def test_km_to_m(self, km: float, metres: int) -> None:
        assert km_to_m(km) == metres

    @pytest.mark.parametrize("metres", [500, 452_000, 1_250_000])
    def test_round_trip(self, metres: int) -> None:
        assert km_to_m(m_to_km(metres)) == metres


class TestFrequency:
    @pytest.mark.parametrize(
        ("thz", "mhz"),
        [(193.7, 193_700_000), (191.35, 191_350_000), (196.1, 196_100_000)],
    )
    def test_thz_to_mhz(self, thz: float, mhz: int) -> None:
        assert thz_to_mhz(thz) == mhz

    @pytest.mark.parametrize("mhz", [191_350_000, 193_700_000, 196_100_000])
    def test_round_trip(self, mhz: int) -> None:
        assert thz_to_mhz(mhz_to_thz(mhz)) == mhz

    @pytest.mark.parametrize(
        ("mhz", "ghz"),
        [
            # The three seeded widths, and the band they are placed in. A width
            # is read in gigahertz and a centre frequency in terahertz, which is
            # why this scale exists beside the one above rather than instead of it.
            (44_400, 44.4),
            (79_600, 79.6),
            (150_000, 150.0),
            (4_800_000, 4800.0),
        ],
    )
    def test_mhz_to_ghz(self, mhz: int, ghz: float) -> None:
        assert mhz_to_ghz(mhz) == pytest.approx(ghz)


class TestDispersion:
    def test_scaling(self) -> None:
        # G.652 coefficient, 17 ps/nm/km
        assert ps_per_nm_to_fs_per_nm(17.0) == 17_000
        # Paris to Madrid accumulated, 1250 km at 17 ps/nm/km
        assert ps_per_nm_to_fs_per_nm(21_250.0) == 21_250_000
        assert fs_per_nm_to_ps_per_nm(21_250_000) == pytest.approx(21_250.0)


class TestChannelGrid:
    def test_constants_match_itu_g694_1(self) -> None:
        assert GRID_FIRST_CHANNEL_MHZ == 191_350_000
        assert GRID_SPACING_MHZ == 50_000
        assert GRID_CHANNEL_COUNT == 96

    @pytest.mark.parametrize(
        ("channel", "mhz"),
        [
            (1, 191_350_000),
            (48, 193_700_000),  # 191.35 + 47 * 0.05 = 193.70 THz
            (96, 196_100_000),  # 191.35 + 95 * 0.05 = 196.10 THz
        ],
    )
    def test_channel_to_frequency(self, channel: int, mhz: int) -> None:
        assert channel_to_frequency_mhz(channel) == mhz

    def test_the_grid_spans_the_extended_c_band(self) -> None:
        # Last channel must land exactly on 196.10 THz, not one step past it.
        assert channel_to_frequency_mhz(GRID_CHANNEL_COUNT) == 196_100_000

    @pytest.mark.parametrize("channel", [0, -1, 97, 1000])
    def test_a_channel_outside_the_grid_raises(self, channel: int) -> None:
        with pytest.raises(ValueError, match="outside the 96-channel"):
            channel_to_frequency_mhz(channel)

    @pytest.mark.parametrize("channel", range(1, GRID_CHANNEL_COUNT + 1))
    def test_round_trip(self, channel: int) -> None:
        assert frequency_mhz_to_channel(channel_to_frequency_mhz(channel)) == channel

    def test_a_frequency_off_the_grid_raises(self) -> None:
        with pytest.raises(ValueError, match="not on the 50 GHz grid"):
            frequency_mhz_to_channel(193_712_345)

    def test_a_frequency_outside_the_band_raises(self) -> None:
        with pytest.raises(ValueError, match="outside the 96-channel"):
            frequency_mhz_to_channel(200_000_000)


class TestOccupiedWidth:
    """The width every capacity claim in the model rests on.

    Reference values are hand-computed from `baud x 1.1 + 9.2 GHz`, per mode, and
    not regenerated from the code. That is the whole point of them: a change to the
    roll-off or the guard has to break a named case here rather than pass and
    quietly move what fits on a section.
    """

    def test_the_constants_are_the_fitted_pair(self) -> None:
        assert SPECTRAL_ROLLOFF_MILLI == 100  # root-raised-cosine 0.1
        assert GUARD_BAND_MHZ == 9_200  # fitted, not measured

    @pytest.mark.parametrize(
        ("mode", "baud_mbaud", "width_mhz"),
        [
            # Every mode in objects/03_optical_modes.yml. Ten modes over six
            # distinct symbol rates, because the three OpenZR+ rates and the two
            # 32 GBd modes share a width and still deserve their own case: a mode
            # named here cannot be re-rated without this file noticing.
            ("DP-QPSK 32GBd 100G", 32_000, 44_400),
            ("DP-16QAM 32GBd 200G", 32_000, 44_400),
            ("400ZR", 59_840, 75_024),
            ("OpenZR+ 400G", 60_140, 75_354),
            ("OpenZR+ 300G", 60_140, 75_354),
            ("OpenZR+ 200G", 60_140, 75_354),
            ("DP-16QAM 64GBd 400G", 64_000, 79_600),
            ("DP-64QAM 64GBd 600G", 64_000, 79_600),
            ("800ZR", 118_000, 139_000),
            ("DP-QPSK 128GBd 400G", 128_000, 150_000),
        ],
    )
    def test_seeded_mode_widths(self, mode: str, baud_mbaud: int, width_mhz: int) -> None:
        assert occupied_width_mhz(baud_mbaud) == width_mhz

    def test_128_gbd_lands_on_exactly_150_ghz(self) -> None:
        """The guard's most sensitive consequence, asserted rather than described.

        128 GBd sits precisely on 150.000 GHz, so it moves in either direction
        under any change to the guard. A 10 GHz guard would put it at 150.8 GHz
        and change what fits on a section.
        """
        assert occupied_width_mhz(128_000) == 150_000
        assert 128_000 * 11 // 10 + 10_000 == 150_800, "a 10 GHz guard is the counterfactual"

    def test_the_widest_seeded_mode_is_three_fixed_channels(self) -> None:
        """150 GHz is exactly three 50 GHz channels. Nothing in the model rounds
        it up to four, and a plan that assumed four would waste 50 GHz a carrier."""
        assert occupied_width_mhz(128_000) == 3 * GRID_SPACING_MHZ

    @pytest.mark.parametrize("baud_mbaud", [0, -1, -32_000])
    def test_a_non_positive_symbol_rate_raises(self, baud_mbaud: int) -> None:
        """Fail closed. A mode with no usable symbol rate has no width, and a
        zero would report a carrier that occupies the guard band and nothing else."""
        with pytest.raises(ValueError, match="must be positive"):
            occupied_width_mhz(baud_mbaud)


class TestCarrierInterval:
    """Half-open intervals, and where the odd megahertz goes."""

    def test_an_even_width_is_symmetric(self) -> None:
        """32 GBd is 44400 MHz, so 22200 either side of the anchor centre."""
        center = channel_to_frequency_mhz(20)
        assert carrier_interval_mhz(center, 32_000) == (center - 22_200, center + 22_200)

    def test_an_odd_width_puts_the_extra_megahertz_on_the_upper_side(self) -> None:
        """32001 MBd is not a catalogue rate. It is here because it is the cheapest
        way to pin the tie-break: 44401 MHz cannot be split evenly, and two callers
        that disagree about which side gets the megahertz disagree about overlap.
        """
        assert occupied_width_mhz(32_001) == 44_401
        center = channel_to_frequency_mhz(20)
        lower, upper = carrier_interval_mhz(center, 32_001)
        assert (lower, upper) == (center - 22_200, center + 22_201)
        assert upper - lower == 44_401

    def test_intervals_that_meet_at_one_frequency_do_not_overlap(self) -> None:
        """The half-open decision, stated as arithmetic rather than described.

        Two carriers placed exactly one width apart touch and do not overlap. With
        closed intervals this pair would report a collision on its own boundary,
        which is the false positive every densely packed plan would hit.
        """
        lower_a, upper_a = carrier_interval_mhz(channel_to_frequency_mhz(20), 32_001)
        lower_b, _ = carrier_interval_mhz(channel_to_frequency_mhz(20) + 44_401, 32_001)
        assert upper_a == lower_b
        assert not (lower_a < lower_b < upper_a)

    def test_the_band_edges_are_half_a_channel_outside_the_end_centres(self) -> None:
        assert CBAND_LOWER_EDGE_MHZ == GRID_FIRST_CHANNEL_MHZ - GRID_SPACING_MHZ // 2
        assert CBAND_UPPER_EDGE_MHZ == channel_to_frequency_mhz(GRID_CHANNEL_COUNT) + GRID_SPACING_MHZ // 2
        assert CBAND_EXTENT_MHZ == CBAND_UPPER_EDGE_MHZ - CBAND_LOWER_EDGE_MHZ
        assert CBAND_EXTENT_MHZ == GRID_CHANNEL_COUNT * GRID_SPACING_MHZ

    def test_the_extent_is_edge_to_edge_not_centre_to_centre(self) -> None:
        """4800 GHz, not 4750. The difference is one 50 GHz channel of capacity,
        and spending it is invisible until a plan is one carrier short."""
        centre_to_centre = channel_to_frequency_mhz(GRID_CHANNEL_COUNT) - GRID_FIRST_CHANNEL_MHZ
        assert centre_to_centre == 4_750_000
        assert CBAND_EXTENT_MHZ - centre_to_centre == GRID_SPACING_MHZ

    def test_a_narrow_carrier_fits_on_both_end_channels(self) -> None:
        """32 GBd on channel 1 and channel 96, both inside the band with margin."""
        assert carrier_interval_mhz(channel_to_frequency_mhz(1), 32_000) == (191_327_800, 191_372_200)
        assert carrier_interval_mhz(channel_to_frequency_mhz(96), 32_000) == (196_077_800, 196_122_200)
        assert anchor_fits_band(channel_to_frequency_mhz(1), 32_000)
        assert anchor_fits_band(channel_to_frequency_mhz(96), 32_000)

    @pytest.mark.parametrize("channel", [2, 95])
    def test_the_widest_mode_fits_flush_against_an_edge(self, channel: int) -> None:
        """Channel 2 puts a 150 GHz carrier exactly on the lower edge and channel
        95 exactly on the upper one. An interval that ends on an edge fits: the
        band is what the line system passes, and touching it uses nothing beyond."""
        lower, upper = carrier_interval_mhz(channel_to_frequency_mhz(channel), 128_000)
        assert lower == CBAND_LOWER_EDGE_MHZ or upper == CBAND_UPPER_EDGE_MHZ
        assert anchor_fits_band(channel_to_frequency_mhz(channel), 128_000)

    @pytest.mark.parametrize("channel", [1, 96])
    def test_the_widest_mode_does_not_fit_on_the_end_channels(self, channel: int) -> None:
        """The negative result the re-seed has to respect. A 128 GBd carrier needs
        75 GHz either side and channels 1 and 96 have only 25 GHz beyond them, so
        two of the ninety-six anchors are unusable for the widest seeded mode."""
        assert not anchor_fits_band(channel_to_frequency_mhz(channel), 128_000)

    def test_the_usable_anchor_range_narrows_with_the_mode(self) -> None:
        """Derived here rather than discovered by the dataset script through trial.

        Only the 100G mode can anchor on all ninety-six. Both 64 GBd and 128 GBd
        lose channels 1 and 96, and they lose them for different amounts of
        overhang: 14.8 GHz against 50 GHz.
        """
        usable = {
            baud: tuple(
                c for c in range(1, GRID_CHANNEL_COUNT + 1) if anchor_fits_band(channel_to_frequency_mhz(c), baud)
            )
            for baud in (32_000, 64_000, 128_000)
        }
        assert usable[32_000] == tuple(range(1, 97))
        assert usable[64_000] == tuple(range(2, 96))
        assert usable[128_000] == tuple(range(2, 96))


class TestCwdmPlan:
    """ITU-T G.694.2, the coarse plan. Eighteen wavelengths and nothing else.

    The band split is computed from the edges rather than listed. The whole
    placement argument for the CWDM tail rests on exactly two of the eighteen
    reaching the erbium window, and a listed pair would assert a transcription
    instead of the arithmetic.
    """

    def test_constants_match_itu_g694_2(self) -> None:
        assert CWDM_FIRST_WAVELENGTH_NM == 1271
        assert CWDM_SPACING_NM == 20
        assert CWDM_CHANNEL_COUNT == 18

    @pytest.mark.parametrize(
        ("index", "nm"),
        [
            (1, 1271),
            (11, 1471),  # 1271 + 10 * 20, the first wavelength the tail lights
            (18, 1611),  # 1271 + 17 * 20, the last of the plan
        ],
    )
    def test_index_to_wavelength(self, index: int, nm: int) -> None:
        assert cwdm_index_to_wavelength_nm(index) == nm

    def test_the_plan_is_eighteen_wavelengths_at_twenty_nanometre_spacing(self) -> None:
        wavelengths = [cwdm_index_to_wavelength_nm(index) for index in range(1, CWDM_CHANNEL_COUNT + 1)]
        assert wavelengths == list(range(1271, 1611 + 1, 20))
        assert len(wavelengths) == 18

    @pytest.mark.parametrize("index", [0, -1, 19, 1000])
    def test_an_index_outside_the_plan_raises(self, index: int) -> None:
        with pytest.raises(ValueError, match="outside the 18-wavelength"):
            cwdm_index_to_wavelength_nm(index)

    def test_the_band_split_is_five_five_three_two_three(self) -> None:
        """Computed over the edges. 1260 to 1360 O, 1360 to 1460 E, 1460 to 1530
        S, 1530 to 1565 C, 1565 to 1625 L, each half-open at the top."""
        split = Counter(
            wavelength_nm_to_band(cwdm_index_to_wavelength_nm(index)) for index in range(1, CWDM_CHANNEL_COUNT + 1)
        )
        assert split == {"o": 5, "e": 5, "s": 3, "c": 2, "l": 3}
        assert sum(split.values()) == CWDM_CHANNEL_COUNT

    def test_the_two_c_band_wavelengths_are_1531_and_1551(self) -> None:
        """The only two of the eighteen an erbium amplifier could reach. Both
        sit off the 50 GHz grid, so neither is writable as a DWDM channel."""
        c_band = [
            cwdm_index_to_wavelength_nm(index)
            for index in range(1, CWDM_CHANNEL_COUNT + 1)
            if wavelength_nm_to_band(cwdm_index_to_wavelength_nm(index)) == "c"
        ]
        assert c_band == [1531, 1551]

    @pytest.mark.parametrize(
        ("nm", "band"),
        [
            (1260, "o"),  # the lower edge is inclusive
            (1359, "o"),
            (1360, "e"),  # the upper edge belongs to the next band
            (1529, "s"),
            (1530, "c"),
            (1564, "c"),
            (1565, "l"),
            (1624, "l"),
        ],
    )
    def test_the_band_edges_are_half_open(self, nm: int, band: str) -> None:
        assert wavelength_nm_to_band(nm) == band

    @pytest.mark.parametrize("nm", [0, 1259, 1625, 2000])
    def test_a_wavelength_outside_the_itu_bands_raises(self, nm: int) -> None:
        with pytest.raises(ValueError, match="outside the ITU bands"):
            wavelength_nm_to_band(nm)

    def test_the_plan_carries_no_frequency_conversion(self) -> None:
        """A deliberate absence, so a reader who reaches for one finds this test.

        c over 1271 nm is 235,871,328.09 MHz. It is not a whole megahertz and it
        is on no grid this repository models, so the only honest result of the
        function is a rounded number nothing reads.
        """
        assert not [name for name in dir(units) if "wavelength" in name and ("mhz" in name or "thz" in name)]


class TestPropagation:
    def test_physical_constants(self) -> None:
        assert C_M_PER_S == 299_792_458
        # G.652 group index at 1550 nm, 1.468.
        assert GROUP_INDEX_G652_MILLI == 1468

    def test_g652_propagates_at_4897_ns_per_km(self) -> None:
        # The figure the documentation and the AI latency report both publish.
        assert propagation_delay_ns(1_000, GROUP_INDEX_G652_MILLI) == 4897

    @pytest.mark.parametrize(
        ("km", "expected_ns"),
        [
            # Every length below is now produced by the loaded dataset, not only
            # asserted here. tests/unit/test_geant_dataset.py recomputes each one
            # from objects/ by enumerating simple paths over the twenty-one
            # sections, so the two layers cannot disagree.
            (800, 3_917_377),  # Berlin to Amsterdam via Hamburg
            (1010, 4_945_688),  # Berlin to Amsterdam via Frankfurt
            (1220, 5_974_000),  # Berlin to Amsterdam via Prague and Frankfurt
            (780, 3_819_442),  # Frankfurt to Milan direct, the JUPITER-Leonardo pair
            (990, 4_847_754),  # Frankfurt to Milan via Geneva
            (1250, 6_120_901),  # Paris to Madrid, the longest single section
            (2970, 14_543_261),  # Madrid to Warsaw, the longest modelled route
        ],
    )
    def test_modelled_routes(self, km: int, expected_ns: int) -> None:
        assert propagation_delay_ns(km * 1_000, GROUP_INDEX_G652_MILLI) == expected_ns

    def test_the_detour_fits_a_five_millisecond_budget(self) -> None:
        """A 5 ms latency budget does not make the Geneva detour unprovisionable.

        4 847 754 ns is 4.85 ms, inside 5 ms with 152 µs to spare, and nothing
        closes that gap: oFEC adds 9 µs. Four milliseconds is the budget the
        arithmetic supports, and this asserts the boundary so the figure cannot
        drift back to 5 ms.
        """
        direct = propagation_delay_ns(780_000, GROUP_INDEX_G652_MILLI)
        detour = propagation_delay_ns(990_000, GROUP_INDEX_G652_MILLI)
        assert detour < 5_000_000, "the old 5 ms claim was arithmetically false"
        assert direct < 4_000_000 < detour, "4 ms is what separates the two routes"

    def test_zero_length_is_zero_delay(self) -> None:
        assert propagation_delay_ns(0, GROUP_INDEX_G652_MILLI) == 0

    def test_a_negative_length_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            propagation_delay_ns(-1, GROUP_INDEX_G652_MILLI)
