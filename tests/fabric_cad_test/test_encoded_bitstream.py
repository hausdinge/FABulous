"""Load real generated bitstreams through the unchanged configuration controller."""

import csv
import pickle
import subprocess
from pathlib import Path

import pytest
from fabulous_bit_gen.bit_gen import gen_bitstream

from fabulous.fabric_cad.gen_bitstream_spec import generateBitstreamSpec
from fabulous.fabric_definition.bel import Bel
from fabulous.fabric_definition.fabric import Fabric
from fabulous.fabric_definition.frame_strobe import FrameStrobeEncoding
from fabulous.fabric_definition.switch_matrix import SwitchMatrix
from fabulous.fabric_definition.tile import Tile
from fabulous.fabric_generator.code_generator.code_generator_Verilog import (
    VerilogCodeGenerator,
)
from fabulous.fabric_generator.gen_fabric.gen_configmem import generateConfigMem
from fabulous.fabulous_settings import get_context
from tests.conftest import VERILOG_SOURCE_PATH


def _memory(root: Path, name: str, bits: int, encoding: FrameStrobeEncoding) -> Tile:
    """Build an actual BEL feature map and its matching generated memory."""
    tile_dir = root / name
    tile_dir.mkdir()
    bel = Bel(
        src=tile_dir / "MEM.v",
        prefix="",
        module_name="MEM",
        internal=[],
        external=[],
        configPort=[],
        sharedPort=[],
        configBit=bits,
        belMap={f"INIT_{i}": {i: {0: 1}} for i in range(bits)},
        userCLK=False,
        ports_vectors={},
        carry={},
        localShared={},
    )
    tile = Tile(
        name=name,
        ports=[],
        bels=[bel],
        tileDir=tile_dir / f"{name}.csv",
        switch_matrix=SwitchMatrix(
            matrix_file=tile_dir / "matrix.list", connections={}
        ),
        gen_ios=[],
        userCLK=False,
        pinOrderConfig={},
    )
    writer = VerilogCodeGenerator()
    writer.outFileName = tile_dir / f"{name}_ConfigMem.v"
    generateConfigMem(
        writer,
        name,
        bits,
        tile_dir / f"{name}_ConfigMem.csv",
        frame_strobe_encoding=encoding,
    )
    return tile


def _frame_config_bits(tile: Tile) -> list[list[int]]:
    """Read which stored bits belong to each frame, independent of address masks."""
    with (tile.tileDir.parent / f"{tile.name}_ConfigMem.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    result = []
    for row in rows:
        if int(row["bits_used_in_frame"]):
            high, low = map(int, row["ConfigBits_ranges"].split(":"))
            result.append(list(range(low, high + 1)))
        else:
            result.append([])
    return result


@pytest.mark.slow
@pytest.mark.parametrize(("q", "n"), [(1, 20), (2, 6), (2, 7), (3, 6)])
def test_binary_configures_rtl(tmp_path: Path, q: int, n: int) -> None:
    """Every packet writes only its frame/column; two passes exercise every bit."""
    encoding = FrameStrobeEncoding(q=q, n=n)
    capacity = len(encoding.masks()) * 32
    sizes = [min(890, capacity), 17, 64, capacity]
    tiles = [
        _memory(tmp_path, f"MEM{i}", bits, encoding) for i, bits in enumerate(sizes)
    ]
    fabric = Fabric(
        fabric_dir=tmp_path,
        tile=[tiles[:2], tiles[2:]],
        numberOfRows=2,
        numberOfColumns=2,
        tileDic={tile.name: tile for tile in tiles},
        frame_strobe_encoding=encoding,
    )
    spec = generateBitstreamSpec(fabric)
    assert spec["ArchSpecs"]["MaxFramesPerCol"] == 20
    spec_path = tmp_path / "spec.bin"
    spec_path.write_bytes(pickle.dumps(spec))
    sources = [tile.tileDir.parent / f"{tile.name}_ConfigMem.v" for tile in tiles]
    hdl = [
        """
module tb;
reg CLK = 0;
always #5 CLK = ~CLK;
reg reset_n = 0, write_strobe = 0;
reg [31:0] write_data = 0;
wire [31:0] address;
wire long_strobe;
wire [4:0] row_select;
ConfigFSM #(.NumberOfRows(2)) controller (
    .CLK(CLK), .reset_n(reset_n), .write_data(write_data),
    .write_strobe(write_strobe), .fsm_reset(1'b0),
    .frame_address_register(address), .long_frame_strobe(long_strobe),
    .row_select(row_select));
task send_word(input [31:0] data);
begin
    @(negedge CLK); write_data = data; write_strobe = 1;
    @(negedge CLK); write_strobe = 0;
    repeat (4) @(negedge CLK);
end
endtask
"""
    ]
    for col in range(2):
        hdl.append(f"""
wire [19:0] strobes{col};
Frame_Select #(.Col({col})) column{col} (
    .FrameStrobe_I(address[19:0]), .FrameStrobe_O(strobes{col}),
    .FrameSelect(address[31:27]), .FrameStrobe(long_strobe));
""")
    for row in range(2):
        hdl.append(f"""
wire [31:0] data{row};
Frame_Data_Reg #(.Row({row + 1})) row{row} (
    .CLK(CLK), .FrameData_I(write_data), .FrameData_O(data{row}),
    .RowSelect(row_select));
""")
        for col in range(2):
            i = row * 2 + col
            hdl.append(f"""
wire [{sizes[i] - 1}:0] bits{i};
MEM{i}_ConfigMem memory{i} (.FrameData(data{row}), .FrameStrobe(strobes{col}),
    .ConfigBits(bits{i}), .ConfigBits_N());
""")
    hdl.append("initial begin\nrepeat (3) @(negedge CLK); reset_n = 1;\n")
    stored = [0] * 4
    frame_bits = [_frame_config_bits(tile) for tile in tiles]

    def check_values() -> None:
        """Check all tiles, including those outside the addressed column."""
        for i, value in enumerate(stored):
            hdl.append(
                f"if (bits{i} !== {sizes[i]}'h{value:x}) "
                f'$fatal(1, "memory {i} mismatch");\n'
            )

    # First initialize every latch; then write complementary patterns so each
    # bit must hold both zero and one and no previous write can hide a failure.
    for phase in range(3):
        desired = [0] * 4
        features = []
        for i, size in enumerate(sizes):
            for bit in range(size):
                value = phase > 0 and (((bit * 7 + i * 3) % 11 < 5) == (phase == 1))
                if value:
                    desired[i] |= 1 << bit
                    features.append(f"X{i % 2}Y{i // 2}.A.INIT_{bit}")
        fasm = tmp_path / f"phase{phase}.fasm"
        fasm.write_text("\n".join(features) + "\n")
        binary = tmp_path / f"phase{phase}.bin"
        gen_bitstream(str(fasm), str(spec_path), str(binary))
        data = binary.read_bytes()
        header = bytes.fromhex(spec["ArchSpecs"]["SyncHeaderHex"])
        assert data.startswith(header)
        assert data[-4:] == (1 << 20).to_bytes(4, "big")
        for offset in range(0, len(header), 4):
            hdl.append(f"send_word(32'h{data[offset : offset + 4].hex()});\n")
        offset = len(header)
        for col in range(2):
            for frame, mask in enumerate(encoding.masks()):
                assert (
                    int.from_bytes(data[offset : offset + 4], "big")
                    == (col << 27) | mask
                )
                for word in range(3):
                    payload = data[offset + 4 * word : offset + 4 * word + 4].hex()
                    hdl.append(f"send_word(32'h{payload});\n")
                offset += 12
                for row in range(2):
                    i = row * 2 + col
                    bits_mask = sum(1 << b for b in frame_bits[i][frame])
                    stored[i] = (stored[i] & ~bits_mask) | (desired[i] & bits_mask)
                if phase:
                    check_values()
        assert offset == len(data) - 4
        assert stored == desired
        check_values()
        # A packet with no active strobes must not write anything.
        hdl.append("send_word(0); send_word(32'hffffffff); send_word(32'hffffffff);\n")
        check_values()
        if q == 1 and phase == 2:
            # Legacy direct strobes still permit a multi-frame write.
            hdl.append(
                "send_word(3); send_word(32'hffffffff); send_word(32'hffffffff);\n"
            )
            for i in (0, 2):
                stored[i] |= sum(
                    1 << bit for frame in (0, 1) for bit in frame_bits[i][frame]
                )
            check_values()
        hdl.append("send_word(32'h00100000);\n")
        # After desynchronization, apparent address/data words are ignored.
        hdl.append("send_word(1); send_word(32'hffffffff); send_word(32'hffffffff);\n")
        check_values()
    hdl.append(
        '$display("PASS: complete encoded bitstream"); $finish; end\nendmodule\n'
    )
    testbench = tmp_path / "tb.v"
    testbench.write_text("".join(hdl))
    controller_dir = VERILOG_SOURCE_PATH / "Fabric"
    sources += [
        controller_dir / f"{name}.v"
        for name in ("models_pack", "ConfigFSM", "Frame_Select", "Frame_Data_Reg")
    ]
    settings = get_context()
    compiled = tmp_path / "sim.vvp"
    subprocess.run(
        [
            str(settings.iverilog_path),
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(compiled),
            *map(str, sources),
            str(testbench),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = subprocess.run(
        [str(settings.vvp_path), str(compiled)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "PASS: complete encoded bitstream" in result.stdout

    # The writer's emulation image must use the same enlarged frame space.
    emulation = ['`include "phase2.vh"\nmodule emulation_tb;\n']
    for i, size in enumerate(sizes):
        emulation.append(f"wire [{size - 1}:0] bits{i};\n")
        emulation.append(
            f"MEM{i}_ConfigMem #(.Emulate_Bitstream("
            f"`Tile_X{i % 2}Y{i // 2}_Emulate_Bitstream)) memory{i} "
            f"(.FrameData(32'b0), .FrameStrobe(20'b0), .ConfigBits(bits{i}), "
            ".ConfigBits_N());\n"
        )
    emulation.append("initial begin #1;\n")
    for i, value in enumerate(desired):
        emulation.append(
            f"if (bits{i} !== {sizes[i]}'h{value:x}) "
            f'$fatal(1, "emulation memory {i}");\n'
        )
    emulation.append('$display("PASS: emulation image"); $finish; end\nendmodule\n')
    emulation_tb = tmp_path / "emulation_tb.v"
    emulation_tb.write_text("".join(emulation))
    subprocess.run(
        [
            str(settings.iverilog_path),
            "-g2012",
            "-DEMULATION",
            "-I",
            str(tmp_path),
            "-s",
            "emulation_tb",
            "-o",
            str(compiled),
            *map(str, sources),
            str(emulation_tb),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = subprocess.run(
        [str(settings.vvp_path), str(compiled)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "PASS: emulation image" in result.stdout
