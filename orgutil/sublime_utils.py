"""
Sublime Text utility functions
"""

import re
import uuid
import sublime
import sublime_plugin
from math import ceil
from collections import defaultdict
from timeit import default_timer
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from OrgExtended.orgutil.util import safe_call, seconds_fmt
from OrgExtended.orgutil.typecompat import Literal


REGEX_LENGTH = re.compile(r'([0-9\.]+)(.*)')

DimensionType = Literal['width', 'height']
SupportedLengthUnit = Literal[
    'cm', 
    'mm', 
    'in',
    'px',
    'pc',
    'pt',
    '%',
    'vh',
    'vw',
    'rem',
    None
]


def convert_length_to_px(
    view: sublime.View, 
    value: Union[str, int, float],
    unit: SupportedLengthUnit = None,
    dtype: DimensionType = 'width',
    scale: Optional[float] = 1.0) -> float:
    """
    Supports converting some length units to pixels (px).\n
    - Absolute units: cm, mm, in, pc, pt, px
    - Relative units: %, vh, vw, rem

    Ref: [MDN: CSS values and units](https://developer.mozilla.org/en-US/docs/Learn/CSS/Building_blocks/Values_and_units)
    """
    if unit is None:
        value = str(value)
        match = REGEX_LENGTH.match(value)
        if not match:
            return -1
        value, unit = match.groups()
    
    vw, vh = view.viewport_extent()
    if not unit or unit == 'px':
        return float(value)
    elif unit == '%' and dtype == 'width':
        return vw * float(value) / 100 * scale
    elif unit == '%' and dtype == 'height':
        return vh * float(value) / 100 * scale
    elif unit == 'vw':
        return vw * float(value) * scale
    elif unit == 'vh':
        return vh * float(value) * scale
    elif unit == 'rem':
        font_size = view.settings().get('font_size', 10)
        return float(font_size) * float(value)
    elif unit == 'cm':
        return 37.8 * float(value)
    elif unit == 'mm':
        return 37.8 * float(value) / 10
    elif unit == 'in':
        return 96 * float(value)
    elif unit == 'pc':
        return 96 * float(value) / 6
    elif unit == 'pt':
        return 96 * float(value) / 72
    else:
        return -1


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

    An another approach but cannot guarantee both accuracy and speed:
    https://forum.sublimetext.com/t/is-there-a-good-way-to-run-find-by-selector-over-a-region-instead-of-the-entire-view/44796
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


def get_syntax_by_scope(scope: str) -> Union[sublime.Syntax, None]:
    """
    Example:
    ```py
    get_syntax_by_scope('text.orgmode')
    # Syntax('Packages/OrgExtended/OrgExtended.sublime-syntax', '', False, 'text.orgmode')
    ```
    """
    syntaxes = sublime.list_syntaxes()
    for syntax in syntaxes:
        if syntax.scope == scope:
            return syntax


def get_view_by_id(view_id: int) -> Union[sublime.View, None]:
    """
    Gets a view by identifier number.
    """
    windows = sublime.windows()
    for window in windows:
        for view in window.views():
            if view.id() == view_id:
                return view


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


def move_to_region(
    view: sublime.View, 
    region: sublime.Region, 
    animate: bool = False
) -> None:
    """
    Move the cursor to a specific position in the view and select that 
    region

    :param      animate:  Smoothy scroll
    """
    selection = view.sel()
    if len(selection):
        selection.clear()
    selection.add_all([region])
    view.show_at_center(region, animate)


def move_to_rowcol(
    view: sublime.View,
    row: int = 0,
    col: int = 0,
    extend: int = 0,
    animate: bool = False,
) -> None:
    """
    Move the cursor to a specified position in the view.

    :param      extend:   Number of characters to extend the selection to the right
    :param      animate:  Smoothy scroll
    """
    text_point = view.text_point(row, col)
    region = sublime.Region(text_point, text_point + extend)
    move_to_region(view, region, animate)


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
        if region.begin() >= begin and region.end() <= end:
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
        sublime.set_timeout_async(lambda a = arg: async_operation(a))


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
        self.pids = set()        # type: Set[int]
        self.key_refs = dict()   # type: Dict[str, str]
        self.data_refs = dict()  # type: Dict[str, Dict]
        self.change_histories = []  # type: List[List[int]]

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
        Note: This method only adds phantoms, doesn't track the 
        replacement between them. If you want to replace an existing 
        phantom and tracking them also, use .replace_phantom() instead 
        of this method.
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

    def find_history(self, pid: int) -> Union[List[int], None]:
        """
        Find the phantom change history of any region. A change history 
        is represented by a list of pids, with the last element being the 
        latest pid used for that region.
        """
        for pid_history in self.change_histories:
            if pid in pid_history:
                return pid_history

    def get_all_overseeing_regions(self) -> List[sublime.Region]:
        """
        Get all available regions that have been attached with phantom.
        """
        return self.view.query_phantoms(list(self.pids))

    def get_data_by_pid(self, pid: int) -> Optional[Dict]:
        """
        Get the relevant data that have been provided when the phantom
        being initialize.
        """
        return self.data_refs.get(pid)

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
        return self.view.query_phantom(self.transform_pid(pid))

    def transform_pid(self, pid: int) -> int:
        """
        
        """
        history = self.find_history(pid)
        return history[-1] if history else pid

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

    def update_history(self, pid: int, old_pid: Optional[int] = None) -> 'PhantomsManager':
        """
        Find and add a pid to the history of related pids. Related pids 
        can be understood as pids that have previously pointed to the 
        same initial region.
        """
        history = self.find_history(old_pid)
        if not history:
            history = []
            self.change_histories.append(history)
        history.append(pid)
        return self

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
        Check if a view is being managed. Only returns `True` if `use()` 
        has been used at least once for the view and has not been 
        `remove()`.
        """
        return view.id() in cls.hub

    @classmethod
    def adopt(cls, view: sublime.View) -> 'PhantomsManager':
        """
        Alias of '.of'
        """
        return cls.of(view)

    @classmethod
    def of(cls, view: sublime.View) -> 'PhantomsManager':
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
        self._stopped = True
        self.view.set_status(self.name, status_message)
        sublime.set_timeout(lambda: self.clear(), 3000)

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

    def clear(self) -> 'SublimeStatusIndicator':
        """
        Clear the status message
        """
        self.view.erase_status(self.name)

    def succeed(self, n: int = 1, update: bool = False) -> 'SublimeStatusIndicator':
        """
        Tells the indicator that a unit of work has been succeed.
        """
        self._counter.succeed += n
        if update and self._started and not self._stopped:
            self.update_status(self._bar.before, self._bar.after, self.message)
        return self

    def failed(self, n: int = 1, update: bool = False) -> 'SublimeStatusIndicator':
        """
        Tells the indicator that a unit of work has been failed.
        """
        self._counter.failed += n
        if update and self._started and not self._stopped:
            self.update_status(self._bar.before, self._bar.after, self.message)
        return self

    def autoupdate(self, i: int = 0, move: int = 1) -> None:
        """
        Automatically update the status message in period.
        """
        if self._stopped:
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
        self.update_status(before, after, self.message)
        sublime.set_timeout(lambda: self.autoupdate(i, move), self.update_interval)

    def update_status(
        self,
        before: int,
        after: int,
        message: Optional[str] = None) -> None:
        """
        Aggressively update the status message.
        """
        message = message or self.message
        if self._counter.total is not None:
            status_message = '[{}={}] {} [{}✔️/{}❌/{}❔]'.format(
                ' ' * before,
                ' ' * after,
                message,
                self._counter.succeed,
                self._counter.failed,
                self._counter.total)
        else:
            status_message = '[{}={}] {}'.format(
                ' ' * before, 
                ' ' * after,
                message)
        self.view.set_status(self.name, status_message)

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


class ContextData(object):
    """
    The missing Sublime Text view.set_data() and view.get_data()
    """
    hub = dict()

    @classmethod
    def available_for(cls, view: sublime.View) -> bool:
        vid = view.id()
        return vid in cls.hub

    @classmethod
    def use(cls, view: sublime.View, default: Any = None) -> Any:
        vid = view.id()
        if not cls.available_for(view):
            cls.hub[vid] = default if default is not None else {}
        return cls.hub[vid]

    @classmethod
    def remove(cls, view: sublime.View) -> bool:
        vid = view.id()
        if not cls.available_for(view):
            return False
        del cls.hub[vid]
        return True