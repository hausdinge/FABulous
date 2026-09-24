"""Validated frame-strobe encoding shared by RTL and bitstream specifications."""

from itertools import combinations
from math import comb
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_LOGICAL_FRAMES = 256


class FrameStrobeEncoding(BaseModel):
    """Select frames using q asserted wires from the highest n physical strobes.

    Lower strobes remain direct. The default 1-out-of-20 mapping is the legacy
    identity mapping, including its ability to assert multiple direct strobes.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    q: int = Field(default=1, ge=1, le=20)
    n: int = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        """Reject impossible codes and excessive decoder sizes.

        Returns
        -------
        Self
            Validated encoding.

        Raises
        ------
        ValueError
            If q exceeds n or the decoded capacity exceeds 256 frames.
        """
        if self.q > self.n:
            raise ValueError("FrameStrobeEncoding requires q <= n")
        if 20 - self.n + comb(self.n, self.q) > MAX_LOGICAL_FRAMES:
            raise ValueError("FrameStrobeEncoding exceeds the 256 logical-frame limit")
        return self

    def masks(self, physical_strobes: int = 20) -> tuple[int, ...]:
        """Return physical write masks in logical frame order.

        Parameters
        ----------
        physical_strobes : int
            Width of the unchanged physical FrameStrobe port.

        Returns
        -------
        tuple[int, ...]
            Direct masks followed by lexicographically ordered q-wire combinations.

        Raises
        ------
        ValueError
            If the encoded group exceeds the physical interface.
        """
        if self.n > physical_strobes:
            raise ValueError("FrameStrobeEncoding n exceeds the physical strobe width")
        direct = physical_strobes - self.n
        return tuple(1 << i for i in range(direct)) + tuple(
            sum(1 << i for i in group)
            for group in combinations(range(direct, physical_strobes), self.q)
        )


def frame_strobe_masks(
    physical_strobes: int, encoding: FrameStrobeEncoding | None = None
) -> tuple[int, ...]:
    """Resolve optional encoding for standalone generators with arbitrary widths.

    Parameters
    ----------
    physical_strobes : int
        Physical strobe count.
    encoding : FrameStrobeEncoding | None
        Encoding, or a one-wire identity group when omitted.

    Returns
    -------
    tuple[int, ...]
        Physical write masks indexed by logical frame.
    """
    return (encoding or FrameStrobeEncoding(q=1, n=1)).masks(physical_strobes)
