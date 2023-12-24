"""
Type compatible fallback to using on both Python 3.3 and Python 3.8 
plugin hosts
"""

import types


__all__ = (
    'Literal',
    'Pattern',
    'Point',
)


try:
    from typing import Literal
except ImportError:
    def _type_repr(obj):
        """Return the repr() of an object, special-casing types (internal helper).

        If obj is a type, we return a shorter version than the default
        type.__repr__, based on the module and qualified name, which is
        typically enough to uniquely identify a type.  For everything
        else, we fall back on repr(obj).
        """
        if isinstance(obj, type) and not isinstance(obj, TypingMeta):
            if obj.__module__ == '__builtin__':
                return _qualname(obj)
            return '%s.%s' % (obj.__module__, _qualname(obj))
        if obj is Ellipsis:
            return '...'
        if isinstance(obj, types.FunctionType):
            return obj.__name__
        return repr(obj)


    def _qualname(x):
        # PYSIDE-1286: Support __qualname__ in Python 2
        return getattr(x, "__qualname__", x.__name__)


    def _trim_name(nm):
        whitelist = ('_TypeAlias', '_ForwardRef', '_TypingBase', '_FinalTypingBase')
        if nm.startswith('_') and nm not in whitelist:
            nm = nm[1:]
        return nm


    class TypingMeta(type):
        """Metaclass for most types defined in typing module
        (not a part of public API).

        This also defines a dummy constructor (all the work for most typing
        constructs is done in __new__) and a nicer repr().
        """

        _is_protocol = False

        def __new__(cls, name, bases, namespace):
            return super(TypingMeta, cls).__new__(cls, str(name), bases, namespace)

        @classmethod
        def assert_no_subclassing(cls, bases):
            for base in bases:
                if isinstance(base, cls):
                    raise TypeError("Cannot subclass %s" %
                                    (', '.join(map(_type_repr, bases)) or '()'))

        def __init__(self, *args, **kwds):
            pass

        def _eval_type(self, globalns, localns):
            """Override this in subclasses to interpret forward references.

            For example, List['C'] is internally stored as
            List[_ForwardRef('C')], which should evaluate to List[C],
            where C is an object found in globalns or localns (searching
            localns first, of course).
            """
            return self

        def _get_type_vars(self, tvars):
            pass

        def __repr__(self):
            qname = _trim_name(_qualname(self))
            return '%s.%s' % (self.__module__, qname)


    class _TypingBase(object):
        """Internal indicator of special typing constructs."""
        __metaclass__ = TypingMeta
        __slots__ = ('__weakref__',)

        def __init__(self, *args, **kwds):
            pass

        def __new__(cls, *args, **kwds):
            """Constructor.

            This only exists to give a better error message in case
            someone tries to subclass a special typing object (not a good idea).
            """
            if (len(args) == 3 and
                    isinstance(args[0], str) and
                    isinstance(args[1], tuple)):
                # Close enough.
                raise TypeError("Cannot subclass %r" % cls)
            return super(_TypingBase, cls).__new__(cls)

        # Things that are not classes also need these.
        def _eval_type(self, globalns, localns):
            return self

        def _get_type_vars(self, tvars):
            pass

        def __repr__(self):
            cls = type(self)
            qname = _trim_name(_qualname(cls))
            return '%s.%s' % (cls.__module__, qname)

        def __call__(self, *args, **kwds):
            raise TypeError("Cannot instantiate %r" % type(self))


    class _FinalTypingBase(_TypingBase):
        """Internal mix-in class to prevent instantiation.

        Prevents instantiation unless _root=True is given in class call.
        It is used to create pseudo-singleton instances Any, Union, Optional, etc.
        """

        __slots__ = ()

        def __new__(cls, *args, **kwds):
            self = super(_FinalTypingBase, cls).__new__(cls, *args, **kwds)
            if '_root' in kwds and kwds['_root'] is True:
                return self
            raise TypeError("Cannot instantiate %r" % cls)

        def __reduce__(self):
            return _trim_name(type(self).__name__)


    class _LiteralMeta(TypingMeta):
        """Metaclass for _Literal"""

        def __new__(cls, name, bases, namespace):
            cls.assert_no_subclassing(bases)
            self = super(_LiteralMeta, cls).__new__(cls, name, bases, namespace)
            return self


    class _Literal(_FinalTypingBase):
        """A type that can be used to indicate to type checkers that the
        corresponding value has a value literally equivalent to the
        provided parameter. For example:

            var: Literal[4] = 4

        The type checker understands that 'var' is literally equal to the
        value 4 and no other value.

        Literal[...] cannot be subclassed. There is no runtime checking
        verifying that the parameter is actually a value instead of a type.
        """

        __metaclass__ = _LiteralMeta
        __slots__ = ('__values__',)

        def __init__(self, values=None, **kwds):
            self.__values__ = values

        def __getitem__(self, item):
            cls = type(self)
            if self.__values__ is None:
                if not isinstance(item, tuple):
                    item = (item,)
                return cls(values=item,
                           _root=True)
            raise TypeError('{} cannot be further subscripted'
                            .format(cls.__name__[1:]))

        def _eval_type(self, globalns, localns):
            return self

        def __repr__(self):
            r = super(_Literal, self).__repr__()
            if self.__values__ is not None:
                r += '[{}]'.format(', '.join(map(_type_repr, self.__values__)))
            return r

        def __hash__(self):
            return hash((type(self).__name__, self.__values__))

        def __eq__(self, other):
            if not isinstance(other, _Literal):
                return NotImplemented
            if self.__values__ is not None:
                return self.__values__ == other.__values__
            return self is other
            
    Literal = _Literal(_root=True)

try:
    from typing import TypedDict
except ImportError:
    from .mypy_extensions import TypedDict

try:
    from sublime import Point
except ImportError:
    Point = int

try:
    from re import Pattern
except ImportError:
    import re
    Pattern = re.compile('', 0)