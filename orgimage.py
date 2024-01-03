"""
     ┌─────┐                                    
     │START│                                     
     └──┬──┘                                    
   _____▽______                                 
  ╱            ╲    ┌──────┐                    
 ╱ IMAGE BINARY ╲___│RENDER│                    
 ╲ IN CACHE?    ╱yes└──────┘                    
  ╲____________╱                                
        │no                                     
  ______▽______                                 
 ╱             ╲    ┌──────────────┐            
╱ CAN DOWNLOAD? ╲___│STORE TO CACHE│            
╲               ╱yes└───────┬──────┘            
 ╲_____________╱       _____▽______             
        │no           ╱            ╲    ┌──────┐
        │            ╱ IMAGE BINARY ╲___│RENDER│ 
        │            ╲ IN CACHE?    ╱yes└──────┘
        │             ╲____________╱            
        │                   │no                 
        └────┬──────────────┘                   
          ┌──▽─┐                                
          │STOP│                                
          └────┘     
"""

import re
import traceback
import sublime
import sublime_plugin
import threading
import urllib.parse
import urllib.request
import OrgExtended.orgdb as db
import OrgExtended.asettings as settings
from os import path
from uuid import uuid4
from timeit import default_timer
from sublime import Region, View
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    NewType,
    Optional,
    Set,
    Tuple,
    Union,
    overload
)
from OrgExtended.pymitter import EventEmitter
from OrgExtended.orgparse.startup import Startup as StartupValue
from OrgExtended.orgutil.cache import ConstrainedCache
from OrgExtended.orgutil.events import EventLoop, EventSymbol
from OrgExtended.orgutil.image import (
    Height, 
    Width, 
    image_format_of, 
    image_size_of,
    image_to_base64, 
    image_to_string,
)
from OrgExtended.orgutil.sublime_utils import (
    ContextData,
    PhantomsManager,
    SublimeStatusIndicator,
    convert_length_to_px,
    find_by_selectors,
    find_by_selector_in_region,
    get_cursor_region,
    get_region_by_rows,
    get_syntax_by_scope,
    get_view_by_id,
    lines_from_region,
    move_to_region, 
    region_between,
    set_temporary_status, 
    show_message,
    slice_regions,
    starmap_async,
)
from OrgExtended.orgutil.threads import starmap_pools
from OrgExtended.orgutil.typecompat import Literal, Point, TypedDict
from OrgExtended.orgutil.util import (
    at, 
    download_binary, 
    is_iterable, 
    safe_call, 
    shallow_flatten,
    sizeof_fmt, 
    split_into_chunks
)

DEFAULT_POOL_SIZE = 10
DEFAULT_CACHE_SIZE = 100 * 1024 * 1024      # 100 MB
MIN_CACHE_SIZE = 2 * 1024 * 1024            # 2 MB
MAX_TPM = 9999
STATUS_ID = 'orgextra_image'
ON_CHANGE_KEY = 'orgextra_image'
ERROR_PANEL_NAME = 'orgextra_image_error'
SETTING_FILENAME = settings.configFilename + ".sublime-settings"
COMMON_THREAD_NAME = 'orgextra_image'
RENDER_THREAD_NAME = 'orgextra_image_render'

COMMAND_RENDER_IMAGES = 'org_extra_render_images'
COMMAND_SHOW_IMAGES = 'org_extra_show_images'
COMMAND_SHOW_THIS_IMAGE = 'org_extra_show_this_image'
COMMAND_HIDE_THIS_IMAGE = 'org_extra_hide_this_image'
COMMAND_HIDE_IMAGES = 'org_extra_hide_images'
COMMAND_SHOW_ERROR = 'org_extra_image_show_error'
COMMAND_JUMP_TO_ERROR = 'org_extra_image_jump_to_error'
COMMAND_TAB_CYCLING = 'org_tab_cycling'

EVENT_VIEWPORT_RESIZE = EventSymbol('viewport resize')

MSG_FETCH_IMAGES = '[{}] Fetching images in the {}...'
MSG_RENDER_IMAGES = 'Rendering...'
MSG_DONE = 'Done!'
MSG_NOTHING_CHANGED = 'Nothing to render.'

SELECTOR_ORG_SOURCE = 'text.orgmode'
SELECTOR_ORG_LINK = 'orgmode.link'
SELECTOR_ORG_LINK_TEXT_HREF = 'orgmode.link.text.href'

SETTING_INSTANT_RENDER = 'extra.image.instantRender'
SETTING_USE_LAZYLOAD = 'extra.image.useLazyload'
SETTING_VIEWPORT_SCALE = 'extra.image.viewportScale'
SETTING_CACHE_SIZE = 'extra.image.cacheSize'
SETTING_DEFAULT_TIMEOUT = 'extra.image.defaultTimeout'
SETTING_TIMEOUT_PER_MEGABYTE = 'extra.image.timeoutPerMegabyte'

LIST_SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".webp"]
LIST_VAL_ORG_ATTR = ['width', 'height']
LIST_LIT_ORG_ATTR = ['type']
LIST_OUTSIDE_SECTION_REGION_RANGES = ['all', 'pre-section']
LIST_HEADLINE_SELECTORS = [
    'orgmode.headline',
    'orgmode.headline2',
    'orgmode.headline3',
    'orgmode.headline4',
    'orgmode.headline5',
    'orgmode.headline6',
    'orgmode.headline7',
    'orgmode.headline8',
    'orgmode.headline9',
]
LIST_SUPPORTED_ORG_ATTR = LIST_LIT_ORG_ATTR + LIST_VAL_ORG_ATTR

REGEX_VAL_ORG_ATTR = re.compile(
    ":({})".format(
        '|'.join(LIST_VAL_ORG_ATTR)
    ) \
    + r"\s([\d\.]+)([\w%]*)"
)
REGEX_LIT_ORG_ATTR = re.compile(
    ":({})".format(
        '|'.join(LIST_LIT_ORG_ATTR)
    ) \
    + r"\s([\w]+)(.{0})"
)
REGEX_ORG_LINK = re.compile(r"\[\[([^\[\]]+)\]\s*(\[[^\[\]]*\])?\]")


# Setup
ImageCache = ConstrainedCache.use('image')
ImageCache.alloc(DEFAULT_CACHE_SIZE)
ImageCache.set_flag(ImageCache.FLAG_AUTOFREEUP_ON_SET, False)

emitter = EventEmitter()
_settings = sublime.load_settings(SETTING_FILENAME)

_settings.clear_on_change(ON_CHANGE_KEY)
_settings.add_on_change(ON_CHANGE_KEY, lambda: resize_alloc())


# Types
PID         = NewType('PID', int)
URL         = NewType('URL', str)
Timecost    = NewType('Timecost', float)
TupleRegion = Tuple[int, int]

Action = Literal[
    'open', 
    'save', 
    'unfold', 
    'undefined'
]
Command = Literal[
    'org_extra_render_images',
    'org_extra_show_images',
    'org_extra_show_this_image',
    'org_extra_hide_this_image',
    'org_extra_hide_images',
    'org_extra_image_show_error',
    'org_extra_image_jump_to_error'
]
ImageDict = TypedDict('ImageDict', {
    'original_url': str,
    'resolved_url': str,
    'pids': List[int],
    'regions': List[TupleRegion]
})
CachedImageDict = TypedDict('CachedImageDict', {
    'original_url': str,
    'resolved_url': str,
    'pids': List[int],
    'size': int,
    'width': Optional[float],
    'height': Optional[float],
    'regions': List[TupleRegion]
})
FetchErrorReport = Tuple[URL, Exception]
ErrorPanelRef = TypedDict('ErrorPanelRef', {
    'url': str,
    'error': str,
    'reason': str,
})
Event = Literal[
    'pre_close',
    'render',
    'render_error',
    'render_finish',
]
PhantomRefData = TypedDict('PhantomRefData', {
    'width': float,
    'height': float,
    'with_attr': bool,
    'is_hidden': bool,
    'is_placeholder': bool,
})
OnDataHandler = Callable[['CachedImage', List['CachedImage']], None]
OnErrorHandler = Callable[['Image', Exception], None]
OnFinishHandler = Callable[[List['CachedImage'], Timecost], None]
RegionRange = Literal[
    'all', 
    'folding', 
    'pre-section', 
    'auto'
]
SettingKey = Literal[
    'extra.image.instantRender',
    'extra.image.useLazyload',
    'extra.image.viewportScale',
    'extra.image.cacheSize',
    'extra.image.defaultTimeout',
    'extra.image.timeoutPerMegabyte',
]
Startup = Literal[
    "overview",
    "content",
    "showall",
    "showeverything",
    "fold",
    "nofold",
    "noinlineimages",
    "inlineimages",
    "logdone",
    "lognotedone"
]
SupportedOrgAttrs = Literal[
    'width',
    'height'
]
ViewContextData = TypedDict(
    'ViewContextData', 
    { 
        # Should be `True` once a view gains focus.
        'initialized': bool,
        # Errors encountered while downloading images, collected for 
        # the purpose of displaying them on the output panel 
        'fetch_error_reports': Dict[URL, ErrorPanelRef],
        # Indicate a view is downloading images in background. This 
        # implies that calling the command directly might be unsafe and 
        # could potentially initiate the reloading of images already in 
        # the process of being loaded.
        'is_downloading': bool,
        # Indicates the previous triggered image rendering was based on 
        # which user action. It's necessary to help skip some rendering 
        # steps to achieve instant responsiveness.
        'prev_action': str,
        # The event loop should compare this value between two ticks to 
        # decide whether to render the images again. This aids in 
        # simulating the responsiveness of images, although with a 
        # significant computational effort
        'viewport_extent': Tuple[float, float],
    }
)
PanelContextData = TypedDict(
    'PanelContextData',
    {
        'view_id': int,
        'lines_refs': List[Tuple[PID, ErrorPanelRef]]
    }
)
ParsedOrgAttrs = TypedDict(
    'ParsedOrgAttrs',
    {
        'width': float,
        'height': float,
        'type': str
    }
)
RenderEvents = NamedTuple('RenderEvents', [
    ('render', str),
    ('render_error', str),
    ('render_finish', str),
])
RenderGroups = NamedTuple('RenderGroups', [
    ('using_pids', List[CachedImageDict]),
    ('using_regions', List[CachedImageDict])
])
WindowOrView = Union[sublime.Window, sublime.View]


# Overloading
@overload
def get_settings(key: Literal['extra.image.useLazyload'], default_val: Optional[bool] = None) -> bool: ...
@overload
def get_settings(key: Literal['extra.image.instantRender'], default_val: Optional[bool] = None) -> bool: ...
@overload
def get_settings(key: Literal['extra.image.viewportScale'], default_val: Optional[float] = None) -> float: ...
@overload
def get_settings(key: Literal['extra.image.cacheSize'], default_val: Optional[int] = None) -> int: ...
@overload
def get_settings(key: Literal['extra.image.defaultTimeout'], default_val: Optional[int] = None) -> int: ...
@overload
def get_settings(key: Literal['extra.image.timeoutPerMegabyte'], default_val: Optional[int]) -> int: ...
@overload
def get_settings(key: SettingKey, default_val: Any = None) -> Any: ...


def event_factory(view: Optional[View] = None, event: Optional[Event] = None) -> str:
    """
    Create a string acting as a view event that can only be listened to 
    within the scope of objects authorized to access that view.
    """
    if not isinstance(view, sublime.View):
        return str(event)
    if event is None:
        return ''
    return '{}_{}'.format(event, view.id())


def context_data(view: View) -> ViewContextData:
    """
    Get a context wherein classes with the same view can share data 
    among themselves.
    """
    view_context = ContextData.use(view)
    view_context.setdefault('initialized', False)
    view_context.setdefault('fetch_error_reports', dict())
    view_context.setdefault('is_downloading', False)
    view_context.setdefault('prev_action', '')
    if 'viewport_extent' not in view_context:
        view_context['viewport_extent'] = view.viewport_extent()
    return view_context


def context_data_panel(panel: View) -> PanelContextData:
    """
    Like context_data() but for panels
    """
    panel_states = ContextData.use(panel)
    panel_states.setdefault('view_id', -1)
    panel_states.setdefault('lines_refs', [])
    return panel_states


def prefixed_output_panel(name: str) -> str:
    """
    Add the prefix `output.` before the output panel name to be able to 
    open it with the `show_panel` command.
    """
    return 'output.{}'.format(name)


def get_settings(key, default_val = None):
    """
    Get user's setting value from `OrgExtended.sublime-settings`
    """
    if key == SETTING_USE_LAZYLOAD:
        return settings.Get(SETTING_USE_LAZYLOAD, default_val or True)
    if key == SETTING_INSTANT_RENDER:
        return settings.Get(SETTING_INSTANT_RENDER, default_val or True)
    if key == SETTING_VIEWPORT_SCALE:
        return settings.Get(SETTING_VIEWPORT_SCALE, default_val or 1)
    if key == SETTING_CACHE_SIZE:
        return settings.Get(SETTING_CACHE_SIZE, default_val or DEFAULT_CACHE_SIZE)
    if key == SETTING_DEFAULT_TIMEOUT:
        return settings.Get(SETTING_DEFAULT_TIMEOUT, default_val or 0)
    if key == SETTING_TIMEOUT_PER_MEGABYTE:
        return settings.Get(SETTING_TIMEOUT_PER_MEGABYTE, default_val or 60)


def startup(value: Startup):
    """
    Get a value from Startup enum in the manner of a bedridden old man
    """
    return str(StartupValue[value])


def autodetect_region_range(view: View) -> RegionRange:
    """
    Detect current region range relies on the headings around the 
    cursor
    """
    prev_, next_ = find_headings_around_cursor(view, view.sel()[0])
    if not prev_ and not next_:
        return 'all'
    elif prev_:
        return 'folding'
    else:
        return 'pre-section'


def collect_image_regions(view: View, region: Region) -> List[Region]:
    """
    Collect all inlineimage regions in the specified region.
    """
    href_regions = find_by_selector_in_region(
        view, 
        region, 
        SELECTOR_ORG_LINK_TEXT_HREF)
    image_regions = []
    for region in href_regions:
        url = view.substr(region)
        is_image = any(
            url.endswith(ext) for ext in LIST_SUPPORTED_IMAGE_EXTENSIONS
        )
        if is_image:
            image_regions.append(region)
    return image_regions


def create_placeholder_phantom(view: View, region: Region) -> PID:
    """
    Pre-create an empty phantom to track the region for instant rendering 
    instead of doing so afterward. This enhances the user experience 
    and enables file editing while waiting for images to load
    """
    pm = PhantomsManager.of(view)
    pid = None
    if pm.has_phantom(region):
        pid = pm.get_pid_by_region(region)
        pm.erase_phantom(pid)
    new_pid = pm.add_phantom(region, '', { 'is_placeholder': True })
    pm.update_history(new_pid, pid)
    return new_pid


def fetch_or_load_image(
    url: str, 
    cwd: str, 
    timeout: Optional[float] = None,
    chunk_size: Optional[int] = None,
    termination_hook: Optional[Callable[[], bool]] = None,
    timeout_per_megabyte: Optional[Union[int, float]] = None,
) -> Optional[bytes]:
    """
    Remote fetch or load from local the image binary based on the 
    matching case of provided url.

    :param      url:      Accepts url or local file path
    :param      timeout:  Timeout for HTTP requests (None by default).
    """
    # Handle the case of the current working directory is a http/https url
    if cwd.startswith('http:') or cwd.startswith('https:'):
        relative_path = url
        absolute_url = resolve_remote(relative_path, cwd)
        response = download_binary(
            absolute_url,
            timeout,
            chunk_size,
            termination_hook,
            timeout_per_megabyte)
        return response
    # Handle the case of the provided url is already absolute
    elif url.startswith('http:') or url.startswith('https'):
        response = download_binary(
            url,
            timeout,
            chunk_size,
            termination_hook,
            timeout_per_megabyte)
        return response
    # Handle the case of the provided url is a local file path
    else:
        relative_path = url
        absolute_path = resolve_local(relative_path, cwd)
        with open(absolute_path, 'rb') as file:
            return file.read()


def find_current_headline(point: Point, hregions: List[Region]) -> Region:
    """
    Among many headings, find the heading that has the caret on it
    """
    for region in hregions:
        if region.contains(point):
            return region


def find_headings_around_cursor(
    view: View, 
    cursor: Region) -> Tuple[Optional[Region], Optional[Region]]:
    """
    Find the region of two headlines surrounding the cursor at any level.

    :returns:   A tuple containing the region of the previous and next
                headlines.
    """
    begin, end = cursor.to_tuple()
    regions = find_by_selectors(view, LIST_HEADLINE_SELECTORS)
    cursor_on_heading = any(
        view.match_selector(begin, selector) for selector in LIST_HEADLINE_SELECTORS)
    if cursor_on_heading:
        prev_ = find_current_headline(begin, regions)
        next_ = slice_regions(regions, begin = prev_.end() + 1)
        prev, next = prev_, at(next_, 0)
    else:
        prev_ = slice_regions(regions, end = end)
        next_ = slice_regions(regions, begin = begin + 1)
        prev, next = at(prev_, -1), at(next_, 0)
    return prev, next


def ignore_unchanged(
    view: View,
    regions: List[Region],
    show_hidden: bool = True) -> Tuple[List[Region], List[Region]]:
    """
    Filter out the regions that have already been rendered by 
    checking their existence in PhantomsManager. There are some 
    exceptions that will be included by the filter for re-rendering: \n
    - When ORG_ATTR has been added or removed. \n
    - When the :width or :height values of ORG_ATTR have changed.
    """
    pm = PhantomsManager.of(view)
    lines = view.lines(Region(0, view.size()))
    parser = OrgAttrParser(view)
    rendered_regions = pm.get_all_overseeing_regions()
    phantomless_regions = []
    dimension_changed_regions = []
    for region in regions:
        if region not in rendered_regions:
            phantomless_regions.append(region)
            continue

        current_line = view.line(region)
        index = lines.index(current_line)
        if index < 0:
            return dimension_changed_regions, phantomless_regions
        last_pid = pm.get_pid_by_region(region)
        last_data = pm.get_data_by_pid(last_pid) or {}
        upperline_region = lines[index - 1]
        attr = parser.parse(view.substr(upperline_region))
        width, height = attr.get('width'), attr.get('height')
        with_custom_dimension = width > 0 or height > 0

        if last_data.get('is_placeholder') == True:
            phantomless_regions.append(region)
            continue

        if last_data.get('is_hidden') == True:
            if show_hidden:
                phantomless_regions.append(region)
            continue


        # Re-render if the ORG_ATTR line was added or removed
        if with_custom_dimension != last_data.get('with_attr', False):
            dimension_changed_regions.append(region)

        # Re-render if the ORG_ATTR got the image width or height change
        elif with_custom_dimension == last_data.get('with_attr') == True:
            width_changed = width != last_data.get('width')
            height_changed = height != last_data.get('height')
            if width_changed or height_changed:
                dimension_changed_regions.append(region)
    return dimension_changed_regions, phantomless_regions


def is_folding_section(view: View) -> bool:
    """
    Return True if the cursor is positioned at a headline and the 
    content in that section headline is folding.
    """
    node = db.Get().AtInView(view)
    if node and not node.is_root():
        row, _col = view.curRowCol()
        if node.start_row == row and not node.is_folded(view):
            return True
    return False


def get_cwd(view: View) -> str:
    """
    Return the current working directory (cwd) if the current view is 
    not a scratch buffer. If it is, return the cwd depending on the url 
    of the opened scratch buffer.
    """
    cwd = get_url_from_scratch(view) \
        if view.is_scratch() \
        else path.dirname(view.file_name() or '')
    return cwd


def get_inbuffer_startup(view: View) -> List[str]:
    """
    Get the in-buffer startup values place on the top of the document.
    """
    file = db.Get().FindInfo(view)
    setting_startup = settings.Get('startup', ['showall'])
    inbuffer_startup = file.org[0].startup(setting_startup)
    return inbuffer_startup


def get_url_from_scratch(view: View) -> str:
    """
    A dumb way to get the url from a buffer that is opening a 
    remote .org file
    """
    name = view.name()
    if name.strip().startswith('[org-remote]'):
        url = name.replace('[org-remote]', '').strip()
        return url
    return ''


def resize_alloc() -> None:
    """
    Adjust the image cache size according to user settings.
    """
    amount_alloc = settings.Get(SETTING_CACHE_SIZE, DEFAULT_CACHE_SIZE)
    if amount_alloc < MIN_CACHE_SIZE:
        ImageCache.alloc(MIN_CACHE_SIZE)
        return None
    if amount_alloc != ImageCache.current_max_size:
        ImageCache.alloc(int(amount_alloc))
        return None


def resolve_local(url: str, cwd: str = '/') -> str:
    """
    Convert any case of local path to absolute path, ignoring remote url.
    """
    if not url.startswith('http:') and not url.startswith('https:'):
        filepath = url
        if url.startswith("file:"):
            filepath = url.replace('file:', '')
        if filepath.startswith('/'):
            return filepath
        if filepath.startswith('~'):
            return path.expanduser(filepath)
        else:
            return path.abspath(path.join(cwd, filepath))
    return url


def resolve_remote(filepath: str, cwd: str = '') -> str:
    """
    Convert any remote URL to an absolute URL. This function can be used 
    to transform relative links in a remote .org file into absolute 
    links, making images renderable.
    """
    if filepath.startswith('http:') or filepath.startswith('https:'):
        return filepath
    if cwd.startswith('http:') or cwd.startswith('https:'):
        if filepath.startswith('file:'):
            filepath = filepath.replace('file:', '')
        if filepath.startswith('~'):
            filepath = filepath[1:]
        return urllib.parse.urljoin(cwd, filepath)
    return ''


def select_region(view: View, render_range: RegionRange) -> Region:
    """
    Obtain the region corresponding to the render_range. \n
    Example:
    ```py
    select_region(view, 'all')
    # Region(0, view.size())
    ```
    """
    if render_range == 'pre-section':
        prev_, next_ = find_headings_around_cursor(view, Region(0, 0))
        return select_region('all') if not next_ else region_between(view, Region(0, 0), next_)
    if render_range == 'folding':
        cursor_region = get_cursor_region(view)
        prev_, next_ = find_headings_around_cursor(view, cursor_region)
        return region_between(view, prev_, next_)
    return Region(0, view.size())


def matching_context(view: View) -> bool:
    """
    Run this function on top of the method to filter out most of 
    unappropriate scope.
    """
    if not view.match_selector(0, SELECTOR_ORG_SOURCE):
        return False
    return True



class OrgExtraImageLifeCycle(sublime_plugin.EventListener):
    """
    Initialize the context upon entering the view in the first time and 
    purging them on closing.
    """
    def on_init(self, views) -> None:
        """
        Adjust the image cache size according to user settings.
        """
        resize_alloc()


    def on_activated(self, view: View) -> None:
        """
        Modify the default values from their initial values
        """
        if not matching_context(view):
            return None
        view_context = context_data(view)
        if not view_context.get('initialized') == True:
            view_context['initialized'] = True
            view_context['prev_action'] = 'open'


    def on_pre_close(self, view: sublime.View) -> None:
        """
        Should remove relevant data before closing out the view to
        avoid memory leaks.
        """
        if matching_context(view):
            preclose = event_factory(view, 'pre_close')
            emitter.emit(preclose)
        PhantomsManager.abandon(view)
        ContextData.remove(view)



class AutoloadInlineImages(sublime_plugin.EventListener):
    """
    Autoload inlineimages at startup
    """
    def on_activated(self, view: View) -> None:
        """
        Reworked the show images on startup feature with performance
        optimization. It applies to the files opened by Goto Anything
        as well.
        Why not on_load?
        1. It won't solve the Goto Anything case
        2. Sometimes it would trigger the command twice
        """
        self.autoload(view)


    @classmethod
    def autoload(cls, view: View) -> None:
        """
        Display all images in the view. This action should only be 
        performed once each time the file is opened with the in-buffer
        startup 'inlineimages' value
        """
        if not matching_context(view):
            return None
        if PhantomsManager.available_for(view):
            return None
        inbuffer_startup = get_inbuffer_startup(view)
        PhantomsManager.adopt(view)
        if startup('inlineimages') in inbuffer_startup:
            view.run_command(COMMAND_SHOW_IMAGES, 
                args = {
                    'region_range': 'all'
                }
            )



class Lazyload(sublime_plugin.EventListener):
    """
    Render images upon unfolding a section.
    """
    def on_post_text_command(self, view: View, cmd: str, args: Dict) -> Any:
        if cmd != COMMAND_TAB_CYCLING:
            return None
        if not matching_context(view):
            return None
        lazyload_images = get_settings(SETTING_USE_LAZYLOAD)
        if not lazyload_images:
            return None
        status_message = view.get_status(STATUS_ID)
        if status_message:
            return None
        if is_folding_section(view):
            view.run_command(COMMAND_SHOW_IMAGES, 
                args = {
                    'region_range': 'folding'
                }
            )



class RerenderChangedAttributes(sublime_plugin.EventListener):
    """
    Instantly re-render images upon saving the file if any ORG_ATTR 
    values associated with them are changed.
    """
    def on_post_save(self, view: View):
        if not matching_context(view):
            return None
        if not PhantomsManager.available_for(view):
            return None
        image_regions = self.find_resized_images(
            view,
            show_hidden = False)

        if len(image_regions) > 0:
            self.quick_rerender(view, image_regions)


    @staticmethod
    def find_resized_images(view: View, show_hidden: bool = False) -> List[Region]:
        """
        Collect all cached images that have width or height changed in 
        ORG_ATTR
        """
        region_range = autodetect_region_range(view)
        selected_region = select_region(view, region_range)
        image_regions = collect_image_regions(view, selected_region)
        dimension_changed, _ = ignore_unchanged(
            view,
            image_regions,
            show_hidden)
        return dimension_changed


    @staticmethod
    def quick_rerender(view: View, image_regions: List[Region]) -> None:
        """
        Directly call the render command.
        """
        listener_id = uuid4().hex
        render_finished = event_factory(view, 'render_finish')
        def render_finish_handler(value: Any):
            if value == listener_id:
                view.set_read_only(False)
                emitter.off(render_finished, render_finish_handler)

        cached_images = []
        cwd = get_cwd(view)
        for region in image_regions:
            url = view.substr(region)
            image = Image(cwd, url)
            cached_image = CachedImage(image)
            cached_image.regions.add(region.to_tuple())
            cached_images.append(cached_image.to_dict())
        view.set_read_only(True)
        emitter.once(render_finished, render_finish_handler)
        view.run_command(COMMAND_RENDER_IMAGES,
            args = {
                'images': cached_images,
                'emit_args': listener_id
            }
        )



class OrgExtraShowImagesCommand(sublime_plugin.TextCommand):
    """
    An improved version of the legacy org_show_images, with the ability 
    to load images in independent threads
    Supports rendering range options:\n
    - 'folding': Loads and renders all images in the folding content. 
      (Default option)\n
    - 'all': Loads and renders all images in the document. 
      (Automatically applied to files with the setting #+STARTUP: inlineimages)
    - 'pre-section': Applies to the region preceding the first heading 
      in the document.
    - 'auto': Automatically detects the rendering range.
    """
    def run(
        self,
        edit,
        region_range: RegionRange = 'folding',
        show_hidden: bool = True,
        image_regions: List[TupleRegion] = []
    ) -> None:
        view = self.view
        try:
            if not matching_context(view):
                return None
            self.autofreeup_cache()

            if region_range == 'auto':
                region_range = autodetect_region_range(view)

            cwd = get_cwd(view)
            image_regions = self.rescan_image_regions(
                region_range,
                image_regions)
            dimension_changed, phantomless = ignore_unchanged(
                view,
                image_regions,
                show_hidden)
            image_regions = dimension_changed + phantomless

            # Show the status message: Nothing to render.
            if not image_regions:
                return self.handle_nothing_changed(status_duration = 3)

            images = self.create_images_list(
                cwd, 
                dimension_changed, 
                phantomless
            )
            noncached_images, cached_images = self.classify_images(images)

            error_reports = []
            instant_render = get_settings(SETTING_INSTANT_RENDER)
            tpm = get_settings(SETTING_TIMEOUT_PER_MEGABYTE) or MAX_TPM
            timeout = get_settings(SETTING_DEFAULT_TIMEOUT)
            fetch_status = FetchStatusIndicator(view, region_range, len(images))
            render_status = RenderStatusIndicator(view)
            listener = RenderStatusUpdater(view, render_status)
            timer = listener.timer()
            # listener.on_finish(
            #     lambda: self.handle_fetch_exceptions(error_reports) \
            #     if not instant_render else None
            # )
            fetch_status.start()
            timer.start_fetch()

            if cached_images:
                def early_render(images: List['CachedImage'], _):
                    """
                    If there are no images to download, why not start 
                    the render status by now?
                    """
                    if not noncached_images:
                        fetch_status.stop()
                        listener.start_render()

                self.parallel_requests_using_threads(
                    cwd,
                    split_into_chunks(
                        cached_images,
                        DEFAULT_POOL_SIZE
                    ),
                    on_finish = self.on_fetch_finished(
                        listener.id(),
                        then = early_render
                    )
                )
                fetch_status.succeed(len(cached_images))

            view_context = context_data(view)
            view_context['is_downloading'] = True

            def after_response():
                """
                We should only start the render phase of the timer 
                (timer.start_render()) instead of including the 
                render_status (render_status.start()) to avoid display 
                conflicts with fetch_status when in instant_render mode.
                """
                fetch_status.succeed()
                if instant_render and not timer.is_rendering():
                    listener.start_render(with_status = False)

            def after_finished(
                images: List['CachedImage'],
                timecost: Timecost
            ) -> None:
                """
                If instant_render is not enabled, it means the render 
                status starts running only from this point onward.
                """
                view_context['is_downloading'] = False
                fetch_status.stop(timecost)
                if instant_render:
                    # self.handle_fetch_exceptions(error_reports)
                    pass
                elif not timer.is_rendering():
                    listener.start_render()

            """
            Download the images. It should run on a separate 
            thread to ensure it doesn't block our main thread. Thereby, 
            we can freely edit the file while waiting.
            """
            sublime.set_timeout_async(
                lambda: self.parallel_requests_using_threads(
                    cwd,
                    split_into_chunks(
                        noncached_images,
                        DEFAULT_POOL_SIZE,
                    ),
                    timeout,
                    timeout_per_megabyte = tpm,
                    on_data = self.on_response(
                        emit_args = listener.id(),
                        then = after_response
                    ),
                    on_error = lambda image, error: [
                        fetch_status.failed(),
                        error_reports.append(
                            (
                                image.original_url,
                                error
                            )
                        )
                    ],
                    on_finish = self.on_fetch_finished(
                        emit_args = listener.id(),
                        then = after_finished)
                )
            )
        except Exception as error:
            show_message(error)
            traceback.print_tb(error.__traceback__)


    def autofreeup_cache(self) -> None:
        """
        Manually check and release the image cache to ensure
        there are enough memory space
        """
        return ImageCache.autofreeup()


    def classify_images(self, images: List['Image']) -> Tuple[
        List['Image'],
        List['Image']
    ]:
        """
        Separate a list of images into cached and non-cached.
        """
        noncached_images, cached_images = [], []
        for image in images:
            if ImageCache.has(image.resolved_url):
                cached_images.append(image)
            else:
                noncached_images.append(image)
        return noncached_images, cached_images


    def create_images_list(
        self, 
        cwd: str,
        rerender_regions: List[Region] = [],
        phantomless_regions: List[Region] = []
    ) -> List['Image']:
        """
        Create a list of image objects with placeholder phantoms for 
        precise rendering.
        """
        image_dict = dict()
        pm = PhantomsManager.of(self.view)
        regions = list(rerender_regions) + list(phantomless_regions)
        for region in regions:
            url = self.view.substr(region)
            if url not in image_dict:
                image_dict[url] = Image(cwd, url)
            if region in rerender_regions:
                pid = pm.get_pid_by_region(region)
            else:
                pid = create_placeholder_phantom(self.view, region)
            if pid is not None:
                image_dict[url].pids.add(pid)
        return list(image_dict.values())


    def handle_fetch_exceptions(self, error_reports: List[FetchErrorReport]) -> None:
        """
        Show an output panel indicating what errors have occurred.
        """
        view_context = context_data(self.view)
        fetch_error_reports = view_context.get('fetch_error_reports')
        fetch_error_reports.clear()
        for exc in error_reports:
            url, exception = exc
            fetch_error_reports[url] = {
                'url': url,
                'error': exception.__class__.__name__,
                'reason': str(exception)
            }
        # self.view.run_command(COMMAND_SHOW_ERROR,
        #     args = {
        #         'view_id': self.view.id(),
        #     }
        # )


    def handle_nothing_changed(self, status_duration: int) -> None:
        """
        Only call this method with a return statement when the view has 
        nothing to update.
        
        :param      status_duration:  Delay in second to clear the status message
        """
        return set_temporary_status(
            self.view, 
            STATUS_ID, 
            MSG_NOTHING_CHANGED, 
            status_duration
        )


    def on_response(
        self, 
        emit_args: Any,
        then: Optional[OnDataHandler] = None
    ) -> OnDataHandler:
        """
        Render each image promptly upon download, if instant_render is 
        enabled. The rendered image will be removed from the list to 
        prevent it from being rendered again inappropriately.
        """
        instant_render = get_settings(SETTING_INSTANT_RENDER)
        def _func(image: CachedImage, images: List[CachedImage]):
            if instant_render:
                self.view.run_command(COMMAND_RENDER_IMAGES,
                    args = {
                        'images': [image.to_dict()],
                        'emit_args': emit_args,
                        'block': False,
                    }
                )
                images.remove(image)
            if callable(then):
                safe_call(then, (image, images))
        return _func


    def on_fetch_finished(
        self, 
        emit_args: Any = None,
        then: Optional[OnFinishHandler] = None
    ) -> OnFinishHandler:
        """
        If the image was not rendered earlier due to instant_render, then 
        this is the time to do it.
        """
        def _func(ci: List['CachedImage'], timecost: Timecost):
            if callable(then):
                safe_call(then, (ci, timecost))
            self.view.run_command(COMMAND_RENDER_IMAGES,
                args = {
                    'images': ImageList(ci).to_dict(),
                    'emit_args': emit_args,
                    'block': False,
                }
            )
        return _func


    def parallel_requests_using_sublime_timeout(
        self,
        cwd: str,
        pools: List[List['Image']],
        timeout: Optional[float] = None,
        on_data: Optional[OnDataHandler] = None,
        on_error: Optional[OnErrorHandler] = None,
        on_finish: Optional[OnFinishHandler] = None,
        timeout_per_megabyte: Optional[float] = None,
    ) -> None:
        """
        Asynchronously run fetching image operations, where each 
        operation takes a list of images with URLs/filepaths that can be 
        loaded one after another. The following are the measurement 
        results that can be compared to `.parallel_requests_using_threads()`:
        - 1 image link: 1.482095375999961, 1.335890114999529, 1.298129551000784
        - 10 image links: 8.051044771000306, 7.954113742000118, 8.047068934999515
        - 85 image links: 93.40657268199993
        """
        start = default_timer()
        preclose = event_factory(self.view, 'pre_close')
        stop_event = threading.Event()
        terminate_threads = lambda: stop_event.set()
        emitter.once(preclose, terminate_threads)

        cached_images = []
        def on_error_wrapper(image: 'Image', exc: Exception) -> None:
            if callable(on_error):
                safe_call(
                    on_error,
                    (image, exc)
                )
        def on_finish_wrapper(ci: List['CachedImage']) -> None:
            if is_iterable(ci):
                cached_images.extend(ci)
                if callable(on_finish):
                    safe_call(
                        on_finish,
                        (ci, default_timer() - start)
                    )
        try:
            return starmap_async(
                callback = lambda images: self.thread_execution(
                    cwd,
                    images,
                    timeout,
                    timeout_per_megabyte,
                    on_data,
                    on_error_wrapper,
                    stop_event
                ),
                args = pools,
                on_finish = on_finish_wrapper
            )
        finally:
            emitter.clear_listeners(preclose)


    def parallel_requests_using_threads(
        self,
        cwd: str,
        pools: List[List['Image']],
        timeout: Optional[float] = None,
        on_data: Optional[OnDataHandler] = None,
        on_error: Optional[OnErrorHandler] = None,
        on_finish: Optional[OnFinishHandler] = None,
        timeout_per_megabyte: Optional[float] = None,
    ) -> None:
        """
        Run threads of fetching image operations in parallel, where each 
        thread takes a list of images with urls/filepaths that can be 
        loaded one after another.
        The following are the measurement results that can be compared to 
        `.parallel_requests_using_sublime_timeout()`: \n
        * 1 image link: 1.1709886939997887, 1.2005657659992721, 1.2650837740002316 \n
        * 10 image links: 1.387642825000512, 1.5033762000002753, 2.1559584730002825 \n
        * 85 image links: 16.07192872700034, 12.695106612000018, 9.564963673999955 \n
        """
        start = default_timer()
        stop_event = threading.Event()
        preclose = event_factory(self.view, 'pre_close')
        terminate_threads = lambda: stop_event.set()
        emitter.once(preclose, terminate_threads)

        cached_images = []
        try:
            cached_images = shallow_flatten(
                starmap_pools(
                    lambda images: self.thread_execution(
                        cwd, 
                        images, 
                        timeout,
                        timeout_per_megabyte,
                        on_data,
                        on_error,
                        stop_event),
                    pools,
                    name = COMMON_THREAD_NAME,
                )                
            )
            if callable(on_finish):
                safe_call(
                    on_finish, 
                    (cached_images, default_timer() - start)
                )
        except Exception as error:
            show_message(error)
            traceback.print_tb(error.__traceback__)
        finally:
            emitter.clear_listeners(preclose)
        return cached_images


    def rescan_image_regions(
        self, 
        region_range: RegionRange, 
        given_regions: List[Region] = []
    ) -> List[Region]:
        """
        Rescan for a new list of image regions if `given_regions` is not 
        provided.
        """
        constricted_region = select_region(self.view, region_range)
        if not given_regions or type(given_regions) is not list:
            image_regions = collect_image_regions(self.view, constricted_region)
        else:
            image_regions = []
            for tuple_region in given_regions:
                try:
                    region = Region(*tuple_region)
                    image_regions.append(region)
                except:
                    continue
        return image_regions


    def thread_execution(
        self,
        cwd: str,
        images: List['Image'],
        timeout: Optional[float] = None,
        tpm: Optional[float] = None,
        on_data: Optional[OnDataHandler] = None,
        on_error: Optional[OnErrorHandler] = None,
        stop_event: Optional[threading.Event] = None
    ) -> List['CachedImage']:
        """
        This method specifies what a thread should do, typically 
        revolving around ensuring that the images have been cached and 
        are ready for rendering.
        """
        cached_images = []
        abort_request = lambda: stop_event.is_set()
        for image in images:
            try:
                cached_binary = ImageCache.get(image.resolved_url)
                if type(cached_binary) is not bytes:
                    loaded_binary = fetch_or_load_image(
                        image.original_url,
                        cwd,
                        timeout,
                        termination_hook = abort_request,
                        timeout_per_megabyte = tpm
                    )
                    if loaded_binary:
                        ImageCache.set(image.resolved_url, loaded_binary)
                        cached_binary = loaded_binary
                if isinstance(stop_event, threading.Event):
                    if stop_event.is_set():
                        raise ThreadTermination()
                if cached_binary is None and callable(on_error):
                    safe_call(
                        on_error,
                        (image, CorruptedImage('got a corrupted image'))
                    )
                cached_image = CachedImage(image)
                cached_images.append(cached_image)
                if callable(on_data):
                    safe_call(
                        on_data,
                        (cached_image, cached_images)
                    )
            except Exception as error:
                print(error)
                traceback.print_tb(error.__traceback__)
                safe_call(on_error, [image, error])
                continue
        return cached_images



class OrgExtraShowThisImageCommand(sublime_plugin.TextCommand):
    """
    Show a single image
    """
    def run(
        self,
        edit,
        image_region: Optional[Tuple[int, int]] = [None, None]):
        view = self.view
        try:
            if not matching_context(view):
                return None
            begin, end = image_region
            if (type(begin) is not int) or (type(end) is not int):
                sel = view.sel()
                region = view.expand_to_scope(
                    sel[0].begin(),
                    SELECTOR_ORG_LINK_TEXT_HREF)
                if region is None:
                    return None
                image_region = region.to_tuple()
            
            view_context = context_data(view)
            if not view_context['is_downloading']:
                return view.run_command(COMMAND_SHOW_IMAGES,
                    args = {
                        'region_range': 'auto',
                        'show_hidden': True,
                        'image_regions': [image_region]
                    }
                )
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)



class OrgExtraHideThisImageCommand(sublime_plugin.TextCommand):
    """
    Hide the image in the region specified by `image_region`. If this 
    parameter is not provided, automatically expand to a 
    `orgmode.link.text.href` scope around the cursor if exist.
    Essentially, this command is similar to the legacy `org_hide_image` 
    command, except that it only hides images that are being managed by 
    PhantomsManager.
    Furthermore, even though those images is hidden from view, the phantoms 
    containing those images are still retained (which is why you see the 
    next line looks a little bulged, about 1-2px). This helps the 
    `show_images` command distinguish between hidden images and newly 
    added/phantomless images through the attached `is_hidden` flag.
    """
    def run(
        self, 
        edit, 
        image_region: Optional[Tuple[int, int]] = [None, None]):
        view = self.view
        try:
            if not matching_context(view):
                return None
            begin, end = image_region
            if type(begin) is int and type(end) is int:
                region = Region(begin, end)
                pm = PhantomsManager.of(view)
                pid = pm.get_pid_by_region(region)
                if pid is not None:
                    is_erased = pm.erase_phantom(pid)
                    if is_erased:
                        new_pid = pm.add_phantom(region, '', { 'is_hidden': True })
                        pm.update_history(new_pid, pid)
            else:
                sel = view.sel()
                region = view.expand_to_scope(
                    sel[0].begin(),
                    SELECTOR_ORG_LINK_TEXT_HREF)
                if region is not None:
                    view.run_command(
                        COMMAND_HIDE_THIS_IMAGE, { 'image_region': region.to_tuple() })
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)



class OrgExtraHideImagesCommand(sublime_plugin.TextCommand):
    """
    Selecting and hiding images in region.
    """
    status_id = STATUS_ID + '_hide'
    status_duration_in_second = 3

    def run(
        self,
        edit,
        region_range: RegionRange = 'folding'
    ):
        view = self.view
        try:
            if not matching_context(view):
                return None

            if region_range == 'auto':
                region_range = autodetect_region_range(view)

            pm = PhantomsManager.of(view)
            selected_region = select_region(view, region_range)
            image_regions = collect_image_regions(view, selected_region)
            erased_count = 0
            for region in image_regions:
                pid = pm.get_pid_by_region(region)
                if pid is not None:
                    data = pm.get_data_by_pid(pid)
                    if data.get('is_hidden'):
                        continue
                    is_erased = pm.erase_phantom(pid)
                    if is_erased:
                        erased_count += 1
                        new_pid = pm.add_phantom(region, '', { 'is_hidden': True })
                        pm.update_history(new_pid, pid)

            if erased_count > 0:
                set_temporary_status(
                    view,
                    self.status_id,
                    ('Success! {} image is now hidden' \
                        if erased_count == 1 \
                        else 'Success! {} images are now hidden').format(erased_count),
                    self.status_duration_in_second)
        except Exception as error:
            show_message(error, level = 'error')
            traceback.print_tb(error.__traceback__)



class OrgExtraRenderImagesCommand(sublime_plugin.TextCommand):
    """
    Render cached image to the view with the ideal size.\n
    ---
    Firstly, you need to know that ImageCache is a ConstrainedCache with 
    keys being image URLs or absolute filepath, facilitating access to 
    their cached binary data.\n
    ---
    The rendering mechanism operates based on previously marked regions 
    with placeholder phantoms, through the corresponding pids list of 
    each CachedImageDict. This helps it render super accurately 
    regardless of whether users unintentionally or intentionally change 
    the regions of those [[image]] while editing the file. Once the 
    position for rendering is determined, it fetches the image from the 
    cache and renders it instantly.
    While this is the most accurate rendering method, it only works when 
    placeholder phantoms are obtained from calling the 
    'org_extra_show_images' command, which sometimes may not meet your 
    performance expectations. Therefore, if you want to achieve both 
    accuracy and performance, implement by yourself a function to attach 
    placeholders for the images you want to render.
    Additionally, there is another faster but less secure option for you, 
    which is to render directly through the region. It's faster because 
    it doesn't rescan change history from the PhantomsManager, but less 
    secure because the region is a static area, while its content may 
    change based on user editing. So make sure you have a way to prevent 
    that from happening before choosing this option (such as using 
    set_read_only, for example).
    """
    def run(
        self,
        edit,
        images: List[CachedImageDict] = [],
        emit_args: Any = None,
        block: bool = True
    ) -> None:
        try:
            if not matching_context(self.view) or not images:
                return None
            cached_images = self.ignore_uncached(images)
            render_groups = self.group_images(cached_images)
            render_context = RenderContext(self.view)
            event_dispatcher = RenderEventDispatcher(self.view, emit_args)
            cumulator_cb = CumulativeCallbackTrigger(
                threshold = 2,
                func = lambda: [
                    event_dispatcher.render_finish(),
                ]
            )
            self.try_render(
                render_context,
                event_dispatcher,
                cumulator_cb,
                render_groups,
                block
            )
        except Exception as error:
            show_message(error)
            traceback.print_tb(error.__traceback__)


    def extract_image_dimensions(
        self,
        ctx: 'RenderContext',
        attr_line: str,
        image_binary: bytes) -> Tuple[int, int, bool]:
        """
        Extract image width, height from its binary.

        :returns: A tuple containing width, height, and a boolean value 
        indicating whether the image is resized using ORG_ATTR.
        """
        attr = OrgAttrParser(ctx.view).parse(attr_line)
        width, height = attr.get('width'), attr.get('height')
        if width > 0 and height > 0:
            return width, height, True
        else:
            b_width, b_height = image_size_of(image_binary)
            if b_width is None: b_width = 0
            if b_height is None: b_height = 0
            if width > 0:
                return width, (width * b_height / b_width), True
            elif height > 0:
                return (height * b_width / b_height), height, True
            else:
                return b_width, b_height, False


    def fit_to_viewport(
        self, 
        vw: Union[int, float], 
        width: int, 
        height: int) -> Tuple[int, int]:
        """
        Adjust the image width to prevent it from overflowing the 
        viewport, and simultaneously adjust the height to maintain 
        proportional scaling with the width.
        """
        if width > vw:
            temp = vw / width
            height *= temp
            width = vw
        return width, height


    def clone_as_vacant(self, image: CachedImageDict) -> CachedImageDict:
        """
        Return a clone image dict, but with empty pids and regions.
        """
        clone = image.copy()
        clone['pids'] = []
        clone['regions'] = []
        return clone


    def group_images(self, images: List[CachedImageDict]) -> RenderGroups:
        """
        Split the images list into two array groups: render by provided 
        pids and render by provided regions.
        """
        using_pids, using_regions = [], []
        render_groups = RenderGroups(using_pids, using_regions)
        for image in images:
            pids, regions = image.get('pids'), image.get('regions')
            if not pids and not regions:
                continue
            if pids:
                using_pids.append(image.copy())
            if regions:
                using_regions.append(image.copy())
        return render_groups


    def handle_render(
        self,
        ctx: 'RenderContext',
        region: Region,
        image: CachedImageDict
    ) -> bool:
        """
        DRY render method. Return a boolean value as render result
        """
        line_region = ctx.view.line(region)
        line_text = ctx.view.substr(line_region)
        row, _ = ctx.view.rowcol(region.begin())
        preceding_region = get_region_by_rows(ctx.view, row - 1)
        preceding_line = ctx.view.substr(preceding_region)
        indent_level = len(line_text) - len(line_text.lstrip())
        image_binary = ImageCache.get(image.get('resolved_url'))
        vw, _ = ctx.view.viewport_extent()
        width, height, is_resized = self.extract_image_dimensions(
            ctx,
            preceding_line,
            image_binary,
        )
        if image.get('width') > 0:
            width = image.get('width')
        if image.get('height') > 0:
            height = image.get('height')
        width, height = self.fit_to_viewport(vw, width, height)
        _, new_pid = self.render_image(
            image,
            region,
            width,
            height,
            indent_level,
            is_resized)
        return new_pid is not -1


    def ignore_uncached(
        self, 
        images: List[CachedImageDict]) -> List[CachedImageDict]:
        """
        Remove the uncached images from the list. The responsibility of 
        this command is to render images, not to re-download them.
        """
        cached_images = []
        cwd = get_cwd(self.view)
        for image in images.copy():
            resolved_url = image.get('resolved_url')
            if not resolved_url:
                resolved_url = resolve_local(image.get('original_url'), cwd)
                image['resolved_url'] = resolved_url
            if not ImageCache.has(resolved_url):
                continue
            cached_images.append(image)
        return cached_images


    def render_image(
        self,
        image: CachedImageDict,
        image_region: Region,
        image_width: Width,
        image_height: Height,
        indent_level: int = 0, 
        with_attr: bool = False
    ) -> Tuple[Union[PID, None], Union[PID, None]]:
        """
        Render the image. The rendered image can be hovered over to 
        show the absolute URL/path and filesize.
        :returns: Old PID and new PID
        """
        image_binary = ImageCache.get(image.get('resolved_url'))
        width, height = int(image_width), int(image_height)
        image_format = image_format_of(image_binary)
        space_indent = '&nbsp;' * (indent_level * 2)
        pm = PhantomsManager.of(self.view)
        pid = None
        if image_format == 'svg':
            html = image_to_string(image_binary)
        else:
            base64 = image_to_base64(image_binary)
            size = image.get('size')
            title = '[{}x{}] {} ({})'.format(
                width,
                height,
                image.get('resolved_url'),
                sizeof_fmt(size if size > 0 else len(image_binary))
            )
            encoded_image = '{};base64,{}'.format(
                image_format, 
                base64
            )
            attributes = 'width="{}" height="{}" title="{}"'.format(
                width, 
                height,
                title,
            )
            html = u'{}<img src="data:image/{}" class="centerImage" {}>'.format(
                space_indent,
                encoded_image,
                attributes,
            )
        if pm.has_phantom(image_region):
            pid = pm.get_pid_by_region(image_region)
            pm.erase_phantom(pid)
        
        new_pid = pm.add_phantom(image_region, html, { 
            'width': width, 
            'height': height,
            'with_attr': with_attr })
        if new_pid != -1:
            pm.update_history(new_pid, pid)
        return pid, new_pid


    def render_using_pids(
        self,
        ctx: 'RenderContext',
        pub: 'RenderEventDispatcher',
        cul: 'CumulativeCallbackTrigger',
        images: List[CachedImageDict],
        allow_edit: bool = True,
        _recursive: bool = False,
    ):
        if not allow_edit:
            ctx.view.set_read_only(True)
        for image in images:
            try:
                region_set = set(
                    tuple(region) for region in image.get('regions', [])
                )
                for pid in image.get('pids', []).copy():
                    region = ctx.region_of(pid)
                    if region is not None:
                        if region.to_tuple() not in region_set:
                            render_success = self.handle_render(ctx, region, image)
                            if render_success:
                                pub.render_success(region, image)
                            else:
                                rimage = self.clone_as_vacant(image)
                                rimage.get('pids').append(pid)
                                self.render_using_pids(
                                    ctx,
                                    pub,
                                    cul,
                                    [rimage],
                                    allow_edit = False,
                                    _recursive = True
                                )
            except Exception as error:
                print(error)
                traceback.print_tb(error.__traceback__)
                pub.render_error(error)
        if not allow_edit:
            ctx.view.set_read_only(False)
        if not _recursive:
            cul.charge()


    def render_using_regions(
        self, 
        ctx: 'RenderContext', 
        pub: 'RenderEventDispatcher',
        cul: 'CumulativeCallbackTrigger',
        images: List[CachedImageDict],
        allow_edit: bool = True,
        _recursive: bool = False,
    ):
        if not allow_edit:
            ctx.view.set_read_only(True)
        for image in images:
            try:
                region_set = set(tuple(region) for region in image.get('regions', []))
                for region_tuple in region_set.copy():
                    region = Region(*region_tuple)
                    render_success = self.handle_render(ctx, region, image)
                    if render_success:
                        pub.render_success(region, image)
                    else:
                        rimage = self.clone_as_vacant(image)
                        rimage.get('regions').append(region)
                        self.render_using_pids(
                            ctx,
                            pub,
                            cul,
                            [rimage],
                            allow_edit = False,
                            _recursive = True)
            except Exception as error:
                print(error)
                traceback.print_tb(error.__traceback__)
                pub.render_error(error)
        if not allow_edit:
            ctx.view.set_read_only(False)
        if not _recursive:
            cul.charge()


    def try_render(
        self, 
        ctx: 'RenderContext',
        pub: 'RenderEventDispatcher',
        cul: 'CumulativeCallbackTrigger',
        groups: RenderGroups,
        block: bool = True,
    ) -> None:
        render_using_pids = lambda: self.render_using_pids(
            ctx,
            pub,
            cul,
            groups.using_pids
        )
        render_using_regions = lambda: self.render_using_regions(
            ctx,
            pub,
            cul,
            groups.using_regions
        )
        if block:
            render_using_pids()
            render_using_regions()
        else:
            sublime.set_timeout_async(render_using_regions)                
            threading.Thread(
                target = render_using_pids,
                name = RENDER_THREAD_NAME
            ).start()



class TimeMeasurement(object):
    """
    Calculate the accumulated timecost
    """
    def __init__(self) -> None:
        self.timecost = 0
        self.fetch_begin = 0
        self.fetch_end = 0
        self.render_begin = 0
        self.render_end = 0
        self.fetching = False
        self.rendering = False
        self.finished = False

    def is_fetching(self) -> bool:
        return self.fetching

    def is_rendering(self) -> bool:
        return self.rendering

    def is_lazy(self) -> bool:
        return not self.fetching and not self.rendering

    def is_finished(self) -> bool:
        return self.finished

    def start_fetch(self) -> 'TimeMeasurement':
        if self.fetch_end > 0:
            return self
        self.fetch_begin = default_timer()
        self.fetching = True
        return self

    def stop_fetch(self) -> 'TimeMeasurement':
        if self.fetch_end > 0:
            return self
        self.fetch_end = default_timer()
        self.fetching = False
        self.timecost = self.timecost + (self.fetch_end - self.fetch_begin)
        return self

    def start_render(self, stop_fetch: bool = True) -> 'TimeMeasurement':
        if stop_fetch:
            self.stop_fetch()
        if self.render_end > 0:
            return self
        self.render_begin = default_timer()
        self.rendering = True
        return self

    def stop_render(self) -> 'TimeMeasurement':
        self.stop_fetch()
        if self.render_end > 0:
            return self
        self.render_end = default_timer()
        self.rendering = False
        self.timecost = self.timecost + (self.render_end - self.render_begin)
        self.finished = True
        return self

    def fetch_timecost(self) -> float:
        if self.fetching:
            return default_timer() - self.fetch_begin
        return self.fetch_end - self.fetch_begin

    def render_timecost(self) -> float:
        if self.rendering:
            return default_timer() - self.render_begin
        return self.render_end - self.render_begin

    def accumulated_timecost(self) -> float:
        if self.fetching or self.rendering:
            return default_timer() - self.fetch_begin
        return self.timecost



class CumulativeCallbackTrigger(object):
    """
    Call a function when the .charge() method is called a sufficient 
    number of times (threshold).
    """
    __slots__ = (
        '_charge_count',
        '_func',
        '_threshold',
        '_triggered',
    )

    def __init__(self, threshold: int, func: Callable):
        self._func = func
        self._charge_count = 0
        self._threshold = threshold
        self._triggered = False

    def charge(self) -> 'CumulativeCallbackTrigger':
        self._charge_count += 1
        if self._triggered:
            return self
        if self._charge_count >= self._threshold:
            safe_call(self._func)
            self._triggered = True
        return self



class OrgAttrParser(object):
    """
    ORG_ATTR comment parser
    """
    __slots__ = (
        '_view',
        '_default_attrs'
    )

    def __init__(self, view: View):
        self._view = view
        self._default_attrs = { 'width': -1, 'height': -1 } # type: ParsedOrgAttrs

    @overload
    def setdefault(self, attr: Literal['width'], value: float) -> 'OrgAttrParser': ...
    @overload
    def setdefault(self, attr: Literal['height'], value: float) -> 'OrgAttrParser': ...
    @overload
    def setdefault(self, attr: SupportedOrgAttrs, value: Any) -> 'OrgAttrParser': ...

    @overload
    def calc(self, attr: Literal['width'], value: float, unit: str) -> float: ...
    @overload
    def calc(self, attr: Literal['height'], value: float, unit: str) -> float: ...
    @overload
    def calc(self, attr: SupportedOrgAttrs, value: Any, unit: str) -> Any: ...

    def getdefault(self) -> ParsedOrgAttrs:
        return self._default_attrs.copy()

    def setdefault(self, attr, value) -> 'OrgAttrParser':
        if attr in LIST_SUPPORTED_ORG_ATTR:
            self._default_attrs[attr] = value
        return self

    def calc(self, attr, value, unit) -> Any:
        viewport_scale = get_settings(SETTING_VIEWPORT_SCALE)
        if attr == 'width':
            return convert_length_to_px(
                self._view,
                value,
                unit,
                'width',
                viewport_scale)
        if attr == 'height':
            return convert_length_to_px(
                self._view,
                value,
                unit,
                'height',
                viewport_scale)
        return None

    def parse(self, textline: str) -> ParsedOrgAttrs:
        textline = textline or ''
        if not textline.strip().startswith('#+ORG_ATTR:'):
            return self._default_attrs.copy()
        try:
            org_attrs = self._default_attrs.copy()
            for match in REGEX_VAL_ORG_ATTR.finditer(textline):
                key, value, unit = match.groups()
                org_attrs[key] = self.calc(key, value, unit)
            for match in REGEX_LIT_ORG_ATTR.finditer(textline):
                key, value = match.groups()
                org_attrs[key] = value
            return org_attrs
        except Exception as error:
            print(error)
            traceback.print_tb(error.__traceback__)
            return self._default_attrs.copy()



class FetchStatusIndicator(SublimeStatusIndicator):
    """
    Display a loading indicator with messages on the status bar 
    while fetching images.
    """
    def __init__(
        self, 
        view: View, 
        region_range: RegionRange,
        total_count: Optional[int] = None, 
        update_interval: int = 100
    ) -> None:        
        if region_range == 'all':
            mode = 'inlineimages'
            area = 'document'
        else:
            mode = 'unfold'
            area = 'folding section'

        super().__init__(
            view,
            STATUS_ID,
            message = MSG_FETCH_IMAGES.format(mode, area),
            finish_message = MSG_RENDER_IMAGES,
            total_count = total_count,
            update_interval = update_interval
        )



class RenderStatusIndicator(SublimeStatusIndicator):
    """
    Display a loading indicator with messages on the status bar 
    while rendering images.
    """
    STRF_RENDER = '{} ({})'
    STRF_FINISH = '{} ({}) (Fetch: {}s, Render: {}s)'

    def __init__(
        self, 
        view: View, 
        update_interval: int = 100,
    ) -> None:
        super().__init__(
            view,
            STATUS_ID,
            message = self.STRF_RENDER.format(MSG_RENDER_IMAGES, 0),
            finish_message = self.STRF_FINISH.format(MSG_DONE, 0, 0, 0),
            update_interval = update_interval
        )

    def succeed(self, count: int) -> 'RenderStatusIndicator':
        self.message = self.STRF_RENDER.format(
            MSG_RENDER_IMAGES,
            count
        )
        return super().succeed(0, update = True)

    def stop(self, count: int, ft: float = 0, rt: float = 0) -> 'RenderStatusIndicator':
        self.finish_message = self.STRF_FINISH.format(MSG_DONE, count, ft, rt)
        print(self.finish_message)
        return super().stop()



class RenderContext(object):
    """
    An utility class to prevent too much parameters passing through
    functions
    """
    __slots__ = (
        'view',
        'pm',
        'lines',
    )
    def __init__(self, view: View) -> None:
        entire = Region(0, view.size())

        self.view = view
        self.pm = PhantomsManager.of(view)
        self.lines = tuple(lines_from_region(view, entire))

    def region_of(self, pid: int) -> Union[Region, None]:
        regions = self.pm.get_region_by_pid(pid)
        if not regions:
            return None
        return regions.pop(0)



class RenderEventDispatcher(object):
    """
    Can be used to fire events throughout the rendering process. You can 
    extend the RenderEventListener class to capture and handle these 
    events.
    """
    __slots__ = (
        '_event',
        '_args',
    )

    def __init__(self, view: View, args: Any):
        self._args = args
        self._event = RenderEvents(
            event_factory(view, 'render'),
            event_factory(view, 'render_error'),
            event_factory(view, 'render_finish')
        )

    def render_success(self, region: Region, image: CachedImageDict):
        emitter.emit(
            self._event.render, 
            self._args,
            region,
            image,
        )

    def render_error(self, error: Exception):
        emitter.emit(
            self._event.render_error,
            self._args,
            error
        )

    def render_finish(self):
        emitter.emit(
            self._event.render_finish,
            self._args
        )



class RenderEventListener(object):
    """
    Listen and handle events coming from RenderEventDispatcher
    """
    __slots__ = (
        '_event',
        '_render_listeners',
        '_error_listeners',
        '_finish_listeners'
    )

    def __init__(self, view: View) -> None:
        self._event = RenderEvents(
            event_factory(view, 'render'),
            event_factory(view, 'render_error'),
            event_factory(view, 'render_finish')
        )
        self._render_listeners = []
        self._error_listeners = []
        self._finish_listeners = []
        emitter.on(self._event.render, self.__handle_render)
        emitter.on(self._event.render_error, self.__handle_error)
        emitter.once(self._event.render_finish, self.__handle_finish)

    def on_render(
        self, 
        func: Callable[[Any, Region, CachedImageDict], None]
    ) -> 'RenderEventListener':
        if callable(func):
            self._render_listeners.append(func)
        return self

    def on_error(
        self,
        func: Callable[[Any, Exception], None]
    ) -> 'RenderEventListener':
        if callable(func):
            self._error_listeners.append(func)
        return self

    def on_finish(
        self,
        func: Callable[[Any], None]
    ) -> 'RenderEventListener':
        if callable(func):
            self._finish_listeners.append(func)
        return self

    def __handle_render(
        self, 
        value: Any, 
        image_region: Region, 
        image: CachedImageDict
    ) -> None:
        for listener in self._render_listeners:
            safe_call(listener, (value, image_region, image))

    def __handle_error(self, value: Any, error: Exception) -> None:
        for listener in self._error_listeners:
            safe_call(listener, (value, error))

    def __handle_finish(self, value: Any) -> None:
        for listener in self._finish_listeners:
            safe_call(listener, tuple([value]))
        emitter.clear_listeners(self._event.render)
        emitter.clear_listeners(self._event.render_error)
        emitter.clear_listeners(self._event.render_finish)



class RenderStatusUpdater(RenderEventListener):
    """
    Listen to render events and update the status message of 
    RenderStatusIndicator.
    """
    __slots__ = (
        '_view',
        '_id',
        '_timer',
        '_count',
        '_render_status',
    )

    def __init__(
        self, 
        view: View, 
        render_status: RenderStatusIndicator,
    ) -> None:
        super().__init__(view)
        self._view = view
        self._id = uuid4().hex
        self._timer = TimeMeasurement()
        self._count = 0
        self._render_status = render_status
        self.on_render(self._update_status)
        self.on_finish(self._halt_status)

    def id(self) -> str:
        return self._id

    def render_status(self) -> RenderStatusIndicator:
        return self._render_status

    def timer(self) -> TimeMeasurement:
        return self._timer

    def start_render(self, with_status: bool = True) -> None:
        if with_status:
            self.timer().start_render()
            self.render_status().start()
        else:
            self.timer().start_render(stop_fetch = False)

    def stop_render(
        self, 
        ft: Optional[Timecost] = None, 
        rt: Optional[Timecost] = None
    ) -> None:
        timer = self.timer()
        timer.stop_render()
        self.render_status().stop(
            self._count, 
            round(ft, 3), 
            round(rt, 3)
        )

    def _update_status(
        self, 
        value: Any, 
        image_region: Region, 
        image: CachedImageDict
    ) -> None:
        if value == self.id():
            self._count += 1
            self.render_status().succeed(self._count)

    def _halt_status(self, value: Any):
        if value == self.id():
            timer = self.timer()
            self.stop_render(
                timer.fetch_timecost(),
                timer.render_timecost()
            )



class Image(object):
    """
    A data object containing the necessary information for rendering an 
    image.
    """
    __slots__ = (
        'original_url', 
        'resolved_url',
        'pids',
        'regions'
    )
    def __init__(self, cwd: str, url: str) -> None:
        self.original_url = url
        self.resolved_url = resolve_local(url, cwd)
        self.pids = set()       # type: Set[PID]
        self.regions = set()    # type: Set[TupleRegion]

    def to_dict(self) -> CachedImageDict:
        cid = dict()
        for slot in self.__slots__:
            value = getattr(self, slot)
            if type(value) is set:
                cid[slot] = list(value)
            else:
                cid[slot] = value
        return cid



class CachedImage(object):
    """
    An image has been cached and is ready for rendering
    """
    __slots__ = (
        'original_url', 
        'resolved_url',
        'pids',
        'regions',
        'size',
        'width',
        'height'
    )
    def __init__(
        self, 
        image: Image,
        size: int = -1,
        width: float = -1,
        height: float = -1,
    ) -> None:
        self.original_url = image.original_url
        self.resolved_url = image.resolved_url
        self.pids = image.pids.copy()
        self.regions = image.regions.copy()
        self.size = size
        self.width = width
        self.height = height

    def to_dict(self) -> CachedImageDict:
        cid = dict()
        for slot in self.__slots__:
            value = getattr(self, slot)
            if type(value) is set:
                cid[slot] = list(value)
            else:
                cid[slot] = value
        return cid



class ImageList(object):
    """
    It may contains both cached images and non-cached images
    """
    __slots__ = ('_images')

    def __init__(
        self, 
        images: List[
            Union[
                Image,
                CachedImage
            ]
        ]
    ) -> None:
        self._images = images

    def to_dict(self) -> List[Union[ImageDict, CachedImageDict]]:
        return list(
            map(
                lambda i: i.to_dict(),
                self._images
            )
        )



class ThreadTermination(Exception):
    """
    Indicates that a thread has been terminated from within by some 
    reason.
    """
    pass



class CorruptedImage(Exception):
    """
    Indicate that the response has corrupted image binary, resulting in 
    rendering impossibility.
    """
    pass