"""Check optional encoded strobes without changing the physical interface."""

import csv
import subprocess
from itertools import combinations
from pathlib import Path

import pytest

from fabulous.custom_exception import InvalidFabricParameter
from fabulous.fabric_definition.fabric import Fabric
from fabulous.fabric_definition.frame_strobe import FrameStrobeEncoding
from fabulous.fabric_generator.code_generator.code_generator_Verilog import (
    VerilogCodeGenerator,
)
from fabulous.fabric_generator.code_generator.code_generator_VHDL import (
    VHDLCodeGenerator,
)
from fabulous.fabric_generator.gen_fabric.gen_configmem import generateConfigMem
from fabulous.fabric_generator.parser.parse_csv import parseFabricCSV
from fabulous.fabulous_api import FABulous_API
from fabulous.fabulous_settings import get_context, init_context


@pytest.mark.parametrize(
    ("q", "n", "frames"),
    [(1, 20, 20), (2, 5, 25), (2, 6, 29), (2, 7, 34), (3, 6, 34), (2, 20, 190)],
)
def test_encoding_masks(q: int, n: int, frames: int) -> None:
    """Every valid command selects exactly one frame and avoids address fields."""
    masks = FrameStrobeEncoding(q=q, n=n).masks()
    assert len(masks) == len(set(masks)) == frames
    assert masks[: 20 - n] == tuple(1 << i for i in range(20 - n))
    for mask in masks:
        assert 0 < mask < 1 << 20
        assert sum((mask & other) == other for other in masks) == 1
    assert not any((0 & mask) == mask for mask in masks)


@pytest.mark.parametrize(
    ("q", "n"), [(0, 6), (7, 6), (2, 21), (10, 20), (-1, 6), (True, 6)]
)
def test_invalid_encoding(q: int, n: int) -> None:
    """Reject invalid values before allocating a combinatorial mask table."""
    with pytest.raises(ValueError, match="FrameStrobeEncoding"):
        FrameStrobeEncoding(q=q, n=n)


@pytest.mark.parametrize("setting", [None, "q_of_n,1,20", "q_of_n,2,6"])
def test_csv_encoding(tmp_path: Path, setting: str | None) -> None:
    """The new option changes storage capacity, never physical bus dimensions."""
    path = tmp_path / "fabric.csv"
    option = "" if setting is None else f"FrameStrobeEncoding,{setting}\n"
    path.write_text(
        "FabricBegin\nNULL\nFabricEnd\nParametersBegin\n" + option + "ParametersEnd\n"
    )
    fabric = parseFabricCSV(str(path))
    assert fabric.maxFramesPerCol == 20
    assert fabric.frameBitsPerRow == 32
    assert fabric.desync_flag == 20
    assert fabric.logical_frames_per_col == (29 if setting == "q_of_n,2,6" else 20)


@pytest.mark.parametrize(
    "setting",
    [
        "binary,2,6",
        "q_of_n,2",
        "q_of_n,two,6",
        "q_of_n,2,21",
        "q_of_n,10,20",
        "q_of_n,2,6\nFrameStrobeEncoding,q_of_n,2,7",
    ],
)
def test_invalid_csv_encoding(tmp_path: Path, setting: str) -> None:
    """Malformed, duplicate, and excessive settings fail at the CSV boundary."""
    path = tmp_path / "fabric.csv"
    path.write_text(
        f"FabricBegin\nNULL\nFabricEnd\nParametersBegin\nFrameStrobeEncoding,{setting}\nParametersEnd\n"
    )
    with pytest.raises(InvalidFabricParameter, match="FrameStrobeEncoding"):
        parseFabricCSV(str(path))


@pytest.mark.parametrize(
    ("writer_type", "suffix"),
    [(VerilogCodeGenerator, ".v"), (VHDLCodeGenerator, ".vhdl")],
)
def test_default_rtl_is_unchanged(
    tmp_path: Path, writer_type: type, suffix: str
) -> None:
    """Implicit direct strobes and explicit 1-of-20 produce identical RTL/CSV."""
    outputs = []
    for index, encoding in enumerate((None, FrameStrobeEncoding())):
        writer = writer_type()
        writer.outFileName = tmp_path / f"memory{index}{suffix}"
        mapping = tmp_path / f"memory{index}.csv"
        generateConfigMem(writer, "TEST", 616, mapping, frame_strobe_encoding=encoding)
        outputs.append((writer.outFileName.read_text(), mapping.read_text()))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    ("writer_type", "suffix"),
    [(VerilogCodeGenerator, ".v"), (VHDLCodeGenerator, ".vhdl")],
)
def test_encoded_configmem_capacity(
    tmp_path: Path, writer_type: type, suffix: str
) -> None:
    """The 890-bit tile fits; unused frames need no decoder gates."""
    writer = writer_type()
    writer.outFileName = tmp_path / f"memory{suffix}"
    mapping = tmp_path / "memory.csv"
    encoding = FrameStrobeEncoding(q=2, n=6)
    generateConfigMem(writer, "TEST", 890, mapping, frame_strobe_encoding=encoding)
    with mapping.open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 29
    assert sum(int(row["bits_used_in_frame"]) for row in rows) == 890
    rtl = writer.outFileName.read_text()
    assert "DecodedFrameStrobe_27" in rtl
    assert "DecodedFrameStrobe_28" not in rtl
    with pytest.raises(ValueError, match="exceeds fabric capacity"):
        generateConfigMem(
            writer,
            "TEST",
            929,
            tmp_path / "too_large.csv",
            frame_strobe_encoding=encoding,
        )


@pytest.mark.parametrize("bad_index", [-1, 29, 0])
def test_invalid_frame_indices(tmp_path: Path, bad_index: int) -> None:
    """A malformed CSV must never silently select another encoded frame."""
    writer = VerilogCodeGenerator()
    writer.outFileName = tmp_path / "memory.v"
    mapping = tmp_path / "memory.csv"
    encoding = FrameStrobeEncoding(q=2, n=6)
    generateConfigMem(writer, "TEST", 890, mapping, frame_strobe_encoding=encoding)
    mapping.write_text(
        mapping.read_text().replace("frame28,28,", f"frame28,{bad_index},")
    )
    with pytest.raises(ValueError, match="frame indices"):
        generateConfigMem(writer, "TEST", 890, mapping, frame_strobe_encoding=encoding)


def test_physical_limits_remain(tmp_path: Path) -> None:
    """Encoding must not relax physical address-word restrictions."""
    with pytest.raises(ValueError, match="maxFramesPerCol must be 20"):
        Fabric(fabric_dir=tmp_path, maxFramesPerCol=29)


def test_encoded_project_generation(project: Path) -> None:
    """The public API carries encoding through memories, tiles and supertiles."""
    config = project / "fabric.csv"
    config.write_text(
        config.read_text().replace(
            "ParametersEnd", "FrameStrobeEncoding,q_of_n,2,6\nParametersEnd"
        )
    )
    init_context(project)
    api = FABulous_API(VerilogCodeGenerator())
    api.loadFabric(config)
    for tile in api.fabric.tileDic.values():
        directory = tile.tileDir.parent
        mapping = directory / f"{tile.name}_ConfigMem.csv"
        # This fixture is a newly created project; regenerate its default maps.
        mapping.unlink(missing_ok=True)
        api.setWriterOutputFile(directory / f"{tile.name}_ConfigMem.v")
        api.genConfigMem(tile.name, mapping)
        api.setWriterOutputFile(directory / f"{tile.name}_switch_matrix.v")
        api.genSwitchMatrix(tile.name)
        rtl_path = directory / f"{tile.name}.v"
        api.setWriterOutputFile(rtl_path)
        api.genTile(tile.name)
        rtl = rtl_path.read_text()
        assert "parameter MaxFramesPerCol=20" in rtl
        assert "[927:0]" in rtl
    for tile in api.fabric.superTileDic.values():
        directory = tile.tileDir.parent
        api.setWriterOutputFile(directory / f"{tile.name}_ConfigMem.v")
        api.gen_super_tile_config_mem(tile.name)
        api.setWriterOutputFile(directory / f"{tile.name}.v")
        api.genSuperTile(tile.name)
        assert "[927:0]" in (directory / f"{tile.name}.v").read_text()
    spec = api.genBitStreamSpec()
    assert spec["ArchSpecs"]["MaxFramesPerCol"] == 20
    assert len(spec["ArchSpecs"]["FrameStrobeMasks"]) == 29


@pytest.mark.slow
def test_vhdl_decoder_simulation(tmp_path: Path) -> None:
    """Exercise all decoded strobes and incomplete selections in VHDL."""
    writer = VHDLCodeGenerator()
    writer.outFileName = tmp_path / "TEST_ConfigMem.vhd"
    generateConfigMem(
        writer,
        "TEST",
        928,
        tmp_path / "memory.csv",
        frame_strobe_encoding=FrameStrobeEncoding(q=2, n=6),
    )
    masks = [1 << i for i in range(14)] + [
        (1 << a) | (1 << b) for a, b in combinations(range(14, 20), 2)
    ]
    hdl = [
        """library ieee;
use ieee.std_logic_1164.all;
entity tb is end;
architecture test of tb is
signal data : std_logic_vector(31 downto 0) := (others => '0');
signal strobe : std_logic_vector(19 downto 0) := (others => '0');
signal bits, bits_n : std_logic_vector(927 downto 0);
begin
memory: entity work.TEST_ConfigMem port map(data, strobe, bits, bits_n);
process begin
"""
    ]
    for mask in masks:
        hdl.append(f'strobe <= x"{mask:05x}"; wait for 10 ns;\n')
    hdl.append("strobe <= (others => '0'); wait for 10 ns;\n")
    expected = 0
    for phase in range(2):
        for frame, mask in enumerate(masks):
            data = (frame + 1) * 0x01020304 ^ (0xFFFFFFFF if phase else 0)
            shift = (28 - frame) * 32
            expected = (expected & ~(0xFFFFFFFF << shift)) | (data << shift)
            hdl.append(f'data <= x"{data:08x}"; wait for 10 ns;\n')
            hdl.append(f'strobe <= x"{mask:05x}"; wait for 10 ns;\n')
            hdl.append(
                f'assert bits = x"{expected:0232x}" '
                f'report "frame {frame}" severity failure;\n'
            )
            hdl.append("assert bits_n = not bits severity failure;\n")
            hdl.append("strobe <= (others => '0'); wait for 10 ns;\n")
        for bit in range(14, 20):
            hdl.append(
                f'data <= x"deadbeef"; strobe <= x"{1 << bit:05x}"; wait for 10 ns;\n'
            )
            hdl.append(
                f'assert bits = x"{expected:0232x}" '
                'report "incomplete selection" severity failure;\n'
            )
            hdl.append("strobe <= (others => '0'); wait for 10 ns;\n")
    hdl.append(
        'report "PASS: decoded frames"; std.env.finish; wait; end process; end;\n'
    )
    tb = tmp_path / "tb.vhd"
    tb.write_text("".join(hdl))
    sources = [
        Path(__file__).parents[1] / "testdata/models.vhd",
        writer.outFileName,
        tb,
    ]
    ghdl = str(get_context().ghdl_path)
    for args in (
        ["-a", "--std=08", *map(str, sources)],
        ["-e", "--std=08", "tb"],
        ["-r", "--std=08", "tb", "--assert-level=error"],
    ):
        result = subprocess.run(
            [ghdl, *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert "PASS: decoded frames" in result.stdout
