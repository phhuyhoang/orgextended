import threading
from typing import Any, Callable, Iterable, List, Mapping, Optional, Union


nil = lambda: None

Timeout = Union[float, None]


def batch_pools(
    pools: List[Callable], 
    name: Optional[str] = None,
    timeout: Timeout = None) -> List[Any]:
    """
    Run a list of callbacks in separate threads and then return a list 
    contains their results in order
    """
    threads = []
    for callback in pools:
        thread = Thread(
            name = name,
            target = callback if callable(callback) else nil)
        thread.start()
        threads.append(thread)
    return [t.join(timeout) for t in threads]


def starmap_pools(
    callback: Callable, 
    args: List[Any] = [],
    name: Optional[str] = None,
    timeout: Timeout = None) -> List[Any]:
    """
    Run the same callback on every argument in separate threads and then
    return a list contains their results in order
    """
    threads = []
    for arg in args:
        thread = Thread(
            name = name,
            target = lambda p = arg: callback(p))
        thread.start()
        threads.append(thread)
    return [t.join() for t in threads]


class Thread(threading.Thread):
    """
    A custom thread that supports returning results (whereas the 
    original `threading.Thread` does not)
    """
    def __init__(
        self,
        group: None = None,
        target: Union[Callable[..., object], None] = None,
        name: Union[str, None] = None,
        args: Iterable[Any] = (),
        kwargs: Union[Mapping[str, Any], None] = {},
        daemon: Union[bool, None] = None,
    ) -> None:
        super().__init__(
            group = group,
            target = target,
            name = name,
            args = args,
            kwargs = kwargs,
            daemon = daemon
        )
        self._returnValue = None

    def run(self) -> None:
        """
        Method representing the thread's activity.
        """
        try:
            if callable(self._target):
                self._returnValue = self._target(*self._args, **self._kwargs)
        finally:
            # Avoid a refcycle if the thread is running a function with 
            # an argument that has a member that points to the thread.
            del self._target, self._args, self._kwargs

    def join(
        self, 
        timeout: Union[float, None] = None
    ) -> None:
        """
        Return the stored output after joining the threads 
        """
        super().join(timeout)
        return self._returnValue

