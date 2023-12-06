import time
import threading
import traceback
from collections import defaultdict
from typing import Any, Callable, List, NewType, Optional

from OrgExtended.orgutil.util import safe_call
from OrgExtended.orgutil.metaclass import Singleton


Event = NewType('Event', Any)
EventTarget = Optional[Any]
EventListener = Callable[[Any], Any]
EventCondition = Callable[[EventTarget], bool]


class NoSuchEventError(Exception):
    """
    Should be thrown when no such event has been defined
    """
    def __init__(self, event: Event, message = 'No such event!') -> None:
        self.event = event
        self.message = message
        super().__init__(self.message)


class EventSymbol(object):
    """
    This class can be used as a key for an event that you 
    don't want to share the permission of adding the listeners.
    """
    def __init__(self, description: str = '') -> None:
        self.description = description

    def __repr__(self) -> str:
        return "Event('{}')".format(self.description)


class EventLoop(threading.Thread):
    """
    A self-operating event mechanism runs in a separate thread, 
    functioning as an event observer. Instead of just waiting for 
    something to call .emit() or .notify() to inform listeners, it takes 
    in callbacks as event conditions to actively check them in an event 
    loop. Whenever a condition is satisfied, the event will be triggered, 
    and listener callbacks will be invoked.\n
    Notice: Each time you initialize a new EventLoop instance, it will
    create an additional thread to run in. If you have basic needs, 
    consider using GlobalEventLoop() instead
    --------------------------------------------------------------------
    This code is extremely rudimentary and hasn't been thoroughly tested 
    in all the cases. I'm only using it for very specific functionalities. 
    Use it at your own risk.\n

    Considerations when using:\n
    * If one of the event handlers is extremely costly or never completes, 
      it will block all subsequent event handlers of the same event. 
      All efforts to listen to that event will be considered impossible 
      from that point onwards.
    * Although the handlers list of each event runs in a fork thread, 
      the condition checking will be handled in the root thread. 
      Therefore, be aware that blocking the event condition checking 
      will also block the entire event loop. Ideally, the event condition 
      should be a function with minimal processing overhead.
    """
    def __init__(
        self,
        loop_interval: int = 0.1
    ) -> None:
        super().__init__()
        self._is_started = False
        self._is_stopped = False
        self._loop_interval = loop_interval
        self._running_flag = threading.Event()
        self._conditions = dict()
        self._targets = dict()
        self._blacklist = []
        self._threads = defaultdict(threading.Thread)
        self._listeners = defaultdict(list)

    @property
    def _active_events(self) -> List[Any]:
        """
        List all available events
        """
        return list(self._conditions.keys())

    def is_running(self) -> bool:
        """
        Returns True if the event loop is running. In other words, an
        event loop is considered to be running during the period between 
        calling .start() to initiate the thread and .stop() to terminate it.
        """
        return self._running_flag.is_set()

    def start(self) -> None:
        """
        Start the event loop. It should only be run once.
        """
        if self._is_started:
            return None
        self._is_started = True
        self._running_flag.set()
        return super().start()

    def stop(self) -> None:
        """
        Stop the event loop.
        """
        self._is_stopped = True
        self._running_flag.clear()

    def pause(self) -> None:
        """
        Pause the event loop.
        """
        self._running_flag.clear()

    def resume(self) -> None:
        """
        Resume the event loop from pause.
        """
        self._running_flag.set()

    def run(self) -> None:
        """
        Method representing the event loop’s activity.
        """
        while not self._is_stopped:
            self._running_flag.wait()
            conditions = self._conditions.copy()
            for event in conditions:
                # To avoid the RuntimeError: dictionary changed size 
                # during iteration
                if event not in self._conditions:
                    continue
                if event in self._blacklist:
                    continue
                condition = self._conditions[event]
                target = self._targets[event]
                # We shouldn't let some damn error be thrown and break 
                # our event loop
                try:
                    trigger_event = safe_call(condition, [target])
                except:
                    self._blacklist.append(event)
                    trigger_event = False
                    traceback.print_exc()
                # If the thread that handles the previous triggered 
                # event has not been completed, we shouldn't fork a 
                # new thread.
                if trigger_event and not self._threads[event].is_alive():
                    # An event must not wait for unrelated events to 
                    # complete before running. It should fork a new 
                    # thread.
                    self._threads[event] = threading.Thread(target = lambda e = event: self.fire(e))
                    self._threads[event].start()
            time.sleep(self._loop_interval)

    def add_event(
        self,
        event: Event, 
        condition: EventCondition,
        target: EventTarget = None
    ) -> 'EventLoop':
        """
        Adds an event. An event must always be accompanied by a condition 
        for that event to occur, in the form of a callback. If desired, 
        you can also attach a target, which will be stored as a snapshot 
        and passed as a parameter to the aforementioned condition 
        callback, or manage it yourself.
        You can also update that target through the `.update_target()` 
        method.
        """
        self._conditions[event] = condition
        self._targets[event] = target
        return self

    def remove_event(
        self,
        event: Event,
        condition: Optional[EventCondition] = None
    ) -> 'EventLoop':
        """
        Completely destroy a registered event. Literally, it will 
        also delete all associated targets and registered listeners for 
        that event.
        """
        tracking_condition = self._conditions[event]
        if condition == tracking_condition or len(self._listeners[event]) == 0:
            if event in self._targets:
                del self._targets[event]
            if event in self._conditions:
                del self._conditions[event]
            if event in self._listeners:
                del self._listeners[event]
            if event in self._threads:
                del self._threads[event]
            if event in self._blacklist:
                self._blacklist.remove(event)
        return self

    def add_listener(
        self,
        event: Event,
        listener: EventListener
    ) -> 'EventLoop':
        """
        Add an event listener for the registered event. An event listener 
        can be understood as a handler responsible for processing that 
        event when it occurs. Nothing will happen if you have never 
        added such an event before.
        """
        if event not in self._active_events:
            raise NoSuchEventError
        self._listeners[event].append(listener)
        return self

    def prepend_listener(
        self,
        event: Event,
        listener: EventListener
    ) -> 'EventLoop':
        """
        Add an event listener to the beginning of listeners list. It 
        will be prioritized for handling first until another event 
        listener is prepended.
        """
        if event not in self._active_events:
            raise NoSuchEventError
        self._listeners[event].insert(0, listener)
        return self

    def remove_listener(
        self,
        event: Event,
        listener: EventListener
    ) -> 'EventLoop':
        """
        Remove an event listener from the listeners list.
        """
        if event not in self._active_events:
            raise NoSuchEventError
        listeners = self._listeners[event]
        if listener in listeners:
            listeners.remove(listener)
        return self

    def remove_all_listener(self, event: Event) -> 'EventLoop':
        """
        Remove all event listeners.
        """
        if event not in self._active_events:
            raise NoSuchEventError
        self._listeners[event].clear()
        return self

    def fire(self, event: Event) -> 'EventLoop':
        """
        Trigger all listeners of an event
        """
        if event not in self._active_events:
            raise NoSuchEventError
        if event in self._blacklist:
            return self
        listeners = self._listeners.get(event, [])
        target = self._targets[event]
        for listener in listeners:
            try:
                safe_call(listener, [target])
            except:
                traceback.print_exc()
        return self

    def update_target(self, event: Event, target: EventTarget) -> 'EventLoop':
        """
        Update the tracked target. This causes the parameter passed to 
        the EventCondition to change as well.
        """
        if event not in self._targets:
            raise NoSuchEventError
        self._targets[event] = target
        return self

    def on(self, event: Event, callback: EventListener) -> 'EventLoop':
        """
        An alias of `.add_listener()`
        """
        self.add_listener(event, listener = callback)
        return self

    def once(self, event: Event, callback: EventListener) -> 'EventLoop':
        """
        An alias of `.add_listener()`, but run once.
        """
        def wrapped_callback(*args):
            safe_call(callback, args)
            self.remove_listener(event, wrapped_callback)
        self.add_listener(event, wrapped_callback)
        return self

    def listeners(self, event: Event) -> List[EventListener]:
        """
        Returns a list of handlers for a specific event. If there is no 
        such event, the return value will be an empty list.
        """
        return self._listeners[event] if event in self._listeners else []

    def listener_count(self, event: Event) -> int:
        """
        Returns the number of listeners listening for a specific event.
        """
        return len(self.listeners(event))


class GlobalEventLoop(EventLoop, metaclass = Singleton):
    """
    A singleton event loop cannot be customized, automatically .start() 
    upon initialization. You can't stop() it by yourself as well.
    """
    __doc__ += EventLoop.__doc__

    def __init__(self) -> None:
        super().__init__()
        self.start()

    def stop(self) -> None:
        """
        You can't stop the global event loop by yourself
        """
        return None