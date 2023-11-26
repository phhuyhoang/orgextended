"""
Sublime Text utility functions
"""

import uuid
import sublime
from math import ceil
from timeit import default_timer
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from OrgExtended.orgutil.util import safe_call, seconds_fmt


def find_by_selectors(
    view: sublime.View, 
    selectors: List[str]
) -> List[sublime.Region]:
    """
    Same as `view.find_by_selector()` but accept multiple selectors at
    once.
    """
    regions = []
    for selector in selectors:
        matched_regions = view.find_by_selector(selector)
        regions.extend(matched_regions)
    regions.sort()
    return regions


def find_by_selector_in_region(
    view: sublime.View, 
    region: sublime.Region, 
    selector: str
) -> List[sublime.Region]:
    """
    Find by selector within specific region.
    """
    global_matched = view.find_by_selector(selector)
    regional_matched = slice_regions(global_matched, begin = region.begin(), end = region.end())
    return regional_matched


def get_cursor_region(view: sublime.View) -> sublime.Region:
    """
    Retrieve begin and end region of the unexpanded cursor in 
    the view.
    """
    cursors = view.sel()
    if len(cursors) > 0:
        initial_cursor = cursors[0]
        begin, end = initial_cursor.begin(), initial_cursor.end()
        return sublime.Region(begin, end)
    return sublime.Region(-1, -1)


def is_active_plugin(plugin: str) -> bool:
    """
    Check if the given Sublime Text plugin is active based on installed
    and ignored packages.
    """
    package_settings = sublime.load_settings('Package Control.sublime-settings')
    user_settings = sublime.load_settings('Preferences.sublime-settings')
    installed_packages = package_settings.get('installed_packages', [])
    ignored_packages = user_settings.get('ignored_packages', [])
    return plugin in installed_packages and plugin not in ignored_packages


def lines_from_region(view: sublime.View, region: sublime.Region) -> List[str]:
    """
    :returns: A list of string lines (in sorted order) intersecting the 
    provided `Region`
    """
    lines = view.lines(region)
    return [view.substr(line) for line in lines]


def region_between(
    view: sublime.View, 
    r1: sublime.Region, 
    r2: sublime.Region) -> sublime.Region:
    """
    Return the region between two provided regions, handling cases where
    either or both regions are None.
    """
    Region = sublime.Region
    if not r1 and not r2:
        return Region(0, 0)
    if not r1:
        return Region(0, r2.begin())
    elif not r2:
        return Region(r1.end(), view.size())
    else:
        return Region(r1.end(), r2.begin())


def set_temporary_status(
    view: sublime.View,
    name: str, 
    message: str, 
    timeout: int = 5) -> None:
    """
    Show a temporary status, automatically clear out after a set of time.
    
    :param      timeout:  Timeout in seconds
    """
    view.set_status(name, message)
    sublime.set_timeout(lambda: view.erase_status(name), timeout * 1000)


def show_message(message: Any, title = 'Sublime Text', level = 'info') -> None:
    """
    Display a system notification with the given message, title, and
    level if the 'SubNotify' plugin is active; otherwise, print to
    console.
    """
    if is_active_plugin('SubNotify'):
        sublime.run_command(
            'sub_notify', { 
                'title': title, 
                'msg': str(message), 
                'level': level 
            }
        )
    print(message)


def slice_regions(
    regions: Tuple[sublime.Region],
    begin: float = 0, 
    end: float = float('inf'),
    aregion: Optional[sublime.Region] = None
) -> List[sublime.Region]:
    """
    Filter regions that are within another region, returning a list of 
    sliced regions.
    """
    sliced_regions = []
    if type(aregion) is sublime.Region:
        begin, end = aregion.begin(), aregion.end()
    for region in regions:
        if region.begin() >= begin and region.end() < end:
            sliced_regions.append(region)
    sliced_regions.sort()
    return sliced_regions


def starmap_async(
    callback: Callable,
    args: List[Any] = [],
    on_data: Callable[[Any], None] = None,
    on_finish: Callable[[List[Any]], None] = None,
    delay: Optional[int] = None
) -> None:
    """
    Asynchronously run the same callback on every argument in parallel.
    You can take the results via the on_finish callback
    """
    results = []
    def async_operation(arg: Any) -> None:
        result = callback(arg)
        results.append(result)
        if callable(on_data):
            safe_call(on_data, [result])
        if len(results) >= len(args):
            safe_call(on_finish, [results])
    for arg in args:
        sublime.set_timeout(lambda a = arg: async_operation(a))


def substring_region(
    view: sublime.View, 
    region: sublime.Region, 
    child_substr: str) -> Optional[sublime.Region]:
    """
    Return calculated region of the `child_substr` if it is a substring of 
    the `region`. Otherwise, return nothing
    """
    parent_substr = view.substr(region)
    if child_substr not in parent_substr:
        return None
    index = parent_substr.index(child_substr)
    begin = region.begin() + index
    end = begin + len(child_substr)
    return sublime.Region(begin, end)


class PhantomsManager:
    """
    Manage active phantoms in a view. There are two usage options: \n
    * Use the PhantomsManager.use() static method if you want each view
    to be managed by just one instance of this class
    * Create a new PhantomsManager() instance if you want to manage them 
    by yourself. Each view can be managed by multiple instances.
    """
    hub = dict()

    def __init__(self, view: sublime.View) -> None:
        self.view = view
        self.pids = set()
        self.key_refs = dict()
        self.data_refs = dict()

    def add_phantom(
        self,
        region: sublime.Region,
        html: str, 
        data: Dict = {}
    ) -> int:
        """
        Adds a phantom. If the region causes a collision with other 
        created phantoms, the adding action will be canceled.
        Return a phantom id that can be used to manipulate the 
        created phantom.
        """
        if self.will_cause_duplication(region):
            return -1
        key = self.create_unique_key(self.key_refs.copy())
        pid = self.view.add_phantom(key, region, html, sublime.LAYOUT_BLOCK)
        self.create_ref(pid, key, data)
        return pid

    def erase_phantom(
        self,
        pid: int
    ) -> bool:
        """
        Delete an existing phantom. It also destroy all relevant data
        which have bound with the phantom.
        """
        key = self.key_refs.get(pid)
        if key is None:
            return False
        self.view.erase_phantoms(key)
        self.view.erase_phantom_by_id(pid)
        self.kill_ref(pid)
        return True

    def get_all_overseeing_regions(self) -> List[sublime.Region]:
        """
        Get all available regions that have been attached with phantom.
        """
        return self.view.query_phantoms(list(self.pids))

    def get_pid_by_region(
        self, 
        region: sublime.Region
    ) -> Optional[int]:
        """
        Find a phantom id by provided region. Phantom ID (pid) was
        a number returned by `view.add_phantom()` method that helps
        query the current phantom regions.
        Return `None` if the provided region does not have any
        created phantom nearby.
        """
        for pid in self.pids:
            regions = self.get_region_by_pid(pid)
            if region in regions:
                return pid
        return None

    def get_region_by_pid(self, pid: int) -> List[sublime.Region]:
        """
        Find a region with a phantom nearby. If there aren't any such 
        regions, the return array will contain nothing.
        """
        return self.view.query_phantom(pid)

    def get_data_by_pid(self, pid: int) -> Optional[Dict]:
        """
        Get the relevant data that have been provided when the phantom
        being initialize.
        """
        return self.data_refs.get(pid)

    def has_phantom(self, region: sublime.Region) -> bool:
        """
        Check the phantom existence by its proceding region
        """
        pid = self.get_pid_by_region(region)
        if pid is None:
            return False
        actual_region = self.view.query_phantom(pid)[0]
        return actual_region.begin() > 0 and actual_region.end() > 0

    def will_cause_duplication(self, rhs: sublime.Region) -> bool:
        """
        Predict whether the phantom about to be created is likely to
        cause duplication/region collision or not.
        """
        overseeing_regions = self.get_all_overseeing_regions()
        for region in overseeing_regions:
            intersecting = \
                region.contains(rhs.begin()) or \
                region.contains(rhs.end())
            if intersecting:
                return True
        return False

    def create_ref(
        self, 
        pid: int, 
        key: str, 
        data: Dict = {}
    ) -> None:
        """
        Attach relevant data to the newly created phantom
        """
        self.key_refs[pid] = key
        self.data_refs[pid] = data
        self.pids.add(pid)

    def kill_ref(self, pid: int) -> bool:
        """
        Remove all relevant data about a phantom
        """
        if pid not in self.pids:
            return False
        self.pids.remove(pid)
        del self.key_refs[pid]
        del self.data_refs[pid]
        return True

    @classmethod
    def create_unique_key(cls, key_refs: Dict) -> str:
        """
        Using a str(region) as a phantom key is not recommended because 
        the region that has been created phantom will change on editing, 
        a unique key should be used instead.
        """
        key = uuid.uuid4().hex[:6]
        return key if key not in key_refs.values() else cls.create_unique_key(key_refs)

    @classmethod
    def is_being_managed(cls, view: sublime.View) -> bool:
        """
        Check if a view is being centrally managed. Only returns `True` 
        if `use()` has been used at least once for the view and has not 
        been `remove()` before.
        """
        return view.id() in cls.hub

    @classmethod
    def use(cls, view: sublime.View) -> 'PhantomsManager':
        """
        Add the current view into centrally manage context and return
        the newly PM if there aren't. Otherwise, return the 
        corresponding PM to that view.
        """
        vid = view.id()
        if vid not in cls.hub:
            cls.hub[vid] = PhantomsManager(view)
        return cls.hub[vid]

    @classmethod
    def remove(cls, view: sublime.View) -> bool:
        """
        Remove the current view from manage context and delete all
        relevant data. Calling `use()` if you want to let the class
        manage it again.
        """
        vid = view.id()
        if vid not in cls.hub:
            return False
        del cls.hub[vid]
        return True
        


class SublimeStatusIndicator:
    """
    A controllable Sublime status updater that can be used to show a 
    message when waiting for something to run in background.
    It is almost similar to the `Loading repositories [  = ]` that you 
    can see when running the command `Package Control: Install Package`
    Each instance created should only be run once.
    """
    def __init__(
        self,
        view: sublime.View,
        name: str,
        message: str,
        total_count: Optional[int] = None,
        finish_message: str = '',
        update_interval: int = 100,
    ) -> None:
        self.view = view
        self.name = name
        self.message = message
        self.finish_message = finish_message
        self.update_interval = update_interval
        self._id = uuid.uuid4().hex
        self._bar = self.indicator_bar()
        self._counter = self.status_counter()
        self._timer = self.status_timer()
        self._started = False
        self._stopped = False
        self._counter.total = total_count

    def is_running(self) -> bool:
        """
        Determines if the indicator is running.
        """
        return self._timer.start > 0 and not self._stopped

    def start(self) -> 'SublimeStatusIndicator':
        """
        Start to run the indicator. 
        """
        if self.is_running() or self._stopped:
            return self
        self._timer.start = default_timer()
        self.autoupdate()
        return self

    def stop(self, timer_result: Optional[float] = None) -> 'SublimeStatusIndicator':
        """
        Stop the indicator once and for all. Accepts an external 
        `timer_result` to display in place of the local timer.
        """
        if self._stopped:
            return self
        if timer_result:
            duration = ceil(timer_result * 1000) / 1000
        else:
            self._timer.end = default_timer()
            duration = ceil((self._timer.end - self._timer.start) * 1000) / 1000
        if self._counter.total is not None:
            status_message = '{} [{}✔️/{}❌/{}❔] ({})'.format(
                self.finish_message,
                self._counter.succeed,
                self._counter.failed,
                self._counter.total,
                seconds_fmt(duration)
            )
        else:
            status_message = '{} ({})'.format(
                self.finish_message,
                seconds_fmt(duration)
            )
        self.view.set_status(self.name, status_message)

    def set(
        self,
        message: Optional[str] = None,
        finish_message: Optional[str] = None,
        update_interval: Optional[int] = None
    ) -> 'SublimeStatusIndicator':
        """
        This method allow you update some things even while it is running
        """
        self.message = message or self.message
        self.finish_message = finish_message or self.finish_message
        self.update_interval = update_interval or self.update_interval
        return self

    def succeed(self) -> 'SublimeStatusIndicator':
        """
        Tells the indicator that a unit of work has been succeed.
        """
        self._counter.succeed += 1
        return self

    def failed(self) -> 'SublimeStatusIndicator':
        """
        Tells the indicator that a unit of work has been failed.
        """
        self._counter.failed += 1
        return self

    def autoupdate(self, _id: str, i: int = 0, move: int = 1) -> None:
        """
        Automatically update the status message in period.
        """
        if self._stopped or _id != self._id:
            return None
        before = i % 8
        after = (7) - before
        if not after:
            move -= 1
        if not before:
            move = 1
        i += move
        self._bar.before = before
        self._bar.after = after
        if self._counter.total is not None:
            status_message = '[{}={}] {} [{}✔️/{}❌/{}❔]'.format(
                ' ' * before,
                ' ' * after,
                self.message,
                self._counter.succeed,
                self._counter.failed,
                self._counter.total)
        else:
            status_message = '[{}={}] {}'.format(
                ' ' * before, 
                ' ' * after,
                self.message)
        self.view.set_status(self.name, status_message)
        sublime.set_timeout(lambda: self.autoupdate(self._id, i, move), self.update_interval)

    @staticmethod
    def status_counter():
        """
        Return a dataclass for internal uses
        """
        class StatusCounter:
            total = 0
            succeed = 0
            failed = 0
        return StatusCounter()

    @staticmethod
    def status_timer():
        """
        Return a dataclass for internal uses
        """
        class StatusTimer:
            start = 0
            end = 0
        return StatusTimer()

    @staticmethod
    def indicator_bar():
        """
        Return a dataclass for internal uses
        """
        class IndicatorBar:
            before = 0
            after = 0
        return IndicatorBar()
