from sys import getsizeof
from typing import Any, Dict, Optional, Union


class ConstrainedCache:
    """
    A simple caching provider automatically releases memory when it 
    reaches its allocation limit. There are two usage options: \n
    * Use the ConstrainedCache.use() static method if you want to create
    a caching store that can be share its data between modules. This 
    method takes a `name` argument that helps access the corresponding 
    store.
    * Create a new ConstrainedCache() instance if you want to use it in
    your module internally.

    Notice: This thing will not calculate the size of reference data 
    types or structured data, such as Dict, List, Class, etc. This cause 
    memory leaks, which no one wants. \n
    Btw, its just best to store things like strings, bytes, numbers, etc.
    """
    hub = dict()

    def __init__(self, name: str) -> None:
        self.__name = name
        self.__cache = dict()
        self.__max_size = float('inf')

    @property
    def name(self) -> str:
        """
        Its name.
        """
        return self.__name

    @property
    def cache(self) -> Dict:
        """
        The internal caching data store
        """
        return self.__cache

    @property
    def current_max_size(self) -> Union[int, float]:
        """
        The current limitation in bytes
        """
        return self.__max_size

    def alloc(
        self, 
        size: Union[float, int] = float('inf')
    ) -> 'ConstrainedCache':
        """
        Set the caching limitation.
        """
        self.__max_size = size
        return self

    def get(self, key: str) -> Any:
        """
        Returns the cached value of the specified key
        """
        return self.cache.get(key)

    def has(self, key: str) -> bool:
        """
        Return a boolean indicating whether the cache has a specified
        value corresponding to the given key
        """
        return key in self.cache or bool(self.get(key))

    def set(self, key: str, value: Any) -> 'ConstrainedCache':
        """
        Store the given data value to cache. The cached data can be 
        accessed later by the corresponding key.
        """
        # Automatically flush the cache if it reaches the limit
        self.autofreeup()
        self.cache[key] = value
        return self

    def size(self) -> int:
        """
        Calculate total size of storing data in bytes.
        """
        values = self.cache.values()
        total = 0
        for data in values:
            total += getsizeof(data, 2) or 0
        return total

    def autofreeup(self, force: bool = False) -> 'ConstrainedCache':
        """
        Will delete the cached data if it exceeds the allocated limit
        """
        if self.size() > self.current_max_size or force:
            print('Cleared cache: {}'.format(self.name))
            self.cache.clear()
        return self

    @classmethod
    def can_access(cls, name: str) -> bool:
        """
        Return True if there is a caching instance with that name
        """
        return cls.hub.get(name) is not None

    @classmethod
    def use(cls, name: str, max_size: Optional[int] = None) -> 'ConstrainedCache':
        """
        Create a newly instance with the corresponding name if there 
        aren't. Otherwise, return the caching store found.
        """
        if cls.can_access(name):
            cache = cls.hub.get(name)
        else:
            cache = ConstrainedCache(name)
            cls.hub[name] = cache
        if type(max_size) is int:
            cache.alloc(max_size)
        return cache

    @classmethod
    def destroy(cls, name: str) -> bool:
        """
        Delete an existing caching store by name.
        """
        if cls.can_access(name):
            del cls.hub[name]
            return True
        return False
