#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""RenPy-RPA-Auto-Unpacker - unpack Ren'Py .rpa archives.

Copyright (C) 2025 TesterNaN

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

--------------------------------------------------------------------------
Entry point.  The implementation lives in the ``renpy_unpack`` package.

Version 2 replaces the old approach -- cutting the archive readers out of
``renpy/loader.py`` with ``str.find()`` offsets -- with real AST extraction.
See README.md for why, and run with ``--help`` for the full option list.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import sys

from renpy_unpack.core import Unpacker, UnpackError, build_arg_parser, main

__all__ = ["Unpacker", "UnpackError", "build_arg_parser", "main"]

if __name__ == "__main__":
    sys.exit(main())
