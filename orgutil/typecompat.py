"""
Type compatible fallback to using on both Python 3.3 and Python 3.8 
plugin hosts
"""

__all__ = (
    'Literal',
    'Pattern',
    'Point',
)


class _FakeLiteral:
    def __getitem__(self, value):
        return value


try:
    from typing import Literal
except ImportError:
    Literal = _FakeLiteral()

try:
    from sublime import Point
except ImportError:
    Point = int

try:
    from re import Pattern
except ImportError:
    import re
    Pattern = re.compile('', 0)