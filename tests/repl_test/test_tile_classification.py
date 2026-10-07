"""Classify tiles from the fabric model rather than library subdirectories."""

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from fabulous.fabric_definition.fabric import Fabric
from fabulous.fabric_definition.supertile import SuperTile
from fabulous.fabulous_api import FABulous_API
from fabulous.fabulous_repl.cmd_fabric_gen import FabricGenCommandSet
from tests.conftest import make_empty_tile


@pytest.mark.parametrize("unused", [False, True])
@pytest.mark.parametrize("folders", [(), ("common", "tiles", "doc", "macro")])
def test_regular_tile_with_library_folders(
    tmp_path: Path, mocker: MockerFixture, unused: bool, folders: tuple[str, ...]
) -> None:
    """Library folders must not turn a regular tile into a supertile."""
    tile = make_empty_tile("LOGIC", pinOrderConfig={})
    fabric = Fabric(fabric_dir=tmp_path)
    (fabric.unusedTileDic if unused else fabric.tileDic)[tile.name] = tile
    for folder in folders:
        (tmp_path / "Tile" / tile.name / folder).mkdir(parents=True)
    api = mocker.Mock(spec=FABulous_API, fabric=fabric)
    repl = SimpleNamespace(
        projectDir=tmp_path,
        fabulousAPI=api,
        extension="v",
        exit_code=0,
        onecmd_plus_hooks=mocker.Mock(),
    )

    inspect.unwrap(FabricGenCommandSet.do_gen_tile)(
        SimpleNamespace(_cmd=repl), [tile.name]
    )

    assert repl.onecmd_plus_hooks.call_args_list == [
        mocker.call("gen_switch_matrix LOGIC"),
        mocker.call("gen_config_mem LOGIC"),
    ]
    api.genTile.assert_called_once_with("LOGIC")
    api.genSuperTile.assert_not_called()
    api.gen_super_tile_switch_matrix.assert_not_called()
    api.gen_super_tile_config_mem.assert_not_called()


@pytest.mark.parametrize("unused", [False, True])
def test_supertile_uses_declared_members(
    tmp_path: Path, mocker: MockerFixture, unused: bool
) -> None:
    """Generate declared members in model order, ignoring unrelated directories."""
    members = [make_empty_tile(name, pinOrderConfig={}) for name in ("LOW", "HIGH")]
    tile = SuperTile("PAIR", tmp_path / "Tile/PAIR", members, [members])
    fabric = Fabric(fabric_dir=tmp_path)
    (fabric.unusedSuperTileDic if unused else fabric.superTileDic)[tile.name] = tile
    # No member directories: discovery must depend only on the parsed model.
    for folder in ("common", "tiles", "doc", "macro"):
        (tile.tileDir / folder).mkdir(parents=True)
    api = mocker.Mock(spec=FABulous_API, fabric=fabric)
    repl = SimpleNamespace(
        projectDir=tmp_path,
        fabulousAPI=api,
        extension="v",
        onecmd_plus_hooks=mocker.Mock(),
    )

    inspect.unwrap(FabricGenCommandSet.do_gen_tile)(
        SimpleNamespace(_cmd=repl), [tile.name]
    )

    assert api.genTile.call_args_list == [mocker.call("LOW"), mocker.call("HIGH")]
    assert api.genSwitchMatrix.call_args_list == [
        mocker.call("LOW"),
        mocker.call("HIGH"),
    ]
    assert api.genConfigMem.call_args_list == [
        mocker.call(name, tmp_path / f"Tile/PAIR/{name}/{name}_ConfigMem.csv")
        for name in ("LOW", "HIGH")
    ]
    api.gen_super_tile_switch_matrix.assert_called_once_with("PAIR")
    api.gen_super_tile_config_mem.assert_called_once_with("PAIR")
    api.genSuperTile.assert_called_once_with("PAIR")
    repl.onecmd_plus_hooks.assert_not_called()
