#!/usr/bin/env python3
"""Backward-compatible import for older installations.

The runtime implementation now lives entirely in flatpack.py. Keeping this
module avoids breaking any existing imports while preventing method-signature
mismatches between two controller implementations.
"""

from flatpack import FlatpackController, STATES

__all__ = ["FlatpackController", "STATES"]
